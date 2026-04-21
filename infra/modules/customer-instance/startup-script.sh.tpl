#!/bin/bash
# Agnes VM startup script — templated by Terraform.
# Idempotent — runs on every boot.
set -euo pipefail
exec > /var/log/agnes-startup.log 2>&1
chmod 640 /var/log/agnes-startup.log  # defense in depth — not readable by non-root

CUSTOMER_NAME="${customer_name}"
IMAGE_REPO="${image_repo}"
IMAGE_TAG="${image_tag}"
UPGRADE_MODE="${upgrade_mode}"
TLS_MODE="${tls_mode}"
DOMAIN="${domain}"
ACME_EMAIL="${acme_email}"
DATA_SOURCE="${data_source}"
KEBOOLA_STACK_URL="${keboola_stack_url}"
SEED_ADMIN_EMAIL="${seed_admin_email}"
ROLE="${role}"
COMPOSE_REF="${compose_ref}"

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

# Fetch docker-compose files pinned to $COMPOSE_REF (defaults to `main`; pin to a
# stable-YYYY.MM.N tag for reproducibility across VM rebuilds).
RAW_BASE="https://raw.githubusercontent.com/keboola/agnes-the-ai-analyst/$${COMPOSE_REF}"
curl -fsSL "$${RAW_BASE}/docker-compose.yml" -o docker-compose.yml
curl -fsSL "$${RAW_BASE}/docker-compose.prod.yml" -o docker-compose.prod.yml
# Overlay which binds `data` volume to host /data (persistent disk mounted above)
curl -fsSL "$${RAW_BASE}/docker-compose.host-mount.yml" -o docker-compose.host-mount.yml

# TLS overlay (Caddy + Let's Encrypt) — fetch only when actually needed; surface failures
if [ "$TLS_MODE" = "caddy" ] && [ -n "$DOMAIN" ]; then
    curl -fsSL "$${RAW_BASE}/Caddyfile" -o Caddyfile
fi

# --- 4. Fetch secrets from Secret Manager — fail loudly if missing ---
KEBOOLA_TOKEN=""
if [ "$DATA_SOURCE" = "keboola" ]; then
    # No `|| echo ""` fallback — if the token secret is missing, boot should fail
    # loudly rather than silently start an app that will fail sync cryptically later.
    KEBOOLA_TOKEN=$(gcloud secrets versions access latest --secret=keboola-storage-token)
fi
JWT_KEY=$(gcloud secrets versions access latest --secret=agnes-$${CUSTOMER_NAME}-jwt-secret)

# Resolve the actual version/commit behind the requested tag so the UI can
# show specific `stable-2026.04.47` + commit SHA instead of just `stable`.
IMAGE_DIGEST=$(docker pull "$IMAGE_REPO:$IMAGE_TAG" 2>/dev/null | grep -o 'sha256:[a-f0-9]*' | head -1 || echo "unknown")
IMAGE_INFO=$(curl -fsSL "https://ghcr.io/v2/keboola/agnes-the-ai-analyst/manifests/$IMAGE_TAG" -H "Accept: application/vnd.oci.image.manifest.v1+json" 2>/dev/null || echo "{}")

# Channel derived from tag prefix (stable-*/dev-*/release-*) — simple heuristic.
case "$IMAGE_TAG" in
    stable*) RELEASE_CHANNEL="stable" ;;
    dev*)    RELEASE_CHANNEL="dev" ;;
    release*) RELEASE_CHANNEL="release" ;;
    *)       RELEASE_CHANNEL="custom" ;;
esac

# Version extracted from versioned tags (stable-2026.04.N); floating tags stay "dev".
case "$IMAGE_TAG" in
    *-[0-9]*.[0-9]*.[0-9]*) AGNES_VERSION="$${IMAGE_TAG#*-}" ;;
    *)                      AGNES_VERSION="$IMAGE_TAG" ;;
esac

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
AGNES_VERSION=$AGNES_VERSION
RELEASE_CHANNEL=$RELEASE_CHANNEL
AGNES_COMMIT_SHA=$IMAGE_DIGEST
ACME_EMAIL=$ACME_EMAIL
ENVEOF
chmod 600 "$APP_DIR/.env"

# --- 5. Start Agnes ---
COMPOSE_PROFILES_ARG=""
if [ "$TLS_MODE" = "caddy" ] && [ -n "$DOMAIN" ]; then
    COMPOSE_PROFILES_ARG="--profile tls"
fi

COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml"

docker compose $COMPOSE_FILES $COMPOSE_PROFILES_ARG pull
docker compose $COMPOSE_FILES $COMPOSE_PROFILES_ARG up -d

# --- 6. Auto-upgrade via cron (pulls new image digest every 5 min) ---
if [ "$UPGRADE_MODE" = "auto" ]; then
    # Cron script sources /opt/agnes/.env for AGNES_TAG — so if operator edits .env
    # (e.g. to pin a specific stable-YYYY.MM.N), cron picks it up immediately. No
    # drift between what compose up reads and what the digest-check inspects.
    cat > /usr/local/bin/agnes-auto-upgrade.sh <<'SCRIPTEOF'
#!/bin/bash
# Runs from cron — pulls new image if one is available, restarts containers.
set -euo pipefail
cd /opt/agnes
# Source .env so AGNES_TAG reflects any operator edits since boot.
# shellcheck disable=SC1091
set -a; . /opt/agnes/.env; set +a
IMAGE="ghcr.io/keboola/agnes-the-ai-analyst:$${AGNES_TAG:-stable}"
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml"
BEFORE=$(docker images --no-trunc --format '{{.Digest}}' "$IMAGE" | head -1)
docker compose $COMPOSE_FILES pull >/dev/null 2>&1
AFTER=$(docker images --no-trunc --format '{{.Digest}}' "$IMAGE" | head -1)
if [ "$BEFORE" != "$AFTER" ]; then
    echo "$(date): new image digest for $IMAGE — recreating containers"
    docker compose $COMPOSE_FILES up -d
    docker image prune -f >/dev/null 2>&1
fi
SCRIPTEOF
    chmod +x /usr/local/bin/agnes-auto-upgrade.sh

    # Install cron entry idempotently: remove any prior agnes-auto-upgrade line, then append ours.
    CRON_LINE="*/5 * * * * /usr/local/bin/agnes-auto-upgrade.sh >> /var/log/agnes-auto-upgrade.log 2>&1"
    (crontab -l 2>/dev/null | grep -v agnes-auto-upgrade || true; echo "$CRON_LINE") | crontab -
fi

echo "=== [Agnes $CUSTOMER_NAME $ROLE] Startup complete at $(date) ==="
docker compose ps
