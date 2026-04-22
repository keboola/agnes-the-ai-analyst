#!/bin/bash
# Deployed to /usr/local/bin/agnes-auto-upgrade.sh on the VM.
# Cron fires it every 5 min; pulls latest image for the pinned AGNES_TAG
# and recreates containers only if the digest moved.
set -euo pipefail
cd /opt/agnes
# shellcheck disable=SC1091
set -a; . /opt/agnes/.env; set +a
IMAGE="ghcr.io/keboola/agnes-the-ai-analyst:${AGNES_TAG:-stable}"
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml"
BEFORE=$(docker images --no-trunc --format '{{.Digest}}' "$IMAGE" | head -1)
docker compose $COMPOSE_FILES pull >/dev/null 2>&1
AFTER=$(docker images --no-trunc --format '{{.Digest}}' "$IMAGE" | head -1)
if [ "$BEFORE" != "$AFTER" ]; then
    echo "$(date): new digest for $IMAGE — recreating containers"
    docker compose $COMPOSE_FILES up -d
    docker image prune -f >/dev/null 2>&1
fi
