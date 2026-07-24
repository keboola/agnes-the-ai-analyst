variable "gcp_project_id" {
  description = "GCP project ID where the instance will be deployed."
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
  description = "Short customer identifier (e.g. acme, example). Used as a prefix for created resources."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.customer_name))
    error_message = "customer_name must be lowercase, start with a letter, 2-21 chars."
  }
}

variable "prod_instance" {
  description = <<-EOT
    Production VM configuration.

    `image_tag` MUST point to an image that contains `/opt/agnes-host/`
    (this directory was added in v0.26.0). Older tags will fail at first
    boot with `docker cp: No such file or directory` because the startup
    script extracts host artifacts from the image instead of curling
    them. Existing VMs are unaffected by this constraint — the module
    sets `lifecycle { ignore_changes = [metadata_startup_script] }` so
    the new script only runs on freshly-created VMs.
  EOT
  type = object({
    name         = string
    machine_type = optional(string, "e2-small")
    disk_size_gb = optional(number, 30)
    data_disk_gb = optional(number, 50)
    image_tag    = optional(string, "stable")
    upgrade_mode = optional(string, "auto")
    # Standard 5-field cron expression consumed by startup-script.sh.tpl's
    # crontab install line. Default matches the historical fixed cadence —
    # override to reduce upgrade-triggered blips on a customer-facing
    # instance (e.g. a nightly window) while dev/staging instances stay on fast
    # iteration.
    upgrade_schedule = optional(string, "*/5 * * * *")
    tls_mode         = optional(string, "caddy")
    domain           = optional(string, "")
    # Container memory caps written to /opt/agnes/.env and read by
    # docker-compose.yml (mem_limit: $${AGNES_APP_MEM_LIMIT:-4g}). Defaults
    # match the compose defaults; raise on a larger VM together with the
    # app's per-connection DuckDB budgets (DuckDB sizes a fresh connection
    # to ~80% of the cgroup limit, so an under-sized cap OOM-kills uvicorn
    # mid-WAL-write).
    app_mem_limit       = optional(string, "4g")
    scheduler_mem_limit = optional(string, "2g")
    # Container CPU caps written to /opt/agnes/.env and read by
    # docker-compose.yml (cpus: $${AGNES_APP_CPUS:-2.0}). Raise app_cpus on
    # hosts with more cores (e.g. "3.0" on a 4-core VM) for headroom under
    # concurrent load; keep app_cpus + scheduler_cpus <= host cores.
    app_cpus       = optional(string, "2.0")
    scheduler_cpus = optional(string, "1.0")
    # Opt-in LLM dispatcher (token-arbitrage PoC) on this VM. Per-VM so a
    # dev-first rollout doesn't touch prod. Requires the module-level
    # dispatcher_* variables to be set — see their docs.
    dispatcher_enabled = optional(bool, false)
  })
}

variable "dev_instances" {
  description = <<-EOT
    List of dev VMs. Empty list = no dev VMs.

    tls_mode + domain are optional and default to plain HTTP on :8000. Set
    tls_mode = "caddy" + domain to enable Caddy + Let's Encrypt (or whatever
    CADDY_TLS env var is configured to in the Caddyfile — see Caddyfile docs).

    Same `image_tag >= v0.26.0` constraint as `prod_instance` — older tags
    lack `/opt/agnes-host/` and the startup `docker cp` fails-loud.
  EOT
  type = list(object({
    name         = string
    machine_type = optional(string, "e2-small")
    image_tag    = optional(string, "dev")
    tls_mode     = optional(string, "none")
    domain       = optional(string, "")
    # Role label used by per-VM OAuth secret naming
    # (var.oauth_secret_name_template `{role}` placeholder), VM tagging in
    # downstream cron/log filters, and dev_defaults selection. Defaults to
    # "dev" so existing callers don't have to set it; override per VM to
    # introduce `stage`, `perf`, etc. without any module-side code change
    # (matching Secret Manager entries — `*-stage` / `*-perf` — must exist
    # if the per-VM OAuth template uses {role}). MUST be declared on the
    # object type, not only in dev_defaults: Terraform silently drops
    # attributes that aren't in the object type during conversion, so a
    # caller-supplied `role = "stage"` would never reach the merge() below
    # if the type omits it.
    role = optional(string, "dev")
    # See prod_instance for the rationale; same defaults.
    app_mem_limit       = optional(string, "4g")
    scheduler_mem_limit = optional(string, "2g")
    app_cpus            = optional(string, "2.0")
    scheduler_cpus      = optional(string, "1.0")
    dispatcher_enabled  = optional(bool, false)
    # See prod_instance for the rationale; same default.
    upgrade_schedule = optional(string, "*/5 * * * *")
  }))
  default = []
}

variable "oauth_secret_name_template" {
  description = <<-EOT
    Template for per-VM OAuth client (Sign-in with Google) Secret Manager
    secret names. Supports placeholders:
      {kind} -> "id" or "secret" (REQUIRED — otherwise both fetches resolve
                to the same secret, which is broken)
      {role} -> "prod" for the prod VM; for dev VMs, whatever was passed in
                via `dev_instances[*].role` (defaults to "dev"). Set
                `role = "stage"` / "perf" / etc. on a dev_instances entry to
                introduce a new env class — the matching
                <template-expanded-stage> secrets must already exist in SM.
      {name} -> the VM name from prod_instance.name / dev_instances[*].name
                (one OAuth client per VM, regardless of role)

    Empty (default) -> legacy shared `google-oauth-client-{id,secret}`
    (v1.x default — same OAuth client across every VM in the module call).

    Examples:
      "agnes-google-oauth-client-{kind}-{role}"  -> one client per role
                                                    (prod, dev share across
                                                    multiple dev VMs)
      "agnes-oauth-{kind}-{name}"                -> one client per VM
                                                    (every VM isolated; needed
                                                    for per-engineer dev VMs
                                                    on shared OAuth domain)

    Resolved names must already exist in Secret Manager — the module grants
    the VM SA secretAccessor on the resolved set; it does NOT create the
    secret rows themselves (those carry the OAuth credentials issued in
    Cloud Console, which has no public API).

    Caveat: do NOT also list a derived name in `var.runtime_secrets` — the
    same `google_secret_manager_secret_iam_member` would land twice for the
    same (project, secret, role, member) tuple and apply errors with
    "already exists". Keep `runtime_secrets` strictly for OTHER secrets the
    VM needs (e.g. `keboola-storage-token`) when the template is in use.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.oauth_secret_name_template == "" || strcontains(var.oauth_secret_name_template, "{kind}")
    error_message = "oauth_secret_name_template must contain the {kind} placeholder when non-empty (otherwise id and secret resolve to the same Secret Manager name)."
  }

  validation {
    condition = var.oauth_secret_name_template == "" || (
      strcontains(var.oauth_secret_name_template, "{role}") ||
      strcontains(var.oauth_secret_name_template, "{name}")
    )
    error_message = "oauth_secret_name_template should contain {role} or {name} for per-VM differentiation — otherwise every VM resolves to the same Secret Manager name and you've just renamed the legacy shared client (which is fine, but pointless to do via this variable; set runtime_secrets instead)."
  }
}

variable "seed_admin_email" {
  description = "Email of the initial admin user."
  type        = string
}

variable "enable_seed_password" {
  description = "If true, the seed admin user immediately gets a password_hash from seed_admin_password (dev helper). Keep false in prod — the admin sets a password via /auth/bootstrap or Google OAuth."
  type        = bool
  default     = false
}

variable "seed_admin_password" {
  description = "Plain-text password for the seed admin. Only used when enable_seed_password=true. WARNING: stored in Terraform state."
  type        = string
  default     = ""
  sensitive   = true
}

variable "data_source" {
  description = "Data source type — keboola | bigquery | csv."
  type        = string
  default     = "keboola"
}

variable "keboola_stack_url" {
  description = "Keboola Stack URL (used when data_source = keboola)."
  type        = string
  default     = ""
}

variable "image_repo" {
  description = "Docker image repo"
  type        = string
  default     = "ghcr.io/keboola/agnes-the-ai-analyst"
}

variable "compose_ref" {
  description = "DEPRECATED — no longer used. Compose files now ship inside the docker image at /opt/agnes-host/ and are extracted via `docker cp` from the same `image_tag` the operator pinned. Pin `image_tag` instead. Variable retained for one release cycle to avoid breaking existing terraform plans; will be removed in a future major bump."
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
  description = "Names of existing Secret Manager secrets the VM needs to read at runtime (e.g. Keboola Storage token). VM SA gets scoped secretAccessor on each. Use this for secrets the startup script handles explicitly (KEBOOLA_STORAGE_TOKEN, GOOGLE_CLIENT_ID/SECRET — names are hardcoded in startup-script.sh.tpl). For new app-level secrets (E2B_API_KEY, ANTHROPIC_API_KEY, SLACK_*), prefer `runtime_secret_env` below."
  type        = list(string)
  default     = ["keboola-storage-token"]
}

variable "runtime_secret_env" {
  description = "Map of Secret Manager secret name to env var name to inject into /opt/agnes/.env. Module auto-grants secretAccessor and the startup script fetches each via gcloud secrets versions access latest --secret=<key> and writes a line <env_var>=<fetched> to .env. Missing/403 -> empty string (silent), so production deploys can roll out a secret name before the value lands. Example map: e2b-api-key -> E2B_API_KEY, anthropic-api-key -> ANTHROPIC_API_KEY."
  type        = map(string)
  default     = {}
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

variable "home_route" {
  description = "Landing page after auth, applied instance-wide by writing AGNES_HOME_ROUTE into /opt/agnes/.env. One of /home (state-aware onboarding), /dashboard (legacy table inventory), /setup, /catalog. Empty (default) omits the env line entirely so the app falls through to instance.home_route in instance.yaml and then its built-in /dashboard default — keeping the route operator-settable at runtime via /admin/server-config. Set a non-empty value to pin it at deploy time (the env var overrides the YAML, so don't do both). Per-VM divergence isn't exposed yet; it applies to prod + all dev VMs in the instance."
  type        = string
  default     = ""
  validation {
    condition     = var.home_route == "" || contains(["/home", "/dashboard", "/setup", "/catalog"], var.home_route)
    error_message = "home_route must be empty or one of: /home, /dashboard, /setup, /catalog"
  }
}

variable "studio_enabled" {
  description = "Expose the authoring Studio (/admin/studio). Set false to hide it and close its routes for this instance (plumbed to the app as AGNES_STUDIO_ENABLED)."
  type        = bool
  default     = true
}

variable "enable_watchdog" {
  description = "Install the host-side watchdog + daily DB backup on every VM. The watchdog (5-min systemd timer) greps container logs for known incident signatures — DuckDB fatal crash loops, the invalidated-database \"zombie\" state (app answers /api/health 200 while every write 500s), WAL salvage data-loss events, index-desync errors — plus container restart bursts, cgroup OOM kills, scheduler failure streaks and /data disk pressure. The backup (daily systemd timer) copies system.duckdb+WAL to /data/backups/system-duckdb/ with 7-day retention and proves each copy restorable via a canary open+replay. Complements enable_monitoring: uptime checks see the VM from outside; the watchdog sees failure states the health endpoint cannot express, and PD snapshots preserve a corrupted file faithfully while the canary verify catches the corruption."
  type        = bool
  default     = true
}

variable "dispatcher_image" {
  description = <<-EOT
    Image for the opt-in LLM dispatcher (token-arbitrage PoC), e.g.
    "ghcr.io/keboola/token-arbitrage-dispatcher:<sha>". Pin to a commit-sha
    tag — CI publishes :<sha> and :latest on every token-arbitrage main push;
    deploying :latest makes rollouts non-reproducible.

    The dispatcher runs as an extra compose service (overlay written by the
    startup script) on any VM whose instance object sets
    `dispatcher_enabled = true`; the Agnes chat broker then routes chat
    completions to it via LLM_DISPATCHER_URL (see app/api/broker.py).
    Required (with the other dispatcher_* variables) when any instance
    enables the dispatcher.
  EOT
  type        = string
  default     = ""
}

variable "dispatcher_policies" {
  description = <<-EOT
    Routing-policy YAML CONTENT for the dispatcher (pass file(...) from the
    deployment repo — policy is deployment-owned). Delivered to the VM as
    /opt/agnes/dispatcher/policies.yaml via the startup script. Must parse
    under the dispatcher's PolicyEngine (top-level `default:` route is
    mandatory). Required when any instance enables the dispatcher.
  EOT
  type        = string
  default     = ""
}

variable "dispatcher_key_secret" {
  description = <<-EOT
    Secret Manager secret name holding the dispatcher API key (the value the
    Agnes broker sends as x-api-key; doubles as the cost-ledger team
    identity). The startup script builds /opt/agnes/dispatcher/keys.yaml
    mapping this key to team "agnes" and writes LLM_DISPATCHER_API_KEY into
    /opt/agnes/.env. Module grants the VM SA secretAccessor. Fetch fails
    LOUDLY at boot when the dispatcher is enabled — a dispatcher without its
    key would 401 every chat request. Required when any instance enables the
    dispatcher.
  EOT
  type        = string
  default     = ""
}

variable "dispatcher_vertex_sa_secret" {
  description = <<-EOT
    Secret Manager secret name holding a GCP service-account KEY JSON with
    Vertex AI access in the project the routing policy targets (the VM's own
    SA usually lives in a different project, so ADC is not enough). Written
    to /opt/agnes/dispatcher/vertex-sa.json and mounted into the dispatcher
    container as GOOGLE_APPLICATION_CREDENTIALS. Module grants the VM SA
    secretAccessor; fetch fails loudly at boot when the dispatcher is
    enabled. Required when any instance enables the dispatcher.
  EOT
  type        = string
  default     = ""
}

variable "alert_webhook_url" {
  description = "Webhook for watchdog + backup-verify alerts (Slack / Google Chat compatible: POST {\"text\": ...}). Empty (default) = alerts go to journald + /var/log/agnes-watchdog.log only. Lands on the VM in /etc/agnes-watchdog.env (root, 0600). An operator may hand-edit that file; the startup script preserves a hand-edited value when this variable is empty and overwrites it when set — same precedence pattern as AGNES_TAG."
  type        = string
  default     = ""
  sensitive   = true
}
