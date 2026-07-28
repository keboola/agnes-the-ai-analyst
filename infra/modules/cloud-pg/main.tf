/* Cloud SQL Postgres for the app-state state-machine's CLOUD backend.
 *
 * The customer-instance VM holds the app + side-car container; this
 * module provisions the managed Postgres that the operator migrates
 * to via /admin/database (the "graduate from side-car" step). Used
 * standalone too — initial cutover from DuckDB straight to cloud is
 * supported by the state machine.
 *
 * Cost shape (db-custom-2-7680, no HA, no backups, 20 GB SSD,
 * europe-west1, as of 2026): ~$70/month. db-f1-micro variant runs
 * ~$10/month but requires `edition = "ENTERPRISE"`.
 *
 * Network model: public IP + authorized_cidrs allowlist. Private IP
 * via VPC peering is out of scope for this module — operators that
 * need it should fork the module and add a `private_network` block.
 *
 * Provider requirement: `google` and `google-beta` 5.x. Cloud SQL
 * Admin API must be enabled on the project before apply
 * (`gcloud services enable sqladmin.googleapis.com`).
 */

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

# Read the password from Secret Manager. Stored in TF state (the gcp
# provider has no resource for "set password from secret ref"), so
# the operator's TF state bucket needs the same access posture as
# the secret itself. Documented in README.md.
data "google_secret_manager_secret_version" "app_password" {
  project = var.project
  secret  = var.password_secret_id
}

resource "google_sql_database_instance" "this" {
  project          = var.project
  name             = var.name
  region           = var.region
  database_version = var.postgres_version

  deletion_protection = var.deletion_protection

  settings {
    tier              = var.tier
    edition           = var.edition
    availability_type = var.high_availability ? "REGIONAL" : "ZONAL"
    disk_size         = var.storage_size_gb
    disk_type         = "PD_SSD"
    disk_autoresize   = true

    backup_configuration {
      enabled                        = var.backup_enabled
      start_time                     = var.backup_start_time
      point_in_time_recovery_enabled = var.backup_enabled
      transaction_log_retention_days = var.backup_enabled ? 7 : 1
    }

    ip_configuration {
      ipv4_enabled = true
      # Empty list = the instance has a public IP but no one is
      # authorized to reach it (only Cloud SQL Auth Proxy works).
      # Operators that want pure-public access supply at least one
      # CIDR — typically the customer-instance VM's external IP.
      dynamic "authorized_networks" {
        for_each = var.authorized_cidrs
        content {
          name  = authorized_networks.value.name
          value = authorized_networks.value.value
        }
      }
    }

    # Daily maintenance window. UTC 04:00 Sunday — quiet for most
    # workloads. Override per-instance by editing this block on a
    # fork if your operator hours differ.
    maintenance_window {
      day          = 7      # Sunday
      hour         = 4
      update_track = "stable"
    }

    insights_config {
      query_insights_enabled  = true
      query_string_length     = 1024
      record_application_tags = false
      record_client_address   = false
    }
  }
}

resource "google_sql_database" "agnes" {
  project  = var.project
  name     = var.database_name
  instance = google_sql_database_instance.this.name
}

resource "google_sql_user" "app" {
  project  = var.project
  instance = google_sql_database_instance.this.name
  name     = var.app_user
  password = data.google_secret_manager_secret_version.app_password.secret_data
}
