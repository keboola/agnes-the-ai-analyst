#!/bin/bash
# Agnes VM startup script — templated by Terraform.
# Idempotent — spustí se při každém boot.
set -euo pipefail
exec > /var/log/agnes-startup.log 2>&1

CUSTOMER_NAME="${customer_name}"
IMAGE_REPO="${image_repo}"
IMAGE_TAG="${image_tag}"
UPGRADE_MODE="${upgrade_mode}"
TLS_MODE="${tls_mode}"
DOMAIN="${domain}"
DATA_SOURCE="${data_source}"
KEBOOLA_STACK_URL="${keboola_stack_url}"
SEED_ADMIN_EMAIL="${seed_admin_email}"
ROLE="${role}"

echo "=== [Agnes $CUSTOMER_NAME $ROLE] Startup at $(date) ==="

# --- 1. Docker (install if missing) ---
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version &>/dev/null; then
    apt-get update && apt-get install -y docker-compose-plugin
fi

# --- 2. Persistent data disk mount ---
DATA_DEV="/dev/disk/by-id/google-data"
DATA_MNT="/data"
if [ -b "$DATA_DEV" ]; then
    if ! blkid "$DATA_DEV" | grep -q ext4; then
        mkfs.ext4 -F "$DATA_DEV"
    fi
    mkdir -p "$DATA_MNT"
    mountpoint -q "$DATA_MNT" || mount -o discard,defaults "$DATA_DEV" "$DATA_MNT"
    grep -qF "$DATA_DEV" /etc/fstab || echo "$DATA_DEV $DATA_MNT ext4 discard,defaults,nofail 0 2" >> /etc/fstab
    mkdir -p "$DATA_MNT/state" "$DATA_MNT/analytics" "$DATA_MNT/extracts"
fi

# --- 3. App directory + docker-compose files from public repo ---
APP_DIR="/opt/agnes"
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Fetch minimal docker-compose from public repo (main branch — stable)
curl -fsSL "https://raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main/docker-compose.yml" -o docker-compose.yml
curl -fsSL "https://raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main/docker-compose.prod.yml" -o docker-compose.prod.yml

# TLS overlay (Caddy + Let's Encrypt) — jen pokud potřeba
if [ "$TLS_MODE" = "caddy" ] && [ -n "$DOMAIN" ]; then
    curl -fsSL "https://raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main/Caddyfile" -o Caddyfile 2>/dev/null || true
fi

# --- 4. Fetch secrets from Secret Manager ---
KEBOOLA_TOKEN=""
if [ "$DATA_SOURCE" = "keboola" ]; then
    KEBOOLA_TOKEN=$(gcloud secrets versions access latest --secret=keboola-storage-token 2>/dev/null || echo "")
fi
JWT_KEY=$(gcloud secrets versions access latest --secret=agnes-$${CUSTOMER_NAME}-jwt-secret)

cat > "$APP_DIR/.env" <<ENVEOF
JWT_SECRET_KEY=$JWT_KEY
DATA_DIR=$DATA_MNT
DATA_SOURCE=$DATA_SOURCE
KEBOOLA_STORAGE_TOKEN=$KEBOOLA_TOKEN
KEBOOLA_STACK_URL=$KEBOOLA_STACK_URL
SEED_ADMIN_EMAIL=$SEED_ADMIN_EMAIL
LOG_LEVEL=info
DOMAIN=$DOMAIN
AGNES_TAG=$IMAGE_TAG
ACME_EMAIL=admin@$${DOMAIN#*.}
ENVEOF
chmod 600 "$APP_DIR/.env"

# --- 5. Start Agnes ---
COMPOSE_PROFILES_ARG=""
if [ "$TLS_MODE" = "caddy" ] && [ -n "$DOMAIN" ]; then
    COMPOSE_PROFILES_ARG="--profile tls"
fi

docker compose -f docker-compose.yml -f docker-compose.prod.yml $COMPOSE_PROFILES_ARG pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml $COMPOSE_PROFILES_ARG up -d

# --- 6. Auto-upgrade via cron (pullne nový tag každých 5 min) ---
if [ "$UPGRADE_MODE" = "auto" ]; then
    cat > /usr/local/bin/agnes-auto-upgrade.sh <<'SCRIPTEOF'
#!/bin/bash
# Spouští se z cronu — pullne nový image, pokud je, a restartne containers.
set -euo pipefail
cd /opt/agnes
BEFORE=$(docker images --no-trunc --format '{{.Digest}}' ghcr.io/keboola/agnes-the-ai-analyst:$${AGNES_TAG:-stable} | head -1)
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull >/dev/null 2>&1
AFTER=$(docker images --no-trunc --format '{{.Digest}}' ghcr.io/keboola/agnes-the-ai-analyst:$${AGNES_TAG:-stable} | head -1)
if [ "$BEFORE" != "$AFTER" ]; then
    echo "$(date): new image digest — recreating containers"
    docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
    docker image prune -f >/dev/null 2>&1
fi
SCRIPTEOF
    chmod +x /usr/local/bin/agnes-auto-upgrade.sh

    # Přidat do crontab (idempotentně — `sort -u` vyhodí duplikáty)
    (crontab -l 2>/dev/null; echo "*/5 * * * * AGNES_TAG=$IMAGE_TAG /usr/local/bin/agnes-auto-upgrade.sh >> /var/log/agnes-auto-upgrade.log 2>&1") | sort -u | crontab -
fi

echo "=== [Agnes $CUSTOMER_NAME $ROLE] Startup complete at $(date) ==="
docker compose ps
