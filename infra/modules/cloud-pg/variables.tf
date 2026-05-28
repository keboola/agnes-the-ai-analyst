/* Inputs to the Cloud SQL Postgres module.
 *
 * The module is GCP-specific by nature but stays vendor-neutral in
 * naming and defaults so the customer-instance + cloud-pg pair reads
 * as one product surface: one VM + one managed DB, both pinned via
 * the same `infra-vX.Y.Z` tag, both consumable from the same root
 * module.
 *
 * No customer-specific defaults — every project ID, region, and
 * authorized IP is required input. See README.md for a complete
 * usage example.
 */

variable "name" {
  description = "Cloud SQL instance name. Final identifier inside the project; lowercase letters, digits, and hyphens only."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.name))
    error_message = "name must start with a lowercase letter and contain only lowercase letters, digits, and hyphens."
  }
}

variable "project" {
  description = "GCP project ID that owns the Cloud SQL instance. The Cloud SQL Admin API (sqladmin.googleapis.com) must be enabled on this project."
  type        = string
}

variable "region" {
  description = "GCP region. Keep this aligned with the customer-instance VM's region — cross-region traffic is billed and adds latency to every app request."
  type        = string
}

variable "tier" {
  description = "Cloud SQL machine tier. `db-f1-micro` is the smallest (shared-core, ~$10/month) and works only on Enterprise edition. For production prefer `db-custom-2-7680` (2 vCPU, 7.5 GB) or larger."
  type        = string
  default     = "db-custom-2-7680"
}

variable "edition" {
  description = "Cloud SQL edition. ENTERPRISE supports shared-core tiers (db-f1-micro, db-g1-small); ENTERPRISE_PLUS requires db-perf-optimized-N-* tiers. Default ENTERPRISE keeps the tiny-tier path open for dev/test."
  type        = string
  default     = "ENTERPRISE"
  validation {
    condition     = contains(["ENTERPRISE", "ENTERPRISE_PLUS"], var.edition)
    error_message = "edition must be ENTERPRISE or ENTERPRISE_PLUS."
  }
}

variable "postgres_version" {
  description = "Postgres major version. Must match the alembic schema target — the app's repository code is tested against PG 16. Bumping requires verifying alembic + extension compatibility."
  type        = string
  default     = "POSTGRES_16"
}

variable "storage_size_gb" {
  description = "Initial data disk size in GB. Auto-grow is enabled by default, so this is a floor, not a cap. 20 GB covers a few years of typical app-state growth for a single instance."
  type        = number
  default     = 20
}

variable "database_name" {
  description = "Application database name created inside the instance. Match the `agnes` default that the app's URL template assumes; override only if the operator runs multiple Agnes deployments in one instance."
  type        = string
  default     = "agnes"
}

variable "app_user" {
  description = "Application Postgres user. The app's connection URL has the shape postgresql+psycopg://USER:PASSWORD@HOST:5432/DB. Default mirrors the side-car container's user so admins moving from container PG to cloud PG don't have to rotate roles in app code."
  type        = string
  default     = "agnes"
}

variable "authorized_cidrs" {
  description = "List of CIDR blocks allowed to reach the instance over public IP. At minimum include the customer-instance VM's external IP as a /32. Empty list disables external access — the instance is then only reachable from inside its VPC (which this module doesn't yet wire up)."
  type = list(object({
    name  = string
    value = string
  }))
  default = []
}

variable "deletion_protection" {
  description = "If true, `terraform destroy` cannot remove the instance until this is flipped to false in a separate apply. Default true for prod; set false on dev/test stacks that get torn down regularly."
  type        = bool
  default     = true
}

variable "backup_enabled" {
  description = "Enable automated daily backups + point-in-time recovery. Off by default to keep the dev cost floor down (~$0 instead of ~$1.50/month for the smallest-disk backups). Operators MUST enable this for any instance that holds non-recreatable data."
  type        = bool
  default     = false
}

variable "backup_start_time" {
  description = "UTC time-of-day when the daily backup window opens, format HH:MM. Only effective when `backup_enabled = true`. Default 03:00 is most regions' off-peak; align with the app's quiet-hour window if known."
  type        = string
  default     = "03:00"
}

variable "high_availability" {
  description = "Enable regional HA (synchronous replica in a second zone). Doubles the instance cost. Recommended for production; off by default to keep dev cheap."
  type        = bool
  default     = false
}

variable "password_secret_id" {
  description = "Pre-existing Secret Manager secret ID that the module reads the app user's password from. Operator provisions + populates the secret out-of-band; the module's TF state never sees the plaintext password (it only goes through `google_sql_user.password`, which IS in state — a hard limitation of the gcp provider). Format: `projects/<project>/secrets/<name>`."
  type        = string
}
