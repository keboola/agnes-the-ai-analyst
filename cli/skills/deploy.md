# Deploy — Complete server deployment guide for AI agents

## Prerequisites

You need:
- A Linux server with SSH access (Ubuntu 22.04+ recommended)
- Docker + Docker Compose installed on the server
- A domain pointing to the server IP (optional but recommended for SSL)
- Keboola Storage Token + Stack URL + Project ID (for data source)

## Step-by-step deployment

### 1. Connect to server

```bash
ssh user@your-server-ip
```

### 2. Clone the repository

```bash
git clone https://github.com/padak/tmp_oss.git /opt/data-analyst
cd /opt/data-analyst
git checkout feature/v2-fastapi-duckdb-docker-cli
```

### 3. Create .env file

```bash
cp config/.env.template .env
```

Edit `.env` with these REQUIRED values:
```
JWT_SECRET_KEY=<random 32+ char string>
DATA_DIR=/data
DATA_SOURCE=keboola
KEBOOLA_STORAGE_TOKEN=<your token>
KEBOOLA_STACK_URL=https://connection.keboola.com
KEBOOLA_PROJECT_ID=<your project id>
```

Generate a random JWT secret:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 4. Start Docker

```bash
docker compose up -d
```

Wait for health check:
```bash
sleep 5
curl http://localhost:8000/api/health
```

Expected: `{"status": "healthy", ...}`

### 5. Bootstrap first admin user

From your LOCAL machine (not the server):

```bash
da setup init --server http://SERVER_IP:8000
da setup bootstrap admin@company.com
```

Or directly via curl:
```bash
curl -X POST http://SERVER_IP:8000/auth/bootstrap \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@company.com", "name": "Admin"}'
```

This returns a JWT token. Save it.

### 6. Trigger first data sync

```bash
da setup first-sync
```

Or via curl:
```bash
curl -X POST http://SERVER_IP:8000/api/sync/trigger \
  -H "Authorization: Bearer <TOKEN>"
```

### 7. Verify everything works

```bash
da setup verify --json
```

Expected: all checks PASS.

### 8. Add more users

```bash
da admin add-user analyst@company.com --role analyst
```

## Troubleshooting

### Server not reachable
```bash
# Check containers
docker compose ps

# Check logs
docker compose logs app --tail 50

# Restart
docker compose restart app
```

### Data sync fails
```bash
# Check logs
docker compose logs app --tail 50 | grep -i error

# Common issues:
# - KEBOOLA_STORAGE_TOKEN expired → get new token
# - KEBOOLA_STACK_URL wrong → check URL
# - data_description.md missing → needs table configuration
```

### No tables after sync
```bash
# Check if data_description.md exists
docker compose exec app ls /data/docs/data_description.md

# Check sync state
curl -H "Authorization: Bearer <TOKEN>" http://SERVER_IP:8000/api/sync/manifest
```

### Out of memory
```bash
# Check memory
docker stats --no-stream

# Increase Docker memory limit in docker-compose.yml:
# services.app.deploy.resources.limits.memory: 4g
```

## For Google OAuth (optional)

1. Go to https://console.cloud.google.com/apis/credentials
2. Create OAuth 2.0 Client ID (Web application)
3. Add authorized redirect URI: `https://your-domain.com/auth/google/callback`
4. Add to .env:
   ```
   GOOGLE_CLIENT_ID=<client id>
   GOOGLE_CLIENT_SECRET=<client secret>
   ```
5. Restart: `docker compose restart app`

## For SSL (optional)

Use Caddy as reverse proxy (simplest auto-SSL):
```bash
# Install Caddy
apt install caddy

# /etc/caddy/Caddyfile
your-domain.com {
    reverse_proxy localhost:8000
}

# Restart
systemctl restart caddy
```

## Agent deployment flow (automated)

An AI agent should follow this sequence:
1. `ssh user@server` — connect
2. Clone repo, create .env
3. `docker compose up -d` — start
4. `curl /api/health` — verify server
5. `curl -X POST /auth/bootstrap` — create admin
6. `curl -X POST /api/sync/trigger` — sync data
7. Wait 30-60 seconds
8. `curl /api/sync/manifest` — verify data
9. Report success with URL + admin credentials
