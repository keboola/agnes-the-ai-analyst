# Automated Installation Log

Step-by-step record of deploying the platform on a clean Ubuntu 24.04 VM (DigitalOcean).

## Infrastructure

- **Provider**: DigitalOcean
- **Droplet**: s-1vcpu-2gb (1 vCPU, 2GB RAM, 50GB disk)
- **Region**: ams3 (Amsterdam)
- **OS**: Ubuntu 24.04.3 LTS
- **IP**: 165.22.199.226

## Prerequisites Discovered

1. **DigitalOcean API token needs `ssh_key` scope** to register SSH keys
   - Without `ssh_keys` field in droplet creation, DO forces password expiry
   - Cloud-init `user_data` cannot override this (DO scripts run after cloud-init)
   - Solution: register key via `/v2/account/keys`, then reference in `ssh_keys` array

2. **`python3-venv` must be installed** before `server/setup.sh`
   - Ubuntu 24.04 doesn't include it by default
   - Fix: `apt install python3.12-venv` before running setup

## Step 0: Create GitHub Repo & Push

```bash
# Repo was created on GitHub: padak/tmp_oss (private)
git remote add origin https://github.com/padak/tmp_oss.git
git push -u origin main
```

## Step 1: VM Setup

### 1a: Create Droplet via API

```bash
# First: register SSH key (requires ssh_key scope)
curl -s -X POST -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $DO_TOKEN" \
    -d '{"name":"my-key","public_key":"ssh-ed25519 AAAA..."}' \
    "https://api.digitalocean.com/v2/account/keys"

# Get key ID from response, then create droplet
curl -s -X POST -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $DO_TOKEN" \
    -d '{
      "name":"oss-devel-1",
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

# Wait for apt lock to release (auto-updates run on first boot)
# Then install python3-venv
apt install -y python3.12-venv python3-pip
```

### 1c: Clone Repo & Run Setup

```bash
# Generate deploy key on VM
ssh-keygen -t ed25519 -f /root/.ssh/deploy_key -N ""
# Add deploy_key.pub to GitHub repo as deploy key

# Configure SSH for GitHub
cat > /root/.ssh/config << 'EOF'
Host github.com
  IdentityFile /root/.ssh/deploy_key
  StrictHostKeyChecking no
EOF

# Clone and setup
git clone git@github.com:YOUR_ORG/YOUR_REPO.git /opt/data-analyst/repo
cd /opt/data-analyst/repo
REPO_URL="git@github.com:YOUR_ORG/YOUR_REPO.git" bash server/setup.sh
```

### Step 1 Checklist Results

| # | Check | Result |
|---|-------|--------|
| 1.1 | Groups created | data-ops, dataread, data-private - OK |
| 1.2 | Deploy user exists | uid=999(deploy), groups: deploy, data-ops - OK |
| 1.3 | Directory structure | /opt/data-analyst/{repo,.venv,logs} - OK |
| 1.4 | Python venv works | Flask 3.1.3 loaded - OK |
| 1.5 | Management scripts | add-analyst, list-analysts, add-admin, remove-analyst - OK |

## Step 2: Webapp Setup

### 2a: Run webapp-setup.sh

```bash
export SERVER_HOSTNAME="165.22.199.226"  # or your domain
bash server/webapp-setup.sh
```

**Issue**: Nginx config assumes SSL/domain. For IP-only testing, replace with HTTP-only config:

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

### 2b: Configure .env

```bash
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

cat > /opt/data-analyst/.env << EOF
WEBAPP_SECRET_KEY="${SECRET_KEY}"
SERVER_HOST="YOUR_IP"
SERVER_HOSTNAME="YOUR_IP_OR_DOMAIN"
GOOGLE_CLIENT_ID="your-google-client-id"
GOOGLE_CLIENT_SECRET="your-google-client-secret"
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

### Step 2 Checklist Results

| # | Check | Result |
|---|-------|--------|
| 2.1 | Nginx running | active - OK |
| 2.2 | Webapp running | active (gunicorn with 2 workers) - OK |
| 2.3 | SSL cert | SKIPPED (IP-only, no domain) |
| 2.4 | Health endpoint | Returns JSON with disk/load/services - OK |
| 2.5 | Login page loads | HTTP 200 - OK |

## Issues Found & Fixes

### Issue 1: `python3-venv` not installed
- **Symptom**: `server/setup.sh` fails at venv creation
- **Fix**: `apt install python3.12-venv` before running setup
- **TODO**: Add to `server/setup.sh` package list

### Issue 2: Nginx SSL config with IP address
- **Symptom**: Nginx fails to start - no SSL cert for "YOUR_DOMAIN"
- **Fix**: Replace nginx config with HTTP-only version for IP-only deployments
- **TODO**: `webapp-setup.sh` should detect IP vs domain and generate appropriate config

### Issue 3: DigitalOcean cloud-init limitations
- **Symptom**: `user_data` cloud-init cannot prevent password expiry
- **Fix**: Must use `ssh_keys` API field with registered key
- **Lesson**: DO initialization scripts run after cloud-init and override password settings

## Step 3: Instance Configuration

```bash
cat > /opt/data-analyst/repo/config/instance.yaml << 'YAML'
instance:
  name: "OSS Data Analyst"
  subtitle: "Test Deployment"
  copyright: "Test"

server:
  hostname: "165.22.199.226"
  host: "165.22.199.226"
  app_dir: "/opt/data-analyst"

auth:
  allowed_domain: "test.com"       # any domain for testing
  webapp_secret_key: "${WEBAPP_SECRET_KEY}"

data_source:
  type: "local"

catalog:
  categories: {}
YAML

systemctl restart webapp
```

### Step 3 Checklist Results

| # | Check | Result |
|---|-------|--------|
| 3.1 | Config loads | OK - webapp starts without errors |
| 3.2 | Instance name shown | "OSS Data Analyst" on login page |

## Step 4: Authentication (Email Magic Link)

No Google OAuth needed! The email magic link provider works without any external service.

### How it works

1. Login page shows "Sign in with Email" button
2. User enters email with allowed domain (e.g., `user@test.com`)
3. System generates a signed magic link (valid 15 minutes)
4. Without SMTP: link shown directly in browser (development mode)
5. With SMTP: link sent via email
6. Click link -> logged in, redirected to dashboard

### Test Results

```bash
# Login page shows both providers
curl -s http://localhost/login | grep 'Sign in with'
# Sign in with Google
# Sign in with Email

# Email form accessible
curl -s -o /dev/null -w "%{http_code}" http://localhost/login/email
# 200

# Magic link generated (dev mode - shown in browser)
curl -s -X POST -d "email=admin@test.com" http://localhost/login/email/send
# Shows magic link URL

# Click magic link -> redirect to dashboard
curl -s -D - -L -c cookies.txt "http://localhost/login/email/verify/TOKEN"
# HTTP 302 -> /dashboard -> HTTP 200
```

### Step 4 Checklist Results

| # | Check | Result |
|---|-------|--------|
| 4.1 | Email auth available | "Sign in with Email" shown on login page |
| 4.2 | Magic link generated | Token URL generated for valid domain email |
| 4.3 | Domain restriction | Rejects emails from wrong domain |
| 4.4 | Login works | Magic link redirects to /dashboard with session |
| 4.5 | Dev mode works | Link shown in browser when no SMTP configured |

## Issues Found & Fixes

### Issue 1: `python3-venv` not installed
- **Symptom**: `server/setup.sh` fails at venv creation
- **Fix**: `apt install python3.12-venv` before running setup
- **TODO**: Add to `server/setup.sh` package list

### Issue 2: Nginx SSL config with IP address
- **Symptom**: Nginx fails to start - no SSL cert for "YOUR_DOMAIN"
- **Fix**: Replace nginx config with HTTP-only version for IP-only deployments
- **TODO**: `webapp-setup.sh` should detect IP vs domain and generate appropriate config

### Issue 3: DigitalOcean cloud-init limitations
- **Symptom**: `user_data` cloud-init cannot prevent password expiry
- **Fix**: Must use `ssh_keys` API field with registered key
- **Lesson**: DO initialization scripts run after cloud-init and override password settings

## Current State

- **Step 0**: GitHub repo created and pushed - DONE
- **Step 1**: VM setup (groups, users, venv, scripts) - DONE
- **Step 2**: Webapp (nginx, gunicorn, .env) - DONE
- **Step 3**: Instance configuration - DONE
- **Step 4**: Authentication (email magic link) - DONE
- **Step 5+**: Discovery API, Table Registry, Data Sync - NEXT (needs data source)

## Server Access

```bash
ssh -i ~/.ssh/id_ed25519 root@165.22.199.226
```

## Quick Verification

```bash
# Health check
curl http://165.22.199.226/health | python3 -m json.tool

# Login page
curl -s -o /dev/null -w "%{http_code}" http://165.22.199.226/login
# Expected: 200

# Email auth form
curl -s -o /dev/null -w "%{http_code}" http://165.22.199.226/login/email
# Expected: 200
```
