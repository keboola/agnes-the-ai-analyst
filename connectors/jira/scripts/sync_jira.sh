#!/bin/bash
# Sync Jira support tickets from server
#
# This is a sub-script called by sync_data.sh when jira is enabled in config.
# Can also be run standalone for manual Jira sync.
#
# Usage:
#   bash server/scripts/sync_jira.sh              # Sync Jira data + create DuckDB views
#   bash server/scripts/sync_jira.sh --dry-run    # Preview what would sync
#   bash server/scripts/sync_jira.sh --views-only # Only create/refresh DuckDB views
#
# Config is managed via the web portal (Data Settings page)
# Settings are stored on server in ~/.sync_settings.yaml

set -e

DRY_RUN=""
VIEWS_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN="--dry-run" ;;
        --views-only) VIEWS_ONLY=true ;;
    esac
done

# --- Rsync reliability settings (Issue #197) ---
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

# Check if rsync is available
USE_RSYNC=true
if ! command -v rsync >/dev/null 2>&1; then
    USE_RSYNC=false
fi

# Read config to check if attachments sync is enabled
# Config is downloaded from server to /tmp/ by sync_data.sh
SYNC_CONFIG="/tmp/.sync_settings_$(id -u).yaml"
SYNC_ATTACHMENTS=false
if [[ -f "$SYNC_CONFIG" ]] && grep -qE '^\s*jira_attachments:\s*true' "$SYNC_CONFIG" 2>/dev/null; then
    SYNC_ATTACHMENTS=true
fi

# Activate virtual environment (required for DuckDB)
activate_venv() {
    if [[ -d ".venv" ]]; then
        if [[ -f ".venv/bin/activate" ]]; then
            source .venv/bin/activate
        elif [[ -f ".venv/Scripts/activate" ]]; then
            source .venv/Scripts/activate
        fi
    elif [[ -d "venv" ]]; then
        if [[ -f "venv/bin/activate" ]]; then
            source venv/bin/activate
        elif [[ -f "venv/Scripts/activate" ]]; then
            source venv/Scripts/activate
        fi
    fi
}

# Create/refresh DuckDB views for Jira parquet files
create_jira_views() {
    local DB_PATH="user/duckdb/analytics.duckdb"
    if [[ ! -f "$DB_PATH" ]]; then
        return 0  # DB doesn't exist yet, skip
    fi

    # Activate venv to get duckdb module
    activate_venv

    local PY=python3
    command -v python3 >/dev/null 2>&1 || PY=python

    $PY -c "
import duckdb, glob
conn = duckdb.connect('$DB_PATH')
tables = {
    'jira_issues': 'server/parquet/jira/issues/*.parquet',
    'jira_comments': 'server/parquet/jira/comments/*.parquet',
    'jira_attachments': 'server/parquet/jira/attachments/*.parquet',
    'jira_changelog': 'server/parquet/jira/changelog/*.parquet',
    'jira_issuelinks': 'server/parquet/jira/issuelinks/*.parquet',
    'jira_remote_links': 'server/parquet/jira/remote_links/*.parquet',
}
created = 0
for view, pattern in tables.items():
    if glob.glob(pattern):
        conn.execute(f\"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{pattern}')\")
        count = conn.execute(f'SELECT COUNT(*) FROM {view}').fetchone()[0]
        print(f'   {view}: {count:,} rows')
        created += 1
conn.close()
if created:
    print(f'   {created} Jira view(s) created')
"
}

# Handle --views-only mode
if [[ "$VIEWS_ONLY" == true ]]; then
    create_jira_views
    exit 0
fi

echo "📋 Syncing Jira support tickets..."

if [[ "$USE_RSYNC" == true ]]; then
    # Sync Jira parquet files (issues, comments, attachments metadata, changelog)
    rsync_reliable -av --delete --progress $DRY_RUN data-analyst:server/parquet/jira/ ./server/parquet/jira/
else
    if [[ -n "$DRY_RUN" ]]; then
        echo "  [dry-run] Would sync: data-analyst:server/parquet/jira/ -> ./server/parquet/jira/"
    else
        mkdir -p ./server/parquet/jira
        scp -r "data-analyst:server/parquet/jira/"* ./server/parquet/jira/ 2>/dev/null || true
    fi
fi

# Sync attachment files if enabled (large - ~500MB+)
if [[ "$SYNC_ATTACHMENTS" == true ]]; then
    echo ""
    echo "📎 Syncing Jira attachment files (this may take a while)..."

    if [[ "$USE_RSYNC" == true ]]; then
        # Use --copy-links to resolve symlink and copy actual files
        rsync_reliable -av --copy-links --progress $DRY_RUN data-analyst:server/jira_attachments/ ./server/jira_attachments/
    else
        if [[ -n "$DRY_RUN" ]]; then
            echo "  [dry-run] Would sync: data-analyst:server/jira_attachments/ -> ./server/jira_attachments/"
        else
            mkdir -p ./server/jira_attachments
            scp -r "data-analyst:server/jira_attachments/"* ./server/jira_attachments/ 2>/dev/null || true
        fi
    fi

    if [[ -z "$DRY_RUN" ]]; then
        echo "✅ Jira attachments synced"
    fi
else
    echo ""
    echo "💡 Attachment files not synced (jira_attachments: false)"
    echo "   To enable: visit the web portal -> Data Settings"
fi

if [[ -z "$DRY_RUN" ]]; then
    echo "✅ Jira data synced"
    echo ""
    echo "🦆 Creating Jira DuckDB views..."
    create_jira_views
fi
