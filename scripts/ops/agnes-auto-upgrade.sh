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
# Array form (vs. word-split string) — quoted expansion survives paths
# with spaces and is the modern bash idiom. Functionally identical here
# since /opt/agnes paths are tame, but it's a cheap habit to keep.
COMPOSE_FILES=( -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml )
PROFILE_ARGS=()
# `-s` (size > 0) instead of `-f` — guards against the corner case where
# rotate.sh wrote a 0-byte cert and exited (or got SIGKILLed mid-write).
# Bringing up the tls profile against an empty cert would just crash
# Caddy on start; better to fall back to plain :8000 until rotate
# regenerates real bytes.
if [ -s /data/state/certs/fullchain.pem ] && [ -s /data/state/certs/privkey.pem ]; then
    COMPOSE_FILES+=( -f docker-compose.tls.yml )
    PROFILE_ARGS=( --profile tls )
fi
BEFORE=$(docker images --no-trunc --format '{{.Digest}}' "$IMAGE" | head -1)
docker compose "${COMPOSE_FILES[@]}" pull >/dev/null 2>&1
AFTER=$(docker images --no-trunc --format '{{.Digest}}' "$IMAGE" | head -1)
if [ "$BEFORE" != "$AFTER" ]; then
    echo "$(date): new digest for $IMAGE — recreating containers"
    # ${arr[@]+"${arr[@]}"} pattern: expands to nothing when array is
    # empty (vs. plain "${arr[@]}" which trips `set -u` on bash <4.4).
    docker compose "${COMPOSE_FILES[@]}" ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} up -d
    docker image prune -f >/dev/null 2>&1
fi
