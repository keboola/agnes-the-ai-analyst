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
PENDING_JOB=""
if [ -d "$JOBS_DIR" ]; then
    for f in "$JOBS_DIR"/*.json; do
        [ -e "$f" ] || continue
        st=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('status',''))" "$f" 2>/dev/null || echo "")
        if [ "$st" = "pending" ]; then
            PENDING_JOB="$f"
            break
        fi
    done
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
PY
}

write_instance_yaml() {
    # Single-key writer for instance.yaml::database.backend. We don't
    # pull in a YAML parser on the host — the file is small + the
    # contract is one well-known key.
    local backend=$1 url=${2:-}
    local tmp=/data/state/instance.yaml.tmp
    {
        echo "database:"
        echo "  backend: $backend"
        if [ -n "$url" ]; then
            echo "  url: \"$url\""
        fi
    } > "$tmp"
    chown 999:999 "$tmp"
    mv "$tmp" /data/state/instance.yaml
}

# --- Lifecycle: ensure postgres container matches the flag ----------------
case "$TARGET" in
    side-car-enabled)
        mkdir -p /data/postgres
        chown 70:70 /data/postgres
        chmod 700 /data/postgres
        if ! docker ps --format '{{.Names}}' | grep -q '^agnes-postgres-1$'; then
            dc up -d postgres
            # Wait for postgres to accept connections — the migrator
            # we'll launch in a moment opens a TCP connection on
            # postgres:5432 and we'd rather fail fast here than have
            # the migrator timeout on its first ALEMBIC operation.
            for _ in $(seq 1 30); do
                docker exec agnes-postgres-1 pg_isready -U agnes >/dev/null 2>&1 && break
                sleep 2
            done
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

JOB_ID=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["job_id"])' "$PENDING_JOB")
TARGET_URL=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("target_url",""))' "$PENDING_JOB")
TARGET_BACKEND=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("target_backend",""))' "$PENDING_JOB")
SOURCE_BACKEND=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("source_backend",""))' "$PENDING_JOB")

IMAGE="ghcr.io/keboola/agnes-the-ai-analyst:${AGNES_TAG:-stable}"

# Source URL — included in the pending job for every PG→PG transition
# (side_car→cloud and cloud→side_car). The API endpoint reads
# instance.yaml::database.url before flipping the state to
# *_in_progress and persists it on the job; reading it back from the
# job file is more reliable than re-reading instance.yaml at this
# point (which already shows *_in_progress).
SOURCE_URL=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("source_url") or "")' "$PENDING_JOB")
SOURCE_URL_ARGS=()
if [ -n "$SOURCE_URL" ]; then
    SOURCE_URL_ARGS=( --source-url "$SOURCE_URL" )
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

set +e
docker run --rm \
    "${NETWORK_ARGS[@]}" \
    -v /data:/data \
    -e DATA_DIR=/data \
    "$IMAGE" \
    python -m scripts.db_state_migrator \
        --job-id   "$JOB_ID" \
        --to       "$TARGET_BACKEND" \
        --source-backend "$SOURCE_BACKEND" \
        --target-url "$TARGET_URL" \
        "${SOURCE_URL_ARGS[@]}" \
        --duckdb-path /data/state/system.duckdb \
        --jobs-dir   "$JOBS_DIR" \
        --backups-dir /data/state/backups
MIG_RC=$?
set -e

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
    write_instance_yaml "$SOURCE_BACKEND"
fi

# 4. Bring the app back up. After-state app reads instance.yaml and
#    opens the chosen backend on startup.
dc up -d --force-recreate app scheduler >/dev/null 2>&1 || true
