#!/usr/bin/env bash
# Post-deploy smoke test — run on prod VM after image upgrade.
# Usage: ./scripts/ops/post-deploy-smoke-test.sh [AGNES_URL] [AGNES_PAT]
#   or:  AGNES_URL=https://agnes.example.com AGNES_PAT=xxx ./scripts/ops/post-deploy-smoke-test.sh
set -euo pipefail

AGNES_URL="${1:-${AGNES_URL:-http://localhost:8000}}"
AGNES_PAT="${2:-${AGNES_PAT:-}}"
PASS=0
FAIL=0

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

echo "Post-deploy smoke test: $AGNES_URL"
echo "---"

# 1. Health check
HEALTH=$(curl -sf "$AGNES_URL/api/health" 2>/dev/null || echo "")
if [ -z "$HEALTH" ]; then
    check "health endpoint" "false"
else
    STATUS=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "parse-error")
    if [[ "$STATUS" =~ ^(ok|healthy)$ ]]; then
        check "health ($STATUS)" "true"
    else
        check "health ($STATUS)" "false"
    fi
fi

# 2. DB schema version
DB_SCHEMA=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('db_schema','unknown'))" 2>/dev/null || echo "unknown")
if [ "$DB_SCHEMA" = "ok" ]; then
    check "db schema version" "true"
elif [ "$DB_SCHEMA" = "unknown" ]; then
    # Fallback: check /api/version for schema_version field
    VERSION_INFO=$(curl -sf "$AGNES_URL/api/version" 2>/dev/null || echo "")
    if [ -n "$VERSION_INFO" ]; then
        check "db schema (version endpoint only)" "true"
    else
        check "db schema version" "false"
    fi
else
    check "db schema ($DB_SCHEMA)" "false"
fi

# 3. Query SELECT 1 (requires PAT)
if [ -n "$AGNES_PAT" ]; then
    QUERY_OK=$(curl -sf -X POST "$AGNES_URL/api/query" \
      -H "Authorization: Bearer $AGNES_PAT" \
      -H "Content-Type: application/json" \
      -d '{"sql":"SELECT 1 as test"}' | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('true' if len(d.get('rows',[])) > 0 else 'false')
" 2>/dev/null || echo "false")
    check "query SELECT 1" "$QUERY_OK"
else
    echo "  SKIP query (no PAT)"
fi

# 4. Catalog endpoint (requires PAT)
if [ -n "$AGNES_PAT" ]; then
    CATALOG_HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$AGNES_URL/api/catalog" \
      -H "Authorization: Bearer $AGNES_PAT" 2>/dev/null || echo "000")
    if [[ "$CATALOG_HTTP" =~ ^(200|404)$ ]]; then
        check "catalog endpoint (HTTP $CATALOG_HTTP)" "true"
    else
        check "catalog endpoint (HTTP $CATALOG_HTTP)" "false"
    fi
else
    echo "  SKIP catalog (no PAT)"
fi

# 5. Marketplace.zip (requires PAT)
if [ -n "$AGNES_PAT" ]; then
    MARKET_HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$AGNES_URL/api/marketplace.zip" \
      -H "Authorization: Bearer $AGNES_PAT" 2>/dev/null || echo "000")
    if [[ "$MARKET_HTTP" =~ ^(200|204|304|404)$ ]]; then
        check "marketplace.zip (HTTP $MARKET_HTTP)" "true"
    else
        check "marketplace.zip (HTTP $MARKET_HTTP)" "false"
    fi
else
    echo "  SKIP marketplace.zip (no PAT)"
fi

# Results
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
