#!/usr/bin/env bash
# Integration test for scripts/ops/agnes-state-applier.sh.
#
# Stubs `docker` and `logger` with a fake on PATH that records every
# invocation to a transcript file. Drives the applier through the
# expected lifecycle (pending job + side-car-enabled flag) and asserts
# the recorded docker calls match what the design contract promises:
#
#   1. compose up -d postgres                    (lifecycle settle)
#   2. exec agnes-postgres-1 pg_isready -U agnes (health wait)
#   3. stop agnes-app-1 agnes-scheduler-1        (release DuckDB lock)
#   4. run --rm ... db_state_migrator ...        (migrator launch)
#   5. compose up -d --force-recreate app scheduler  (restart on new backend)
#
# Run with: bash tests/test_state_applier_host_script.sh
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
script=$repo_root/scripts/ops/agnes-state-applier.sh

# --- Sandbox ---------------------------------------------------------------
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/data/state/db-jobs" "$tmp/opt/agnes"
echo "AGNES_TAG=stable" > "$tmp/opt/agnes/.env"
touch "$tmp/opt/agnes/docker-compose.yml" \
      "$tmp/opt/agnes/docker-compose.prod.yml" \
      "$tmp/opt/agnes/docker-compose.host-mount.yml"

# Pending job — exactly what app/api/db_state.py writes today.
JOB_ID="d4f2c5e3-test-1234-5678-host-applier01"
cat > "$tmp/data/state/db-jobs/$JOB_ID.json" <<JSON
{
  "job_id": "$JOB_ID",
  "schema_version": 1,
  "status": "pending",
  "source_backend": "duckdb",
  "target_backend": "side_car",
  "target_url": "postgresql+psycopg://agnes:agnes@postgres:5432/agnes",
  "progress_pct": 0,
  "current_step": "queued"
}
JSON
echo -n "side-car-enabled" > "$tmp/data/state/db-state-target.flag"

# --- Fake docker -----------------------------------------------------------
transcript=$tmp/transcript.log
fake_bin=$tmp/bin
mkdir -p "$fake_bin"
cat > "$fake_bin/docker" <<'FAKE'
#!/usr/bin/env bash
echo "docker $*" >> "$TRANSCRIPT"
case "$1" in
    network)
        # network ls --format ... → echo a name so applier picks it
        echo agnes_default
        ;;
    ps)
        # `docker ps --format '{{.Names}}'` — first call returns empty
        # (no postgres yet), second call (inside `if ... grep` after
        # `compose up`) returns postgres + the app/scheduler that the
        # migrator should later stop.
        if [ ! -f "$TRANSCRIPT.ps_called" ]; then
            : > "$TRANSCRIPT.ps_called"
        else
            echo agnes-postgres-1
            echo agnes-app-1
            echo agnes-scheduler-1
        fi
        ;;
    exec)
        # pg_isready stub — succeed immediately so the wait loop exits.
        exit 0
        ;;
    run)
        # Migrator invocation. Update the job file to success on the
        # caller's behalf so the applier proceeds down the happy path.
        python3 - "$JOB_FILE" <<'PY'
import json, os, sys
p = sys.argv[1]
data = json.load(open(p))
data["status"] = "success"
data["progress_pct"] = 100
data["current_step"] = "done"
tmp = p + ".tmp"
json.dump(data, open(tmp, "w"))
os.replace(tmp, p)
PY
        ;;
    stop|rm|compose)
        : ;;
esac
FAKE
chmod +x "$fake_bin/docker"

cat > "$fake_bin/logger" <<'FAKE'
#!/usr/bin/env bash
shift  # drop -t
shift  # drop tag
echo "logger: $*" >> "$TRANSCRIPT"
FAKE
chmod +x "$fake_bin/logger"

cat > "$fake_bin/chown" <<'FAKE'
#!/usr/bin/env bash
# chown is a no-op in the sandbox (we can't change ownership without root).
exit 0
FAKE
chmod +x "$fake_bin/chown"

cat > "$fake_bin/chmod" <<'FAKE'
#!/usr/bin/env bash
# chmod is a no-op in the sandbox to avoid permission surprises.
exit 0
FAKE
chmod +x "$fake_bin/chmod"

# `flock` is Linux-only; stub it to a no-op so the test can run on
# macOS dev laptops. Production hosts have it natively (util-linux).
cat > "$fake_bin/flock" <<'FAKE'
#!/usr/bin/env bash
exit 0
FAKE
chmod +x "$fake_bin/flock"

# --- Patch script paths for sandbox ----------------------------------------
sandboxed=$tmp/applier.sh
sed -e "s|FLAG=/data/state/db-state-target.flag|FLAG=$tmp/data/state/db-state-target.flag|" \
    -e "s|JOBS_DIR=/data/state/db-jobs|JOBS_DIR=$tmp/data/state/db-jobs|" \
    -e "s|COMPOSE_DIR=/opt/agnes|COMPOSE_DIR=$tmp/opt/agnes|" \
    -e "s|LOCK_FILE=/data/state/db-state-applier.lock|LOCK_FILE=$tmp/data/state/db-state-applier.lock|" \
    -e "s|/data/postgres|$tmp/data/postgres|g" \
    -e "s|/data/state/certs|$tmp/data/state/certs|g" \
    -e "s|/data/state/instance.yaml|$tmp/data/state/instance.yaml|g" \
    "$script" > "$sandboxed"
chmod +x "$sandboxed"

# --- Run -------------------------------------------------------------------
TRANSCRIPT="$transcript" JOB_FILE="$tmp/data/state/db-jobs/$JOB_ID.json" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed"

# --- Assertions ------------------------------------------------------------
fail() { echo "FAIL: $*"; echo "--- transcript ---"; cat "$transcript"; exit 1; }

grep -q "docker compose -f .* up -d postgres" "$transcript" \
    || fail "expected 'compose up -d postgres' before migrator runs"

grep -q "docker stop agnes-app-1 agnes-scheduler-1" "$transcript" \
    || fail "expected app+scheduler stop before migrator runs"

grep -q "docker run --rm" "$transcript" \
    && grep -q "db_state_migrator" "$transcript" \
    || fail "expected 'docker run --rm ... db_state_migrator ...'"

grep -q "docker compose -f .* up -d --no-deps --force-recreate app scheduler" "$transcript" \
    || fail "expected --no-deps app+scheduler restart after migrator"

# Ordering: stop must come BEFORE run; restart must come AFTER run.
stop_line=$(grep -n "docker stop agnes-app-1" "$transcript" | head -1 | cut -d: -f1)
run_line=$(grep -n "docker run --rm" "$transcript" | head -1 | cut -d: -f1)
restart_line=$(grep -n "docker compose -f .* up -d --no-deps --force-recreate app scheduler" "$transcript" | head -1 | cut -d: -f1)
[ "$stop_line" -lt "$run_line" ] && [ "$run_line" -lt "$restart_line" ] \
    || fail "ordering wrong: stop=$stop_line run=$run_line restart=$restart_line"

# instance.yaml updated to side_car on success.
grep -q "backend: side_car" "$tmp/data/state/instance.yaml" \
    || fail "expected instance.yaml::database.backend = side_car after success"

echo "OK"
