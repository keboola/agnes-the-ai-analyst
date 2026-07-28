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

# Seed instance.yaml with operator-set non-database keys. The applier's
# write_instance_yaml MUST preserve them after the migration (B6).
cat > "$tmp/data/state/instance.yaml" <<'YAML'
logging:
  level: debug
auth_providers:
  google: enabled
database:
  backend: duckdb
YAML

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

# `timeout(1)` (coreutils) may be missing on macOS dev laptops.
# Provide a passthrough fake — when the script runs in this test it
# forwards to the wrapped command without enforcing the limit.
# Tests that want to assert the watchdog logic (C.2) install a
# different stub locally.
cat > "$fake_bin/timeout" <<'FAKE'
#!/usr/bin/env bash
# Skip the timeout-specific flags / duration; run the rest.
while [[ "$1" == --* ]] || [[ "$1" =~ ^[0-9]+(s|m|h)?$ ]]; do
    shift
done
exec "$@"
FAKE
chmod +x "$fake_bin/timeout"

# --- Patch script paths for sandbox ----------------------------------------
sandboxed=$tmp/applier.sh
sed -e "s|FLAG=/data/state/db-state-target.flag|FLAG=$tmp/data/state/db-state-target.flag|" \
    -e "s|JOBS_DIR=/data/state/db-jobs|JOBS_DIR=$tmp/data/state/db-jobs|" \
    -e "s|COMPOSE_DIR=/opt/agnes|COMPOSE_DIR=$tmp/opt/agnes|" \
    -e "s|LOCK_FILE=/data/state/db-state-applier.lock|LOCK_FILE=$tmp/data/state/db-state-applier.lock|" \
    -e "s|/data/postgres|$tmp/data/postgres|g" \
    -e "s|/data/state/certs|$tmp/data/state/certs|g" \
    -e "s|/data/state/instance.yaml|$tmp/data/state/instance.yaml|g" \
    -e "s|/data/state/agnes-state-applier.tick|$tmp/data/state/agnes-state-applier.tick|g" \
    "$script" > "$sandboxed"
chmod +x "$sandboxed"

# --- B5 regression: stuck-running job seed (before applier run) ------------
# A job that died mid-migration (host reboot, OOM, docker daemon crash) stays
# at status=running forever. The recovery loop must detect it via the alive
# file mtime and flip it to failed so forward progress can resume.
STUCK_ID="stuck-job-deadbeef-1234"
cat > "$tmp/data/state/db-jobs/$STUCK_ID.json" <<JSON
{
  "job_id": "$STUCK_ID",
  "status": "running",
  "source_backend": "duckdb",
  "target_backend": "side_car",
  "current_step": "data_copy"
}
JSON
# Create an alive file with mtime 200s in the past.
# touch -d is GNU; touch -A is BSD macOS. Fall back to python3 if both fail.
if touch -d "200 seconds ago" "$tmp/data/state/db-jobs/$STUCK_ID.alive" 2>/dev/null; then
    :
elif touch -A -0200 "$tmp/data/state/db-jobs/$STUCK_ID.alive" 2>/dev/null; then
    :
else
    python3 -c "
import os, time
p = '$tmp/data/state/db-jobs/$STUCK_ID.alive'
open(p, 'w').close()
old = time.time() - 200
os.utime(p, (old, old))
"
fi

# --- Run -------------------------------------------------------------------
TRANSCRIPT="$transcript" JOB_FILE="$tmp/data/state/db-jobs/$JOB_ID.json" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed"

# --- Assertions ------------------------------------------------------------
fail() { echo "FAIL: $*"; echo "--- transcript ---"; cat "$transcript"; exit 1; }

# E.2 — semantic matchers. Each helper finds the FIRST transcript
# line satisfying its required-keyword set (regardless of flag order
# within that line) and prints the line number. Reordering a flag
# inside one of those invocations no longer breaks the test; only
# DROPPING a required argument does. Combined with the cross-line
# ordering assertions below, we still get a strict end-to-end
# contract without coupling to argv layout.
# Args: file required-keyword [required-keyword …]
line_matching_keywords() {
    local file=$1; shift
    awk -v keywords="$*" '
    BEGIN {
        n = split(keywords, kw, " ")
    }
    {
        ok = 1
        for (i = 1; i <= n; i++) {
            if (index($0, kw[i]) == 0) { ok = 0; break }
        }
        if (ok) { print NR; exit }
    }
    ' "$file"
}
require_line() {
    local descr=$1; shift
    local line
    line=$(line_matching_keywords "$@")
    if [ -z "$line" ]; then
        fail "no transcript line matches all of [$*] — $descr"
    fi
    echo "$line"
}

postgres_up_line=$(require_line "compose up -d postgres" "$transcript" "docker" "compose" "up" "-d" "postgres")
stop_line=$(require_line "app/scheduler stop"           "$transcript" "docker" "stop" "agnes-app-1" "agnes-scheduler-1")
migrator_run_line=$(require_line "migrator run"          "$transcript" "docker" "run" "--rm" "db_state_migrator")
restart_line=$(require_line "no-deps app+scheduler restart" "$transcript" "docker" "compose" "up" "-d" "--no-deps" "--force-recreate" "app" "scheduler")

# Ordering across lines is the load-bearing invariant — flag-order
# inside a single line is not.
[ "$postgres_up_line" -lt "$stop_line" ] \
    || fail "ordering wrong: postgres-up=$postgres_up_line not before stop=$stop_line"
[ "$stop_line" -lt "$migrator_run_line" ] \
    || fail "ordering wrong: stop=$stop_line not before migrator=$migrator_run_line"
[ "$migrator_run_line" -lt "$restart_line" ] \
    || fail "ordering wrong: migrator=$migrator_run_line not before restart=$restart_line"

# instance.yaml updated to side_car on success.
grep -q "backend: side_car" "$tmp/data/state/instance.yaml" \
    || fail "expected instance.yaml::database.backend = side_car after success"

# B6 regression: write_instance_yaml must preserve non-database keys.
grep -q "level: debug" "$tmp/data/state/instance.yaml" \
    || fail "logging.level was destroyed by write_instance_yaml (B6)"
grep -q "google: enabled" "$tmp/data/state/instance.yaml" \
    || fail "auth_providers.google was destroyed by write_instance_yaml (B6)"
# And the database.backend must have been flipped to the target.
grep -q "backend: side_car" "$tmp/data/state/instance.yaml" \
    || fail "backend not updated to side_car (B6)"
echo "OK: instance.yaml preserves non-database keys (B6)"

# B5 regression: stuck-running job must have been flipped to failed.
STUCK_STATUS=$(python3 -c "import json;print(json.load(open('$tmp/data/state/db-jobs/$STUCK_ID.json'))['status'])")
if [ "$STUCK_STATUS" != "failed" ]; then
    fail "stuck-running job not recovered (status=$STUCK_STATUS)"
fi
STUCK_MSG=$(python3 -c "import json;print(json.load(open('$tmp/data/state/db-jobs/$STUCK_ID.json')).get('error',{}).get('message',''))")
case "$STUCK_MSG" in
    *"stuck running"*) ;;
    *) fail "stuck-running recovery message missing 'stuck running' (got: $STUCK_MSG)" ;;
esac
echo "OK: stuck-running recovery (B5)"

# Phase 4 — applier must touch tick file at start of every invocation.
test -e "$tmp/data/state/agnes-state-applier.tick" \
    || { echo "FAIL: applier did not touch agnes-state-applier.tick"; exit 1; }
echo "OK: applier touched tick file (Phase 4)"

# --- B.2 regression: pending-job expiry (H8) ---------------------------------
# A separate, isolated harness run because the previous run already
# consumed its pending job. Seed a NEW pending job with queued_at 2h
# in the past + PENDING_JOB_MAX_AGE_SEC=3600 (1h); applier must mark
# it failed/expired without invoking the migrator.
tmp2=$(mktemp -d)
mkdir -p "$tmp2/data/state/db-jobs" "$tmp2/opt/agnes"
echo "AGNES_TAG=stable" > "$tmp2/opt/agnes/.env"
touch "$tmp2/opt/agnes/docker-compose.yml" \
      "$tmp2/opt/agnes/docker-compose.prod.yml" \
      "$tmp2/opt/agnes/docker-compose.host-mount.yml"
EXPIRED_ID="expired-pending-2h-old"
QUEUED_2H_AGO=$(python3 -c "
from datetime import datetime, timezone, timedelta
print((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
")
cat > "$tmp2/data/state/db-jobs/$EXPIRED_ID.json" <<JSON
{
  "job_id": "$EXPIRED_ID",
  "schema_version": 1,
  "status": "pending",
  "source_backend": "duckdb",
  "target_backend": "side_car",
  "target_url": "postgresql+psycopg://agnes:agnes@postgres:5432/agnes",
  "progress_pct": 0,
  "current_step": "queued",
  "queued_at": "$QUEUED_2H_AGO"
}
JSON
# No flag => applier exits without processing any lifecycle. We still
# want the expiry loop to run, which it does BEFORE the flag check.
# Set a target-flag so the lifecycle settles to duckdb (no postgres up).
echo -n "duckdb" > "$tmp2/data/state/db-state-target.flag"
sandboxed2=$tmp2/applier.sh
sed -e "s|FLAG=/data/state/db-state-target.flag|FLAG=$tmp2/data/state/db-state-target.flag|" \
    -e "s|JOBS_DIR=/data/state/db-jobs|JOBS_DIR=$tmp2/data/state/db-jobs|" \
    -e "s|COMPOSE_DIR=/opt/agnes|COMPOSE_DIR=$tmp2/opt/agnes|" \
    -e "s|LOCK_FILE=/data/state/db-state-applier.lock|LOCK_FILE=$tmp2/data/state/db-state-applier.lock|" \
    -e "s|/data/postgres|$tmp2/data/postgres|g" \
    -e "s|/data/state/certs|$tmp2/data/state/certs|g" \
    -e "s|/data/state/instance.yaml|$tmp2/data/state/instance.yaml|g" \
    -e "s|/data/state/agnes-state-applier.tick|$tmp2/data/state/agnes-state-applier.tick|g" \
    "$script" > "$sandboxed2"
chmod +x "$sandboxed2"
transcript2=$tmp2/transcript.log
PENDING_JOB_MAX_AGE_SEC=3600 \
    TRANSCRIPT="$transcript2" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed2"
EXPIRED_STATUS=$(python3 -c "import json;print(json.load(open('$tmp2/data/state/db-jobs/$EXPIRED_ID.json'))['status'])")
[ "$EXPIRED_STATUS" = "failed" ] \
    || { echo "FAIL (B.2): expired pending status=$EXPIRED_STATUS, want failed"; cat "$transcript2"; exit 1; }
EXPIRED_CLASS=$(python3 -c "import json;print(json.load(open('$tmp2/data/state/db-jobs/$EXPIRED_ID.json')).get('error',{}).get('class',''))")
[ "$EXPIRED_CLASS" = "PendingJobExpired" ] \
    || { echo "FAIL (B.2): expired error.class=$EXPIRED_CLASS, want PendingJobExpired"; exit 1; }
# Critically: the migrator must NOT have been invoked.
if grep -q "docker run --rm" "$transcript2"; then
    echo "FAIL (B.2): applier ran the migrator for an expired pending job"
    cat "$transcript2"
    exit 1
fi
rm -rf "$tmp2"
echo "OK: B.2 pending-job expiry (H8)"

# --- B.3 regression: cloud→side_car failure clears FLAG ----------------------
# Source=cloud, target=side_car (DR rollback). Migrator fails post-copy
# so applier hits the failure branch. The FLAG file says
# 'side-car-enabled', but with the rollback putting instance.yaml back
# to 'cloud', the next applier tick would otherwise re-enable the
# postgres container with no data. The fix: clear FLAG when the
# rollback lands on a non-side_car state.
tmp3=$(mktemp -d)
mkdir -p "$tmp3/data/state/db-jobs" "$tmp3/opt/agnes"
echo "AGNES_TAG=stable" > "$tmp3/opt/agnes/.env"
touch "$tmp3/opt/agnes/docker-compose.yml" \
      "$tmp3/opt/agnes/docker-compose.prod.yml" \
      "$tmp3/opt/agnes/docker-compose.host-mount.yml"
B3_ID="b3-cloud-to-sidecar-fail"
B3_QUEUED=$(python3 -c "
from datetime import datetime, timezone
print(datetime.now(timezone.utc).isoformat())
")
cat > "$tmp3/data/state/db-jobs/$B3_ID.json" <<JSON
{
  "job_id": "$B3_ID",
  "schema_version": 1,
  "status": "pending",
  "source_backend": "cloud",
  "target_backend": "side_car",
  "target_url": "postgresql+psycopg://agnes:agnes@postgres:5432/agnes",
  "source_url": "postgresql+psycopg://agnes:pw@cloud.example.com:5432/agnes",
  "progress_pct": 0,
  "current_step": "queued",
  "queued_at": "$B3_QUEUED"
}
JSON
echo -n "side-car-enabled" > "$tmp3/data/state/db-state-target.flag"
cat > "$tmp3/data/state/instance.yaml" <<'YAML'
database:
  backend: cloud
  url: postgresql+psycopg://agnes:pw@cloud.example.com:5432/agnes
YAML
sandboxed3=$tmp3/applier.sh
sed -e "s|FLAG=/data/state/db-state-target.flag|FLAG=$tmp3/data/state/db-state-target.flag|" \
    -e "s|JOBS_DIR=/data/state/db-jobs|JOBS_DIR=$tmp3/data/state/db-jobs|" \
    -e "s|COMPOSE_DIR=/opt/agnes|COMPOSE_DIR=$tmp3/opt/agnes|" \
    -e "s|LOCK_FILE=/data/state/db-state-applier.lock|LOCK_FILE=$tmp3/data/state/db-state-applier.lock|" \
    -e "s|/data/postgres|$tmp3/data/postgres|g" \
    -e "s|/data/state/certs|$tmp3/data/state/certs|g" \
    -e "s|/data/state/instance.yaml|$tmp3/data/state/instance.yaml|g" \
    -e "s|/data/state/agnes-state-applier.tick|$tmp3/data/state/agnes-state-applier.tick|g" \
    "$script" > "$sandboxed3"
chmod +x "$sandboxed3"
# Override the docker fake so `docker run` (migrator) leaves the job at
# status=pending — applier's post-migrator detection then marks it failed.
fake_bin3=$tmp3/bin
mkdir -p "$fake_bin3"
cat > "$fake_bin3/docker" <<'FAKE'
#!/usr/bin/env bash
echo "docker $*" >> "$TRANSCRIPT"
case "$1" in
    network) echo agnes_default ;;
    ps)
        # Return postgres-1 + app + scheduler so stop commands run.
        echo agnes-postgres-1
        echo agnes-app-1
        echo agnes-scheduler-1
        ;;
    exec) exit 0 ;;
    run)
        # Migrator "ran" but failed without updating the JSON — applier
        # will mark the job failed itself.
        : ;;
    stop|rm|compose) : ;;
esac
FAKE
chmod +x "$fake_bin3/docker"
# Reuse other fakes from the first run.
cp "$fake_bin/logger" "$fake_bin3/logger"
cp "$fake_bin/chown" "$fake_bin3/chown"
cp "$fake_bin/chmod" "$fake_bin3/chmod"
cp "$fake_bin/flock" "$fake_bin3/flock"
# `timeout` passthrough so the C.2-introduced watchdog wrapping
# doesn't break this scenario on macOS dev laptops without coreutils.
cp "$fake_bin/timeout" "$fake_bin3/timeout"
transcript3=$tmp3/transcript.log
TRANSCRIPT="$transcript3" JOB_FILE="$tmp3/data/state/db-jobs/$B3_ID.json" \
    PATH="$fake_bin3:$PATH" \
    bash "$sandboxed3"
B3_STATUS=$(python3 -c "import json;print(json.load(open('$tmp3/data/state/db-jobs/$B3_ID.json'))['status'])")
[ "$B3_STATUS" = "failed" ] \
    || { echo "FAIL (B.3): status=$B3_STATUS, want failed"; cat "$transcript3"; exit 1; }
# The critical assertion: FLAG file is gone so the next tick doesn't
# re-enable postgres lifecycle for a backend that doesn't need it.
if [ -e "$tmp3/data/state/db-state-target.flag" ]; then
    echo "FAIL (B.3): FLAG file still present after cloud→side_car failure rollback"
    ls -la "$tmp3/data/state/"
    cat "$transcript3"
    exit 1
fi
rm -rf "$tmp3"
echo "OK: B.3 cloud→side_car failure clears FLAG (DR rollback)"

# --- C.2 regression: migrator subprocess watchdog (H5, applier side) ---------
# When `timeout(1)` fires (rc=124 or 137), the applier must mark the
# pending job failed with an actionable message and not flip
# instance.yaml to the target backend.
tmp4=$(mktemp -d)
mkdir -p "$tmp4/data/state/db-jobs" "$tmp4/opt/agnes"
echo "AGNES_TAG=stable" > "$tmp4/opt/agnes/.env"
touch "$tmp4/opt/agnes/docker-compose.yml" \
      "$tmp4/opt/agnes/docker-compose.prod.yml" \
      "$tmp4/opt/agnes/docker-compose.host-mount.yml"
C2_ID="c2-migrator-watchdog"
C2_QUEUED=$(python3 -c "
from datetime import datetime, timezone
print(datetime.now(timezone.utc).isoformat())
")
cat > "$tmp4/data/state/db-jobs/$C2_ID.json" <<JSON
{
  "job_id": "$C2_ID",
  "schema_version": 1,
  "status": "pending",
  "source_backend": "duckdb",
  "target_backend": "side_car",
  "target_url": "postgresql+psycopg://agnes:agnes@postgres:5432/agnes",
  "progress_pct": 0,
  "current_step": "queued",
  "queued_at": "$C2_QUEUED"
}
JSON
echo -n "side-car-enabled" > "$tmp4/data/state/db-state-target.flag"
cat > "$tmp4/data/state/instance.yaml" <<'YAML'
database:
  backend: duckdb
YAML
sandboxed4=$tmp4/applier.sh
sed -e "s|FLAG=/data/state/db-state-target.flag|FLAG=$tmp4/data/state/db-state-target.flag|" \
    -e "s|JOBS_DIR=/data/state/db-jobs|JOBS_DIR=$tmp4/data/state/db-jobs|" \
    -e "s|COMPOSE_DIR=/opt/agnes|COMPOSE_DIR=$tmp4/opt/agnes|" \
    -e "s|LOCK_FILE=/data/state/db-state-applier.lock|LOCK_FILE=$tmp4/data/state/db-state-applier.lock|" \
    -e "s|/data/postgres|$tmp4/data/postgres|g" \
    -e "s|/data/state/certs|$tmp4/data/state/certs|g" \
    -e "s|/data/state/instance.yaml|$tmp4/data/state/instance.yaml|g" \
    -e "s|/data/state/agnes-state-applier.tick|$tmp4/data/state/agnes-state-applier.tick|g" \
    "$script" > "$sandboxed4"
chmod +x "$sandboxed4"
# Fake bin with a `timeout` that always returns 124 (watchdog fired)
# and ignores its wrapped command.
fake_bin4=$tmp4/bin
mkdir -p "$fake_bin4"
cp "$fake_bin/docker" "$fake_bin4/docker"
cp "$fake_bin/logger" "$fake_bin4/logger"
cp "$fake_bin/chown" "$fake_bin4/chown"
cp "$fake_bin/chmod" "$fake_bin4/chmod"
cp "$fake_bin/flock" "$fake_bin4/flock"
cat > "$fake_bin4/timeout" <<'FAKE'
#!/usr/bin/env bash
# Watchdog-fires fake: skip the timeout flags / duration, log to
# transcript, exit 124 without running anything.
while [[ "$1" == --* ]] || [[ "$1" =~ ^[0-9]+(s|m|h)?$ ]]; do
    shift
done
echo "timeout-stub: would-have-run $*" >> "$TRANSCRIPT"
exit 124
FAKE
chmod +x "$fake_bin4/timeout"
transcript4=$tmp4/transcript.log
TRANSCRIPT="$transcript4" JOB_FILE="$tmp4/data/state/db-jobs/$C2_ID.json" \
    MIGRATOR_TIMEOUT_SEC=2 \
    PATH="$fake_bin4:$PATH" \
    bash "$sandboxed4"
C2_STATUS=$(python3 -c "import json;print(json.load(open('$tmp4/data/state/db-jobs/$C2_ID.json'))['status'])")
[ "$C2_STATUS" = "failed" ] \
    || { echo "FAIL (C.2): status=$C2_STATUS, want failed (watchdog should mark failed)"; cat "$transcript4"; exit 1; }
C2_MSG=$(python3 -c "import json;print(json.load(open('$tmp4/data/state/db-jobs/$C2_ID.json')).get('error',{}).get('message',''))")
case "$C2_MSG" in
    *exceeded*timeout*) ;;
    *) echo "FAIL (C.2): error.message='$C2_MSG' lacks 'exceeded ... timeout'"; cat "$transcript4"; exit 1 ;;
esac
# instance.yaml must NOT have been flipped to side_car.
grep -q "backend: duckdb" "$tmp4/data/state/instance.yaml" \
    || { echo "FAIL (C.2): instance.yaml flipped despite watchdog firing"; cat "$tmp4/data/state/instance.yaml"; exit 1; }
rm -rf "$tmp4"
echo "OK: C.2 migrator subprocess watchdog (H5 applier side)"

# --- E.5 regression: ERR trap rolls back on unexpected abort -----------------
# Simulate an unexpected abort by making `docker run` itself fail
# with rc=99 (not a watchdog 124/137 — something else entirely). The
# applier's existing post-migrator detection catches that, but if a
# python heredoc raises BEFORE that block, the script aborts due to
# set -e. The ERR trap must idempotently mark the pending job failed
# and (where appropriate) revert instance.yaml.
#
# The cleanest reproducer: stub `read` in the field-parsing block to
# fail. We can't easily monkey-patch shell builtins, so instead we
# pre-corrupt the pending job JSON so the python3 heredoc parses it
# but exits with an unhandled exception. That triggers the heredoc's
# subprocess to exit nonzero — process substitution then carries the
# error through the read block and the ERR trap fires.
tmp5=$(mktemp -d)
mkdir -p "$tmp5/data/state/db-jobs" "$tmp5/opt/agnes"
echo "AGNES_TAG=stable" > "$tmp5/opt/agnes/.env"
touch "$tmp5/opt/agnes/docker-compose.yml" \
      "$tmp5/opt/agnes/docker-compose.prod.yml" \
      "$tmp5/opt/agnes/docker-compose.host-mount.yml"
E5_ID="e5-abort-mid-flight"
E5_QUEUED=$(python3 -c "
from datetime import datetime, timezone
print(datetime.now(timezone.utc).isoformat())
")
# Valid pending JSON — applier processes it through the field parse,
# then we make `docker run` fail in a way that combines with set -e
# to trigger ERR.
cat > "$tmp5/data/state/db-jobs/$E5_ID.json" <<JSON
{
  "job_id": "$E5_ID",
  "schema_version": 1,
  "status": "pending",
  "source_backend": "duckdb",
  "target_backend": "side_car",
  "target_url": "postgresql+psycopg://agnes:agnes@postgres:5432/agnes",
  "progress_pct": 0,
  "current_step": "queued",
  "queued_at": "$E5_QUEUED"
}
JSON
echo -n "side-car-enabled" > "$tmp5/data/state/db-state-target.flag"
cat > "$tmp5/data/state/instance.yaml" <<'YAML'
database:
  backend: duckdb
YAML
sandboxed5=$tmp5/applier.sh
sed -e "s|FLAG=/data/state/db-state-target.flag|FLAG=$tmp5/data/state/db-state-target.flag|" \
    -e "s|JOBS_DIR=/data/state/db-jobs|JOBS_DIR=$tmp5/data/state/db-jobs|" \
    -e "s|COMPOSE_DIR=/opt/agnes|COMPOSE_DIR=$tmp5/opt/agnes|" \
    -e "s|LOCK_FILE=/data/state/db-state-applier.lock|LOCK_FILE=$tmp5/data/state/db-state-applier.lock|" \
    -e "s|/data/postgres|$tmp5/data/postgres|g" \
    -e "s|/data/state/certs|$tmp5/data/state/certs|g" \
    -e "s|/data/state/instance.yaml|$tmp5/data/state/instance.yaml|g" \
    -e "s|/data/state/agnes-state-applier.tick|$tmp5/data/state/agnes-state-applier.tick|g" \
    "$script" > "$sandboxed5"
# Inject an abort right after the field-parse block so we exercise
# the ERR trap with PENDING_JOB and SOURCE_BACKEND populated.
python3 - <<PY
import pathlib, re
p = pathlib.Path("$sandboxed5")
text = p.read_text()
# Insert 'false' right after the IMAGE= line so set -e aborts before
# the docker run (which would otherwise carry its own happy-path mock).
needle = 'IMAGE="ghcr.io/keboola/agnes-the-ai-analyst:'
i = text.find(needle)
assert i != -1, "could not find injection point"
eol = text.index("\n", i) + 1
text = text[:eol] + "false  # E.5 injected abort\n" + text[eol:]
p.write_text(text)
PY
chmod +x "$sandboxed5"
fake_bin5=$tmp5/bin
mkdir -p "$fake_bin5"
cp "$fake_bin/docker" "$fake_bin5/docker"
cp "$fake_bin/logger" "$fake_bin5/logger"
cp "$fake_bin/chown" "$fake_bin5/chown"
cp "$fake_bin/chmod" "$fake_bin5/chmod"
cp "$fake_bin/flock" "$fake_bin5/flock"
cp "$fake_bin/timeout" "$fake_bin5/timeout"
transcript5=$tmp5/transcript.log
# The applier exits nonzero (the trap returns its rc); capture that.
set +e
TRANSCRIPT="$transcript5" JOB_FILE="$tmp5/data/state/db-jobs/$E5_ID.json" \
    PATH="$fake_bin5:$PATH" \
    bash "$sandboxed5"
applier_rc=$?
set -e
[ "$applier_rc" -ne 0 ] \
    || { echo "FAIL (E.5): applier exited 0 despite injected abort"; cat "$transcript5"; exit 1; }
E5_STATUS=$(python3 -c "import json;print(json.load(open('$tmp5/data/state/db-jobs/$E5_ID.json'))['status'])")
[ "$E5_STATUS" = "failed" ] \
    || { echo "FAIL (E.5): ERR trap did not mark pending failed (status=$E5_STATUS)"; cat "$transcript5"; exit 1; }
E5_MSG=$(python3 -c "import json;print(json.load(open('$tmp5/data/state/db-jobs/$E5_ID.json')).get('error',{}).get('message',''))")
case "$E5_MSG" in
    *"applier aborted"*|*"ERR trap"*) ;;
    *) echo "FAIL (E.5): error.message='$E5_MSG' lacks ERR-trap signature"; cat "$transcript5"; exit 1 ;;
esac
# instance.yaml reverted to duckdb (source backend).
grep -q "backend: duckdb" "$tmp5/data/state/instance.yaml" \
    || { echo "FAIL (E.5): instance.yaml not reverted to source"; cat "$tmp5/data/state/instance.yaml"; exit 1; }
# FLAG file removed (source = duckdb doesn't need side-car lifecycle).
if [ -e "$tmp5/data/state/db-state-target.flag" ]; then
    echo "FAIL (E.5): FLAG file not cleared by ERR trap rollback"
    exit 1
fi
rm -rf "$tmp5"
echo "OK: E.5 ERR trap rolls back on unexpected abort"

echo "OK"
