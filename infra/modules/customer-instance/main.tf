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

# --- Persistent data disks + VMs (prod + dev) ---

resource "google_compute_disk" "data" {
  for_each = { for inst in local.all_instances : inst.name => inst }

  name    = "${each.value.name}-data"
  project = var.gcp_project_id
  zone    = var.zone
  size    = each.value.data_disk_gb
  type    = "pd-ssd"
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
