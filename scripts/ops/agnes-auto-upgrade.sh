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

# Fail-fast guard: if the VM has a config disk attached, it MUST be
# mounted at /data/state before any container action. Otherwise the
# app would write state onto /data (sdb) and lose it on the next
# container recreate — the regression that motivated this guard.
# Three retries (mount may race with udev on cold boot) then hard exit.
CONFIG_DEVICE=/dev/disk/by-id/google-config-disk
if [ -e "$CONFIG_DEVICE" ]; then
  attempt=0
  while [ $attempt -lt 3 ]; do
    attempt=$((attempt + 1))
    if mountpoint -q /data/state; then
      expected_dev=$(readlink -f "$CONFIG_DEVICE")
      actual_dev=$(findmnt -n -o SOURCE /data/state)
      if [ "$expected_dev" = "$actual_dev" ]; then
        break
      fi
      logger -t agnes-auto-upgrade "WARN: /data/state on $actual_dev, expected $expected_dev — attempting remount"
      umount /data/state 2>/dev/null || true
    fi
    mount "$CONFIG_DEVICE" /data/state 2>/dev/null || true
    sleep $((attempt * 2))
  done

  if ! mountpoint -q /data/state || \
     [ "$(readlink -f "$CONFIG_DEVICE")" != "$(findmnt -n -o SOURCE /data/state)" ]; then
    logger -t agnes-auto-upgrade "FATAL: config disk not mounted at /data/state — refusing to start containers"
    echo "FATAL: /data/state is not backed by the config disk." >&2
    echo "       Refusing to run docker compose — app state must NEVER land on /data (sdb)." >&2
    echo "       Inspect: mount | grep /data/state ; ls /dev/disk/by-id/google-config-disk" >&2
    exit 1
  fi

  # Re-apply propagation in case a prior container teardown reset it.
  # Idempotent — safe to call when already private.
  mount --make-rprivate /data 2>/dev/null || true
  mount --make-rprivate /data/state 2>/dev/null || true
fi

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
