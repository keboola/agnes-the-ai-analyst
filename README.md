# Agnes — AI Data Analyst

Agnes is an open-source data distribution platform for AI analytical systems. It extracts data from configured sources into DuckDB, serves it via a FastAPI backend, and distributes Parquet files to analysts who query them locally using Claude Code and DuckDB.

Each data source produces a self-describing `extract.duckdb` file. The `SyncOrchestrator` attaches all extract databases into a master `analytics.duckdb`, making every table available through a unified view layer without copying data unnecessarily.

## Architecture: extract.duckdb Contract

Every connector produces the same output structure:

```
/data/extracts/{source_name}/
├── extract.duckdb          ← _meta table + views
└── data/                   ← parquet files (local sources only)
```

The orchestrator scans `/data/extracts/*/extract.duckdb`, attaches each into `analytics.duckdb`, and creates master views.

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   Keboola    │  │   BigQuery   │  │   Jira       │
│  extractor   │  │  extractor   │  │  webhooks    │
│ (DuckDB ext) │  │ (remote BQ)  │  │ (incremental)│
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       ▼                 ▼                 ▼
   extract.duckdb    extract.duckdb    extract.duckdb
   + data/*.parquet  (views → BQ)      + data/*.parquet
       │                 │                 │
       └─────────────────┼─────────────────┘
                         ▼
              SyncOrchestrator.rebuild()
              ATTACH → master views in analytics.duckdb
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
          FastAPI      CLI
          (serve)    (da sync)
```

## Supported Data Sources

| Source | Mode | Description |
|--------|------|-------------|
| **Keboola** | Batch pull | DuckDB Keboola extension downloads tables to Parquet on a schedule |
| **BigQuery** | Remote attach | DuckDB BQ extension; queries execute in BigQuery, no local download |
| **Jira** | Real-time push | Webhook receiver updates Parquet files incrementally |

Adding a new source means creating `connectors/<name>/extractor.py` that produces `extract.duckdb` with a `_meta` table (`table_name`, `description`, `rows`, `size_bytes`, `extracted_at`, `query_mode`). The orchestrator attaches it automatically.

## Quick Start with Docker

```bash
# Clone the repository
git clone https://github.com/keboola/agnes-the-ai-analyst.git
cd agnes-the-ai-analyst

# Copy and edit configuration
cp config/instance.yaml.example config/instance.yaml
cp config/.env.template .env
# Edit both files for your environment

# Start the app and scheduler
docker compose up

# Start with all optional services (Telegram bot, etc.)
docker compose --profile full up
```

Once running, the FastAPI app is available at `http://localhost:8000`. Trigger a manual sync:

```bash
curl -X POST http://localhost:8000/api/sync/trigger
```

## Development Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv && source .venv/bin/activate

# Install dependencies
uv pip install ".[dev]"

# Run FastAPI locally with hot reload
uvicorn app.main:app --reload

# Run the test suite
pytest tests/ -v
```

## Project Structure

```
├── src/                    # Core engine
│   ├── db.py               # DuckDB schema (system.duckdb, analytics.duckdb)
│   ├── orchestrator.py     # SyncOrchestrator — ATTACHes extract.duckdb files
│   ├── repositories/       # DuckDB-backed CRUD (sync_state, table_registry, users, etc.)
│   ├── profiler.py         # Data profiling
│   └── catalog_export.py   # OpenMetadata catalog export
├── app/                    # FastAPI application
│   ├── main.py             # App setup, router registration
│   ├── api/                # REST API (sync, data, catalog, admin, auth)
│   ├── auth/               # Auth providers (Google OAuth, email magic link, desktop JWT)
│   └── web/                # HTML dashboard routes
├── connectors/             # Data source connectors (extract.duckdb contract)
│   ├── keboola/            # Keboola: extractor.py (DuckDB extension) + client.py (fallback)
│   ├── bigquery/           # BigQuery: extractor.py (remote-only via DuckDB BQ extension)
│   └── jira/               # Jira: webhook + incremental parquet → extract.duckdb
├── cli/                    # CLI tool (`da sync`, `da query`, `da admin`)
├── services/               # Standalone services (scheduler, telegram_bot, ws_gateway, etc.)
├── scripts/                # Utility + migration scripts
├── config/                 # Configuration templates (instance.yaml.example)
├── docs/                   # Documentation + metric YAML definitions
└── tests/                  # Test suite (633 tests)
```

## Configuration

| File | Purpose |
|------|---------|
| `config/instance.yaml` | Instance-specific settings: branding, data source type, auth provider, Google domain |
| `.env` | Secrets and environment variables — never committed |
| `system.duckdb` `table_registry` table | Table definitions managed via `POST /api/admin/tables/{id}` or the web UI |

Copy the example to get started:

```bash
cp config/instance.yaml.example config/instance.yaml
```

See `config/instance.yaml.example` for all available options.

## Documentation

- [Hackathon TL;DR](docs/HACKATHON.md) — condensed deploy + dev playbooks (for both humans and AI agents)
- [Onboarding Guide](docs/ONBOARDING.md) — end-to-end Terraform deployment into a GCP project (recommended for production)
- [Cloudflare Access Auth](docs/auth-cloudflare.md) — SSO via Cloudflare Zero Trust tunnel
- [Deployment Guide](docs/DEPLOYMENT.md) — chooses between Terraform and Docker Compose; covers OSS self-host
- [Configuration Reference](docs/CONFIGURATION.md) — `instance.yaml`, env vars, per-instance options
- [Architecture](docs/architecture.md) — orchestrator, extractors, DB layout
- [Quickstart](docs/QUICKSTART.md) — local development

## Contributing

1. Fork the repository and create a feature branch.
2. Run `pytest tests/ -v` to verify all tests pass before opening a pull request.
3. Keep commits focused and messages concise.
4. Open a pull request against `main` with a clear description of the change.

For bugs and feature requests, open a GitHub issue.

## License

This project is licensed under the [MIT License](LICENSE).
