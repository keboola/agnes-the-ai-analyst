# Quick Start Guide

## Prerequisites

- Python 3.10+
- Docker + Docker Compose (for production deployment)
- Data source credentials (Keboola token, BigQuery project, etc.)

## Local Development Setup

1. Clone the repository:
   ```bash
   git clone <repo-url>
   cd ai-data-analyst
   ```

2. Create virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   uv pip install ".[dev]"
   ```

3. Configure your instance:
   ```bash
   cp config/instance.yaml.example config/instance.yaml
   # Edit config/instance.yaml with your settings
   ```

4. Set up environment variables:
   ```bash
   cp config/.env.template .env
   # Edit .env with your data source credentials
   ```

5. Register your tables via the admin API or CLI:
   ```bash
   # Via CLI
   da admin register-table --source-type keboola --bucket "in.c-crm" --table "company" --query-mode local

   # Or start the server and use the web UI at /admin/tables
   ```

6. Start the FastAPI server:
   ```bash
   uvicorn app.main:app --reload
   ```

7. Trigger a data sync:
   ```bash
   curl -X POST http://localhost:8000/api/sync/trigger
   # Or: da sync
   ```

## Docker Deployment

```bash
# Start app + scheduler
docker compose up

# Include telegram bot
docker compose --profile full up

# HTTPS mode — Caddy + corporate-CA certs
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tls.yml \
    --profile tls up -d
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for full server setup instructions.

## Using with Claude Code

Open the project in Claude Code. The CLAUDE.md file will guide the AI assistant through setup and analysis workflows.

### Analyst Setup

1. Visit your instance URL (e.g., https://data.yourcompany.com)
2. Sign in with your company email
3. Access data through the API or download parquets for local analysis

### Analysis Workflow

1. Sync latest data: `curl -X POST https://data.yourcompany.com/api/sync/trigger`
2. Open Claude Code in your project directory
3. Ask Claude to analyze your data using DuckDB

## Hackathon

See [`HACKATHON.md`](HACKATHON.md) for the deploy-and-develop playbook. Per-developer dev VMs are the supported pattern — point your VM at your branch image with `gcloud compute ssh <vm> --command "sudo sed -i 's/^AGNES_TAG=.*/AGNES_TAG=dev-<slug>/' /opt/agnes/.env && sudo /usr/local/bin/agnes-auto-upgrade.sh"`.
