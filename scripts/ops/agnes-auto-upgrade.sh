#!/bin/bash
# Deployed to /usr/local/bin/agnes-auto-upgrade.sh on the VM.
# Cron fires it every 5 min; pulls latest image for the pinned AGNES_TAG
# and recreates containers only if the digest moved.
#
# Cert-aware: if ${STATE_DIR}/certs/{fullchain,privkey}.pem both exist
# (populated by agnes-tls-rotate.sh), enables the tls overlay so Caddy
# fronts :443. Absence → plain HTTP on :8000.
#
# STATE_DIR is the host path that backs the writable state disk. It
# defaults to /data/state for backward compatibility with the legacy
# nested-mount layout (sdb at /data, sdc nested under /data/state).
# Set STATE_DIR=/data-state in /opt/agnes/.env for the flat layout
# (sdb at /data, sdc parallel at /data-state) — see docs/state-dir.md.
set -euo pipefail
cd /opt/agnes
# shellcheck disable=SC1091
set -a; . /opt/agnes/.env; set +a

STATE_DIR="${STATE_DIR:-/data/state}"

# Fail-fast guard: if the VM has a config disk attached, it MUST be
# mounted at $STATE_DIR before any container action. Otherwise the
# app would write state onto the parent filesystem and lose it on the
# next container recreate — the regression that motivated this guard.
# Three retries (mount may race with udev on cold boot) then hard exit.
CONFIG_DEVICE=/dev/disk/by-id/google-config-disk
if [ -e "$CONFIG_DEVICE" ]; then
  attempt=0
  while [ $attempt -lt 3 ]; do
    attempt=$((attempt + 1))
    if mountpoint -q "$STATE_DIR"; then
      expected_dev=$(readlink -f "$CONFIG_DEVICE")
      actual_dev=$(findmnt -n -o SOURCE "$STATE_DIR")
      if [ "$expected_dev" = "$actual_dev" ]; then
        break
      fi
      logger -t agnes-auto-upgrade "WARN: $STATE_DIR on $actual_dev, expected $expected_dev — attempting remount"
      umount "$STATE_DIR" 2>/dev/null || true
    fi
    mount "$CONFIG_DEVICE" "$STATE_DIR" 2>/dev/null || true
    sleep $((attempt * 2))
  done

  if ! mountpoint -q "$STATE_DIR" || \
     [ "$(readlink -f "$CONFIG_DEVICE")" != "$(findmnt -n -o SOURCE "$STATE_DIR")" ]; then
    logger -t agnes-auto-upgrade "FATAL: config disk not mounted at $STATE_DIR — refusing to start containers"
    echo "FATAL: $STATE_DIR is not backed by the config disk." >&2
    echo "       Refusing to run docker compose — app state must land on the config disk, not the parent filesystem." >&2
    echo "       Inspect: mount | grep $STATE_DIR ; ls /dev/disk/by-id/google-config-disk" >&2
    exit 1
  fi

  # Re-apply propagation in case a prior container teardown reset it.
  # Idempotent — safe to call when already private.
  mount --make-rprivate /data 2>/dev/null || true
  mount --make-rprivate "$STATE_DIR" 2>/dev/null || true
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
# regenerates real bytes. Same `-s` rule for Caddyfile: without it (or
# with an empty one) the caddy service crash-loops while the tls overlay
# has already closed :8000 — net effect is "app unreachable". Skipping
# the overlay keeps the app on plain :8000 until config lands.
if [ -s "$STATE_DIR/certs/fullchain.pem" ] && [ -s "$STATE_DIR/certs/privkey.pem" ] && [ -s Caddyfile ]; then
    COMPOSE_FILES+=( -f docker-compose.tls.yml )
    PROFILE_ARGS=( --profile tls )
elif [ -s "$STATE_DIR/certs/fullchain.pem" ] && [ -s "$STATE_DIR/certs/privkey.pem" ]; then
    logger -t agnes-auto-upgrade "WARN: certs present but Caddyfile missing/empty — skipping tls overlay"
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
