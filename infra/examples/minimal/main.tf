# Minimal example: single-VM Agnes deploy.
# Pro OSS self-hoster, co chce prod VM bez dev, bez TLS.
terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = "europe-west1"
}

variable "gcp_project_id" {
  description = "GCP project ID (must have billing enabled)"
  type        = string
}

variable "admin_email" {
  description = "Email for first admin user"
  type        = string
}

module "agnes" {
  source = "../../modules/customer-instance"

  gcp_project_id   = var.gcp_project_id
  customer_name    = "self-hosted"
  seed_admin_email = var.admin_email

  prod_instance = {
    name         = "agnes"
    machine_type = "e2-small"
    data_disk_gb = 30
    image_tag    = "stable"
    upgrade_mode = "auto"
    tls_mode     = "none"
    domain       = ""
  }

  dev_instances = []

  # Customize below for your setup
  data_source = "keboola"
}

output "agnes_ip" {
  description = "SSH in via: ssh <user>@<ip>; UI at http://<ip>:8000"
  value       = module.agnes.prod_ip
}
