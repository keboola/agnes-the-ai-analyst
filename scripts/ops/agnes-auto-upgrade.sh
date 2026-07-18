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

# Read only the three infra-controlled keys this script needs from
# /opt/agnes/.env, rather than bash-sourcing the whole file. The .env also
# carries free-text app config (AGNES_INSTANCE_CUSTOM_PREAMBLE,
# AGNES_INSTANCE_BRAND, …) whose values can contain shell metacharacters
# (backticks, `>`, `$`, quotes). `. /opt/agnes/.env` executed those and
# aborted with a syntax error, silently blocking every 5-min upgrade tick.
# Docker Compose parses .env with its own safe parser, so only this
# host-side sourcing was affected. AGNES_TAG / STATE_DIR / COMPOSE_FILE are
# all simple tokens (tag, path, colon-separated file list) — no free text —
# so extracting them line-by-line is both sufficient and injection-proof.
_env_get() {
  # First KEY= line wins (startup.sh writes each once; env-reconcile upserts
  # in place). Strips one surrounding layer of single/double quotes; the
  # VALUE is never shell-evaluated. Always exits 0 (missing key → empty →
  # the ${VAR:-default} fallbacks below apply).
  grep -m1 -E "^$1=" /opt/agnes/.env 2>/dev/null \
    | sed -e "s/^$1=//" -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" || true
}
# Assign separately from `export` so shellcheck (SC2155) doesn't flag the
# command substitution's return value as masked.
AGNES_TAG="$(_env_get AGNES_TAG)"
STATE_DIR="$(_env_get STATE_DIR)"
COMPOSE_FILE="$(_env_get COMPOSE_FILE)"
# SCHEDULER_API_TOKEN: same shared secret startup-script.sh.tpl mints for
# the scheduler container (see services/scheduler/__main__.py) — reused
# here as the `Authorization: Bearer` credential for the data-refresh-job
# defer probe below (GET /api/jobs is Depends(require_admin), and this
# token resolves to the synthetic scheduler@system.local admin user).
# COMPOSE_PROFILES: docker compose honors this exactly like COMPOSE_FILE
# (both are picked up automatically once exported) — an operator opts a
# VM into the role-split (m-tier) topology purely by setting COMPOSE_FILE
# to include docker-compose.mtier.yml and COMPOSE_PROFILES=mtier in
# /opt/agnes/.env; no change to how this script invokes `docker compose`
# is needed. Wiring the Terraform module to set these by default is a
# later wave (this one only makes the host scripts role-split-ready).
SCHEDULER_API_TOKEN="$(_env_get SCHEDULER_API_TOKEN)"
COMPOSE_PROFILES="$(_env_get COMPOSE_PROFILES)"
export AGNES_TAG STATE_DIR COMPOSE_FILE SCHEDULER_API_TOKEN COMPOSE_PROFILES

STATE_DIR="${STATE_DIR:-/data/state}"

# Shared alert-webhook config (same file + payload contract as
# agnes-watchdog.sh / agnes-db-backup.sh): empty WEBHOOK_URL = log-only.
# Used below when a role-split rolling recreate has to abort mid-rollout.
[ -f /etc/agnes-watchdog.env ] && . /etc/agnes-watchdog.env
WEBHOOK_URL="${WEBHOOK_URL:-}"

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
# COMPOSE_FILE is sourced from /opt/agnes/.env above (startup-script.sh.tpl
# writes the full ``docker-compose.yml:docker-compose.prod.yml:docker-compose.postgres.yml:docker-compose.host-mount.yml``
# list so the prod + postgres + host-mount overlays engage by default on
# every tick — note host-mount loads LAST so its !override on
# data-migrate.volumes can see the service already defined by
# docker-compose.postgres.yml). Fall back to the historical prod + host-mount baseline when
# .env doesn't set it — keeps long-uptime VMs whose .env predates the
# COMPOSE_FILE line behaving identically.
#
# The colon-separated COMPOSE_FILE form is the documented alternative to
# explicit ``-f`` args (docker.com/compose/reference/envvars/compose_file).
# The conditional TLS overlay (further down) APPENDS via the same
# colon-separator so docker compose sees a unified list — interleaving
# ``-f`` args and a COMPOSE_FILE env var is unspecified behaviour.
export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml:docker-compose.prod.yml:docker-compose.host-mount.yml}"
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
  docker-compose.postgres.yml docker-compose.postgres-host-mount.yml
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
    # Append tls overlay onto the colon-separated COMPOSE_FILE list. Idempotent
    # check guards against the cron tick re-appending across self-update
    # iterations (unlikely since COMPOSE_FILE is re-sourced from .env each
    # run, but cheap insurance).
    case ":$COMPOSE_FILE:" in
      *:docker-compose.tls.yml:*) : ;;
      *) export COMPOSE_FILE="$COMPOSE_FILE:docker-compose.tls.yml" ;;
    esac
    PROFILE_ARGS=( --profile tls )
elif [ -s "$STATE_DIR/certs/fullchain.pem" ] && [ -s "$STATE_DIR/certs/privkey.pem" ]; then
    logger -t agnes-auto-upgrade "WARN: certs present but Caddyfile missing/empty — skipping tls overlay"
fi

# gcplogs overlay — ships container stdout/stderr to GCP Cloud Logging. Gated
# purely on file presence (mirrors the tls append above): the file is NOT baked
# into the image and is NOT in CONFIG_FILES, so it lands ONLY when the GCE deploy
# layer (Terraform startup-script / infra startup.sh) placed it. On non-GCP hosts
# the file is absent → the overlay is never appended → containers stay on the
# default json-file driver (gcplogs would otherwise fail without a GCE metadata
# server). Idempotent case-check guards against re-appending across ticks.
if [ -f docker-compose.gcp-logging.yml ]; then
    case ":$COMPOSE_FILE:" in
      *:docker-compose.gcp-logging.yml:*) : ;;
      *) export COMPOSE_FILE="$COMPOSE_FILE:docker-compose.gcp-logging.yml" ;;
    esac
fi

# COMPOSE_FILE is exported above; docker compose picks it up automatically.
# `|| …` so a pull failure (registry outage, transient network/auth blip)
# doesn't abort the script under `set -e` BEFORE drift detection runs —
# a pending upgrade whose image was already pulled on a previous tick
# must still be applied from the local store. The warning keeps the
# failure visible in syslog instead of silently masking it.
docker compose pull >/dev/null 2>&1 \
  || logger -t agnes-auto-upgrade "WARN: docker compose pull failed — proceeding with locally available images"

# ---------------------------------------------------------------------------
# Role-split (m-tier) rolling-recreate support (spec §3.8/§3.9).
#
# list_api_replicas: the resolved compose config (COMPOSE_FILE +
# COMPOSE_PROFILES, both sourced from /opt/agnes/.env above) defines
# dedicated `worker` + `gateway` services alongside 2+ named api-replica
# services (api1, api2, ...) only for a role-split topology — the exact
# shape docker-compose.mtier.yml ships. `docker compose config --services`
# already filters by active profiles the same way `up` does, so an
# operator opts into this path purely via .env, no change to how this
# script invokes `docker compose` elsewhere. Single-container (S tier)
# deployments never define `worker`/`gateway` at all, so this always
# prints nothing there — the one-shot recreate path below is unchanged.
list_api_replicas() {
    local services
    services=$(docker compose ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} config --services 2>/dev/null) || return 0
    grep -qx worker <<<"$services" || return 0
    grep -qx gateway <<<"$services" || return 0
    grep -E '^api[0-9]+$' <<<"$services" | sort || true
}

# wait_for_readyz: polls /readyz INSIDE the replica's own container — api
# replicas are proxy-only exposure under docker-compose.mtier.yml (no host
# port to curl from the VM directly), so this shells into the container via
# `docker compose exec`. /readyz (app/api/health_probes.py) answers 503
# until ready, 200 with {"status":"ready", ...} once it is — `curl -sf`
# already fails (empty stdout) on the 503 case, so the grep is a cheap
# double-check rather than the only signal. Both knobs are overridable env
# (the bash-harness unit test uses a near-zero interval/timeout instead of
# sleeping through the production defaults).
READYZ_POLL_INTERVAL="${AGNES_AUTO_UPGRADE_READYZ_INTERVAL:-5}"
READYZ_POLL_TIMEOUT="${AGNES_AUTO_UPGRADE_READYZ_TIMEOUT:-120}"
wait_for_readyz() {
    local svc="$1"
    local waited=0
    while :; do
        if docker compose exec -T "$svc" curl -sf -m 5 http://localhost:8000/readyz 2>/dev/null \
            | grep -q '"status"[[:space:]]*:[[:space:]]*"ready"'; then
            return 0
        fi
        [ "$waited" -ge "$READYZ_POLL_TIMEOUT" ] && return 1
        sleep "$READYZ_POLL_INTERVAL"
        waited=$((waited + READYZ_POLL_INTERVAL))
    done
}

# send_rollout_alert: same shared webhook config + JSON-escaping as
# agnes-watchdog.sh / agnes-db-backup.sh (Slack/Google-Chat compatible
# `{"text": "..."}` POST). Always logs; only POSTs when WEBHOOK_URL is set.
send_rollout_alert() {
    local msg="$1"
    logger -t agnes-auto-upgrade "$msg"
    [ -n "$WEBHOOK_URL" ] || return 0
    local esc
    esc=$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n", $0}')
    curl -sf -m 10 -X POST -H 'Content-Type: application/json' \
        -d "{\"text\": \"$esc\"}" "$WEBHOOK_URL" >/dev/null 2>&1 \
        || logger -t agnes-auto-upgrade "webhook send failed"
}

# recreate_role_split: worker + gateway recreate together first (they sit
# behind no load balancer, so no readyz gate buys anything for them); the
# api replicas passed as args then recreate ONE AT A TIME, each gated on
# its own /readyz before the next is even touched. A replica that never
# reports ready within the bounded timeout ABORTS the whole rollout —
# alerts and returns non-zero WITHOUT recreating the remaining replicas,
# which stay on the previous image and keep serving traffic. The initial
# worker+gateway recreate gets the same abort posture: a hard failure
# there (bad image, compose config error, daemon hiccup) must not fall
# through into recreating api replicas against a worker/gateway pair that
# never came up — alert and abort before touching any api replica.
recreate_role_split() {
    if ! docker compose ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} up -d --no-deps worker gateway; then
        send_rollout_alert "agnes-auto-upgrade: ABORTED role-split rolling recreate — worker/gateway recreate failed (docker compose up -d --no-deps worker gateway exited non-zero). Api replicas were not touched and remain on the previous image."
        return 1
    fi

    local svc
    for svc in "$@"; do
        docker compose ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} up -d --no-deps "$svc"
        if ! wait_for_readyz "$svc"; then
            send_rollout_alert "agnes-auto-upgrade: ABORTED role-split rolling recreate — $svc did not report /readyz ready within ${READYZ_POLL_TIMEOUT}s. Remaining api replicas left on the previous image."
            return 1
        fi
        logger -t agnes-auto-upgrade "rolling recreate: $svc ready, proceeding"
    done
}

# sync_or_refresh_busy: populates the DEFER_REASON array (global) and
# returns true (0) when either signal says "busy" — the original
# /api/sync/status lock, OR (new) a queued/claimed `data-refresh` job
# actually running in the worker (GET /api/jobs, same require_admin gate
# every other admin endpoint uses, accepting the SCHEDULER_API_TOKEN
# bearer per app/api/jobs.py). Needed because sync now runs in the worker
# process under role-split — /api/sync/status alone only reflects this
# process's in-memory lock and under-reports when the worker is a separate
# container. Fails OPEN on an unreachable app or a missing token, exactly
# like the original probe: being stuck on a wedged previous version is
# worse than interrupting a hypothetical sync.
sync_or_refresh_busy() {
    DEFER_REASON=()
    local lock_json jobs_json
    lock_json=$(curl -sf --max-time 5 http://localhost:8000/api/sync/status 2>/dev/null || true)
    if echo "$lock_json" | grep -q '"locked"[[:space:]]*:[[:space:]]*true'; then
        DEFER_REASON+=("sync/status locked")
    fi
    if [ -n "$SCHEDULER_API_TOKEN" ]; then
        jobs_json=$(curl -sf --max-time 5 \
            -H "Authorization: Bearer $SCHEDULER_API_TOKEN" \
            "http://localhost:8000/api/jobs?kind=data-refresh&status=running&limit=1" 2>/dev/null || true)
        if echo "$jobs_json" | grep -q '"id"'; then
            DEFER_REASON+=("data-refresh job running")
        fi
    fi
    [ "${#DEFER_REASON[@]}" -gt 0 ]
}

# Drift-based change detection — STATELESS on the image side, marker-based
# on the config side. The previous implementation compared the local tag
# digest before/after the pull; that permanently LOST a deferred upgrade:
# the deferring tick's pull had already moved the local tag, so the next
# tick saw before == after, concluded "no change", and never recreated —
# the container stayed on the old image until the *next* release shipped
# (observed live: a VM ran a stale image for 8+ hours with the new tag
# sitting pulled beside it). Comparing what is actually RUNNING against
# what the tag points to has no such window: drift persists across ticks
# until a recreate succeeds.
# Role-split topology check runs here (not earlier) so it sees the fully
# resolved COMPOSE_FILE/COMPOSE_PROFILES (including any tls/gcplogs overlay
# appended above) — `docker compose config --services` needs the same
# environment `up`/`ps` below will use. Single-container deployments never
# define `worker`/`gateway`, so API_REPLICAS is always empty there and the
# one-shot recreate path is exercised unchanged.
API_REPLICAS=()
while IFS= read -r _api_svc; do
    [ -n "$_api_svc" ] && API_REPLICAS+=("$_api_svc")
done < <(list_api_replicas || true)

if [ "${#API_REPLICAS[@]}" -ge 1 ]; then
    ROLE_SPLIT=1
    # All role services extend the same base `app` image, so any one of
    # them is a valid drift reference; `worker` is never gated behind the
    # tls/gcplogs profile logic above, unlike `app` itself under mtier
    # (profiles: ["standalone"], never started there).
    DRIFT_REF_SERVICE=worker
else
    ROLE_SPLIT=0
    DRIFT_REF_SERVICE=app
fi

TAG_ID=$(docker images --no-trunc --format '{{.ID}}' "$IMAGE" | head -1)
RUNNING_CID=$(docker compose ps -q "$DRIFT_REF_SERVICE" 2>/dev/null | head -1)
RUNNING_ID=""
if [ -n "$RUNNING_CID" ]; then
    RUNNING_ID=$(docker inspect --format '{{.Image}}' "$RUNNING_CID" 2>/dev/null || true)
fi
# No local tag image (pull failed on a fresh host) → nothing to compare,
# skip. No running reference container → recreate (compose up starts it).
IMAGE_DRIFT=0
if [ -n "$TAG_ID" ] && [ "$RUNNING_ID" != "$TAG_ID" ]; then
    IMAGE_DRIFT=1
fi

# Config drift had the same lost-defer hole — the re-fetch above already
# rewrote the files before the defer exits, so a before/after comparison
# forgets the change by the next tick. Track the hash that was in effect
# at the last successful recreate in a marker file; drift = current hash
# != marker. Lazily initialized so the first tick after this script lands
# on a VM records the status quo instead of forcing a spurious recreate.
CONFIG_MARKER=/opt/agnes/.agnes-config-applied
if [ ! -f "$CONFIG_MARKER" ]; then
    printf '%s\n' "$CONFIG_AFTER" > "$CONFIG_MARKER"
fi
CONFIG_APPLIED=$(cat "$CONFIG_MARKER" 2>/dev/null || true)
CONFIG_DRIFT=0
if [ "$CONFIG_AFTER" != "$CONFIG_APPLIED" ]; then
    CONFIG_DRIFT=1
fi

if [ "$IMAGE_DRIFT" = "1" ] || [ "$CONFIG_DRIFT" = "1" ]; then
    REASON=()
    [ "$IMAGE_DRIFT" = "1" ] && REASON+=("image drift")
    [ "$CONFIG_DRIFT" = "1" ] && REASON+=("config files")

    # Sync-in-flight defer guard. ``docker compose up -d`` recreates the
    # uvicorn worker, which kills any in-flight extractor / materialized
    # pass that was holding ``_sync_lock`` — so we delay the upgrade until
    # the current sync finishes (typically minutes for small tables,
    # longer for big Snowflake UNLOADs). Deferring is SAFE with the drift
    # detection above: the drift persists until a recreate actually
    # succeeds, so the next 5-min tick re-detects the same pending change
    # — there is no state to lose. Both probes fail OPEN (see
    # sync_or_refresh_busy above): an unreachable app, an older release
    # without one of the endpoints, or a missing SCHEDULER_API_TOKEN
    # proceed with the upgrade rather than wedge on the previous version.
    if sync_or_refresh_busy; then
        echo "$(date): sync/refresh in flight (${DEFER_REASON[*]}; pending: ${REASON[*]}) — deferring recreate to next tick"
        logger -t agnes-auto-upgrade "deferred recreate: sync/refresh in flight (${DEFER_REASON[*]})"
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
    # when the image actually changed.
    if [ "$IMAGE_DRIFT" = "1" ]; then
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
    # COMPOSE_FILE (incl. any conditionally-appended overlays) is exported
    # above and picked up by docker compose automatically.
    #
    # Role-split topology: sequential /readyz-gated rolling recreate
    # (worker+gateway, then api replicas one at a time) instead of the
    # one-shot recreate — see recreate_role_split above. A persistent
    # readyz failure aborts here WITHOUT touching the config marker below,
    # so the next tick re-detects the same pending drift and retries; the
    # remaining api replicas are left serving the previous image.
    if [ "$ROLE_SPLIT" = "1" ]; then
        if ! recreate_role_split "${API_REPLICAS[@]}"; then
            echo "$(date): role-split rolling recreate ABORTED (${REASON[*]}) — see logs/alert" >&2
            exit 1
        fi
    else
        docker compose ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} up -d
    fi
    # Record the config hash that is now in effect — config drift is
    # declared against this marker on subsequent ticks.
    printf '%s\n' "$CONFIG_AFTER" > "$CONFIG_MARKER"
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
