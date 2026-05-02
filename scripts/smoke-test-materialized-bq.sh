#!/usr/bin/env bash
# Smoke test — query_mode='materialized' for BigQuery.
#
# Runs the full happy-path + 3 adversarial scenarios against a live Agnes
# instance that has BigQuery configured. Cheap (uses bigquery-public-data
# samples; ~36 rows, < 1 KB scan) — safe to run against staging.
#
# Usage:
#   ./scripts/smoke-test-materialized-bq.sh [host:port]
#
# Required environment:
#   AGNES_PAT       — admin PAT for the target instance
#   BQ_TEST_BIG     — (optional) name of a BQ table > 10 GiB to test the
#                     cost guardrail. Defaults to a public dataset that
#                     scans ~50 GB on full SELECT.
#
# Defaults: AGNES_HOST=http://localhost:8000.
#
# Cleans up the test rows on exit (trap), even on SIGINT.

set -euo pipefail

HOST="${1:-${AGNES_HOST:-http://localhost:8000}}"
PAT="${AGNES_PAT:?AGNES_PAT must be set (admin token)}"
BIG_TABLE="${BQ_TEST_BIG:-bigquery-public-data.github_repos.commits}"

PASS=0
FAIL=0

# Test rows we'll create — captured for cleanup.
CREATED_IDS=()

cleanup() {
    echo
    echo "--- Cleanup ---"
    for tid in "${CREATED_IDS[@]}"; do
        curl -sS -X DELETE "$HOST/api/admin/registry/$tid" \
            -H "Authorization: Bearer $PAT" -o /dev/null -w "  DELETE %{http_code} $tid\n" || true
    done
}
trap cleanup EXIT INT TERM

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

http() {
    # POST/PUT/DELETE helper that returns the HTTP status + body separately.
    local method="$1" path="$2" body="${3:-}"
    if [ -n "$body" ]; then
        curl -sS -o /tmp/smoke-mat-body -w "%{http_code}" \
            -X "$method" "$HOST$path" \
            -H "Authorization: Bearer $PAT" \
            -H "Content-Type: application/json" \
            -d "$body"
    else
        curl -sS -o /tmp/smoke-mat-body -w "%{http_code}" \
            -X "$method" "$HOST$path" \
            -H "Authorization: Bearer $PAT"
    fi
}

echo "Materialized BQ smoke: $HOST"
echo "Big table for cost-guardrail test: $BIG_TABLE"
echo "---"

# ---------------------------------------------------------------------------
# Scenario A — Happy path: register tiny materialized table, trigger,
#              verify parquet on disk + manifest carries hash.
# ---------------------------------------------------------------------------
echo
echo "[A] Happy path (Shakespeare sample, ~36 rows)"
SQL_A='SELECT corpus, COUNT(*) AS c FROM `bigquery-public-data.samples.shakespeare` GROUP BY 1 ORDER BY 1'
TID_A="smoke_mat_shakespeare_$(date +%s)"
CREATED_IDS+=("$TID_A")

STATUS=$(http POST /api/admin/register-table "{
    \"name\": \"$TID_A\",
    \"source_type\": \"bigquery\",
    \"query_mode\": \"materialized\",
    \"source_query\": $(printf '%s' "$SQL_A" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))"),
    \"sync_schedule\": \"every 1m\"
}")
[ "$STATUS" = "201" ] && check "register 201" true || { check "register 201 (got $STATUS)" false; cat /tmp/smoke-mat-body; }

echo "    triggering sync..."
http POST /api/sync/trigger '{}' >/dev/null
sleep 5  # background task

# Manifest must list the row with query_mode + non-empty hash.
http GET /api/sync/manifest >/dev/null
HASH=$(python3 -c "
import json
m = json.load(open('/tmp/smoke-mat-body'))
t = m.get('tables', {}).get('$TID_A')
print(t.get('hash', '') if t else '')
")
[ -n "$HASH" ] && [ "$HASH" != "null" ] && check "manifest hash present" true || check "manifest hash present (got '$HASH')" false

# Parquet on disk (assumes co-located filesystem, e.g. local docker compose).
PARQUET="${DATA_DIR:-./data}/extracts/bigquery/data/$TID_A.parquet"
if [ -f "$PARQUET" ]; then
    ROWS=$(python3 -c "import duckdb; print(duckdb.connect().execute(\"SELECT count(*) FROM read_parquet('$PARQUET')\").fetchone()[0])" 2>/dev/null || echo "0")
    [ "$ROWS" -gt 0 ] && check "parquet has $ROWS rows" true || check "parquet rows ($ROWS)" false
else
    echo "    note: parquet at $PARQUET not visible from this host (skip if Agnes is remote)"
fi

# ---------------------------------------------------------------------------
# Scenario B — Cost guardrail: register a large-scan materialized SQL,
#              trigger, expect MaterializeBudgetError logged + row skipped.
# ---------------------------------------------------------------------------
echo
echo "[B] Cost guardrail (\`$BIG_TABLE\` full SELECT)"
TID_B="smoke_mat_huge_$(date +%s)"
CREATED_IDS+=("$TID_B")
SQL_B="SELECT * FROM \`$BIG_TABLE\`"

STATUS=$(http POST /api/admin/register-table "{
    \"name\": \"$TID_B\",
    \"source_type\": \"bigquery\",
    \"query_mode\": \"materialized\",
    \"source_query\": $(printf '%s' "$SQL_B" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))"),
    \"sync_schedule\": \"every 1m\"
}")
[ "$STATUS" = "201" ] && check "register 201" true || check "register 201 (got $STATUS)" false

echo "    triggering sync (expect cap to fire)..."
http POST /api/sync/trigger '{}' >/dev/null
sleep 5

# Manifest should NOT have a hash for the huge row (materialize was skipped).
http GET /api/sync/manifest >/dev/null
HUGE_HASH=$(python3 -c "
import json
m = json.load(open('/tmp/smoke-mat-body'))
t = m.get('tables', {}).get('$TID_B')
print(t.get('hash', '') if t else 'absent')
")
if [ "$HUGE_HASH" = "absent" ] || [ -z "$HUGE_HASH" ] || [ "$HUGE_HASH" = "null" ]; then
    check "huge row skipped (no hash in manifest)" true
else
    check "huge row skipped (got hash '$HUGE_HASH' — guardrail did not fire)" false
fi
echo "    grep server logs for: 'MaterializeBudgetError' or 'Materialize cap exceeded'"

# ---------------------------------------------------------------------------
# Scenario C — 0-row warning: SQL with always-false WHERE.
# ---------------------------------------------------------------------------
echo
echo "[C] 0-row WARNING (filter to empty result)"
TID_C="smoke_mat_empty_$(date +%s)"
CREATED_IDS+=("$TID_C")
SQL_C='SELECT corpus FROM `bigquery-public-data.samples.shakespeare` WHERE 1=0'

STATUS=$(http POST /api/admin/register-table "{
    \"name\": \"$TID_C\",
    \"source_type\": \"bigquery\",
    \"query_mode\": \"materialized\",
    \"source_query\": $(printf '%s' "$SQL_C" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))"),
    \"sync_schedule\": \"every 1m\"
}")
[ "$STATUS" = "201" ] && check "register 201" true || check "register 201 (got $STATUS)" false

http POST /api/sync/trigger '{}' >/dev/null
sleep 5

http GET /api/sync/manifest >/dev/null
EMPTY_ROWS=$(python3 -c "
import json
m = json.load(open('/tmp/smoke-mat-body'))
t = m.get('tables', {}).get('$TID_C')
print(t.get('rows', 'absent') if t else 'absent')
")
[ "$EMPTY_ROWS" = "0" ] && check "empty-result rows=0 in manifest" true || check "empty-result rows ($EMPTY_ROWS)" false
echo "    grep server logs for: 'produced 0 rows'"

# ---------------------------------------------------------------------------
# Scenario D — Mode-switch transition clears stale source_query.
# ---------------------------------------------------------------------------
echo
echo "[D] Mode-switch materialized → remote clears source_query"
TID_D="smoke_mat_switch_$(date +%s)"
CREATED_IDS+=("$TID_D")

STATUS=$(http POST /api/admin/register-table "{
    \"name\": \"$TID_D\",
    \"source_type\": \"bigquery\",
    \"query_mode\": \"materialized\",
    \"source_query\": \"SELECT 1\"
}")
[ "$STATUS" = "201" ] && check "register materialized" true || check "register materialized (got $STATUS)" false

# Switch to remote, providing required bucket+source_table.
STATUS=$(http PUT "/api/admin/registry/$TID_D" "{
    \"query_mode\": \"remote\",
    \"bucket\": \"samples\",
    \"source_table\": \"shakespeare\"
}")
[ "$STATUS" = "200" ] && check "switch to remote 200" true || check "switch to remote (got $STATUS)" false

http GET /api/admin/registry >/dev/null
SWITCHED_SQ=$(python3 -c "
import json
r = json.load(open('/tmp/smoke-mat-body'))
row = next((t for t in r.get('tables', []) if t['id'] == '$TID_D'), None)
print(row.get('source_query') if row else 'NOT_FOUND')
")
[ "$SWITCHED_SQ" = "None" ] || [ -z "$SWITCHED_SQ" ] || [ "$SWITCHED_SQ" = "null" ] \
    && check "source_query cleared on switch" true \
    || check "source_query cleared (got '$SWITCHED_SQ')" false

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "---"
echo "Passed: $PASS"
echo "Failed: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
