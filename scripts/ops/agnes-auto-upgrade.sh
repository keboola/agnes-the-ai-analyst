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

# Single-instance guard. GCE live migration / clock-jump events make
# cron deliver several catch-up ticks in a single second (saw 4 ticks
# in ≤2s on a freshly-migrated VM), and parallel runs of this script
# race on `docker compose pull` + `docker images --digest` — different
# runners observe different digest values for the same tag, the diff
# trips the "image digest moved" branch, and a `docker compose up -d`
# fires for an upgrade that hasn't actually happened. flock with -n
# (non-blocking) means the second runner exits cleanly without
# log-spamming; the next regular 5-min tick handles whatever real
# change is pending. /var/lock survives reboots on Ubuntu (tmpfs is
# recreated at boot, but that's fine — the lock only needs to be
# unique among concurrently-running processes).
exec 9>/var/lock/agnes-auto-upgrade.lock
flock -n 9 || {
  logger -t agnes-auto-upgrade "another instance holds the lock — exiting"
  exit 0
}

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
#
# The TLS-overlay decision deliberately runs BELOW the config re-fetch
# (Devin Review caught: this used to live here, evaluating Caddyfile
# existence against the PRE-fetch state. If the fetch added a
# previously-missing Caddyfile, this tick's docker compose would still
# omit `--profile tls` until the next 5-minute tick — a window where
# the recreate uses the wrong overlay set). Base file list is fine to
# initialise here because the tls overlay is the only conditional one.
COMPOSE_FILES=( -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml )
PROFILE_ARGS=()

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

# `-s` (size > 0) instead of `-f` — guards against the corner case where
# rotate.sh wrote a 0-byte cert and exited (or got SIGKILLed mid-write).
# Bringing up the tls profile against an empty cert would just crash
# Caddy on start; better to fall back to plain :8000 until rotate
# regenerates real bytes. Same `-s` rule for Caddyfile: without it (or
# with an empty one) the caddy service crash-loops while the tls overlay
# has already closed :8000 — net effect is "app unreachable". Skipping
# the overlay keeps the app on plain :8000 until config lands.
#
# Evaluated AFTER the config re-fetch above so a freshly-added or
# freshly-removed Caddyfile is reflected in this tick's compose set,
# not the next one.
if [ -s "$STATE_DIR/certs/fullchain.pem" ] && [ -s "$STATE_DIR/certs/privkey.pem" ] && [ -s Caddyfile ]; then
    COMPOSE_FILES+=( -f docker-compose.tls.yml )
    PROFILE_ARGS=( --profile tls )
elif [ -s "$STATE_DIR/certs/fullchain.pem" ] && [ -s "$STATE_DIR/certs/privkey.pem" ]; then
    logger -t agnes-auto-upgrade "WARN: certs present but Caddyfile missing/empty — skipping tls overlay"
fi

BEFORE=$(docker images --no-trunc --format '{{.Digest}}' "$IMAGE" | head -1)
docker compose "${COMPOSE_FILES[@]}" pull >/dev/null 2>&1
AFTER=$(docker images --no-trunc --format '{{.Digest}}' "$IMAGE" | head -1)
if [ "$BEFORE" != "$AFTER" ] || [ "$CONFIG_BEFORE" != "$CONFIG_AFTER" ]; then
    REASON=()
    [ "$BEFORE" != "$AFTER" ] && REASON+=("image digest")
    [ "$CONFIG_BEFORE" != "$CONFIG_AFTER" ] && REASON+=("config files")

    # Sync-in-flight defer guard. ``docker compose up -d`` recreates the
    # uvicorn worker, which kills any in-flight extractor / materialized
    # pass that was holding ``_sync_lock``. The next 5-min cron tick
    # picks up the same change — we just delay the upgrade until the
    # current sync finishes (typically minutes for small tables, longer
    # for big Snowflake UNLOADs). curl with a 5s timeout: if the app is
    # unreachable for any reason (already crashed, port not bound,
    # older app version without /api/sync/status), we proceed with the
    # upgrade — being stuck on a wedged previous version is worse than
    # interrupting a hypothetical sync.
    LOCK_JSON=$(curl -sf --max-time 5 http://localhost:8000/api/sync/status 2>/dev/null || true)
    if echo "$LOCK_JSON" | grep -q '"locked"[[:space:]]*:[[:space:]]*true'; then
        echo "$(date): sync in flight (${REASON[*]} pending) — deferring recreate to next tick"
        logger -t agnes-auto-upgrade "deferred recreate: sync in flight (${REASON[*]})"
        exit 0
    fi

    echo "$(date): change detected (${REASON[*]}) — recreating containers"

    # Re-align ownership of mounted state to the image's runtime user
    # before bringing containers up. Catches root → non-root UID
    # transitions across upgrades — old root-owned files would otherwise
    # cause PermissionError on .session_secret / DuckDB on the new
    # image's first start. Idempotent (no-op when ownership already
    # matches). The Dockerfile pins runtime to uid:gid 999:999 today
    # (`useradd --system --uid 999 ... agnes`); read it back from the
    # image config to stay honest if that ever changes. Only relevant
    # when the image digest actually changed.
    if [ "$BEFORE" != "$AFTER" ]; then
        IMAGE_USER=$(docker image inspect -f '{{.Config.User}}' "$IMAGE" 2>/dev/null || true)
        if [ -n "$IMAGE_USER" ] && [ "$IMAGE_USER" != "root" ] && [ "$IMAGE_USER" != "0" ]; then
            # IMAGE_USER may be "agnes" (name) or "999" or "999:999".
            # Resolve via /etc/passwd inside the image — works without
            # requiring a shell in the runtime layer.
            IMAGE_UIDGID=$(docker run --rm --entrypoint cat "$IMAGE" /etc/passwd 2>/dev/null \
                | awk -F: -v u="${IMAGE_USER%%:*}" '$1==u || $3==u {print $3":"$4; exit}')
            if [ -n "$IMAGE_UIDGID" ]; then
                # /data/tmp is the default ``AGNES_TEMP_DIR`` from
                # docker-compose.yml — Snowflake-UNLOAD slice staging
                # and CSV intermediates land here. Without an explicit
                # mkdir + chown, the runtime user can't create it
                # under a root-owned ``/data`` (the data disk's root
                # comes up root-owned on first mount). ``mkdir -p``
                # is idempotent so existing dirs survive.
                mkdir -p /data/tmp 2>/dev/null || true
                for d in "$STATE_DIR" /data/extracts /data/analytics /data/tmp; do
                    [ -d "$d" ] && chown -R "$IMAGE_UIDGID" "$d" 2>/dev/null || true
                done
            fi
        fi
    fi
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
