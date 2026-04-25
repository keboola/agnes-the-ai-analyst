variable "gcp_project_id" {
  description = "GCP project ID kde bude instance nasazená"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "europe-west1"
}

variable "zone" {
  description = "GCP zone"
  type        = string
  default     = "europe-west1-b"
}

variable "customer_name" {
  description = "Krátký identifikátor zákazníka (např. keboola, grpn). Použije se v prefixu resourců."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.customer_name))
    error_message = "customer_name musí být lowercase, začínat písmenem, 2-21 znaků."
  }
}

variable "prod_instance" {
  description = "Prod VM konfigurace"
  type = object({
    name         = string
    machine_type = optional(string, "e2-small")
    disk_size_gb = optional(number, 30)
    data_disk_gb = optional(number, 50)
    image_tag    = optional(string, "stable")
    upgrade_mode = optional(string, "auto")
    tls_mode     = optional(string, "caddy")
    domain       = optional(string, "")
  })
}

variable "dev_instances" {
  description = <<-EOT
    Seznam dev VMs. Prázdné pole = žádné dev VMs.

    tls_mode + domain are optional and default to plain HTTP on :8000. Set
    tls_mode = "caddy" + domain to enable Caddy + Let's Encrypt (or whatever
    CADDY_TLS env var is configured to in the Caddyfile — see Caddyfile docs).
  EOT
  type = list(object({
    name         = string
    machine_type = optional(string, "e2-small")
    image_tag    = optional(string, "dev")
    tls_mode     = optional(string, "none")
    domain       = optional(string, "")
  }))
  default = []
}

variable "seed_admin_email" {
  description = "Email prvního admin usera"
  type        = string
}

variable "enable_seed_password" {
  description = "Pokud true, seed admin user dostane hned password_hash ze seed_admin_password (dev helper). Ponech false v prod — admin si heslo nastaví přes /auth/bootstrap nebo Google OAuth."
  type        = bool
  default     = false
}

variable "seed_admin_password" {
  description = "Plain-text heslo pro seed admina. Použije se jen když enable_seed_password=true. POZOR: ukládá se do Terraform state."
  type        = string
  default     = ""
  sensitive   = true
}

variable "data_source" {
  description = "Typ data source — keboola | bigquery | csv"
  type        = string
  default     = "keboola"
}

variable "keboola_stack_url" {
  description = "Keboola Stack URL (pokud data_source = keboola)"
  type        = string
  default     = ""
}

variable "image_repo" {
  description = "Docker image repo"
  type        = string
  default     = "ghcr.io/keboola/agnes-the-ai-analyst"
}

variable "compose_ref" {
  description = "Git ref to fetch docker-compose.yml and overlays from (in keboola/agnes-the-ai-analyst). Use `main` for latest, or a tag like `stable-2026.04.47` for reproducibility."
  type        = string
  default     = "main"
}

variable "enable_monitoring" {
  description = "Create uptime checks + alert policies for each VM. Requires notification_channel_ids to be useful."
  type        = bool
  default     = true
}

variable "notification_channel_ids" {
  description = "Full resource IDs of GCP Monitoring notification channels (create in customer project via gcloud alpha monitoring channels create). Empty list = alerts fire but nothing is notified."
  type        = list(string)
  default     = []
}

variable "runtime_secrets" {
  description = "Names of existing Secret Manager secrets the VM needs to read at runtime (e.g. Keboola Storage token). VM SA gets scoped secretAccessor on each."
  type        = list(string)
  default     = ["keboola-storage-token"]
}

variable "firewall_ssh_source_ranges" {
  description = "CIDR ranges allowed to reach SSH (port 22). Default is IAP tunnel range only (use `gcloud compute ssh --tunnel-through-iap`). Override to `[\"0.0.0.0/0\"]` for unrestricted (not recommended)."
  type        = list(string)
  default     = ["35.235.240.0/20"]
}

variable "acme_email" {
  description = "Email for Let's Encrypt account (used when tls_mode=caddy). Defaults to seed_admin_email if empty."
  type        = string
  default     = ""
}
