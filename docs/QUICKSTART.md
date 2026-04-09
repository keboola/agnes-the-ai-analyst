# Quick Start Guide

## Prerequisites

- Python 3.10+
- SSH access to a Linux server (for production deployment)
- Data source credentials (Keboola token, BigQuery service account, etc.)

## Local Development Setup

1. Clone the repository:
   ```bash
   git clone <repo-url>
   cd ai-data-analyst
   ```

2. Run the initialization script:
   ```bash
   bash scripts/init.sh
   ```

3. Configure your instance:
   ```bash
   cp config/instance.yaml.example config/instance.yaml
   # Edit config/instance.yaml with your settings
   ```

4. Set up environment variables:
   ```bash
   # Edit .env with your data source credentials
   ```

5. Register your tables:
   ```bash
   # Tables are registered via the admin API or web UI — no config file needed
   ```

6. Sync data:
   ```bash
   source .venv/bin/activate
   python -m src.data_sync
   ```

## Server Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for full server setup instructions.

## Using with Claude Code

Open the project in Claude Code. The CLAUDE.md file will guide the AI assistant through setup and analysis workflows.

### Analyst Setup

1. Visit your instance URL (e.g., https://data.yourcompany.com)
2. Sign in with your company email
3. Register your SSH key
4. Follow the setup instructions to sync data locally

### Analysis Workflow

1. Sync latest data: `bash server/scripts/sync_data.sh`
2. Open Claude Code in your project directory
3. Ask Claude to analyze your data using DuckDB
