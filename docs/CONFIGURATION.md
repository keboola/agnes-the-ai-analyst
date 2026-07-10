# Configuration Reference

This is the single authoritative map of everything an operator can customize
per instance. Three independent tiers feed an instance's behavior; the knob
table below names every resolver, its env override, its `instance.yaml` path,
and its default.

> The per-instance knob table is guarded by
> `tests/test_config_reference_coverage.py`: every `get_*` resolver in
> `app/instance_config.py` must appear here, so this doc cannot silently drift
> behind the code.

## How configuration resolves

Most knobs resolve in this order — **first non-empty wins**:

1. **Environment variable** (e.g. `AGNES_HOME_ROUTE`, `DATA_SOURCE`,
   `SLACK_TRANSPORT`) — set in `.env` / by Terraform. **Overrides everything.**
2. **`instance.yaml`** — the static base at `config/instance.yaml` deep-merged
   with the admin overlay written to `${DATA_DIR}/state/instance.yaml` by
   `/admin/server-config`. The overlay wins per-leaf.
3. **Built-in default** — baked into the resolver in `app/instance_config.py`.

> **Footgun:** because the env var wins over `instance.yaml`, an instance that
> pins a knob via an env var **cannot** later change it through the
> `/admin/server-config` UI — the UI writes the YAML tier, which the env tier
> shadows. Pin via env **or** manage via YAML, not both. (This is exactly why
> the Terraform `home_route` knob below writes its env line *only* when set to a
> non-empty value — see [Infra patterns](#infra-patterns-and-knob-reachability).)

A few structural knobs (lists/objects that don't round-trip through env vars —
`datasets`, `theme`, `custom_scripts`, …) are **YAML-only**; their rows show
`—` in the env column.

### A separate tier: the Initial Workspace Template

The **analyst workspace payload** and the **init prompt** rendered on `/home`
are NOT in this file's tier system — they come from a registered *Initial
Workspace Template* (IWT) seed repo, resolved as **operator IWT clone > bundled
snapshot in the wheel**. See
[`initial-workspace-override.md`](initial-workspace-override.md) and
[`seed-repo-contract.md`](seed-repo-contract.md). Configure it at
`/admin/server-config` → *Initial Workspace Template*.

### Infra patterns and knob reachability

Whether the **env-var tier** is reachable at deploy time depends on the infra
pattern serving the instance:

- **Self-contained infra** (the deployment writes its own `/opt/agnes/.env`):
  can set any `AGNES_*` env knob directly.
- **Upstream `infra/modules/customer-instance` module** (consumed via an
  `infra-vX.Y.Z` tag): can only set the env knobs the module exposes as
  Terraform variables. If the module doesn't expose a knob, that instance falls
  through to the `instance.yaml` tier (admin UI) for it.

The module's `home_route` variable is the canonical example — it writes
`AGNES_HOME_ROUTE` only when set, otherwise leaving the route YAML-settable.

---

## Per-instance knob reference

Every resolver lives in [`app/instance_config.py`](../app/instance_config.py).
Set the env var in `.env`/Terraform, or the YAML path in `instance.yaml`.

### Branding & UI

| Knob | Env override | `instance.yaml` path | Default | Resolver |
|------|--------------|----------------------|---------|----------|
| Deployment display name (page titles, email subjects) | — | `instance.name` | `AI Harness` | `get_instance_name()` |
| Header subtitle | — | `instance.subtitle` | `""` | `get_instance_subtitle()` |
| Product brand string (hero copy, CTAs, setup script) | `AGNES_INSTANCE_BRAND` | `instance.brand` | `Agnes` | `get_instance_brand()` |
| Inline `<svg>` logo for the header brand slot | `AGNES_INSTANCE_LOGO_SVG` | `instance.logo_svg` | `""` (text brand) | `get_instance_logo_svg()` |
| UI theme/palette (`blue`/`navy`/`dark`/`auto`) | `AGNES_INSTANCE_THEME` | `instance.theme` | `blue` | `get_instance_theme()` |
| Analyst workspace folder name (`~/<name>`) | `AGNES_WORKSPACE_DIR_NAME` | `instance.workspace_dir` | derived from brand (non-alphanumerics stripped) | `get_workspace_dir_name()` |
| Operator-injected HTML/JS blocks (analytics, widgets) | — | `instance.custom_scripts` | `[]` | `get_custom_scripts()` |
| Hide individual `/login` feature cards (keys: `data`, `marketplace`, `mcp`, `memory`, `anywhere`; list or comma-string) | `AGNES_INSTANCE_HIDE_LOGIN_FEATURES` | `instance.hide_login_features` | `""` (nothing hidden) | `get_hidden_login_features()` |
| Legacy theme block (colors/fonts) | — | `theme` | `{}` | `get_theme()` |

### Onboarding & `/home`

| Knob | Env override | `instance.yaml` path | Default | Resolver |
|------|--------------|----------------------|---------|----------|
| Landing route after auth (`/home` vs `/dashboard`) | `AGNES_HOME_ROUTE` | `instance.home_route` | `/dashboard` | `get_home_route()` |
| Show the "turn on auto-accept mode" install block | `AGNES_HOME_SHOW_AUTOMODE` | `instance.home.show_automode` | `true` | `get_home_automode_visibility()` |
| Show the homepage status frame (sync/sessions/tokens) | `AGNES_HOME_SHOW_STATUS_FRAME` | `instance.home.show_status_frame` | `true` | `get_home_status_frame_visibility()` |
| Operator-authored Overview HTML on `/home` | `AGNES_INSTANCE_OVERVIEW` | `instance.overview` | `""` (hidden) | `get_instance_overview()` |
| Operator-authored Support HTML on `/home` | `AGNES_INSTANCE_SUPPORT` | `instance.support` | `""` (hidden) | `get_instance_support()` |
| Operator-authored preamble injected at the TOP of the `agnes init` install prompt (above `Set up the … CLI`). Empty/unset emits zero lines (default prompt byte-identical). `{instance_brand}` and the other server-side placeholders are substituted, but it must NOT contain literal `{server_url}`/`{token}` (those resolve at click time, not in the preamble). | `AGNES_INSTANCE_CUSTOM_PREAMBLE` | `instance.custom_preamble` | `""` (no extra lines) | `get_instance_custom_preamble()` |
| Admin contact address for user-side "email admin" prompts | `AGNES_INSTANCE_ADMIN_EMAIL` | `instance.admin_email` | `""` | `get_instance_admin_email()` |
| Infrastructure/provisioning repo URL (used by operator plugin to name the concrete infra repo for this instance; empty = vendor-neutral OSS default) | `AGNES_INFRA_REPO_URL` | `instance.infra_repo_url` | `""` (unset) | `get_infra_repo_url()` |
| Refresh-cadence string shown in the welcome prompt | — | `instance.sync_interval` | `1 hour` | `get_sync_interval()` |

### Connector pre-provisioning

| Knob | Env override | `instance.yaml` path | Default | Resolver |
|------|--------------|----------------------|---------|----------|
| Shared Google Workspace CLI OAuth client (id/secret/project/insecure-transport) | `AGNES_GWS_CLIENT_ID`, `AGNES_GWS_CLIENT_SECRET`, `AGNES_GWS_PROJECT_ID`, `AGNES_GWS_OAUTHLIB_INSECURE_TRANSPORT` | `instance.gws.{client_id,client_secret,project_id,oauthlib_insecure_transport}` | unset / `1` | `get_gws_oauth_credentials()` |
| Atlassian Cloud site URL baked into the connector prompt | `AGNES_ATLASSIAN_BASE_URL` | `instance.atlassian.base_url` | `""` (ask user) | `get_atlassian_base_url()` |

### Data source, auth & structural sections

| Knob | Env override | `instance.yaml` path | Default | Resolver |
|------|--------------|----------------------|---------|----------|
| Data source type (`keboola`/`bigquery`/`local`) | `DATA_SOURCE` | `data_source.type` | `local` | `get_data_source_type()` |
| Public base URL (used by Slack bot to mint **absolute** `/slack/bind` magic-link + `/chat` deep links — request-less code paths can't synthesize a base URL otherwise) | `PUBLIC_URL` | `server.public_url` | unset (links degrade to root-relative) | `get_public_url()` |
| Inbound Slack transport (`http`/`socket`) | `SLACK_TRANSPORT` | `chat.slack.transport` | `http` | `get_slack_transport()` |
| Allowed login email domains | — | `auth.allowed_domain` | `[]` | `get_allowed_domains()` |
| Full auth block | — | `auth` | `{}` | `get_auth_config()` |
| Dataset registry | — | `datasets` | `{}` | `get_datasets()` |
| Corporate Memory block | — | `corporate_memory` | `{}` | `get_corporate_memory_config()` |

### Flea-market upload guardrails

See [`STORE_GUARDRAILS.md`](STORE_GUARDRAILS.md) for the pipeline these tune.

| Knob | `instance.yaml` path | Default | Resolver |
|------|----------------------|---------|----------|
| Guardrail block | `guardrails` | `{}` | `get_guardrails_config()` |
| Pipeline enabled (operator intent) | `guardrails.enabled` | `true` | `get_guardrails_enabled()` |
| LLM review model tier | `guardrails.review_model` | `haiku` | `get_guardrails_review_model()` |
| Per-submitter blocked-row quota / day | `guardrails.blocked_quota_per_day` | `50` | `get_guardrails_blocked_quota_per_day()` |
| Blocked-bundle byte TTL (days) | `guardrails.blocked_bundle_ttl_days` | `30` | `get_guardrails_blocked_bundle_ttl_days()` |
| Stuck-review reaper grace (seconds) | `guardrails.stuck_review_grace_seconds` | `1800` | `get_guardrails_stuck_review_grace_seconds()` |
| Min description chars | `guardrails.min_description_chars` | `60` | `get_guardrails_min_description_chars()` |
| Min slash-command description chars | `guardrails.min_command_description_chars` | `25` | `get_guardrails_min_command_description_chars()` |
| Min distinct words in a description | `guardrails.min_distinct_words` | `5` | `get_guardrails_min_distinct_words()` |
| Min skill/agent body chars | `guardrails.min_body_chars` | `200` | `get_guardrails_min_body_chars()` |

---

## Annotated `instance.yaml` examples

The main configuration file lives at `config/instance.yaml`. See
`config/instance.yaml.example` for the full annotated template.

### Instance branding

```yaml
instance:
  name: "AI Harness"        # UI title, email subjects (get_instance_name)
  subtitle: "Acme Corp"          # Header subtitle (get_instance_subtitle)
  copyright: "Acme Corp"         # Footer copyright
  brand: "Acme Analyst"          # Product brand string (get_instance_brand)
  theme: "blue"                  # UI palette (get_instance_theme)
  home_route: "/home"            # Landing after auth (get_home_route)
```

### Authentication

```yaml
auth:
  allowed_domain: "acme.com"     # Email domain restriction for login
```

Only emails from this domain can log in via Google OAuth or email magic link.
Google OAuth is optional — if not configured, only email magic link auth is
available.

### Email

```yaml
email:
  from_address: "noreply@acme.com"
  from_name: "Acme Data Analyst"
  smtp_host: "${SMTP_HOST}"
  smtp_port: 587
  smtp_user: "${SMTP_USER}"
  smtp_password: "${SMTP_PASSWORD}"
```

Used for magic link authentication. Without SMTP configured, magic links are
shown directly in the browser (development mode). Compatible with any SMTP relay
(Gmail, Mailgun, SendGrid SMTP, etc.).

### Server

```yaml
server:
  host: "10.0.0.1"              # Server IP
  hostname: "data.acme.com"     # Server DNS name
```

### Desktop App

```yaml
desktop:
  jwt_issuer: "acme-analyst"
  jwt_secret: "${DESKTOP_JWT_SECRET}"
  url_scheme: "acme-analyst"
```

### Data Source

```yaml
data_source:
  type: "keboola"               # keboola, bigquery, local (get_data_source_type)
```

### Users

```yaml
users:
  admin@acme.com:
    display_name: "John Doe"
    km_admin: true              # Corporate Memory admin (optional)

username_mapping: {}            # Map webapp email -> server username if different
```

### Datasets

```yaml
datasets:
  jira:
    label: "Jira Tickets"
    description: "Support tickets"
    size_hint: "~50 MB"
    requires: null
  jira_attachments:
    label: "Jira Attachments"
    description: "File attachments"
    size_hint: "~500 MB+"
    requires: "jira"
```

### Catalog

```yaml
catalog:
  categories:
    sales:
      label: "Sales"
      icon: "sales"
    hr:
      label: "HR"
      icon: "hr"
  order: ["sales", "hr"]
```

---

## .env infrastructure variables

These are deployment secrets and infrastructure paths — distinct from the
per-instance knobs above. Copy `config/.env.template` to `.env` and fill in
values. Never commit `.env`.

### Required

| Variable | Description |
|----------|-------------|
| `JWT_SECRET_KEY` | FastAPI JWT token secret (generate with `secrets.token_hex(32)`) |
| `SESSION_SECRET` | Session cookie secret (generate with `secrets.token_hex(32)`) |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |

### Data Source (Keboola)

| Variable | Description |
|----------|-------------|
| `KEBOOLA_STORAGE_TOKEN` | Keboola Storage API token |
| `KEBOOLA_STACK_URL` | Keboola stack URL |
| `DATA_DIR` | Data directory path (default: `/data` in Docker, `./data` locally) |

### Data Source (BigQuery)

| Variable | Description |
|----------|-------------|
| `BIGQUERY_PROJECT` | GCP project for job execution/billing |
| `BIGQUERY_LOCATION` | BigQuery location (e.g., `US`, `us-central1`) |

### Optional

| Variable | Description |
|----------|-------------|
| `SMTP_HOST` | SMTP relay host for magic link emails |
| `SMTP_PORT` | SMTP port (587 for STARTTLS, 465 for SSL) |
| `SMTP_USER` | SMTP username |
| `SMTP_PASSWORD` | SMTP password |
| `TELEGRAM_BOT_TOKEN` | For Telegram notifications |
| `ANTHROPIC_API_KEY` | For Corporate Memory AI extraction AND `agnes admin ask` (LLM text-to-SQL on telemetry). Without this, both features show a clear 503 error and skip silently. |
| `LLM_API_KEY` | API key for LLM proxy (LiteLLM, OpenRouter, etc.) |
| `JIRA_WEBHOOK_SECRET` | For Jira webhook integration |
| `JIRA_API_TOKEN` | For Jira REST API access |
| `DESKTOP_JWT_SECRET` | Separate secret for desktop app tokens |
| `CONFIG_DIR` | Override config directory path |
| `LOG_LEVEL` | Logging level: `debug`, `info`, `warning`, `error` |
| `DOMAIN` | Public hostname for Caddy TLS (production profile) |
| `AGNES_BASE_URL` | Operator-pinned public origin (see below). Wins over `SERVER_URL`. |
| `SERVER_URL` | Deployment's public URL (see below). |
| `AGNES_INTERNAL_URL` | Data-rails-only server URL for the chat sandbox + workspace seed (see below). |

### Public origin & data-rails URLs

Three URL variables with distinct jobs:

- **`AGNES_BASE_URL`**, then **`SERVER_URL`** — the *public origin* pin
  (`app/auth/public_url.py`). First non-empty wins; feeds MCP OAuth issuer +
  discovery metadata, connector/Cowork bundles, and external links. When both
  are unset, the origin is derived per-request from the incoming host
  (proxy-aware), so most TLS-proxied deployments don't need either.
- **`SERVER_URL`**, then **`AGNES_INTERNAL_URL`** — the *data rails* chain
  (`agnes_server_url()` in `app/chat/manager.py`): the URL the cloud-chat
  sandbox (`AGNES_SERVER` for the agnes CLI) and the seeded analyst workspace
  use to reach the server. Falls back to loopback for local dev.

> **Plain-HTTP deployments** (`tls_mode=none`, no TLS proxy): an `http://`
> non-localhost URL cannot serve as an MCP OAuth issuer (RFC 8414 requires
> HTTPS), so setting `SERVER_URL`/`AGNES_BASE_URL` to one disables the
> streamable MCP connector at `/api/mcp/http` — the app boots and logs a loud
> ERROR, everything else keeps working. To get cloud-chat data rails without
> touching the public origin (and without the ERROR), set `AGNES_INTERNAL_URL`
> instead — it feeds *only* the rails chain, never OAuth metadata.

---

## Related docs

- [`initial-workspace-override.md`](initial-workspace-override.md) — analyst
  workspace payload + init prompt (the IWT tier).
- [`seed-repo-contract.md`](seed-repo-contract.md) — seed-repo layout +
  install-prompt placeholders.
- [`STORE_GUARDRAILS.md`](STORE_GUARDRAILS.md) — flea-market guardrail pipeline.
- `infra/modules/customer-instance/variables.tf` — Terraform knobs the upstream
  module exposes (incl. `home_route`).
