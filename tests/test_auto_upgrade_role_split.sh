#!/usr/bin/env bash
# Integration test for the role-split (m-tier) sequential /readyz-gated
# rolling recreate added to scripts/ops/agnes-auto-upgrade.sh, plus the
# data-refresh-job sync-defer probe.
#
# Stubs `docker`, `curl`, `logger`, and `flock` with fakes on PATH that
# record every invocation to a transcript file, then drives the script
# through six scenarios:
#
#   A. Single-container topology (no worker/gateway/apiN services in the
#      resolved compose config) — the ORIGINAL one-shot `docker compose
#      up -d` recreate must run byte-for-byte unchanged; no `--no-deps`
#      anywhere.
#   B. Role-split topology, healthy — worker+gateway recreate together
#      first, then api1 (ready immediately) then api2 (503-then-200,
#      polled 3x) recreate ONE AT A TIME, each fully readyz-gated before
#      the next is even touched. Overall exit 0, no alert.
#   C. Role-split topology, 3 replicas, api2 persistently unready — api1
#      recreates+readies, api2 recreates but never reports ready within
#      the bounded timeout: the rollout ABORTS (non-zero exit + webhook
#      alert) and api3 is NEVER recreated (stays on the previous image).
#   D. Sync-defer (existing behavior preserved): /api/sync/status reports
#      locked=true — the script defers the recreate entirely.
#   E. Sync-defer (new behavior): /api/sync/status reports locked=false
#      but GET /api/jobs?kind=data-refresh&status=running returns a
#      running job (worker-side sync under role-split) — the script must
#      still defer, and must authenticate the jobs call with
#      `Authorization: Bearer $SCHEDULER_API_TOKEN`.
#   F. Fail-open: SCHEDULER_API_TOKEN is unset — the jobs probe must never
#      even be attempted, and (with no other busy signal) the recreate
#      proceeds normally.
#
# Run with: bash tests/test_auto_upgrade_role_split.sh
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
script=$repo_root/scripts/ops/agnes-auto-upgrade.sh

fail() {
    echo "FAIL: $*"
    echo "--- transcript ---"
    cat "$transcript" 2>/dev/null || true
    exit 1
}

line_num() {
    # First transcript line number containing the fixed-string pattern, or
    # empty if absent.
    grep -n -F -- "$1" "$transcript" 2>/dev/null | head -1 | cut -d: -f1
}

# --- Shared fake-bin builder --------------------------------------------
# Env read by the fakes (set per-scenario before invoking the script):
#   FAKE_TAG_ID / FAKE_RUNNING_IMAGE_ID   image-drift inputs (differ by default)
#   FAKE_TOPOLOGY=role_split|single       `docker compose config --services` shape
#   FAKE_API_REPLICA_LIST                 newline-separated apiN service names
#   FAKE_SYNC_LOCKED=1                    /api/sync/status -> locked:true
#   FAKE_DATA_REFRESH_RUNNING=1           /api/jobs -> one running data-refresh job
#   FAKE_READYZ_FAIL_COUNT_<svc>=N        that replica's /readyz fails N times, then ready
#   FAKE_READYZ_ALWAYS_FAIL_<svc>=1       that replica's /readyz never reports ready
build_fake_bin() {
    local dir=$1
    mkdir -p "$dir"

    cat > "$dir/docker" <<'FAKE'
#!/usr/bin/env bash
echo "docker $*" >> "$TRANSCRIPT"

if [ "${1:-}" = "compose" ]; then
    shift
    while [ "${1:-}" = "--profile" ]; do
        shift 2
    done
    sub="${1:-}"
    shift || true
    case "$sub" in
        pull)
            exit 0
            ;;
        config)
            if [ "${FAKE_TOPOLOGY:-single}" = "role_split" ]; then
                printf 'worker\ngateway\nredis\ncaddy-mtier\n'
                printf '%s\n' "$FAKE_API_REPLICA_LIST"
            else
                printf 'app\nscheduler\n'
            fi
            exit 0
            ;;
        ps)
            echo "${FAKE_RUNNING_CID:-runningcid123}"
            exit 0
            ;;
        exec)
            if [ "${1:-}" = "-T" ]; then shift; fi
            svc="${1:-}"
            counter_file="$READYZ_STATE_DIR/readyz_calls_$svc"
            count=0
            [ -f "$counter_file" ] && count=$(cat "$counter_file")
            count=$((count + 1))
            echo "$count" > "$counter_file"

            fail_count_var="FAKE_READYZ_FAIL_COUNT_$svc"
            always_fail_var="FAKE_READYZ_ALWAYS_FAIL_$svc"
            fail_count=${!fail_count_var:-0}
            always_fail=${!always_fail_var:-0}

            if [ "$always_fail" = "1" ]; then
                exit 22
            fi
            if [ "$count" -le "$fail_count" ]; then
                exit 22
            fi
            printf '{"status": "ready"}\n'
            exit 0
            ;;
        up)
            exit "${FAKE_COMPOSE_UP_RC:-0}"
            ;;
        *)
            exit 0
            ;;
    esac
else
    case "${1:-}" in
        images)
            echo "${FAKE_TAG_ID:-sha256:newimage000}"
            exit 0
            ;;
        inspect)
            echo "${FAKE_RUNNING_IMAGE_ID:-sha256:oldimage000}"
            exit 0
            ;;
        image)
            if [ "${2:-}" = "inspect" ]; then
                echo ""
            fi
            exit 0
            ;;
        *)
            exit 0
            ;;
    esac
fi
FAKE
    chmod +x "$dir/docker"

    cat > "$dir/curl" <<'FAKE'
#!/usr/bin/env bash
echo "curl $*" >> "$TRANSCRIPT"

url=""
for a in "$@"; do
    case "$a" in
        http*) url="$a" ;;
    esac
done

if [ -n "${WEBHOOK_URL:-}" ] && [ "$url" = "$WEBHOOK_URL" ]; then
    echo "curl-called" >> "$CURL_CALLED"
    exit 0
fi

case "$url" in
    */api/sync/status)
        if [ "${FAKE_SYNC_LOCKED:-0}" = "1" ]; then
            printf '{"locked": true}\n'
        else
            printf '{"locked": false}\n'
        fi
        exit 0
        ;;
    */api/jobs*)
        if [ "${FAKE_DATA_REFRESH_RUNNING:-0}" = "1" ]; then
            printf '{"jobs": [{"id": "job-1", "kind": "data-refresh", "status": "running"}]}\n'
        else
            printf '{"jobs": []}\n'
        fi
        exit 0
        ;;
    *)
        # RAW_BASE config-file / self-update fetches: simulate a network
        # failure — the script's existing WARN-and-keep-existing-file
        # fallback handles this gracefully (verified by test_ops_env_extraction.py
        # and pre-existing behavior; not re-asserted here).
        exit 7
        ;;
esac
FAKE
    chmod +x "$dir/curl"

    cat > "$dir/logger" <<'FAKE'
#!/usr/bin/env bash
shift  # -t
shift  # tag
echo "logger: $*" >> "$TRANSCRIPT"
FAKE
    chmod +x "$dir/logger"

    # flock is Linux-only (util-linux); stub it to a no-op so this runs on
    # macOS dev laptops too (same rationale as tests/test_state_applier_host_script.sh).
    cat > "$dir/flock" <<'FAKE'
#!/usr/bin/env bash
exit 0
FAKE
    chmod +x "$dir/flock"
}

# --- Sandbox builder ------------------------------------------------------
# Args: tmp_dir
# Patches the absolute host paths the script hardcodes (`/opt/agnes`, the
# flock lockfile, the shared webhook-config file) onto sandbox-local paths,
# same sed-a-copy technique as tests/test_state_applier_host_script.sh /
# tests/test_db_backup_pg_canary.sh.
make_sandboxed_script() {
    local tmp=$1
    mkdir -p "$tmp/opt/agnes"

    local sandboxed=$tmp/agnes-auto-upgrade.sh
    sed \
        -e "s|/opt/agnes|$tmp/opt/agnes|g" \
        -e "s|/var/lock/agnes-auto-upgrade.lock|$tmp/agnes-auto-upgrade.lock|g" \
        -e "s|/etc/agnes-watchdog.env|$tmp/nonexistent-agnes-watchdog.env|g" \
        "$script" > "$sandboxed"
    chmod +x "$sandboxed"
    echo "$sandboxed"
}

write_env() {
    # Args: opt_agnes_dir, scheduler_token ("" to omit the key entirely)
    local dir=$1 token=$2
    {
        echo "AGNES_TAG=test-tag"
        echo "COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.host-mount.yml"
        if [ -n "$token" ]; then
            echo "SCHEDULER_API_TOKEN=$token"
        fi
    } > "$dir/.env"
}

run_scenario() {
    local name=$1
    tmp=$(mktemp -d)
    transcript=$tmp/transcript.log
    curl_called_file=$tmp/curl_called
    readyz_state_dir=$tmp/readyz_state
    mkdir -p "$readyz_state_dir"
    : > "$transcript"
    : > "$curl_called_file"

    sandboxed=$(make_sandboxed_script "$tmp")
    fake_bin=$tmp/bin
    build_fake_bin "$fake_bin"
    write_env "$tmp/opt/agnes" "test-scheduler-token"

    echo "--- scenario $name ---"
}

# =====================================================================
# Scenario A: single-container topology — one-shot recreate unchanged.
# =====================================================================
run_scenario A
rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "A: script must exit 0 on a healthy single-container recreate (got $rc)"

grep -qF "docker compose ps -q app" "$transcript" \
    || fail "A: drift reference must still be the 'app' service for single-container"
grep -qF "docker compose up -d" "$transcript" \
    || fail "A: expected the one-shot recreate"
grep -qF -- "--no-deps" "$transcript" \
    && fail "A: single-container topology must never use the role-split --no-deps path"
grep -q "curl-called" "$curl_called_file" \
    && fail "A: no alert expected on a healthy run"
echo "OK: A — single-container topology keeps the exact one-shot recreate"
rm -rf "$tmp"

# =====================================================================
# Scenario B: role-split, healthy — worker+gateway first, then api1, api2
# ONE AT A TIME, each readyz-gated. api2 answers 503 twice then 200 (waits
# then proceeds).
# =====================================================================
run_scenario B
rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=role_split \
    FAKE_API_REPLICA_LIST=$'api1\napi2' \
    FAKE_READYZ_FAIL_COUNT_api2=2 \
    AGNES_AUTO_UPGRADE_READYZ_INTERVAL=1 \
    AGNES_AUTO_UPGRADE_READYZ_TIMEOUT=10 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "B: script must exit 0 once every replica reports ready (got $rc)"

grep -qF "docker compose ps -q worker" "$transcript" \
    || fail "B: drift reference must be 'worker' under role-split"
l_wg=$(line_num "docker compose up -d --no-deps worker gateway")
l_api1_up=$(line_num "docker compose up -d --no-deps api1")
l_api2_up=$(line_num "docker compose up -d --no-deps api2")
[ -n "$l_wg" ] || fail "B: worker+gateway recreate line missing"
[ -n "$l_api1_up" ] || fail "B: api1 recreate line missing"
[ -n "$l_api2_up" ] || fail "B: api2 recreate line missing"
[ "$l_wg" -lt "$l_api1_up" ] || fail "B: worker+gateway must recreate BEFORE api1"
[ "$l_api1_up" -lt "$l_api2_up" ] || fail "B: api1 must finish (incl. its readyz wait) BEFORE api2 is even touched"
api2_exec_calls=$(grep -cF "docker compose exec -T api2 curl" "$transcript" || true)
[ "$api2_exec_calls" -ge 3 ] || fail "B: api2 should have been polled at least 3x (503, 503, 200) — saw $api2_exec_calls"
grep -q "curl-called" "$curl_called_file" \
    && fail "B: no alert expected when the whole rollout succeeds"
echo "OK: B — role-split rolling recreate: worker/gateway then api1/api2 sequentially, waits then proceeds"
rm -rf "$tmp"

# =====================================================================
# Scenario C: role-split, 3 replicas — api2 never reports ready. The
# rollout must ABORT (non-zero exit + alert) WITHOUT ever touching api3.
# =====================================================================
run_scenario C
rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=role_split \
    FAKE_API_REPLICA_LIST=$'api1\napi2\napi3' \
    FAKE_READYZ_ALWAYS_FAIL_api2=1 \
    AGNES_AUTO_UPGRADE_READYZ_INTERVAL=1 \
    AGNES_AUTO_UPGRADE_READYZ_TIMEOUT=2 \
    WEBHOOK_URL="https://example.invalid/webhook" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" && fail "C: script must exit non-zero when a replica never becomes ready"

grep -qF "docker compose up -d --no-deps api1" "$transcript" \
    || fail "C: api1 must have been recreated"
grep -qF "docker compose up -d --no-deps api2" "$transcript" \
    || fail "C: api2 must have been recreated (and then found unready)"
grep -qF "docker compose up -d --no-deps api3" "$transcript" \
    && fail "C: api3 must NEVER be recreated once api2 aborts the rollout — it must keep serving the previous image"
grep -q "curl-called" "$curl_called_file" \
    || fail "C: a persistent readyz failure must fire the webhook alert"
grep -qF "https://example.invalid/webhook" "$transcript" \
    || fail "C: the alert POST must target WEBHOOK_URL"
grep -q "ABORTED role-split rolling recreate" "$transcript" \
    || fail "C: the abort must be logged"
echo "OK: C — persistent readyz failure aborts the rollout, alerts, and leaves remaining replicas untouched"
rm -rf "$tmp"

# =====================================================================
# Scenario D: sync-defer (existing behavior) — /api/sync/status locked.
# =====================================================================
run_scenario D
rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    FAKE_SYNC_LOCKED=1 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "D: a deferred tick must still exit 0 (got $rc)"

grep -qF "docker compose up -d" "$transcript" \
    && fail "D: no recreate at all should happen while sync/status reports locked"
grep -q "deferred recreate: sync/refresh in flight (sync/status locked)" "$transcript" \
    || fail "D: the defer reason must name 'sync/status locked'"
echo "OK: D — /api/sync/status locked still defers the recreate entirely"
rm -rf "$tmp"

# =====================================================================
# Scenario E: sync-defer (new behavior) — a running data-refresh job,
# authenticated with SCHEDULER_API_TOKEN.
# =====================================================================
run_scenario E
rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    FAKE_SYNC_LOCKED=0 \
    FAKE_DATA_REFRESH_RUNNING=1 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "E: a deferred tick must still exit 0 (got $rc)"

grep -qF "docker compose up -d" "$transcript" \
    && fail "E: no recreate should happen while a data-refresh job is running"
grep -qF "/api/jobs?kind=data-refresh&status=running" "$transcript" \
    || fail "E: the jobs endpoint must be queried with kind=data-refresh&status=running"
grep -qF "Authorization: Bearer test-scheduler-token" "$transcript" \
    || fail "E: the jobs query must authenticate with the SCHEDULER_API_TOKEN bearer"
grep -q "deferred recreate: sync/refresh in flight (data-refresh job running)" "$transcript" \
    || fail "E: the defer reason must name 'data-refresh job running'"
echo "OK: E — a running data-refresh job defers the recreate, authenticated via SCHEDULER_API_TOKEN"
rm -rf "$tmp"

# =====================================================================
# Scenario F: fail-open — no SCHEDULER_API_TOKEN configured. The jobs
# probe must never even be attempted, and (with no other busy signal)
# the recreate proceeds normally.
# =====================================================================
tmp=$(mktemp -d)
transcript=$tmp/transcript.log
curl_called_file=$tmp/curl_called
readyz_state_dir=$tmp/readyz_state
mkdir -p "$readyz_state_dir"
: > "$transcript"
: > "$curl_called_file"
sandboxed=$(make_sandboxed_script "$tmp")
fake_bin=$tmp/bin
build_fake_bin "$fake_bin"
write_env "$tmp/opt/agnes" ""   # no SCHEDULER_API_TOKEN key at all
echo "--- scenario F ---"

rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    FAKE_SYNC_LOCKED=0 \
    FAKE_DATA_REFRESH_RUNNING=1 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "F: recreate should proceed when there is no other busy signal (got $rc)"

grep -qF "/api/jobs" "$transcript" \
    && fail "F: the jobs probe must never be attempted without a SCHEDULER_API_TOKEN"
grep -qF "docker compose up -d" "$transcript" \
    || fail "F: the recreate must proceed when the token is absent and no other signal is busy"
echo "OK: F — missing SCHEDULER_API_TOKEN skips the jobs probe (fails open) and the recreate proceeds"
rm -rf "$tmp"

echo "OK"
