#!/bin/bash
# Backfill missing Jira issues from GitHub issue #101
# Range: SUPPORT-15166 to SUPPORT-15243 (71 missing of 78 total)
# Safe to run while webhook processing is active.
#
# Usage:
#   ssh kids
#   cd /opt/data-analyst/repo
#   source /opt/data-analyst/.venv/bin/activate
#   bash scripts/backfill_gap.sh [--dry-run]

set -euo pipefail

REPO_DIR="/opt/data-analyst/repo"
VENV_DIR="/opt/data-analyst/.venv"
RAW_DIR="/data/src_data/raw/jira"
PARQUET_DIR="/data/src_data/parquet/jira"
LOG_FILE="/opt/data-analyst/logs/backfill_gap.log"
JIRA_PROJECT="${JIRA_PROJECT:-}"
if [ -n "$JIRA_PROJECT" ]; then
    JQL="project = \"${JIRA_PROJECT}\" AND key >= SUPPORT-15166 AND key <= SUPPORT-15243"
else
    JQL='key >= SUPPORT-15166 AND key <= SUPPORT-15243'
fi
RANGE_START=15166
RANGE_END=15243
DRY_RUN=false

# Parse args
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

# Log to both stdout and file
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=== Backfill started: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

cd "$REPO_DIR"

# --- Phase 1: Download raw JSON ---
echo ""
echo "--- Phase 1: Download raw JSON ---"
if $DRY_RUN; then
    python scripts/jira_backfill.py --jql "$JQL" --dry-run
    echo "Dry run complete. Exiting."
    exit 0
fi

python scripts/jira_backfill.py --jql "$JQL" --skip-existing --parallel 4

# --- Phase 2: Incremental Parquet transform ---
echo ""
echo "--- Phase 2: Incremental Parquet transform ---"
success=0
skipped=0
failed=0

for issue_num in $(seq $RANGE_START $RANGE_END); do
    issue_key="SUPPORT-${issue_num}"
    json_file="${RAW_DIR}/issues/${issue_key}.json"

    if [ ! -f "$json_file" ]; then
        echo "SKIP: $issue_key (no JSON)"
        skipped=$((skipped + 1))
        continue
    fi

    echo -n "Transform $issue_key... "
    if python -m src.incremental_jira_transform "$issue_key" 2>&1 | tail -1; then
        success=$((success + 1))
    else
        echo "FAILED: $issue_key"
        failed=$((failed + 1))
    fi

    sleep 0.5  # reduce collision window with live webhooks
done

echo ""
echo "Transform complete: $success ok, $skipped skipped, $failed failed"

# --- Phase 3: Verification ---
echo ""
echo "--- Phase 3: Verification ---"
python -c "
import pyarrow.parquet as pq
from pathlib import Path

parquet_dir = Path('$PARQUET_DIR/issues')
all_keys = set()
for pf in parquet_dir.glob('*.parquet'):
    table = pq.read_table(pf, columns=['issue_key'])
    all_keys.update(table.column('issue_key').to_pylist())

expected = {f'SUPPORT-{n}' for n in range($RANGE_START, $RANGE_END + 1)}
found = expected & all_keys
missing = expected - all_keys
print(f'Found: {len(found)}/{len(expected)} issues in Parquet')
if missing:
    print(f'STILL MISSING ({len(missing)}): {sorted(missing)}')
else:
    print('SUCCESS: All issues present in Parquet')
"

echo ""
echo "=== Backfill finished: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
