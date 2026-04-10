#!/usr/bin/env bash
# Agnes smoke test — verifies a running instance is functional.
# Usage: ./scripts/smoke-test.sh [host:port]
# Default: http://localhost:8000
set -euo pipefail

HOST="${1:-http://localhost:8000}"
PASS=0
FAIL=0
TOKEN=""

check() {
    local name="$1" ok="$2"
    if [ "$ok" = "true" ]; then
        echo "  PASS $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL $name"
        FAIL=$((FAIL + 1))
    fi
}

echo "Smoke test: $HOST"
echo "---"

# 1. Health check
HEALTH=$(curl -sf "$HOST/api/health" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "unreachable")
if [ "$HEALTH" = "unhealthy" ] || [ "$HEALTH" = "unreachable" ]; then
    echo "  FATAL: health=$HEALTH"
    exit 1
fi
check "health ($HEALTH)" "true"

# 2. Health has version fields
HAS_VERSION=$(curl -sf "$HOST/api/health" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('true' if 'version' in d and 'channel' in d and 'schema_version' in d else 'false')
" 2>/dev/null || echo "false")
check "health version fields" "$HAS_VERSION"

# 3. Bootstrap (only works on fresh DB; 403 means users exist)
BOOT_HTTP=$(curl -s -o /tmp/smoke_boot.json -w "%{http_code}" -X POST "$HOST/auth/bootstrap" \
  -H "Content-Type: application/json" \
  -d '{"email":"smoke@test.local","name":"Smoke Test","password":"SmokeTest123!"}' 2>/dev/null || echo "000")

if [ "$BOOT_HTTP" = "200" ]; then
    TOKEN=$(python3 -c "import json; print(json.load(open('/tmp/smoke_boot.json'))['access_token'])" 2>/dev/null || echo "")
    check "bootstrap (new admin)" "true"
elif [ "$BOOT_HTTP" = "403" ]; then
    TOKEN="${SMOKE_TOKEN:-}"
    echo "  SKIP bootstrap (users exist)"
else
    check "bootstrap (HTTP $BOOT_HTTP)" "false"
fi

# 4. Query SELECT 1 (requires auth)
if [ -n "$TOKEN" ]; then
    QUERY_OK=$(curl -sf -X POST "$HOST/api/query" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"sql":"SELECT 1 as test"}' | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('true' if len(d.get('rows',[])) > 0 else 'false')
" 2>/dev/null || echo "false")
    check "query SELECT 1" "$QUERY_OK"
else
    echo "  SKIP query (no token)"
fi

# 5. Sync trigger
if [ -n "$TOKEN" ]; then
    SYNC_HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$HOST/api/sync/trigger" \
      -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "000")
    if [[ "$SYNC_HTTP" =~ ^(200|202)$ ]]; then
        check "sync trigger" "true"
    else
        check "sync trigger (HTTP $SYNC_HTTP)" "false"
    fi
else
    echo "  SKIP sync (no token)"
fi

# 6. Post-sync health (wait briefly)
sleep 5
HEALTH2=$(curl -sf "$HOST/api/health" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "unreachable")
if [ "$HEALTH2" = "unhealthy" ] || [ "$HEALTH2" = "unreachable" ]; then
    check "post-sync health ($HEALTH2)" "false"
else
    check "post-sync health ($HEALTH2)" "true"
fi

# Results
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
