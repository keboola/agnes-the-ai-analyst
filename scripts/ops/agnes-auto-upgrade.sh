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
# regenerates real bytes. Same `-s` rule for Caddyfile: without it (or
# with an empty one) the caddy service crash-loops while the tls overlay
# has already closed :8000 — net effect is "app unreachable". Skipping
# the overlay keeps the app on plain :8000 until config lands.
if [ -s /data/state/certs/fullchain.pem ] && [ -s /data/state/certs/privkey.pem ] && [ -s Caddyfile ]; then
    COMPOSE_FILES+=( -f docker-compose.tls.yml )
    PROFILE_ARGS=( --profile tls )
elif [ -s /data/state/certs/fullchain.pem ] && [ -s /data/state/certs/privkey.pem ]; then
    logger -t agnes-auto-upgrade "WARN: certs present but Caddyfile missing/empty — skipping tls overlay"
fi
BEFORE=$(docker images --no-trunc --format '{{.Digest}}' "$IMAGE" | head -1)
docker compose "${COMPOSE_FILES[@]}" pull >/dev/null 2>&1
AFTER=$(docker images --no-trunc --format '{{.Digest}}' "$IMAGE" | head -1)

# Re-fetch the bind-mounted config files (compose overlays + Caddyfile)
# from the OSS main branch on every tick. Without this, an image-only
# change is fine, but a change to the Caddyfile or any compose overlay
# (e.g. a new bind mount, a route, an env_file path) only lands on VMs
# that get a fresh `startup.sh` boot — leaving long-uptime VMs running
# the new image against stale config. Confirmed live on 2026-05-05
# when a Caddyfile change adding a `data:/srv:ro` mount + a new
# `forward_auth` + `file_server` route for parquet downloads landed
# in main but stayed inert on running VMs because auto-upgrade only
# watched image digests.
#
# Hash before/after to detect content drift; treat as "trigger recreate"
# alongside an image digest change. Atomic move-after-fetch guards
# against a partial download corrupting compose at the next docker
# action — `curl --fail` plus the `.new` rename means a 404 / network
# blip leaves the existing file untouched.
RAW_BASE="https://raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main"
CONFIG_FILES=(
  docker-compose.yml docker-compose.prod.yml docker-compose.host-mount.yml
  docker-compose.tls.yml Caddyfile
)
hash_config_files() {
  # Sort to keep hash stable across operator add/remove, missing files
  # contribute the empty string (sha256 of "" is well-defined). Run
  # from /opt/agnes to keep relative paths terse in the hash input.
  ( cd /opt/agnes && for f in "${CONFIG_FILES[@]}"; do
      sha256sum "$f" 2>/dev/null || printf 'missing %s\n' "$f"
    done ) | sort | sha256sum | awk '{print $1}'
}
CONFIG_BEFORE=$(hash_config_files)
for f in "${CONFIG_FILES[@]}"; do
  if curl -fsSL "$RAW_BASE/$f" -o "/opt/agnes/$f.new" 2>/dev/null; then
    mv -f "/opt/agnes/$f.new" "/opt/agnes/$f"
  else
    rm -f "/opt/agnes/$f.new"
    logger -t agnes-auto-upgrade "WARN: failed to fetch $f from $RAW_BASE — keeping existing /opt/agnes/$f"
  fi
done
CONFIG_AFTER=$(hash_config_files)

if [ "$BEFORE" != "$AFTER" ] || [ "$CONFIG_BEFORE" != "$CONFIG_AFTER" ]; then
    REASON=()
    [ "$BEFORE" != "$AFTER" ] && REASON+=("image digest")
    [ "$CONFIG_BEFORE" != "$CONFIG_AFTER" ] && REASON+=("config files")
    echo "$(date): change detected (${REASON[*]}) — recreating containers"
    # ${arr[@]+"${arr[@]}"} pattern: expands to nothing when array is
    # empty (vs. plain "${arr[@]}" which trips `set -u` on bash <4.4).
    docker compose "${COMPOSE_FILES[@]}" ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} up -d
    docker image prune -f >/dev/null 2>&1
fi

# Self-update: re-fetch *this* script too. Without this, the very fix
# that lets auto-upgrade watch config files would itself never land on
# running VMs — a self-perpetuating "old script" problem. Atomic via
# .new + mv; chmod preserved. The next tick (5 min later) runs the
# new logic. Skipping if curl fails leaves the existing script in place.
if curl -fsSL "$RAW_BASE/scripts/ops/agnes-auto-upgrade.sh" \
   -o /usr/local/bin/agnes-auto-upgrade.sh.new 2>/dev/null; then
  if ! cmp -s /usr/local/bin/agnes-auto-upgrade.sh.new \
                /usr/local/bin/agnes-auto-upgrade.sh; then
    chmod +x /usr/local/bin/agnes-auto-upgrade.sh.new
    mv -f /usr/local/bin/agnes-auto-upgrade.sh.new \
          /usr/local/bin/agnes-auto-upgrade.sh
    logger -t agnes-auto-upgrade "self-update: replaced /usr/local/bin/agnes-auto-upgrade.sh"
  else
    rm -f /usr/local/bin/agnes-auto-upgrade.sh.new
  fi
fi
