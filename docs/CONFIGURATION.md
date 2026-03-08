# Configuration Reference

## instance.yaml

The main configuration file for your AI Data Analyst instance. Located at `config/instance.yaml`.

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
  allowed_domain: "acme.com"     # Google OAuth domain restriction
```

Only emails from this domain can log in via Google OAuth. External users can be added via password auth (requires SendGrid).

### Email

```yaml
email:
  from_address: "noreply@acme.com"
  from_name: "Acme Data Analyst"
```

Used for password auth setup and reset emails. Requires `SENDGRID_API_KEY` in `.env`.

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
  url_scheme: "acme-analyst"
```

### Data Source

```yaml
data_source:
  type: "keboola"               # keboola, csv, bigquery
```

### Users

```yaml
users:
  john.doe:
    name: "John Doe"
    initials: "JD"
  jane.smith:
    name: "Jane Smith"
    initials: "JS"

username_mapping:
  john.doe: john                 # Only if webapp and server names differ
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

### Required

| Variable | Description |
|----------|-------------|
| `WEBAPP_SECRET_KEY` | Flask session secret |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |

### Data Source (Keboola)

| Variable | Description |
|----------|-------------|
| `KEBOOLA_STORAGE_TOKEN` | Keboola Storage API token |
| `KEBOOLA_STACK_URL` | Keboola stack URL |
| `KEBOOLA_PROJECT_ID` | Keboola project ID |
| `DATA_DIR` | Data directory path |

### Optional

| Variable | Description |
|----------|-------------|
| `SENDGRID_API_KEY` | For password auth emails |
| `TELEGRAM_BOT_TOKEN` | For Telegram notifications |
| `ANTHROPIC_API_KEY` | For Corporate Memory AI |
| `JIRA_WEBHOOK_SECRET` | For Jira integration |
| `CONFIG_DIR` | Override config directory path |
