# Deployment Guide

## Server Requirements

- Ubuntu 24.04 LTS
- e2-small (2 vCPU, 2 GB RAM) or larger
- 30 GB SSD boot disk
- Docker + Docker Compose
- Public IP with port 8000 open

## Quick Deploy (GCP)

### 1. Create VM

```bash
gcloud compute instances create data-analyst-dev \
  --project=YOUR_PROJECT \
  --zone=europe-west1-b \
  --machine-type=e2-small \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-ssd \
  --tags=data-analyst-dev
```

### 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in for group change to take effect
```

### 3. Set up deploy key

Generate an SSH key for GitHub access:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/agnes_deploy -N "" -C "agnes-deploy"
cat ~/.ssh/agnes_deploy.pub
# Add the public key as a deploy key on the GitHub repo
```

Configure SSH to use it:

```bash
cat > ~/.ssh/config << 'EOF'
Host github.com
  IdentityFile ~/.ssh/agnes_deploy
  StrictHostKeyChecking no
EOF
chmod 600 ~/.ssh/config
```

### 4. Clone and configure

```bash
sudo mkdir -p /opt/data-analyst
sudo chown $USER:$USER /opt/data-analyst
git clone git@github.com:keboola/agnes-the-ai-analyst.git /opt/data-analyst
cd /opt/data-analyst
```

Create `.env`:

```bash
cat > .env << 'EOF'
JWT_SECRET_KEY=<generate: python3 -c "import secrets; print(secrets.token_hex(32))">
DATA_DIR=/data
LOG_LEVEL=info
KEBOOLA_STORAGE_TOKEN=<your-keboola-token>
KEBOOLA_STACK_URL=<your-keboola-stack-url>
SEED_ADMIN_EMAIL=<admin-email>
EOF
chmod 600 .env
```

Create `config/instance.yaml` (optional, for Keboola source config):

```bash
cp config/instance.yaml.example config/instance.yaml
# Edit with your values
```

### 5. Create data directories

```bash
sudo mkdir -p /data/state /data/analytics /data/extracts
sudo chown -R $USER:$USER /data
```

### 6. Build and start

```bash
cd /opt/data-analyst
docker compose up -d
```

Wait for health check:

```bash
curl -s http://localhost:8000/api/health | python3 -m json.tool
```

### 7. Bootstrap admin user

```bash
curl -X POST http://localhost:8000/auth/bootstrap
```

This creates the first admin user using `SEED_ADMIN_EMAIL` from `.env`.

### 8. Register tables and run first extraction

Register tables via the admin API, then:

```bash
# Stop app first — DuckDB only supports one writer
docker compose down
docker compose run --rm extract
docker compose up -d
```

### 9. Open firewall (GCP)

```bash
gcloud compute firewall-rules create allow-data-analyst-dev \
  --allow tcp:8000 \
  --target-tags=data-analyst-dev \
  --project=YOUR_PROJECT
```

## Important Notes

### DuckDB Write Locking

DuckDB only supports one writer at a time. When running extraction:

```bash
docker compose down          # Stop app + scheduler
docker compose run --rm extract   # Run extraction
docker compose up -d         # Restart
```

The scheduler triggers extraction via the API, which handles locking internally.

### Environment Variable Changes

`docker compose restart` does NOT reload `.env`. Use:

```bash
docker compose down && docker compose up -d
```

### Services

| Service | Profile | Description |
|---------|---------|-------------|
| `app` | default | FastAPI server on port 8000 |
| `scheduler` | default | Periodic sync + extraction |
| `extract` | extract | One-shot data extraction |
| `telegram-bot` | full | Telegram notifications |
| `ws-gateway` | full | WebSocket gateway |
| `corporate-memory` | full | Knowledge collector |
| `session-collector` | full | Session collection |

Start all services: `docker compose --profile full up -d`

### Directory Structure on Server

```
/opt/data-analyst/          # Git repo
  .env                      # Secrets (chmod 600)
  config/instance.yaml      # Instance config

/data/                      # Persistent data (Docker volume)
  state/system.duckdb       # System state (users, registry, sync)
  analytics/server.duckdb   # Analytics views
  extracts/                 # Per-source extract.duckdb + parquets
    keboola/
    bigquery/
    jira/
```

## CI/CD

Push to `main` triggers GitHub Actions:
1. Run test suite (607 tests)
2. Build Docker image
3. Push to GHCR (`ghcr.io/keboola/agnes-the-ai-analyst`)
4. Deploy via Kamal

## Monitoring

- Health: `GET /api/health`
- Logs: `docker compose logs -f app`
- Disk: `df -h /data`
- Tables: `curl -s http://localhost:8000/api/catalog | python3 -m json.tool`
