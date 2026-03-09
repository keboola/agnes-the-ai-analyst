# Plan: Fix rsync synchronization hang (#197)

## Problem

Rsync from GCP server (YOUR_SERVER_IP) hangs after 1-5 minutes. Process exists but has 0% CPU and no network activity. 100% reproducible with ~7000 parquet files.

## Root Cause Analysis

The rsync commands in `scripts/sync_data.sh` use plain `rsync -avz` without:

1. **SSH keepalive** - No `-e "ssh -o ServerAliveInterval=60"` to keep the TCP connection alive through firewall/NAT
2. **I/O timeout** - No `--timeout=N` to detect stalled transfers
3. **Retry logic** - A single connection drop kills the entire sync
4. **Partial resume** - No `--partial-dir` so retries restart file transfers from scratch
5. **Compression skip** - `-z` compresses parquet files that are already snappy-compressed (CPU waste)

A stateful firewall or NAT device (GCP Cloud NAT, VPC firewall, or analyst's home router) drops idle TCP connections. Server-side `ClientAliveInterval=300s` sends keepalive, but the client side sends nothing. The firewall sees idle traffic and silently drops the connection. Neither rsync nor SSH client detects the dead connection.

Note: GCP VPC firewall default idle timeout is 600s (10 min). The 1-5 minute hang suggests a more aggressive timeout on Cloud NAT or analyst's local network.

## Implementation Plan

### Step 1: Create diagnostic tests (`tests/test_sync_data.py`)

Real end-to-end tests that download data to gitignored `data/` folder with detailed logging to pinpoint exactly where and why the hang occurs.

**Live diagnostic tests (require server access, marked `@pytest.mark.live`):**

| Test | Purpose |
|------|---------|
| `test_ssh_connectivity` | Basic SSH connection test with timeout |
| `test_rsync_small_directory` | Sync docs (~few files) to verify baseline works |
| `test_rsync_with_keepalive` | Sync parquet with SSH keepalive options |
| `test_rsync_with_timeout` | Sync parquet with `--timeout=60` to detect hang quickly |
| `test_rsync_per_subdirectory` | Sync each parquet subdir separately to isolate which triggers the hang |
| `test_rsync_without_compression` | Sync parquet without `-z` to test if compression causes stall |

Each test logs to `data/sync_diagnostics/` with timestamps, exit codes, bytes transferred, duration.

**Static regression tests (run in CI without server access):**

| Test | Purpose |
|------|---------|
| `test_rsync_commands_use_ssh_keepalive` | Every rsync in scripts has `ServerAliveInterval` |
| `test_rsync_commands_have_timeout` | Every rsync has `--timeout=N` |
| `test_parquet_rsync_does_not_compress` | Parquet rsync uses `-av` not `-avz` |
| `test_sync_scripts_have_retry_logic` | Scripts define retry wrapper |

### Step 2: Run diagnostic tests and analyze results

Before implementing any fix, run the live diagnostic tests to confirm the root cause:

```bash
pytest tests/test_sync_data.py -v -k "live" --tb=short 2>&1 | tee data/sync_diagnostics/test_run.log
```

**Expected outcomes to validate:**
- `test_ssh_connectivity` passes → SSH connection itself is fine
- `test_rsync_small_directory` passes → small syncs work, problem is scale/duration-related
- `test_rsync_with_timeout` fails with timeout → confirms connection drop (rsync exits instead of hanging forever)
- `test_rsync_with_keepalive` passes → confirms keepalive fixes the issue
- `test_rsync_per_subdirectory` → identifies if specific directory triggers the hang or if it's purely time-based
- `test_rsync_without_compression` → reveals if `-z` contributes to the stall

**Decision gate:** Based on results, confirm which combination of fixes is needed before proceeding to Step 3. If keepalive alone fixes it, the other changes are still applied as defense-in-depth.

### Step 3: Fix `scripts/sync_data.sh`

Add reliability wrapper after argument parsing:

```bash
# --- Rsync reliability settings (Issue #197) ---
RSYNC_SSH_OPTS='ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o ConnectTimeout=30'
RSYNC_TIMEOUT=300
RSYNC_MAX_RETRIES=3
RSYNC_RETRY_DELAY=5

rsync_reliable() {
    local attempt=1
    local delay=$RSYNC_RETRY_DELAY
    while [[ $attempt -le $RSYNC_MAX_RETRIES ]]; do
        if rsync -e "$RSYNC_SSH_OPTS" --timeout="$RSYNC_TIMEOUT" \
                 --partial-dir=.rsync-partial "$@"; then
            return 0
        fi
        local exit_code=$?
        if [[ $attempt -lt $RSYNC_MAX_RETRIES ]]; then
            echo "   Rsync failed (exit $exit_code), retrying in ${delay}s ($attempt/$RSYNC_MAX_RETRIES)..."
            sleep "$delay"
            delay=$((delay * 2))
        fi
        attempt=$((attempt + 1))
    done
    echo "   ERROR: Rsync failed after $RSYNC_MAX_RETRIES attempts"
    return 1
}
```

Replace all rsync invocations to use `rsync_reliable()` wrapper:
- Drop `-z` for parquet transfers (already snappy-compressed)
- Keep `-z` for text transfers (docs, scripts, metadata)
- `--partial-dir=.rsync-partial` enables resume on retry (Gemini review recommendation)

### Step 4: Fix `scripts/sync_jira.sh`

Same `rsync_reliable()` wrapper and fixes.

### Step 5: Update CI

Add `tests/test_sync_data.py` to `.github/workflows/deploy-guard.yml` (static regression tests only, live diagnostics excluded via marker).

### Step 6: Verify fix end-to-end

Run full sync and confirm no hanging:
```bash
bash scripts/sync_data.sh --dry-run   # script syntax OK
bash scripts/sync_data.sh             # full sync completes
```

## Files to Modify

| File | Action | Description |
|------|--------|-------------|
| `tests/test_sync_data.py` | CREATE | Diagnostic + regression tests |
| `scripts/sync_data.sh` | EDIT | Add reliability wrapper, fix all rsync calls |
| `scripts/sync_jira.sh` | EDIT | Same fixes |
| `.github/workflows/deploy-guard.yml` | EDIT | Add static tests to CI |

## Review Notes

Plan reviewed by Google Gemini (2026-02-17). Key feedback incorporated:
- **`--partial-dir=.rsync-partial`** added to wrapper for efficient retry resume
- **Root cause confirmed** as most likely firewall/NAT idle timeout (textbook signature)
- **`ServerAliveInterval=60`** confirmed as correct value (low overhead, safe interval)
- **`--timeout=300`** confirmed as reasonable (long enough to avoid false positives)
- **Drop `-z`** confirmed correct (compressing snappy data wastes CPU)
- **Future optimization**: `--files-from` approach for large file sets if needed
