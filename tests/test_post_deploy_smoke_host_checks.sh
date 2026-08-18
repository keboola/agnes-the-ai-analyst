#!/usr/bin/env bash
# Integration test for the deploy-gate layers added to
# scripts/ops/post-deploy-smoke-test.sh: the CLI-wheel probe, the
# new-instance doctor call (admin bearer resolution + verdict mapping), and
# the host-side consistency checks (COMPOSE_FILE ↔ instance.yaml backend,
# TLS predicate agreement).
#
# Stubs `curl` with a fake on PATH that records every invocation to a
# transcript and answers from FAKE_* env vars, then drives the script
# through seven scenarios:
#
#   A. Laptop mode — no $AGNES_OPT_DIR/.env: host checks and the doctor are
#      SKIPped (no token), the public checks pass, exit 0.
#   B. Sidecar-Postgres drift — instance.yaml says backend: side_car but
#      COMPOSE_FILE lacks the postgres overlay: FAIL + exit 1; the doctor
#      call must authenticate with SCHEDULER_API_TOKEN read from .env.
#   C. Healthy sidecar + corp-PKI TLS — postgres overlay present, certs
#      complete, tls overlay in COMPOSE_FILE: everything passes, exit 0.
#   D. Empty certs directory — $STATE_DIR/certs exists without both pems:
#      the state-applier-would-kill-the-app FAIL fires, exit 1.
#   E. Doctor verdict mapping — ok/error/warning/info rows map to
#      PASS/FAIL/WARN/INFO lines; any error row makes the run exit 1.
#   F. Missing CLI wheel — /cli/download answers 404: FAIL + exit 1.
#   G. Target-flag disagreement — backend duckdb but db-state-target.flag
#      says side-car-enabled: WARN (not FAIL), exit 0.
#
# Run with: bash tests/test_post_deploy_smoke_host_checks.sh
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
script=$repo_root/scripts/ops/post-deploy-smoke-test.sh

fail() {
    echo "FAIL: $*"
    echo "--- output ---"
    echo "${out:-}"
    echo "--- transcript ---"
    cat "$transcript" 2>/dev/null || true
    exit 1
}

build_fake_bin() {
    local dir=$1
    mkdir -p "$dir"
    cat > "$dir/curl" <<'FAKE'
#!/usr/bin/env bash
echo "curl $*" >> "$TRANSCRIPT"
url=""
code_mode=0
for arg in "$@"; do
    case "$arg" in
        http*) url="$arg" ;;
        %\{http_code\}|"%{http_code}") code_mode=1 ;;
    esac
done
case "$url" in
    */api/health)
        echo '{"status":"healthy","db_schema":"ok"}' ;;
    */cli/download)
        echo "${FAKE_WHEEL_HTTP:-200}" ;;
    */api/admin/doctor/new-instance)
        if [ -n "${FAKE_DOCTOR_HTTP_FAIL:-}" ]; then exit 22; fi
        doctor_json="${FAKE_DOCTOR_JSON:-}"
        if [ -z "$doctor_json" ]; then doctor_json='{"status":"ok","checks":[]}'; fi
        echo "$doctor_json" ;;
    */api/version)
        echo '{"version":"0.0.0-test"}' ;;
    *)
        if [ "$code_mode" = 1 ]; then echo "200"; fi ;;
esac
FAKE
    chmod +x "$dir/curl"
}

run_script() {
    set +e
    out=$(AGNES_OPT_DIR="$OPT_DIR" AGNES_URL=http://smoke.test \
          TRANSCRIPT="$transcript" PATH="$fakebin:$PATH" \
          FAKE_WHEEL_HTTP="${FAKE_WHEEL_HTTP:-200}" \
          FAKE_DOCTOR_JSON="${FAKE_DOCTOR_JSON:-}" \
          bash "$script" 2>&1)
    rc=$?
    set -e
}

sandbox=$(mktemp -d)
trap 'rm -rf "$sandbox"' EXIT
fakebin="$sandbox/bin"
build_fake_bin "$fakebin"

new_scenario() {
    OPT_DIR="$sandbox/$1/opt"
    STATE_DIR="$sandbox/$1/state"
    transcript="$sandbox/$1/transcript"
    mkdir -p "$OPT_DIR" "$STATE_DIR"
    : > "$transcript"
    unset FAKE_WHEEL_HTTP FAKE_DOCTOR_JSON || true
}

DOCTOR_ALL_OK='{"status":"ok","checks":[{"name":"login-door","status":"ok","audience":"operator","detail":"usable login door(s): password"}]}'

# --- A. Laptop mode: no .env ------------------------------------------------
new_scenario A
run_script
[ "$rc" -eq 0 ] || fail "A: expected exit 0, got $rc"
echo "$out" | grep -q "SKIP host checks" || fail "A: missing host-check skip"
echo "$out" | grep -q "SKIP doctor" || fail "A: missing doctor skip"
echo "$out" | grep -q "PASS cli wheel" || fail "A: missing wheel pass"
echo "scenario A ok"

# --- B. side_car in instance.yaml, no postgres overlay in .env --------------
new_scenario B
cat > "$OPT_DIR/.env" <<EOF
STATE_DIR=$STATE_DIR
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.host-mount.yml
SCHEDULER_API_TOKEN="sched-secret-token"
EOF
cat > "$STATE_DIR/instance.yaml" <<EOF
database:
  backend: side_car
EOF
FAKE_DOCTOR_JSON="$DOCTOR_ALL_OK"
run_script
[ "$rc" -eq 1 ] || fail "B: expected exit 1, got $rc"
echo "$out" | grep -q "FAIL compose:.*side_car.*docker-compose.postgres.yml" || fail "B: missing compose FAIL"
grep -q "Bearer sched-secret-token" "$transcript" || fail "B: doctor not authenticated with SCHEDULER_API_TOKEN from .env"
echo "$out" | grep -q "PASS doctor:login-door" || fail "B: doctor verdict line missing"
echo "scenario B ok"

# --- C. Healthy sidecar + corp-PKI TLS ---------------------------------------
new_scenario C
cat > "$OPT_DIR/.env" <<EOF
STATE_DIR=$STATE_DIR
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.postgres.yml:docker-compose.host-mount.yml:docker-compose.postgres-host-mount.yml:docker-compose.tls.yml
SCHEDULER_API_TOKEN=sched-secret-token
TLS_MODE=none
EOF
cat > "$STATE_DIR/instance.yaml" <<EOF
database:
  backend: side_car
EOF
echo "side-car-enabled" > "$STATE_DIR/db-state-target.flag"
mkdir -p "$STATE_DIR/certs"
echo "cert" > "$STATE_DIR/certs/fullchain.pem"
echo "key" > "$STATE_DIR/certs/privkey.pem"
FAKE_DOCTOR_JSON="$DOCTOR_ALL_OK"
run_script
[ "$rc" -eq 0 ] || fail "C: expected exit 0, got $rc"
echo "$out" | grep -q "PASS compose: backend=side_car" || fail "C: missing compose PASS"
echo "$out" | grep -q "PASS tls: predicates agree" || fail "C: missing tls PASS"
echo "scenario C ok"

# --- D. Empty certs directory -------------------------------------------------
new_scenario D
cat > "$OPT_DIR/.env" <<EOF
STATE_DIR=$STATE_DIR
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.host-mount.yml
SCHEDULER_API_TOKEN=sched-secret-token
EOF
mkdir -p "$STATE_DIR/certs"
FAKE_DOCTOR_JSON="$DOCTOR_ALL_OK"
run_script
[ "$rc" -eq 1 ] || fail "D: expected exit 1, got $rc"
echo "$out" | grep -q "FAIL tls:.*certs exists but fullchain.pem/privkey.pem" || fail "D: missing empty-certs FAIL"
echo "scenario D ok"

# --- E. Doctor verdict mapping ------------------------------------------------
new_scenario E
cat > "$OPT_DIR/.env" <<EOF
STATE_DIR=$STATE_DIR
SCHEDULER_API_TOKEN=sched-secret-token
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.host-mount.yml
EOF
FAKE_DOCTOR_JSON='{"status":"error","checks":[{"name":"login-door","status":"error","audience":"operator","detail":"NO usable login door"},{"name":"email-delivery","status":"warning","audience":"operator","detail":"default sender"},{"name":"chat-grant","status":"info","audience":"operator","detail":"chat disabled"},{"name":"branding","status":"ok","audience":"operator","detail":"title ok"}]}'
run_script
[ "$rc" -eq 1 ] || fail "E: expected exit 1, got $rc"
echo "$out" | grep -q "FAIL doctor:login-door — NO usable login door" || fail "E: error row not mapped to FAIL"
echo "$out" | grep -q "WARN doctor:email-delivery" || fail "E: warning row not mapped to WARN"
echo "$out" | grep -q "INFO doctor:chat-grant" || fail "E: info row not mapped to INFO"
echo "$out" | grep -q "PASS doctor:branding" || fail "E: ok row not mapped to PASS"
echo "scenario E ok"

# --- F. Missing CLI wheel -----------------------------------------------------
new_scenario F
FAKE_WHEEL_HTTP=404
run_script
[ "$rc" -eq 1 ] || fail "F: expected exit 1, got $rc"
echo "$out" | grep -q "FAIL cli wheel" || fail "F: missing wheel FAIL"
echo "scenario F ok"

# --- G. Target-flag disagreement is a WARN, not a FAIL --------------------------
new_scenario G
cat > "$OPT_DIR/.env" <<EOF
STATE_DIR=$STATE_DIR
SCHEDULER_API_TOKEN=sched-secret-token
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.host-mount.yml
EOF
cat > "$STATE_DIR/instance.yaml" <<EOF
database:
  backend: duckdb
EOF
echo "side-car-enabled" > "$STATE_DIR/db-state-target.flag"
FAKE_DOCTOR_JSON="$DOCTOR_ALL_OK"
run_script
[ "$rc" -eq 0 ] || fail "G: expected exit 0, got $rc"
echo "$out" | grep -q "WARN compose: db-state-target.flag says side-car-enabled" || fail "G: missing flag WARN"
echo "scenario G ok"

echo "ALL SCENARIOS PASSED"
