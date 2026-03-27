# Setup — Complete guide for deploying a new instance

## Prerequisites
- Docker and Docker Compose installed
- Domain name pointing to server IP (for SSL)
- Data source credentials (Keboola token OR BigQuery service account)

## Steps

1. Clone the repository:
   ```bash
   git clone <repo-url>
   cd data-analyst
   ```

2. Create configuration:
   ```bash
   cp config/instance.yaml.example config/instance.yaml
   # Edit instance.yaml with your settings
   ```

3. Create environment file:
   ```bash
   cp config/.env.template .env
   # Fill in: JWT_SECRET_KEY, KEBOOLA_STORAGE_TOKEN (or BIGQUERY_PROJECT), etc.
   ```

4. Start services:
   ```bash
   docker compose up -d
   ```

5. Verify health:
   ```bash
   da status --server http://your-server:8000
   ```

6. Create first admin user:
   ```bash
   da login --email admin@company.com --server http://your-server:8000
   da admin add-user admin@company.com --role admin
   ```

7. Trigger initial data sync:
   ```bash
   da admin trigger-sync
   ```

8. Verify data:
   ```bash
   da status
   ```

## Troubleshooting

- **Cannot connect:** Check `docker compose ps`, verify port 8000 is exposed
- **Auth fails:** Verify JWT_SECRET_KEY is set in .env
- **No data:** Check data source credentials, run `da diagnose`
