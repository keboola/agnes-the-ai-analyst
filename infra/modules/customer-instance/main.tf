terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

locals {
  # Normalize all instances into a single list so for_each is uniform across prod + dev.
  all_instances = concat(
    [merge(var.prod_instance, { role = "prod" })],
    [for d in var.dev_instances : merge(d, {
      role         = "dev"
      disk_size_gb = 30
      data_disk_gb = 20
      upgrade_mode = "auto"
      tls_mode     = "caddy"
      domain       = ""
    })]
  )
}

# --- Secrets ---

resource "google_secret_manager_secret" "jwt" {
  secret_id = "agnes-${var.customer_name}-jwt-secret"
  project   = var.gcp_project_id
  replication {
    auto {}
  }
}

resource "random_password" "jwt" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret_version" "jwt" {
  secret      = google_secret_manager_secret.jwt.id
  secret_data = random_password.jwt.result
}

# --- VM service account (dedikovaný, jen read Secret Manageru) ---

resource "google_service_account" "vm" {
  account_id   = "agnes-${var.customer_name}-vm"
  display_name = "Agnes VM runtime SA (${var.customer_name})"
  project      = var.gcp_project_id
}

resource "google_project_iam_member" "vm_secrets" {
  project = var.gcp_project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

# --- Network ---

resource "google_compute_firewall" "web" {
  name    = "agnes-${var.customer_name}-allow-web"
  project = var.gcp_project_id
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22", "80", "443", "8000"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["agnes-${var.customer_name}"]
}

# --- Backup policy: daily snapshot with 30-day retention ---

resource "google_compute_resource_policy" "daily_backup" {
  name    = "agnes-${var.customer_name}-daily-backup"
  project = var.gcp_project_id
  region  = var.region

  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "02:00"
      }
    }
    retention_policy {
      max_retention_days    = 30
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }
    snapshot_properties {
      labels = {
        app      = "agnes"
        customer = var.customer_name
      }
    }
  }
}

# --- Persistent data disks + VMs (prod + dev) ---

resource "google_compute_disk" "data" {
  for_each = { for inst in local.all_instances : inst.name => inst }

  name    = "${each.value.name}-data"
  project = var.gcp_project_id
  zone    = var.zone
  size    = each.value.data_disk_gb
  type    = "pd-ssd"
}

# Attach daily backup policy to data disks (boot disks are ephemeral,
# app code lives in the image so no need to snapshot them)
resource "google_compute_disk_resource_policy_attachment" "data_backup" {
  for_each = { for inst in local.all_instances : inst.name => inst }

  project = var.gcp_project_id
  zone    = var.zone
  disk    = google_compute_disk.data[each.key].name
  name    = google_compute_resource_policy.daily_backup.name
}

resource "google_compute_address" "ip" {
  for_each = { for inst in local.all_instances : inst.name => inst }

  name    = "${each.value.name}-ip"
  project = var.gcp_project_id
  region  = var.region
}

resource "google_compute_instance" "vm" {
  for_each = { for inst in local.all_instances : inst.name => inst }

  name         = each.value.name
  project      = var.gcp_project_id
  machine_type = each.value.machine_type
  zone         = var.zone
  tags         = ["agnes-${var.customer_name}"]

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = each.value.disk_size_gb
      type  = "pd-ssd"
    }
  }

  attached_disk {
    source      = google_compute_disk.data[each.key].self_link
    device_name = "data"
  }

  network_interface {
    network = "default"
    access_config {
      nat_ip = google_compute_address.ip[each.key].address
    }
  }

  metadata = {
    enable-oslogin = "TRUE"
  }

  metadata_startup_script = templatefile("${path.module}/startup-script.sh.tpl", {
    customer_name     = var.customer_name
    image_repo        = var.image_repo
    image_tag         = each.value.image_tag
    upgrade_mode      = each.value.upgrade_mode
    tls_mode          = each.value.tls_mode
    domain            = each.value.domain
    data_source       = var.data_source
    keboola_stack_url = var.keboola_stack_url
    seed_admin_email  = var.seed_admin_email
    role              = each.value.role
  })

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  labels = {
    app      = "agnes"
    customer = var.customer_name
    role     = each.value.role
    managed  = "terraform"
  }

  # Změna startup scriptu nemění běžící VM (script běží jen na boot).
  # Pro aplikaci změn je potřeba VM restartovat nebo recreate.
  lifecycle {
    ignore_changes = [metadata_startup_script]
  }
}

# --- Monitoring: uptime check on each VM's /api/health endpoint ---

resource "google_monitoring_uptime_check_config" "health" {
  for_each = var.enable_monitoring ? { for inst in local.all_instances : inst.name => inst } : {}

  project      = var.gcp_project_id
  display_name = "agnes-${var.customer_name}-${each.value.name}-health"
  timeout      = "10s"
  period       = "60s"

  http_check {
    path         = "/api/health"
    port         = "8000"
    use_ssl      = false
    validate_ssl = false
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.gcp_project_id
      host       = google_compute_address.ip[each.key].address
    }
  }
}

# --- Monitoring: alert when health fails for > 5 min ---

resource "google_monitoring_alert_policy" "health_failure" {
  for_each = var.enable_monitoring ? { for inst in local.all_instances : inst.name => inst } : {}

  project      = var.gcp_project_id
  display_name = "agnes-${var.customer_name}-${each.value.name}-health-failure"
  combiner     = "OR"

  conditions {
    display_name = "Uptime check failed > 5 min"
    condition_threshold {
      filter = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.labels.check_id=\"${google_monitoring_uptime_check_config.health[each.key].uptime_check_id}\" AND resource.type=\"uptime_url\""
      duration        = "300s"
      comparison      = "COMPARISON_LT"
      threshold_value = 1

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_FRACTION_TRUE"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = var.notification_channel_ids
  enabled               = true
}
