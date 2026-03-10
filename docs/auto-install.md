# Automated Installation Guide

Step-by-step deployment of AI Data Analyst on a clean Ubuntu 24.04 VM.

Two repos are involved:
- **OSS repo** (public/private): application code (`padak/tmp_oss`)
- **Instance repo** (private): your config, secrets template, data schema (`padak/tmp_oss_cfg`)

## Architecture on Server

```
/opt/data-analyst/
├── repo/              # OSS repo clone
│   ├── config/
│   │   └── instance.yaml -> ../../instance/config/instance.yaml  (symlink)
│   ├── webapp/
│   ├── server/
│   └── ...
├── instance/          # Private instance repo clone
│   ├── config/
│   │   ├── instance.yaml          # Branding, auth domains, data source
│   │   └── data_description.md    # Data schema (when configured)
│   ├── docs/setup/                # Custom CLAUDE.md template, etc.
│   ├── .env.example               # Secrets template
│   └── README.md
├── .env               # Secrets (not in git, from .env.example)
├── .venv/             # Python virtual environment
└── logs/              # Application logs
```

Key principle: OSS repo has no secrets/config. Instance repo has no code. Symlinks bridge them.

## Prerequisites

1. **DigitalOcean API token** with `ssh_key` scope (or any Ubuntu 24.04 VM)
2. **Two GitHub repos**: one for OSS code, one for private instance config
3. **SSH key** on your local machine for server access

### Known Issues

- `python3-venv` must be installed before `server/setup.sh` (Ubuntu 24.04 omits it)
- `webapp-setup.sh` generates SSL nginx config - use HTTP-only for IP-only deployments
- DigitalOcean cloud-init cannot override password expiry; must use `ssh_keys` API field

## Step 0: Create Repos

```bash
# Push OSS code to GitHub
git remote add origin git@github.com:YOUR_ORG/YOUR_OSS_REPO.git
git push -u origin main

# Create private instance config repo on GitHub (empty, private)
# We'll populate it from the server after setup
```

## Step 1: Provision VM

### 1a: Create Droplet (DigitalOcean)

```bash
# Register SSH key (requires ssh_key scope on API token)
curl -s -X POST -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $DO_TOKEN" \
    -d '{"name":"my-key","public_key":"ssh-ed25519 AAAA..."}' \
    "https://api.digitalocean.com/v2/account/keys"

# Create droplet with SSH key
curl -s -X POST -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $DO_TOKEN" \
    -d '{
      "name":"data-analyst-1",
      "size":"s-1vcpu-2gb",
      "region":"ams3",
      "image":"ubuntu-24-04-x64",
      "ssh_keys":["KEY_ID_OR_FINGERPRINT"]
    }' \
    "https://api.digitalocean.com/v2/droplets"
```

### 1b: Install Prerequisites

```bash
ssh root@DROPLET_IP

# Wait for apt lock (auto-updates run on first boot)
apt update && apt install -y python3.12-venv python3-pip
```

### 1c: Generate Deploy Keys

Two separate keys - one per repo, for security isolation:

```bash
# Key for OSS repo
ssh-keygen -t ed25519 -f /root/.ssh/deploy_key -N "" -C "oss-app@$(hostname)"

# Key for private instance config repo
ssh-keygen -t ed25519 -f /root/.ssh/instance_key -N "" -C "instance-config@$(hostname)"
```

Add each public key as a **deploy key** on its respective GitHub repo:
- `deploy_key.pub` -> OSS repo Settings > Deploy Keys
- `instance_key.pub` -> Instance repo Settings > Deploy Keys

Configure SSH to use the right key per repo:

```bash
cat > /root/.ssh/config << 'EOF'
# OSS application repo
Host github-oss
  HostName github.com
  IdentityFile /root/.ssh/deploy_key
  StrictHostKeyChecking no

# Instance config repo (private)
Host github-cfg
  HostName github.com
  IdentityFile /root/.ssh/instance_key
  StrictHostKeyChecking no
EOF
chmod 600 /root/.ssh/config
```

### 1d: Clone OSS Repo & Run Setup

```bash
git clone git@github-oss:YOUR_ORG/YOUR_OSS_REPO.git /opt/data-analyst/repo
cd /opt/data-analyst/repo
REPO_URL="git@github-oss:YOUR_ORG/YOUR_OSS_REPO.git" bash server/setup.sh
```

### Step 1 Checklist

| # | Check | Expected |
|---|-------|----------|
| 1.1 | Groups | data-ops, dataread, data-private exist |
| 1.2 | Deploy user | uid deploy, groups: deploy, data-ops |
| 1.3 | Directories | /opt/data-analyst/{repo,.venv,logs} |
| 1.4 | Python venv | Flask loads in .venv |
| 1.5 | Scripts | add-analyst, list-analysts in /usr/local/bin |

## Step 2: Webapp Setup

### 2a: Run webapp-setup.sh

```bash
export SERVER_HOSTNAME="your-domain-or-ip"
bash server/webapp-setup.sh
```

For IP-only (no SSL), replace nginx config:

```bash
cat > /etc/nginx/sites-available/webapp << 'NGINX'
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://unix:/run/webapp/webapp.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
    location /static/ {
        alias /opt/data-analyst/repo/webapp/static/;
        expires 1d;
    }
    location /health {
        proxy_pass http://unix:/run/webapp/webapp.sock;
        proxy_set_header Host $host;
        access_log off;
    }
}
NGINX
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx
```

### 2b: Create .env

```bash
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

cat > /opt/data-analyst/.env << EOF
WEBAPP_SECRET_KEY="${SECRET_KEY}"
SERVER_HOST="YOUR_IP"
SERVER_HOSTNAME="YOUR_IP_OR_DOMAIN"
GOOGLE_CLIENT_ID="placeholder"
GOOGLE_CLIENT_SECRET="placeholder"
DATA_SOURCE="local"
DATA_DIR="/data/src_data"
EOF

chown root:data-ops /opt/data-analyst/.env
chmod 640 /opt/data-analyst/.env
```

### 2c: Create Data Directories & Start

```bash
mkdir -p /data/src_data/{parquet,metadata} /data/docs /data/scripts
chown -R root:data-ops /data
chmod -R 2775 /data

mkdir -p /run/webapp
chown www-data:www-data /run/webapp

systemctl daemon-reload
systemctl start webapp
systemctl enable webapp
```

### Step 2 Checklist

| # | Check | Expected |
|---|-------|----------|
| 2.1 | Nginx | active, port 80 |
| 2.2 | Webapp | active (gunicorn) |
| 2.3 | Health | `curl http://IP/health` returns JSON |
| 2.4 | Login page | HTTP 200 at /login |

## Step 3: Instance Configuration (Private Repo)

### 3a: Clone Instance Repo

```bash
git clone git@github-cfg:YOUR_ORG/YOUR_INSTANCE_REPO.git /opt/data-analyst/instance
chown -R root:data-ops /opt/data-analyst/instance
chmod -R 770 /opt/data-analyst/instance
```

### 3b: Initialize Instance Config (if empty repo)

If this is a fresh instance repo, create the initial config:

```bash
cd /opt/data-analyst/instance
mkdir -p config docs/setup

cat > config/instance.yaml << 'YAML'
instance:
  name: "My Data Analyst"
  subtitle: "My Organization"
  copyright: "My Org"

server:
  hostname: "YOUR_IP_OR_DOMAIN"
  host: "YOUR_IP"
  app_dir: "/opt/data-analyst"

auth:
  allowed_domain: "mycompany.com"
  webapp_secret_key: "${WEBAPP_SECRET_KEY}"

data_source:
  type: "local"

catalog:
  categories: {}
YAML

# Create .env.example as a template for future deployments
cat > .env.example << 'ENV'
WEBAPP_SECRET_KEY="generate-with: python3 -c 'import secrets; print(secrets.token_hex(32))'"
SERVER_HOST="server-ip"
SERVER_HOSTNAME="server-ip-or-domain"
GOOGLE_CLIENT_ID="placeholder"
GOOGLE_CLIENT_SECRET="placeholder"
DATA_SOURCE="local"
DATA_DIR="/data/src_data"
ENV

cat > .gitignore << 'GI'
.env
.env.local
*.swp
*~
.DS_Store
GI

git add -A && git commit -m "Initial instance config" && git push origin main
```

### 3c: Symlink Config into OSS Repo

```bash
# Remove any existing instance.yaml (from manual setup) and symlink
rm -f /opt/data-analyst/repo/config/instance.yaml
ln -s /opt/data-analyst/instance/config/instance.yaml /opt/data-analyst/repo/config/instance.yaml

# Optional: symlink data_description.md when ready
# ln -s /opt/data-analyst/instance/config/data_description.md /opt/data-analyst/repo/docs/data_description.md

systemctl restart webapp
```

### Step 3 Checklist

| # | Check | Expected |
|---|-------|----------|
| 3.1 | Instance repo | /opt/data-analyst/instance/ exists |
| 3.2 | Symlink | config/instance.yaml -> ../../instance/config/instance.yaml |
| 3.3 | Webapp loads | Instance name shown on login page |

## Step 4: Authentication

Email magic link works without any external service.

1. Login page shows "Sign in with Email"
2. User enters email with allowed domain
3. Without SMTP: magic link shown in browser (dev mode)
4. With SMTP: link sent via email
5. Click link -> logged in -> dashboard

Optional: add Google OAuth by setting real `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`.

### Step 4 Checklist

| # | Check | Expected |
|---|-------|----------|
| 4.1 | Email auth | "Sign in with Email" on login page |
| 4.2 | Magic link | Generated for valid domain email |
| 4.3 | Domain check | Rejects wrong domains |
| 4.4 | Login flow | Magic link -> dashboard with session |

## Step 5: Onboarding Flow (End-User)

After server is set up, analysts self-onboard via the webapp:

1. Visit `http://YOUR_SERVER/login` and sign in with email
2. Dashboard shows "Get Started" with 4 steps:
   - Create project folder (`mkdir -p data-analyst && cd data-analyst`)
   - Generate SSH key (`ssh-keygen -t ed25519 -f ~/.ssh/data_analyst_server -N ''`)
   - Copy public key (`cat ~/.ssh/data_analyst_server.pub`)
   - Paste key into form, click "Create Account"
3. After account creation, dashboard shows "Set up your local environment"
4. User runs `claude` in their project folder, pastes setup instructions
5. Claude Code configures SSH, rsyncs data, sets up Python + DuckDB

## Step 6: Sample Data (Try Without a Data Adapter)

Before connecting a real data source, you can load sample data to verify the full pipeline
(Parquet files, DuckDB, analyst rsync, Claude Code analysis).

```bash
cd /opt/data-analyst/repo

# Install generator dependency
/opt/data-analyst/.venv/bin/pip install faker

# Generate synthetic e-commerce data (size m: ~20K orders, 100K sessions)
/opt/data-analyst/.venv/bin/python scripts/generate_sample_data.py \
    --size m --output /tmp/sample_csv --seed 42

# Convert CSVs to Parquet and deploy to data directory
/opt/data-analyst/.venv/bin/python -c "
import pandas as pd
from pathlib import Path

csv_dir = Path('/tmp/sample_csv')
parquet_dir = Path('/data/src_data/parquet')
parquet_dir.mkdir(parents=True, exist_ok=True)

for f in sorted(csv_dir.glob('*.csv')):
    df = pd.read_csv(f)
    out = parquet_dir / f'{f.stem}.parquet'
    df.to_parquet(out, index=False)
    print(f'  {f.stem}: {len(df):,} rows -> {out}')
"

# Set correct permissions
chown -R root:data-ops /data/src_data/parquet
chmod -R 2775 /data/src_data/parquet

# Clean up temporary CSVs
rm -rf /tmp/sample_csv
```

Available sizes: `xs` (50 customers, ~1 MB), `s` (500, ~15 MB), `m` (5K, ~150 MB), `l` (50K, ~1.5 GB).

The sample data covers 9 tables: customers, products, campaigns, web_sessions, web_leads,
orders, order_items, payments, support_tickets. See `docs/sample-data.md` for the full
data model, table reference, and built-in analytical patterns.

### Step 6 Checklist

| # | Check | Expected |
|---|-------|----------|
| 6.1 | Parquet files | `ls /data/src_data/parquet/*.parquet` shows 9 files |
| 6.2 | Permissions | Files owned by root:data-ops, group-readable |
| 6.3 | Analyst sync | Analyst can rsync parquet files to local machine |
| 6.4 | DuckDB loads | `SELECT count(*) FROM read_parquet('orders.parquet')` returns rows |

## Step 7: Real Data Source (Production)

When ready, replace sample data with a real data source adapter in `instance/config/instance.yaml`:

```yaml
data_source:
  type: "keboola"
  keboola:
    storage_token: "${KEBOOLA_STORAGE_TOKEN}"
    stack_url: "https://connection.keboola.com"
    project_id: "12345"
```

Add the token to `.env` and create `config/data_description.md` with table schemas.

Other planned adapters: BigQuery, CSV import.

## Deployment Workflow (Ongoing)

### Update OSS code
```bash
cd /opt/data-analyst/repo && git pull
bash server/deploy.sh   # restarts services, syncs scripts/docs
```

### Update instance config
```bash
cd /opt/data-analyst/instance && git pull
systemctl restart webapp  # picks up new instance.yaml via symlink
```

### Both at once
```bash
cd /opt/data-analyst/repo && git pull
cd /opt/data-analyst/instance && git pull
bash server/deploy.sh
```

## Server Layout Summary

```
/opt/data-analyst/
├── repo/           -> git@github-oss:ORG/OSS_REPO.git
├── instance/       -> git@github-cfg:ORG/INSTANCE_REPO.git
├── .env            # Secrets (not in git)
├── .venv/          # Python
└── logs/           # App logs

/root/.ssh/
├── deploy_key      # For OSS repo (github-oss alias)
├── instance_key    # For instance repo (github-cfg alias)
└── config          # Maps aliases to keys

Symlinks:
  repo/config/instance.yaml -> instance/config/instance.yaml
  repo/docs/data_description.md -> instance/config/data_description.md (optional)
```

## Quick Verification

```bash
# Health check
curl http://YOUR_IP/health | python3 -m json.tool

# Login page
curl -s -o /dev/null -w "%{http_code}" http://YOUR_IP/login
# Expected: 200

# Instance config loaded
curl -s http://YOUR_IP/login | grep 'YOUR_INSTANCE_NAME'
```
