terraform {
  required_version = ">= 1.5"

  backend "gcs" {
    bucket = "agnes-terraform-state"
    prefix = "instances"
  }

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

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# --- Auto-generated secrets ---

resource "random_password" "jwt_secret" {
  length  = 48
  special = false
}

# --- Network ---

resource "google_compute_firewall" "data_analyst" {
  name    = "${var.instance_name}-allow-web"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22", "80", "443", "8000"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = [var.instance_name]
}

# --- Static IP ---

resource "google_compute_address" "data_analyst" {
  name   = "${var.instance_name}-ip"
  region = var.region
}

# --- Startup script ---

locals {
  startup_script = <<-SCRIPT
    #!/bin/bash
    set -euo pipefail
    exec > /var/log/startup.log 2>&1

    echo "=== Installing Docker ==="
    if ! command -v docker &> /dev/null; then
      curl -fsSL https://get.docker.com | sh
      usermod -aG docker ${var.ssh_user}
    fi

    # Install docker compose plugin
    if ! docker compose version &> /dev/null; then
      apt-get update && apt-get install -y docker-compose-plugin
    fi

    echo "=== Cloning repository ==="
    APP_DIR="/opt/data-analyst"
    if [ ! -d "$APP_DIR" ]; then
      git clone https://github.com/keboola/agnes-the-ai-analyst.git "$APP_DIR"
      cd "$APP_DIR"
      git checkout main
    else
      cd "$APP_DIR"
      git pull origin main || true
    fi

    echo "=== Creating .env ==="
    cat > "$APP_DIR/.env" << 'ENVEOF'
    JWT_SECRET_KEY=${random_password.jwt_secret.result}
    DATA_DIR=/data
    DATA_SOURCE=${var.keboola_token != "" ? "keboola" : "local"}
    KEBOOLA_STORAGE_TOKEN=${var.keboola_token}
    KEBOOLA_STACK_URL=${var.keboola_stack_url}
    KEBOOLA_PROJECT_ID=${var.keboola_project_id}
    SEED_ADMIN_EMAIL=${var.admin_email}
    LOG_LEVEL=info
    ENVEOF
    # Strip leading whitespace from heredoc
    sed -i 's/^    //' "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"

    echo "=== Creating instance.yaml ==="
    mkdir -p "$APP_DIR/config"
    cat > "$APP_DIR/config/instance.yaml" << YAMLEOF
instance:
  name: "${var.instance_name}"
  subtitle: "Data Analytics Platform"
server:
  host: "${google_compute_address.data_analyst.address}"
  hostname: "${var.domain != "" ? var.domain : google_compute_address.data_analyst.address}"
  port: 8000
auth:
  allowed_domain: ""
data_source:
  type: "${var.keboola_token != "" ? "keboola" : "local"}"
YAMLEOF

    echo "=== Creating data directory ==="
    mkdir -p /data/state /data/analytics /data/extracts
    chown -R 1000:1000 /data

    echo "=== Starting Docker Compose ==="
    cd "$APP_DIR"
    docker compose pull 2>/dev/null || true
    docker compose build
    docker compose up -d

    echo "=== Startup complete ==="
    docker compose ps
  SCRIPT
}

# --- VM Instance ---

resource "google_compute_instance" "data_analyst" {
  name         = var.instance_name
  machine_type = var.machine_type
  zone         = var.zone

  tags = [var.instance_name]

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = var.disk_size_gb
      type  = "pd-ssd"
    }
  }

  network_interface {
    network = "default"
    access_config {
      nat_ip = google_compute_address.data_analyst.address
    }
  }

  metadata = {
    ssh-keys = "${var.ssh_user}:${file(pathexpand(var.ssh_public_key_path))}"
  }

  metadata_startup_script = local.startup_script

  service_account {
    scopes = ["cloud-platform"]
  }

  labels = {
    app     = "data-analyst"
    managed = "terraform"
  }
}
