# Configuration Reference

## instance.yaml

The main configuration file for your AI Data Analyst instance. Located at `config/instance.yaml`.
See `config/instance.yaml.example` for the full annotated template.

### Instance Branding

```yaml
instance:
  name: "AI Data Analyst"        # UI title, email subjects
  subtitle: "Acme Corp"          # Header subtitle
  copyright: "Acme Corp"         # Footer copyright
```

### Authentication

```yaml
auth:
  allowed_domain: "acme.com"     # Email domain restriction for login
```

Only emails from this domain can log in via Google OAuth or email magic link.
Google OAuth is optional — if not configured, only email magic link auth is available.

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

Used for magic link authentication. Without SMTP configured, magic links are shown
directly in the browser (development mode). Compatible with any SMTP relay (Gmail,
Mailgun, SendGrid SMTP, etc.).

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
  type: "keboola"               # keboola, bigquery, local
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

## Environment Variables (.env)

Copy `config/.env.template` to `.env` and fill in values. The template contains
the full variable list with comments. Never commit `.env`.

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
| `ANTHROPIC_API_KEY` | For Corporate Memory AI (direct Anthropic) |
| `LLM_API_KEY` | API key for LLM proxy (LiteLLM, OpenRouter, etc.) |
| `JIRA_WEBHOOK_SECRET` | For Jira webhook integration |
| `JIRA_API_TOKEN` | For Jira REST API access |
| `DESKTOP_JWT_SECRET` | Separate secret for desktop app tokens |
| `CONFIG_DIR` | Override config directory path |
| `LOG_LEVEL` | Logging level: `debug`, `info`, `warning`, `error` |
| `DOMAIN` | Public hostname for Caddy TLS (production profile) |
