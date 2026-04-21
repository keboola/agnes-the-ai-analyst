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
  description = "Seznam dev VMs. Prázdné pole = žádné dev VMs."
  type = list(object({
    name         = string
    machine_type = optional(string, "e2-small")
    image_tag    = optional(string, "dev")
  }))
  default = []
}

variable "seed_admin_email" {
  description = "Email prvního admin usera"
  type        = string
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
