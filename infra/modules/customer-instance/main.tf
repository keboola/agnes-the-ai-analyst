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
  # Note: merge({defaults}, d) — d overrides defaults (fix for v1.3.0 bug where
  # defaults overrode user-supplied values).
  dev_defaults = {
    role         = "dev"
    disk_size_gb = 30
    data_disk_gb = 20
    upgrade_mode = "auto"
    tls_mode     = "none" # dev VMs default to plain HTTP; TLS requires domain
    domain       = ""
  }
  all_instances = concat(
    [merge(var.prod_instance, { role = "prod" })],
    [for d in var.dev_instances : merge(local.dev_defaults, d)]
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

# --- VM service account (dedicated, read-only on specific secrets only) ---

resource "google_service_account" "vm" {
  account_id   = "agnes-${var.customer_name}-vm"
  display_name = "Agnes VM runtime SA (${var.customer_name})"
  project      = var.gcp_project_id
}

# Grant read access only to the JWT secret this module owns.
# Not project-wide — if the customer adds unrelated secrets (e.g. Stripe key)
# to the same project, Agnes VM must NOT be able to read them.
resource "google_secret_manager_secret_iam_member" "vm_jwt" {
  project   = var.gcp_project_id
  secret_id = google_secret_manager_secret.jwt.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# Grant read access to additional secrets the app needs (e.g. keboola-storage-token).
# Caller specifies these via var.runtime_secrets. Each secret must already exist.
resource "google_secret_manager_secret_iam_member" "vm_runtime" {
  for_each  = toset(var.runtime_secrets)
  project   = var.gcp_project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# --- Network ---

# Web firewall: 80/443 for Caddy (TLS), 8000 only when TLS is disabled (direct HTTP).
# Separate rule for SSH (port 22) — default restricted to IAP tunnel range.
locals {
  # Expose raw :8000 only when any instance has tls_mode != "caddy".
  # If Caddy handles TLS, customers should hit 80/443, not bypass to 8000.
  expose_raw_http_port = anytrue([for inst in local.all_instances : inst.tls_mode != "caddy"])
  web_ports            = local.expose_raw_http_port ? ["80", "443", "8000"] : ["80", "443"]
}

resource "google_compute_firewall" "web" {
  name    = "agnes-${var.customer_name}-allow-web"
  project = var.gcp_project_id
  network = "default"

  allow {
    protocol = "tcp"
    ports    = local.web_ports
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["agnes-${var.customer_name}"]
}

resource "google_compute_firewall" "ssh" {
  name    = "agnes-${var.customer_name}-allow-ssh"
  project = var.gcp_project_id
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = var.firewall_ssh_source_ranges
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
    customer_name       = var.customer_name
    image_repo          = var.image_repo
    image_tag           = each.value.image_tag
    upgrade_mode        = each.value.upgrade_mode
    tls_mode            = each.value.tls_mode
    domain              = each.value.domain
    acme_email          = var.acme_email != "" ? var.acme_email : var.seed_admin_email
    data_source         = var.data_source
    keboola_stack_url   = var.keboola_stack_url
    seed_admin_email    = var.seed_admin_email
    seed_admin_password = var.enable_seed_password ? var.seed_admin_password : ""
    role                = each.value.role
    compose_ref         = var.compose_ref
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

  # Startup script changes do not modify running VMs (script only runs on boot).
  # To propagate module changes, use:
  #   terraform apply -replace='module.agnes.google_compute_instance.vm["agnes-prod"]'
  lifecycle {
    ignore_changes = [metadata_startup_script]
  }

  # Ensure VM SA has read access to required secrets BEFORE the VM boots — otherwise
  # the startup script's `gcloud secrets versions access` can 403 due to IAM lag.
  depends_on = [
    google_secret_manager_secret_iam_member.vm_jwt,
    google_secret_manager_secret_iam_member.vm_runtime,
    google_secret_manager_secret_version.jwt,
  ]
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
      filter   = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.labels.check_id=\"${google_monitoring_uptime_check_config.health[each.key].uptime_check_id}\" AND resource.type=\"uptime_url\""
      duration = "300s"
      # ALIGN_FRACTION_TRUE yields fraction of checks that returned true.
      # If the fraction stays < 1 (i.e. any probe failed) for 5 min → alert.
      comparison      = "COMPARISON_LT"
      threshold_value = 1

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_FRACTION_TRUE"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = var.notification_channel_ids
  enabled               = true
}
