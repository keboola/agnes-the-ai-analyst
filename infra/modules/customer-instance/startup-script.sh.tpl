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
SEED_ADMIN_PASSWORD="${seed_admin_password}"
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
    # Match Dockerfile USER agnes (uid:gid 999:999). A freshly-attached PD is
    # root-owned by default; without this chown the non-root container cannot
    # write to /data/state/system.duckdb and every authed request 500s after
    # the first upgrade that flips USER from root to agnes (regression hit
    # agnes-development on 2026-04-29). Idempotent — safe on reboot.
    chown -R 999:999 "$DATA_MNT"
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

# SCHEDULER_API_TOKEN — shared secret between the app and scheduler containers.
# Both source the same /opt/agnes/.env via Docker Compose env_file:, so the
# scheduler's outbound bearer token always matches the app's expected value.
# See app/auth/scheduler_token.py for the auth path it unlocks.
#
# Preserve across reboots: the token is plumbed into a long-lived synthetic
# user, and rotating it forces a restart of both containers. Read back from
# an existing .env when present; mint fresh only on the first boot.
SCHEDULER_API_TOKEN=""
if [ -f "$APP_DIR/.env" ]; then
    SCHEDULER_API_TOKEN=$(grep -E '^SCHEDULER_API_TOKEN=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
if [ -z "$SCHEDULER_API_TOKEN" ]; then
    # 64 hex chars = 256 bits of /dev/urandom entropy. Floor enforced in
    # app/auth/scheduler_token.SCHEDULER_TOKEN_MIN_LENGTH is 32; 64 leaves
    # headroom for a future tightening without re-provisioning every VM.
    SCHEDULER_API_TOKEN=$(openssl rand -hex 32)
fi

# Optional Google OAuth credentials. If the operator has created
# google-oauth-client-{id,secret} secrets in the project's Secret Manager
# AND wired them via runtime_secrets in the calling Terraform, the VM SA can
# read them — write into .env so the Google sign-in flow works. Missing /
# 403 / empty → silent fallback to "" so password + email auth keep working.
GOOGLE_CLIENT_ID=$(gcloud secrets versions access latest --secret=google-oauth-client-id 2>/dev/null || echo "")
GOOGLE_CLIENT_SECRET=$(gcloud secrets versions access latest --secret=google-oauth-client-secret 2>/dev/null || echo "")

# AGNES_VERSION, RELEASE_CHANNEL, AGNES_COMMIT_SHA are baked into the image
# itself as ENV (see Dockerfile ARG/ENV + release.yml build-args). We do NOT
# set them here — doing so would override the image-level values with the
# floating tag name ("stable"/"dev"), hiding the real CalVer / git SHA.
# The app picks them up from the image's runtime environment.

# CADDY_TLS controls Caddyfile cert provisioning (see Caddyfile inline docs).
# - tls_mode=caddy + ACME_EMAIL set → Let's Encrypt auto-issue (public domain)
# - tls_mode=caddy + no ACME_EMAIL  → Caddy-managed self-signed (lab use)
# - any other tls_mode             → leave CADDY_TLS unset, Caddyfile default
#                                     (cert-file mode for corporate PKI) applies.
# Operators wanting cert-file mode shouldn't set tls_mode at all on the dev
# instance — leave it "none" and let the corp-PKI rotate scripts handle certs.
CADDY_TLS_LINE=""
if [ "$TLS_MODE" = "caddy" ] && [ -n "$DOMAIN" ]; then
    # Value MUST be quoted in the .env file: agnes-auto-upgrade.sh sources
    # /opt/agnes/.env via `set -a; . .env; set +a`, and bash interprets an
    # unquoted `KEY=value with spaces` as `KEY=value` followed by trying to
    # exec `with`/`spaces` as commands → boot succeeds but every cron tick
    # logs "<email>: command not found".
    if [ -n "$ACME_EMAIL" ]; then
        CADDY_TLS_LINE="CADDY_TLS=\"tls $ACME_EMAIL\""
    else
        CADDY_TLS_LINE="CADDY_TLS=\"tls internal\""
    fi
fi

cat > "$APP_DIR/.env" <<ENVEOF
JWT_SECRET_KEY=$JWT_KEY
DATA_DIR=$DATA_MNT
DATA_SOURCE=$DATA_SOURCE
KEBOOLA_STORAGE_TOKEN=$KEBOOLA_TOKEN
KEBOOLA_STACK_URL=$KEBOOLA_STACK_URL
SEED_ADMIN_EMAIL=$SEED_ADMIN_EMAIL
SEED_ADMIN_PASSWORD=$SEED_ADMIN_PASSWORD
SCHEDULER_API_TOKEN=$SCHEDULER_API_TOKEN
LOG_LEVEL=info
DOMAIN=$DOMAIN
AGNES_TAG=$IMAGE_TAG
ACME_EMAIL=$ACME_EMAIL
GOOGLE_CLIENT_ID=$GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET=$GOOGLE_CLIENT_SECRET
$CADDY_TLS_LINE
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
