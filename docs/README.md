# Analyst Documentation

Documentation for **analysts** using the AI Data Analyst platform.

This folder is synced to all analyst machines in the `server/docs/` directory.

## Quick Start
- **[GETTING_STARTED.md](GETTING_STARTED.md)** - New user guide and setup instructions

## Data Reference
- **[data_description.md](data_description.md)** - Single source of truth for table schemas, relationships, and sync strategies
- **[jira_schema.md](jira_schema.md)** - Detailed Jira data schema

## Business Metrics
- **[metrics/](metrics/)** - Standardized metric definitions
  - `metrics/metrics.yml` - Index of all available metrics
  - `metrics/finance/` - Financial metrics (infrastructure costs, retention)
  - `metrics/product_usage/` - Usage metrics (consumption, limits, telemetry)
  - `metrics/sales_revenue/` - Sales metrics (MRR, ARR, expansions)
  - `metrics/weekly_leadership_kpis/` - Weekly KPIs for leadership reporting

## Tools & Integrations
- **[notifications.md](notifications.md)** - How to send Telegram notifications from your analysis scripts
- **[setup/](setup/)** - Bootstrap configuration and Claude Code templates

## For Developers

Server administration, development docs, and internal planning are in the **`dev_docs/`** folder (not synced to analyst machines).
