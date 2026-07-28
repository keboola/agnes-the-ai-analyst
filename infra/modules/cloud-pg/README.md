# `cloud-pg` — Cloud SQL Postgres for the app-state state machine

Provisions a managed Cloud SQL Postgres instance that the app's
`/admin/database` state machine targets via the `cloud` backend. Pairs
with the `customer-instance` module: one VM + one managed DB, both
pinned via the same `infra-vX.Y.Z` tag.

This is GCP-specific (uses `google_sql_database_instance`). The
`customer-instance` module is the cloud-portable surface; this module
exists as a reference for operators who pick Cloud SQL specifically.
Equivalents for AWS RDS / Azure Database for PostgreSQL are
straightforward forks.

## Resources created

- `google_sql_database_instance.this` — the Postgres instance
- `google_sql_database.agnes` — the application database (default `agnes`)
- `google_sql_user.app` — the application user (default `agnes`)

## Provider requirement

```hcl
terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}
```

The Cloud SQL Admin API must be enabled on the project before the
first apply:

```sh
gcloud services enable sqladmin.googleapis.com --project=<project>
```

## Inputs (most-edited)

| name | type | default | notes |
| --- | --- | --- | --- |
| `name` | string | — | Lowercase letters, digits, hyphens. |
| `project` | string | — | GCP project ID. |
| `region` | string | — | Match the customer-instance region. |
| `tier` | string | `db-custom-2-7680` | 2 vCPU / 7.5 GB. `db-f1-micro` works only on `edition = ENTERPRISE`. |
| `edition` | string | `ENTERPRISE` | `ENTERPRISE_PLUS` requires non-shared tiers. |
| `postgres_version` | string | `POSTGRES_16` | App is tested on 16. |
| `storage_size_gb` | number | `20` | Auto-grow is on, this is a floor. |
| `authorized_cidrs` | list(object) | `[]` | At minimum the customer-instance VM's `/32`. |
| `backup_enabled` | bool | `false` | **Set to `true` for any non-recreatable data.** |
| `high_availability` | bool | `false` | Doubles instance cost. |
| `password_secret_id` | string | — | Pre-provisioned Secret Manager secret name. |

## Outputs

| name | description |
| --- | --- |
| `instance_name` | Same as `var.name`. |
| `instance_connection_name` | `<project>:<region>:<instance>` for Cloud SQL Auth Proxy / IAM auth. |
| `public_ip` | Public IPv4 of the instance. |
| `database_name` | Database name (same as `var.database_name`). |
| `app_user` | Postgres user (same as `var.app_user`). |
| `url_template` | URL with a literal `<PASSWORD>` placeholder. |

The full connection URL is intentionally **not** an output — that
string would contain the password and end up in plan artifacts, CI
logs, downstream modules. Compose the final URL from `url_template`
+ the Secret Manager value at cutover time.

## Usage example

```hcl
# 1. Pre-provision the app-user password as a Secret Manager secret
#    out-of-band — keep the plaintext out of TF code AND TF state
#    until the data source resolves it at apply time.
resource "google_secret_manager_secret" "agnes_cloud_pg_password" {
  project   = var.project
  secret_id = "agnes-cloud-pg-app-password"
  replication { auto {} }
}

# `gcloud secrets versions add agnes-cloud-pg-app-password \
#    --data-file=- <<< 'CHANGE_ME_long_random_string'`

# 2. The Agnes VM (provisioned by customer-instance) — its external IP
#    is the canonical authorized network.
module "agnes_vm" {
  source = "git::https://github.com/keboola/agnes-the-ai-analyst.git//infra/modules/customer-instance?ref=infra-v2.0.0"
  # … other inputs …
}

# 3. The Cloud SQL instance.
module "agnes_cloud_pg" {
  source = "git::https://github.com/keboola/agnes-the-ai-analyst.git//infra/modules/cloud-pg?ref=infra-v2.0.0"

  name              = "agnes-prod-pg"
  project           = var.project
  region            = "europe-west1"
  tier              = "db-custom-2-7680"
  postgres_version  = "POSTGRES_16"
  storage_size_gb   = 50
  backup_enabled    = true
  high_availability = true              # prod

  authorized_cidrs = [
    { name = "agnes-vm", value = "${module.agnes_vm.external_ip}/32" },
  ]

  password_secret_id = google_secret_manager_secret.agnes_cloud_pg_password.id
}

# 4. At cutover time, the operator pastes the URL into /admin/database:
#
#    postgresql+psycopg://${module.agnes_cloud_pg.app_user}:<password
#    from Secret Manager>@${module.agnes_cloud_pg.public_ip}:5432/${
#    module.agnes_cloud_pg.database_name}
#
output "agnes_cloud_pg_url_template" {
  value = module.agnes_cloud_pg.url_template
}
```

## State storage caveat

The `google_sql_user.password` attribute is stored in plaintext in
TF state. This is a gcp provider limitation — there is no
"reference Secret Manager directly" alternative. Treat the TF state
backend (typically a GCS bucket) with the same access posture as
the secret itself: minimum-privilege bucket IAM, audit logging on.

## Cost (europe-west1, May 2026)

| variant | tier | HA | backup | est. monthly |
| --- | --- | --- | --- | --- |
| dev/test | `db-f1-micro` + `ENTERPRISE` | no | no | ~$10 |
| small prod | `db-custom-2-7680` | no | yes | ~$75 |
| HA prod | `db-custom-2-7680` | yes | yes | ~$155 |

Run `gcloud sql instances describe <name>` for the live SKU breakdown.

## Operator runbook

End-to-end DuckDB → side-car → Cloud SQL playbook lives in
[`docs/postgres-cutover-runbook.md`](../../../docs/postgres-cutover-runbook.md).
