#!/bin/bash
# Test rsync reliability fixes (Issue #197)
#
# Downloads data into gitignored data/test_sync/ to verify rsync_reliable
# wrapper works end-to-end without touching repo directories.
#
# Usage:
#   bash scripts/test_sync.sh           # Full test sync
#   bash scripts/test_sync.sh --dry-run # Preview only

set -e

DRY_RUN=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN="--dry-run" ;;
    esac
done

# --- Rsync reliability settings (same as sync_data.sh) ---
RSYNC_SSH_OPTS='ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o ConnectTimeout=30'
RSYNC_TIMEOUT=300
RSYNC_MAX_RETRIES=3
RSYNC_RETRY_DELAY=5

rsync_reliable() {
    local attempt=1
    local delay=$RSYNC_RETRY_DELAY
    while [[ $attempt -le $RSYNC_MAX_RETRIES ]]; do
        rsync -e "$RSYNC_SSH_OPTS" --timeout="$RSYNC_TIMEOUT" \
              --partial-dir=.rsync-partial "$@" && return 0
        local exit_code=$?
        # Exit codes 23/24 = partial transfer (permission denied, vanished files) — not retryable
        if [[ $exit_code -eq 23 || $exit_code -eq 24 ]]; then
            echo "   Warning: rsync partial transfer (exit $exit_code), continuing..."
            return 0
        fi
        if [[ $attempt -lt $RSYNC_MAX_RETRIES ]]; then
            echo "   Rsync failed (exit $exit_code), retrying in ${delay}s (attempt $attempt/$RSYNC_MAX_RETRIES)..."
            sleep "$delay"
            delay=$((delay * 2))
        fi
        attempt=$((attempt + 1))
    done
    echo "   ERROR: Rsync failed after $RSYNC_MAX_RETRIES attempts"
    return 1
}

# --- Test destination (gitignored) ---
DEST="./data/test_sync"
LOG_DIR="./data/sync_diagnostics"
mkdir -p "$DEST" "$LOG_DIR"
LOG_FILE="$LOG_DIR/test_sync_$(date '+%Y%m%d_%H%M%S').log"

log() {
    echo "$@" | tee -a "$LOG_FILE"
}

log "=== Rsync reliability test ==="
log "Started: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
log "Destination: $DEST"
log "SSH opts: $RSYNC_SSH_OPTS"
log "Timeout: ${RSYNC_TIMEOUT}s, Retries: $RSYNC_MAX_RETRIES"
log ""

# --- Test 1: SSH connectivity ---
log "--- Test 1: SSH connectivity ---"
START=$(date +%s)
if ssh -o ConnectTimeout=10 data-analyst "echo ok" >> "$LOG_FILE" 2>&1; then
    ELAPSED=$(($(date +%s) - START))
    log "PASS (${ELAPSED}s)"
else
    ELAPSED=$(($(date +%s) - START))
    log "FAIL (${ELAPSED}s) — cannot reach server, aborting"
    exit 1
fi
log ""

# --- Test 2: Small directory (docs) ---
log "--- Test 2: Sync docs (small, text, -avz) ---"
mkdir -p "$DEST/docs"
START=$(date +%s)
rsync_reliable -avz --delete $DRY_RUN "data-analyst:server/docs/" "$DEST/docs/" >> "$LOG_FILE" 2>&1
ELAPSED=$(($(date +%s) - START))
if [[ -z "$DRY_RUN" ]]; then
    DOC_COUNT=$(find "$DEST/docs" -type f 2>/dev/null | wc -l | tr -d ' ')
    DOC_SIZE=$(du -sh "$DEST/docs" 2>/dev/null | cut -f1)
    log "PASS (${ELAPSED}s) — $DOC_COUNT files, $DOC_SIZE"
else
    log "PASS dry-run (${ELAPSED}s)"
fi
log ""

# --- Test 3: Scripts ---
log "--- Test 3: Sync scripts (small, text, -avz) ---"
mkdir -p "$DEST/scripts"
START=$(date +%s)
rsync_reliable -avz --delete $DRY_RUN "data-analyst:server/scripts/" "$DEST/scripts/" >> "$LOG_FILE" 2>&1
ELAPSED=$(($(date +%s) - START))
if [[ -z "$DRY_RUN" ]]; then
    SCRIPT_COUNT=$(find "$DEST/scripts" -type f 2>/dev/null | wc -l | tr -d ' ')
    log "PASS (${ELAPSED}s) — $SCRIPT_COUNT files"
else
    log "PASS dry-run (${ELAPSED}s)"
fi
log ""

# --- Test 4: Core parquet (the big one — no -z, with keepalive) ---
log "--- Test 4: Sync core parquet (large, binary, -av WITHOUT -z) ---"
log "This is the transfer that was hanging. Expect ~7000 files..."
mkdir -p "$DEST/parquet"
START=$(date +%s)
rsync_reliable -av --delete --progress \
    --exclude='jira/' --exclude='kbc_telemetry_expert/' \
    $DRY_RUN data-analyst:server/parquet/ "$DEST/parquet/" 2>&1 | \
    tee -a "$LOG_FILE" | \
    grep -E '(^sent |^total size|^rsync|Warning:|ERROR:)' || true
ELAPSED=$(($(date +%s) - START))
if [[ -z "$DRY_RUN" ]]; then
    PARQUET_COUNT=$(find "$DEST/parquet" -name '*.parquet' -type f 2>/dev/null | wc -l | tr -d ' ')
    PARQUET_SIZE=$(du -sh "$DEST/parquet" 2>/dev/null | cut -f1)
    log "DONE (${ELAPSED}s) — $PARQUET_COUNT parquet files, $PARQUET_SIZE"
else
    log "DONE dry-run (${ELAPSED}s)"
fi
log ""

# --- Summary ---
log "=== Summary ==="
log "Finished: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
if [[ -z "$DRY_RUN" ]]; then
    TOTAL_SIZE=$(du -sh "$DEST" 2>/dev/null | cut -f1)
    TOTAL_FILES=$(find "$DEST" -type f 2>/dev/null | wc -l | tr -d ' ')
    log "Total: $TOTAL_FILES files, $TOTAL_SIZE in $DEST"
fi
log "Full log: $LOG_FILE"
log ""
log "To clean up test data: rm -rf $DEST"
