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
    mkdir -p "$DATA_MNT/state" "$DATA_MNT/analytics" "$DATA_MNT/extracts" "$DATA_MNT/uploads"
    # Match Dockerfile USER agnes (uid:gid 999:999). A freshly-attached PD is
    # root-owned by default; without this chown the non-root container cannot
    # write to /data/state/system.duckdb and every authed request 500s after
    # the first upgrade that flips USER from root to agnes (regression hit
    # agnes-development on 2026-04-29). Idempotent — safe on reboot.
    #
    # /data/uploads holds admin-uploaded marketplace cover images
    # (v50/0.55.0+). app/main.py eagerly mkdirs it at boot for the
    # StaticFiles mount; on host-bind setups where /data root is
    # root-owned, that mkdir 403s and the container crashloops — so
    # pre-create the dir here under the SAME chown -R that already
    # covers state/analytics/extracts. Cheap insurance.
    chown -R 999:999 "$DATA_MNT"
fi

# --- 3. App directory + extract host artifacts from the pinned image ---
APP_DIR="/opt/agnes"
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Pull the pinned image first so we can extract host-side artifacts from it.
# Everything we need on the host (compose files, Caddyfile, agnes-auto-upgrade.sh)
# ships baked into the image at /opt/agnes-host/, released atomically with
# the app. AGNES_TAG is the single version pin for both — no split-brain
# with main-branch curl.
#
# Why image-extract beats curling raw.githubusercontent.com:
#   - Version pin: customer pins AGNES_TAG → extracted artifacts match the
#     same tag. main-branch curls would break that pin silently.
#   - Egress: image is already pulled from the private registry; the public
#     internet is no longer required for boot.
#   - Rollback: revert is one tag bump. Curl-from-main has no per-customer
#     rollback path.
docker pull "$${IMAGE_REPO}:$${IMAGE_TAG}"
EXTRACT_CONTAINER=$(docker create "$${IMAGE_REPO}:$${IMAGE_TAG}")
trap "docker rm '$EXTRACT_CONTAINER' >/dev/null 2>&1 || true" EXIT
docker cp "$EXTRACT_CONTAINER:/opt/agnes-host/." "$APP_DIR/"
docker cp "$EXTRACT_CONTAINER:/opt/agnes-host/agnes-auto-upgrade.sh" /usr/local/bin/agnes-auto-upgrade.sh
chmod +x /usr/local/bin/agnes-auto-upgrade.sh

# docker-compose.tls.yml + Caddyfile land regardless of TLS_MODE. agnes-auto-upgrade.sh
# detects TLS at runtime via cert files on disk; certs can appear after boot via
# agnes-tls-rotate.sh or manual provisioning. The caddy service bind-mounts
# ./Caddyfile, so it must exist on disk before any `docker compose up` even when
# the tls overlay is currently inactive. Cheap to keep them on disk either way.

# --- 4. Fetch secrets from Secret Manager — fail loudly if missing ---
KEBOOLA_TOKEN=""
if [ "$DATA_SOURCE" = "keboola" ]; then
    # No `|| echo ""` fallback — if the token secret is missing, boot should fail
    # loudly rather than silently start an app that will fail sync cryptically later.
    KEBOOLA_TOKEN=$(gcloud secrets versions access latest --secret=keboola-storage-token)
fi
JWT_KEY=$(gcloud secrets versions access latest --secret=agnes-$${CUSTOMER_NAME}-jwt-secret)

# ── Postgres password from Secret Manager + side-car data dir prep ──
# Task 2A.1 provisioned agnes-<customer>-postgres-password with VM SA bound to
# secretAccessor. Pull every boot (idempotent — the secret value is stable
# across reboots; we re-fetch rather than reading from .env because .env may
# have been wiped to force a reset).
POSTGRES_PASSWORD=$(gcloud secrets versions access latest --secret=agnes-$${CUSTOMER_NAME}-postgres-password)

# postgres:16-alpine runs as uid 70 (the Alpine ``postgres`` user). When a
# future overlay bind-mounts /data/postgres into the side-car (mirroring the
# /data:/data pattern in docker-compose.host-mount.yml), the dir must be
# pre-created and owned by uid 70 so initdb / runtime writes succeed.
# Cheap to do unconditionally — the dir is unused when the side-car runs on
# the default named volume.
mkdir -p "$DATA_MNT/postgres"
chown 70:70 "$DATA_MNT/postgres"
chmod 700 "$DATA_MNT/postgres"

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

# Preserve operator overrides on AGNES_TAG. Rationale: this script
# runs on every boot (and the `metadata_startup_script` is in
# `lifecycle.ignore_changes` so a TF apply that changed the
# `image_tag` variable does NOT propagate to a long-lived VM until
# someone explicitly recreates it). Operators commonly hand-edit
# `/opt/agnes/.env` to pin a custom image tag (e.g. for a dev branch
# build, or a staged rollout) — overwriting that on every reboot
# clobbers their decision. Read the existing AGNES_TAG and let it
# win when it disagrees with $IMAGE_TAG; ditto for AGNES_TEMP_DIR
# (a deployment-specific path tweak operators sometimes set to
# steer tempdirs onto a larger volume).
EXISTING_AGNES_TAG=""
EXISTING_AGNES_TEMP_DIR=""
if [ -f "$APP_DIR/.env" ]; then
    EXISTING_AGNES_TAG=$(grep -E '^AGNES_TAG=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
    EXISTING_AGNES_TEMP_DIR=$(grep -E '^AGNES_TEMP_DIR=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
EFFECTIVE_AGNES_TAG="$${EXISTING_AGNES_TAG:-$IMAGE_TAG}"
if [ -n "$EXISTING_AGNES_TAG" ] && [ "$EXISTING_AGNES_TAG" != "$IMAGE_TAG" ]; then
    echo "INFO: preserving operator-edited AGNES_TAG=$EXISTING_AGNES_TAG (TF variable said $IMAGE_TAG; rm /opt/agnes/.env to reset)"
fi
AGNES_TEMP_DIR_LINE=""
if [ -n "$EXISTING_AGNES_TEMP_DIR" ]; then
    AGNES_TEMP_DIR_LINE="AGNES_TEMP_DIR=\"$EXISTING_AGNES_TEMP_DIR\""
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
AGNES_TAG=$EFFECTIVE_AGNES_TAG
ACME_EMAIL=$ACME_EMAIL
GOOGLE_CLIENT_ID=$GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET=$GOOGLE_CLIENT_SECRET
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
DATABASE_URL=postgresql+psycopg://agnes:$POSTGRES_PASSWORD@postgres:5432/agnes
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.postgres.yml:docker-compose.host-mount.yml:docker-compose.postgres-host-mount.yml
$CADDY_TLS_LINE
$AGNES_TEMP_DIR_LINE
ENVEOF
chmod 600 "$APP_DIR/.env"

# --- 5. Start Agnes ---
COMPOSE_PROFILES_ARG=""
if [ "$TLS_MODE" = "caddy" ] && [ -n "$DOMAIN" ]; then
    COMPOSE_PROFILES_ARG="--profile tls"
fi

# Honor COMPOSE_FILE from /opt/agnes/.env. The .env write above sets the
# full list ``docker-compose.yml:docker-compose.prod.yml:docker-compose.postgres.yml:docker-compose.host-mount.yml``
# so the prod + postgres + host-mount overlays engage by default. Order
# matters: host-mount loads LAST so its ``volumes: !override`` on
# data-migrate (in docker-compose.host-mount.yml) can see the service
# defined by docker-compose.postgres.yml. Fall back to the historical
# prod + host-mount baseline when .env doesn't set COMPOSE_FILE — keeps
# existing customer instances behaving identically if an operator removes
# the line. The colon-separated COMPOSE_FILE form is the documented
# alternative to explicit ``-f`` args (docker.com/compose/reference/envvars
# /compose_file); docker compose reads it from the working-directory .env
# automatically. Export so the value is visible to the docker compose
# subprocess regardless of whether docker's own dotenv loader fires first.
COMPOSE_FILE_DEFAULT="docker-compose.yml:docker-compose.prod.yml:docker-compose.host-mount.yml"
# shellcheck disable=SC1091
set -a; . "$APP_DIR/.env"; set +a
export COMPOSE_FILE="$${COMPOSE_FILE:-$COMPOSE_FILE_DEFAULT}"

docker compose $COMPOSE_PROFILES_ARG pull
docker compose $COMPOSE_PROFILES_ARG up -d

# --- 6. Auto-upgrade via cron (pulls new image digest every 5 min) ---
if [ "$UPGRADE_MODE" = "auto" ]; then
    # agnes-auto-upgrade.sh was already extracted to /usr/local/bin/ in
    # section 3 alongside the compose files — the host artifacts ship
    # together from the pinned image. Nothing more to fetch here.
    :

    # Install cron entry idempotently: remove any prior agnes-auto-upgrade line, then append ours.
    CRON_LINE="*/5 * * * * /usr/local/bin/agnes-auto-upgrade.sh >> /var/log/agnes-auto-upgrade.log 2>&1"
    (crontab -l 2>/dev/null | grep -v agnes-auto-upgrade || true; echo "$CRON_LINE") | crontab -
fi

echo "=== [Agnes $CUSTOMER_NAME $ROLE] Startup complete at $(date) ==="
docker compose ps
