#!/bin/bash
# Deployed to /usr/local/bin/agnes-auto-upgrade.sh on the VM.
# Cron fires it every 5 min; pulls latest image for the pinned AGNES_TAG
# and recreates containers only if the digest moved.
#
# Cert-aware: if /data/state/certs/{fullchain,privkey}.pem both exist
# (populated by agnes-tls-rotate.sh), enables the tls overlay so Caddy
# fronts :443. Absence → plain HTTP on :8000.
set -euo pipefail
cd /opt/agnes
# shellcheck disable=SC1091
set -a; . /opt/agnes/.env; set +a
IMAGE="ghcr.io/keboola/agnes-the-ai-analyst:${AGNES_TAG:-stable}"
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml"
PROFILE_ARGS=""
if [ -f /data/state/certs/fullchain.pem ] && [ -f /data/state/certs/privkey.pem ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.tls.yml"
    PROFILE_ARGS="--profile tls"
fi
BEFORE=$(docker images --no-trunc --format '{{.Digest}}' "$IMAGE" | head -1)
docker compose $COMPOSE_FILES pull >/dev/null 2>&1
AFTER=$(docker images --no-trunc --format '{{.Digest}}' "$IMAGE" | head -1)
if [ "$BEFORE" != "$AFTER" ]; then
    echo "$(date): new digest for $IMAGE — recreating containers"
    docker compose $COMPOSE_FILES $PROFILE_ARGS up -d
    docker image prune -f >/dev/null 2>&1
fi
