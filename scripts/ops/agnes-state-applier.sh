#!/bin/bash
# Host-side daemon for the DB-backend state machine.
#
# Two responsibilities, both driven from /data/state:
#   1. Compose lifecycle — bring the postgres container up/down based on
#      the desired-state flag at /data/state/db-state-target.flag.
#   2. Data migration — when a job entry under /data/state/db-jobs/ is in
#      status "pending", stop the app container (releasing its DuckDB
#      file lock — the entire reason migration cannot run as an in-app
#      subprocess), run the migrator on the host with /data bind-mounted,
#      then restart the app on the new backend.
#
# Why the host runs the migrator instead of the FastAPI handler: DuckDB
# >=1.5 holds an exclusive per-process file lock on system.duckdb. Even
# explicit conn.close() + close_singleton_connections() + gc.collect()
# inside the uvicorn worker do not deterministically release that lock
# (Python keeps the file descriptor pinned until the process exits).
# Verified live on agnes-dev: ``lsof`` shows the lock outlives every
# in-process release we tried. Running the migrator from the host with
# the app container fully stopped is the only path that's reliable.
#
# Runs every 30s via systemd timer. Idempotent — if there is no pending
# job and the lifecycle matches the flag, it exits without doing
# anything.
set -euo pipefail

FLAG=/data/state/db-state-target.flag
JOBS_DIR=/data/state/db-jobs
COMPOSE_DIR=/opt/agnes
LOCK_FILE=/data/state/db-state-applier.lock

# Prevent concurrent applier runs (the timer can fire while a previous
# tick is still mid-migration; flock returns immediately if held).
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

# --- Applier heartbeat (Phase 4) -----------------------------------------
# Touch a tick file so /api/admin/db/state can expose
# ``applier_last_tick_age_s`` for UI liveness. Fired on EVERY
# invocation including no-op ticks so the value stays fresh during
# idle periods. None / missing tick = applier has never run (fresh
# install, broken unit, OS reboot wiped the systemd target).
# State dir is guaranteed to exist (LOCK_FILE lives there), so the
# mkdir is just a defensive guard.
mkdir -p "$(dirname "$LOCK_FILE")"
touch /data/state/agnes-state-applier.tick || true

if [ ! -f "$FLAG" ]; then
    exit 0
fi
TARGET="$(tr -d '[:space:]' < "$FLAG")"

cd "$COMPOSE_DIR"
# shellcheck disable=SC1091
set -a; . "$COMPOSE_DIR/.env"; set +a

# Compose chain reused for every invocation. Mirrors the layering in
# agnes-auto-upgrade.sh so this daemon plays well with the existing -f
# argument style on agnes-dev/agnes-prod (no COMPOSE_FILE env coupling).
COMPOSE_FILES=( -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml )
if [ -f "$COMPOSE_DIR/docker-compose.tls.yml" ] && [ -d /data/state/certs ]; then
    COMPOSE_FILES+=( -f docker-compose.tls.yml )
fi
case "$TARGET" in
    side-car-enabled)
        COMPOSE_FILES+=( -f docker-compose.postgres.yml -f docker-compose.postgres-host-mount.yml )
        ;;
esac
dc() { docker compose "${COMPOSE_FILES[@]}" "$@"; }

# --- Pending-job detection ------------------------------------------------
# A job file with status=pending is the signal that the API endpoint
# wants us to actually MIGRATE data, not just shift lifecycle. We pick
# the oldest pending — there should usually only be one because the
# API holds the MigrationLock until it has written the job.
# Sort by mtime so we maintain FIFO ordering if two jobs queued up
# (applier missed a tick, operator submitted back-to-back requests).
#
# H8 — Pending-job expiry. A pending job whose ``queued_at`` is older
# than PENDING_JOB_MAX_AGE_SEC (default 3600s = 1h) is marked failed
# without being processed: the operator may have masked the timer,
# queued a migration, manually fixed state via the CLI, then unmasked
# weeks later — running an old intent against now-incompatible current
# state would be worse than dropping the request. Expiry runs BEFORE
# the candidate scan so an expired pending is excluded from selection.
PENDING_JOB_MAX_AGE_SEC=${PENDING_JOB_MAX_AGE_SEC:-3600}
PENDING_JOB=""
if [ -d "$JOBS_DIR" ]; then
    PENDING_JOB=$(python3 - "$JOBS_DIR" "$PENDING_JOB_MAX_AGE_SEC" <<'PY' 2>/dev/null
import json, os, sys, time
from datetime import datetime, timezone
d = sys.argv[1]
max_age = int(sys.argv[2])
now = datetime.now(timezone.utc)
candidates = []
for f in os.listdir(d):
    if not f.endswith(".json"):
        continue
    p = os.path.join(d, f)
    try:
        data = json.load(open(p))
    except Exception:
        continue
    if data.get("status") != "pending":
        continue
    queued_at = data.get("queued_at")
    age = None
    if queued_at:
        try:
            age = (now - datetime.fromisoformat(queued_at)).total_seconds()
        except Exception:
            age = None
    # No queued_at (pre-H8 jobs) or unparseable timestamp — fall back to
    # filesystem mtime so the expiry guard still bites on legacy files.
    if age is None:
        age = now.timestamp() - os.path.getmtime(p)
    if age > max_age:
        # Atomic-rewrite as failed/expired so the next tick (or the
        # API status endpoint) sees the terminal state.
        data["status"] = "failed"
        data.setdefault("error", {})
        data["error"]["step"] = "queued"
        data["error"]["class"] = "PendingJobExpired"
        data["error"]["message"] = (
            f"pending job expired (queued {int(age)}s ago, threshold {max_age}s); "
            "applier refuses to run stale intent against potentially-divergent state"
        )
        tmp = p + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, p)
        os.chmod(p, 0o600)  # H2-NEW: tmp inherited umask 0644; restore 0600.
        continue
    candidates.append((os.path.getmtime(p), p))
candidates.sort()
print(candidates[0][1] if candidates else "")
PY
    ) || PENDING_JOB=""
fi

# --- Helpers --------------------------------------------------------------
update_job() {
    # Set status + optional error.message on a job file. Atomic via
    # tmp+rename so the API endpoint never reads half-written JSON.
    local file=$1 status=$2 error=${3:-}
    python3 - <<PY "$file" "$status" "$error"
import json, os, sys
p, status, err = sys.argv[1], sys.argv[2], sys.argv[3]
with open(p) as fh:
    data = json.load(fh)
data["status"] = status
if err:
    data.setdefault("error", {})
    data["error"]["message"] = err
    data["error"].setdefault("step", data.get("current_step", "unknown"))
tmp = p + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=2)
os.replace(tmp, p)
os.chmod(p, 0o600)  # H2-NEW: tmp inherited umask 0644; restore 0600.
PY
}

write_instance_yaml() {
    # Preserve all non-database top-level keys (logging, auth_providers,
    # feature_flags, etc. the operator may have set via the admin UI).
    # The previous bash heredoc approach rewrote the file from scratch and
    # silently destroyed them (B6 — review finding).
    #
    # H4-NEW: graceful fallback when PyYAML is unavailable on the host.
    # Provisioning installs python3-yaml so this fallback is defensive-only,
    # but old or stripped VMs should not wedge the state machine on a
    # missing dependency.
    local backend=$1 url=${2:-}
    local path="/data/state/instance.yaml"
    # Try PyYAML route first — preserves any non-database top-level keys
    # the operator set (logging, auth providers, feature flags).
    if python3 -c 'import yaml' 2>/dev/null; then
        python3 - "$path" "$backend" "$url" <<'PY'
import os, sys, yaml
path, backend, url = sys.argv[1], sys.argv[2], sys.argv[3]
existing = {}
if os.path.exists(path):
    try:
        existing = yaml.safe_load(open(path).read()) or {}
    except Exception:
        existing = {}
db = dict(existing.get("database") or {})
db["backend"] = backend
if url:
    db["url"] = url
else:
    db.pop("url", None)
existing["database"] = db
tmp = path + ".tmp"
with open(tmp, "w") as f:
    yaml.safe_dump(existing, f, default_flow_style=False, sort_keys=True)
os.replace(tmp, path)
os.chmod(path, 0o600)
PY
        chown agnes-applier:agnes-applier "$path" 2>/dev/null || true
        return
    fi
    # Pure-bash fallback. H4-NEW — preserves the database section only;
    # any non-database top-level keys are LOST. Provisioning should
    # install python3-yaml so this path is rarely hit; we keep it alive
    # so a missing dependency never wedges the state machine.
    echo "WARN: write_instance_yaml using bash fallback (PyYAML not installed); non-database top-level keys will be dropped" >&2
    local tmp="${path}.tmp"
    {
        echo "database:"
        echo "  backend: ${backend}"
        if [ -n "$url" ]; then
            echo "  url: ${url}"
        fi
    } > "$tmp"
    chmod 0600 "$tmp"
    mv -f "$tmp" "$path"
    chown agnes-applier:agnes-applier "$path" 2>/dev/null || true
}

# --- Stuck-running recovery (B5 + H5-NEW) ------------------------------------
# Extracted into a function so it can be unit-tested and called cleanly.
_recover_stuck_jobs() {
    # H5-NEW + B5: jobs whose heartbeat is older than 120s are marked
    # failed AND the overlay's database.backend is restored to
    # source_backend. Without the restore, the next migration retry reads
    # ``*_in_progress`` as the current backend and the migrator rejects
    # ``source_backend='side_car_in_progress'`` → state machine wedged
    # until an operator manually edits instance.yaml. Recovery now
    # symmetrically calls write_instance_yaml(source_backend, source_url),
    # mirroring the cancel path.
    local jobs_dir="${JOBS_DIR:-/data/state/db-jobs}"
    [ -d "$jobs_dir" ] || return 0
    local now
    now=$(date +%s)
    local job_path alive_path age source_backend source_url
    for job_path in "$jobs_dir"/*.json; do
        [ -f "$job_path" ] || continue
        local st
        st=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('status',''))" "$job_path" 2>/dev/null || echo "")
        [ "$st" = "running" ] || continue
        alive_path="${job_path%.json}.alive"
        if [ -f "$alive_path" ]; then
            age=$(( now - $(stat -c '%Y' "$alive_path" 2>/dev/null || stat -f '%m' "$alive_path") ))
        else
            age=999
        fi
        [ "$age" -gt 120 ] || continue
        # Read source_backend + source_url BEFORE we rewrite the job so
        # the values are captured from the original running record.
        source_backend=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('source_backend','') or '')" "$job_path" 2>/dev/null || echo "")
        source_url=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('source_url','') or '')" "$job_path" 2>/dev/null || echo "")
        logger -t agnes-state-applier "Stale running job $job_path: alive=${age}s, marking failed"
        update_job "$job_path" "failed" "stuck running (no heartbeat for ${age}s, host reboot / OOM / docker crash suspected)"
        # H5-NEW: restore instance.yaml from the *_in_progress placeholder
        # back to source_backend. Symmetric with the cancel path.
        # Empty source_url is correct for duckdb sources — write_instance_yaml
        # handles that by dropping the url key.
        if [ -n "$source_backend" ]; then
            write_instance_yaml "$source_backend" "$source_url" || true
        fi
        rm -f "$alive_path"
    done
}

_recover_stuck_jobs

# --- Lifecycle: ensure postgres container matches the flag ----------------
case "$TARGET" in
    side-car-enabled)
        mkdir -p /data/postgres
        # /data/postgres must be owned 70:70 (the Postgres image uid) so
        # the side-car container can write to its volume. The bootstrap
        # unit (root) already does this at boot; the guard below avoids
        # re-running chown when the applier itself runs as non-root
        # (User=agnes-applier) — non-root chown to a different uid
        # requires CAP_CHOWN and would fail with "Operation not
        # permitted" even when the directory is already correctly
        # owned. Only attempt the chown when ownership is wrong AND
        # we have the privilege; on success the next applier tick
        # finds the dir already in shape and skips silently.
        if [ "$(stat -c '%u:%g' /data/postgres 2>/dev/null || echo '')" != "70:70" ]; then
            chown 70:70 /data/postgres 2>/dev/null || {
                echo "WARN: chown 70:70 /data/postgres failed (insufficient privileges); bootstrap unit should have set this at boot" >&2
            }
        fi
        chmod 700 /data/postgres 2>/dev/null || true  # owner-set only; non-root chmod is a no-op when already 700
        if ! docker ps --format '{{.Names}}' | grep -q '^agnes-postgres-1$'; then
            dc up -d postgres
            # Wait for postgres to accept connections — the migrator
            # we'll launch in a moment opens a TCP connection on
            # postgres:5432 and we'd rather fail fast here than have
            # the migrator timeout on its first ALEMBIC operation.
            PG_READY=0
            for _ in $(seq 1 30); do
                if docker exec agnes-postgres-1 pg_isready -U agnes >/dev/null 2>&1; then
                    PG_READY=1
                    break
                fi
                sleep 2
            done
            if [ "$PG_READY" -ne 1 ]; then
                logger -t agnes-state-applier "postgres did not become ready within 60s — aborting"
                exit 1
            fi
        fi
        ;;
    duckdb|cloud-only)
        # Tear down side-car PG if it's running — but only when there's
        # no pending job, otherwise we'd kill the migrator's source DB
        # before it can read from it.
        if [ -z "$PENDING_JOB" ] && docker ps --format '{{.Names}}' | grep -q '^agnes-postgres-1$'; then
            docker stop agnes-postgres-1 >/dev/null 2>&1 || true
            docker rm   agnes-postgres-1 >/dev/null 2>&1 || true
        fi
        ;;
esac

# --- Run migrator if there's a pending job --------------------------------
if [ -z "$PENDING_JOB" ]; then
    exit 0
fi

logger -t agnes-state-applier "Picked up pending migration job: $PENDING_JOB"

# E.5 — structured rollback on unexpected abort.
# When a python heredoc inside this script raises mid-execution,
# ``set -e`` aborts before we can run the post-migrator update_job /
# write_instance_yaml block. Pre-fix the pending job stayed at
# ``pending`` forever and the next applier tick re-picked it (or
# the H8 expiry caught it 1h later). Post-fix the ERR trap
# idempotently marks the pending job failed and reverts
# instance.yaml::backend to the source state so /api/admin/db/state
# never reports a *_in_progress that won't progress.
__rollback() {
    local rc=$?
    local trapped_at=${BASH_LINENO[0]:-?}
    [ -n "${PENDING_JOB:-}" ] || return $rc
    # Best-effort: any failure inside the trap is swallowed so we
    # don't recurse. Status update first; instance.yaml revert
    # second (only if we know the source backend at this point).
    update_job "$PENDING_JOB" "failed" \
        "applier aborted at line ${trapped_at} (rc=${rc}); recovering via ERR trap" \
        || true
    if [ -n "${SOURCE_BACKEND:-}" ]; then
        # H8-NEW: cloud-source rollback used to drop the url because we
        # only passed SOURCE_BACKEND. write_instance_yaml interprets a
        # missing 2nd arg as "drop the key" → the next app boot then
        # tried to start with backend=cloud and no DATABASE_URL,
        # re-introducing the B4-class outage on the failure path.
        # For duckdb source, SOURCE_URL is empty — write_instance_yaml
        # already handles empty URL by dropping the key (correct).
        write_instance_yaml "$SOURCE_BACKEND" "${SOURCE_URL:-}" || true
        case "$SOURCE_BACKEND" in
            duckdb|cloud) rm -f "$FLAG" 2>/dev/null || true ;;
        esac
    fi
    logger -t agnes-state-applier \
        "Applier aborted (rc=${rc}) — rolled back job ${PENDING_JOB##*/} via ERR trap"
    return $rc
}
trap '__rollback' ERR

# Read all required job fields in a single python invocation. The
# fields land into shell vars via `read`; missing optional fields are
# emitted as empty strings. Newline-separated output + `read -r` is
# more shell-safe than a single space-separated line — URLs contain
# special chars that don't survive whitespace tokenization cleanly.
{ read -r JOB_ID; read -r TARGET_URL; read -r TARGET_BACKEND; read -r SOURCE_BACKEND; read -r SOURCE_URL; } < <(
    python3 - "$PENDING_JOB" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d["job_id"])
print(d.get("target_url", ""))
print(d.get("target_backend", ""))
print(d.get("source_backend", ""))
print(d.get("source_url", "") or "")
PY
)

IMAGE="ghcr.io/keboola/agnes-the-ai-analyst:${AGNES_TAG:-stable}"
SOURCE_URL_ARGS=()
if [ -n "$SOURCE_URL" ]; then
    SOURCE_URL_ARGS=( --source-url "$SOURCE_URL" )
fi

# B2-NEW — applier-side alias guard.
# The API endpoint checks _urls_alias before writing the job; this
# guard catches jobs written before B2-NEW was deployed (old pending
# jobs in the queue) where ``postgres`` (compose service name) resolved
# to the same IP as an explicit ``172.18.0.x`` cloud_url, bypassing
# the string-only guard.  Call the same Python implementation so the
# logic stays centralised.
if [ -n "$SOURCE_URL" ] && [ -n "$TARGET_URL" ]; then
    ALIAS_RESULT=$(python3 - "$SOURCE_URL" "$TARGET_URL" <<'PY' 2>/dev/null
import sys
sys.path.insert(0, "/app")
try:
    from app.api.db_state import _urls_alias
    print("ALIAS" if _urls_alias(sys.argv[1], sys.argv[2]) else "DISTINCT")
except Exception:
    print("DISTINCT")
PY
) || ALIAS_RESULT="DISTINCT"
    if [ "$ALIAS_RESULT" = "ALIAS" ]; then
        update_job "$PENDING_JOB" "failed" \
            "applier alias guard (B2-NEW): source and target URL alias the same Postgres database — refusing to migrate onto self"
        logger -t agnes-state-applier \
            "Migration job $JOB_ID aborted: source and target alias the same DB (B2-NEW guard)"
        exit 0
    fi
fi

# 1. Stop the app + scheduler so DuckDB releases the file lock.
docker stop agnes-app-1 agnes-scheduler-1 >/dev/null 2>&1 || true

# 2. Run the migrator on the host with /data bind-mounted. --network
#    agnes_default is needed so 'postgres' resolves for the side_car
#    target_url; safe to pass even when the postgres container is
#    absent (the migrator's verify step uses the target URL, so for
#    cloud the URL is reachable from outside the compose network).
NETWORK_ARGS=()
if docker network ls --format '{{.Name}}' | grep -q '^agnes_default$'; then
    NETWORK_ARGS=( --network agnes_default )
fi

# C.2 — bound the migrator subprocess wall-clock. Engine-side
# statement_timeout (set in _bounded_engine) only caps SQL queries; a
# hung migrator that doesn't reach a step boundary (e.g. wedged DuckDB
# connection holding the GIL) would otherwise sit forever. coreutils
# `timeout(1)` is universally available on customer-instance VMs.
# Exit codes from `timeout`:
#   124 — TERM fired (limit exceeded)
#   137 — KILL fired (TERM ignored, --kill-after kicked in)
# Both indicate the watchdog triggered.
MIGRATOR_TIMEOUT_SEC=${MIGRATOR_TIMEOUT_SEC:-1800}
set +e
timeout --signal=TERM --kill-after=30 "$MIGRATOR_TIMEOUT_SEC" \
    docker run --rm \
        ${NETWORK_ARGS[@]+"${NETWORK_ARGS[@]}"} \
        -v /data:/data \
        -e DATA_DIR=/data \
        "$IMAGE" \
        python -m scripts.db_state_migrator \
            --job-id   "$JOB_ID" \
            --to       "$TARGET_BACKEND" \
            --source-backend "$SOURCE_BACKEND" \
            --target-url "$TARGET_URL" \
            ${SOURCE_URL_ARGS[@]+"${SOURCE_URL_ARGS[@]}"} \
            --duckdb-path /data/state/system.duckdb \
            --jobs-dir   "$JOBS_DIR" \
            --backups-dir /data/state/backups
MIG_RC=$?
set -e
if [ "$MIG_RC" -eq 124 ] || [ "$MIG_RC" -eq 137 ]; then
    update_job "$PENDING_JOB" "failed" \
        "migrator subprocess exceeded ${MIGRATOR_TIMEOUT_SEC}s timeout (rc=${MIG_RC} — watchdog fired)"
    logger -t agnes-state-applier \
        "Migration job $JOB_ID — migrator subprocess timed out after ${MIGRATOR_TIMEOUT_SEC}s"
fi

# 3. Decide post-migration lifecycle based on whether the migrator updated
#    its job file to success. (The migrator owns the JSON during its
#    invocation; if it crashed without writing we set a generic failure.)
FINAL_STATUS=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("status",""))' "$PENDING_JOB")
if [ "$FINAL_STATUS" = "pending" ] || [ -z "$FINAL_STATUS" ]; then
    update_job "$PENDING_JOB" "failed" "migrator process exited with rc=$MIG_RC without writing a terminal status"
    FINAL_STATUS="failed"
fi

if [ "$FINAL_STATUS" = "success" ]; then
    write_instance_yaml "$TARGET_BACKEND" "$TARGET_URL"
    logger -t agnes-state-applier "Migration job $JOB_ID succeeded — flipped instance.yaml backend to $TARGET_BACKEND"
else
    logger -t agnes-state-applier "Migration job $JOB_ID failed — leaving backend on $SOURCE_BACKEND"
    # Roll the state machine back so the next /api/admin/db/state read
    # shows the (non-transient) source backend, not *_in_progress.
    # H8-NEW: also pass SOURCE_URL on the failed-migration path
    # so cloud-source rollbacks don't wipe the url. Same B4-class
    # outage class as the __rollback site.
    write_instance_yaml "$SOURCE_BACKEND" "${SOURCE_URL:-}"
    # Clear the lifecycle flag if the rollback lands on a non-PG state —
    # otherwise the next applier tick would re-trigger the postgres
    # lifecycle ("side-car-enabled" / "cloud-only") and leave an orphan
    # agnes-postgres-1 container running with no data.
    #
    # B.3 — Both duckdb and cloud sources lack a side-car lifecycle
    # need, so both must clear the flag on rollback. The asymmetric
    # original (duckdb only) silently broke cloud→side_car DR rollback:
    # instance.yaml said "cloud" but the flag still said
    # "side-car-enabled", and the next tick re-enabled the postgres
    # container. For source=side_car we keep the flag as-is because
    # "side-car-enabled" is still the correct lifecycle.
    case "$SOURCE_BACKEND" in
        duckdb|cloud)
            rm -f "$FLAG"
            ;;
    esac
fi

# 4. Bring the app back up. After-state app reads instance.yaml and
#    opens the chosen backend on startup.
#
# `--no-deps` is critical: docker-compose.postgres.yml declares the
# `migrate` and `data-migrate` services with `build: .`. On the
# customer-instance VM the source tree isn't present, so any compose
# command that follows the depends_on chain (migrate → app → scheduler)
# attempts a build, fails with `failed to read dockerfile`, and either
# leaves app+scheduler down or up with a stale config. Our state
# machine already ran the migration on the HOST via `docker run` —
# the in-compose migrate/data-migrate services are vestigial here and
# must not be touched on each cycle.
set +e
RESTART_LOG=$(dc up -d --no-deps --force-recreate app scheduler 2>&1)
RESTART_RC=$?
set -e
if [ "$RESTART_RC" -ne 0 ]; then
    # Don't fail the applier hard — the restart is best-effort recovery.
    # Surface the failure to journalctl so operators see it.
    logger -t agnes-state-applier "WARNING app+scheduler restart exited $RESTART_RC: $RESTART_LOG"
fi
