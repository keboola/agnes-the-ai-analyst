#!/bin/bash
# Sync data from server and upload user files
#
# Usage:
#   bash server/scripts/sync_data.sh            # Full sync (pull server/ + push user/)
#   bash server/scripts/sync_data.sh --dry-run  # Show what would be synced (no changes)
#   bash server/scripts/sync_data.sh --push     # Only upload user/ to server

set -e

# Parse arguments
DRY_RUN=""
PUSH_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN="--dry-run" ;;
        --push)    PUSH_ONLY=true ;;
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

# --- Self-update check (safety net) ---
# If Claude forgot to run rsync first, this catches script updates
if [[ -z "$DRY_RUN" ]]; then
    SCRIPT_PATH="./server/scripts/sync_data.sh"
    if [[ -f "$SCRIPT_PATH" ]]; then
        OLD_CHECKSUM=$(md5sum "$SCRIPT_PATH" 2>/dev/null | cut -d' ' -f1 || echo "none")

        echo "🔄 Checking for script updates..."
        rsync -e "$RSYNC_SSH_OPTS" --timeout=30 -avz --quiet data-analyst:server/scripts/ ./server/scripts/ 2>/dev/null || \
            scp -rq data-analyst:server/scripts/* ./server/scripts/ 2>/dev/null || true

        NEW_CHECKSUM=$(md5sum "$SCRIPT_PATH" 2>/dev/null | cut -d' ' -f1 || echo "none")
        if [[ "$OLD_CHECKSUM" != "$NEW_CHECKSUM" && "$OLD_CHECKSUM" != "none" ]]; then
            echo ""
            echo "⚠️  sync_data.sh was updated! Run sync again to use the new version."
            echo "   Command: bash server/scripts/sync_data.sh"
            exit 1
        fi
        echo "   ✅ Scripts up to date"
    fi
fi

# Check if rsync is available, fall back to scp if not
USE_RSYNC=true
if ! command -v rsync >/dev/null 2>&1; then
    USE_RSYNC=false
    echo "⚠️  rsync not found, using scp as fallback (slower, no incremental sync)"
    echo ""
fi

# Helper function: sync directory from server (handles both rsync and scp)
# Usage: sync_from_server <remote_path> <local_path> [--with-dotfiles]
sync_from_server() {
    local remote_path="$1"
    local local_path="$2"
    local with_dotfiles="$3"

    if [[ "$USE_RSYNC" == true ]]; then
        rsync_reliable -avz --delete $DRY_RUN "data-analyst:${remote_path}/" "${local_path}/"
    else
        if [[ -n "$DRY_RUN" ]]; then
            echo "  [dry-run] Would copy: data-analyst:${remote_path}/* -> ${local_path}/"
            if [[ "$with_dotfiles" == "--with-dotfiles" ]]; then
                echo "  [dry-run] Would copy dotfiles: data-analyst:${remote_path}/.* -> ${local_path}/"
            fi
        else
            mkdir -p "${local_path}"
            # Copy regular files
            scp -r "data-analyst:${remote_path}/"* "${local_path}/" 2>/dev/null || true
            # IMPORTANT: scp with * does NOT copy dotfiles, must copy explicitly
            if [[ "$with_dotfiles" == "--with-dotfiles" ]]; then
                scp "data-analyst:${remote_path}/".* "${local_path}/" 2>/dev/null || true
            fi
        fi
    fi
}

# Helper function: sync directory to server
sync_to_server() {
    local local_path="$1"
    local remote_path="$2"

    if [[ "$USE_RSYNC" == true ]]; then
        rsync_reliable -avz --delete $DRY_RUN "${local_path}/" "data-analyst:${remote_path}/"
    else
        if [[ -n "$DRY_RUN" ]]; then
            echo "  [dry-run] Would upload: ${local_path}/* -> data-analyst:${remote_path}/"
        else
            scp -r "${local_path}/"* "data-analyst:${remote_path}/" 2>/dev/null || true
            scp "${local_path}/".* "data-analyst:${remote_path}/" 2>/dev/null || true
        fi
    fi
}

if [[ "$PUSH_ONLY" == true ]]; then
    if [[ -n "$DRY_RUN" ]]; then
        echo "🔍 DRY RUN MODE - showing what would be pushed..."
    else
        echo "📤 Uploading user files to server..."
    fi
    echo ""
    sync_to_server ./user user
    # Backup CLAUDE.local.md to server home (disaster recovery)
    if [[ -f "./CLAUDE.local.md" ]]; then
        if [[ -n "$DRY_RUN" ]]; then
            echo "  [dry-run] Would backup: ./CLAUDE.local.md -> data-analyst:~/CLAUDE.local.md"
        else
            scp -q ./CLAUDE.local.md "data-analyst:~/CLAUDE.local.md" && \
                echo "📝 CLAUDE.local.md backed up to server"
        fi
    fi
    if [[ -n "$DRY_RUN" ]]; then
        echo ""
        echo "🔍 Dry run complete - no changes made"
        echo "💡 To perform actual push, run: bash server/scripts/sync_data.sh --push"
    else
        echo ""
        echo "✅ User files uploaded to server!"
    fi
    exit 0
fi

if [[ -n "$DRY_RUN" ]]; then
    echo "🔍 DRY RUN MODE - showing what would be synced..."
    echo ""
else
    echo "🔄 Syncing data from server..."
    echo ""
fi

# --- Migration: detect old directory structure and migrate ---
if [[ -z "$DRY_RUN" ]] && [[ -d "./data/parquet" ]] && [[ ! -d "./server/parquet" ]]; then
    echo "🔄 Migrating from old directory structure to server/ + user/ layout..."
    echo ""

    # Create new structure
    mkdir -p ./server/docs ./server/scripts ./server/examples ./server/parquet ./server/metadata
    mkdir -p ./user/duckdb ./user/notifications ./user/artifacts ./user/scripts ./user/parquet ./user/sessions

    # Move data
    if [[ -d "./data/parquet" ]]; then
        cp -r ./data/parquet/* ./server/parquet/ 2>/dev/null || true
    fi
    if [[ -d "./data/metadata" ]]; then
        cp -r ./data/metadata/* ./server/metadata/ 2>/dev/null || true
    fi
    if [[ -d "./data/duckdb" ]]; then
        cp -r ./data/duckdb/* ./user/duckdb/ 2>/dev/null || true
    fi
    if [[ -d "./docs" ]] && [[ ! -L "./docs" ]]; then
        cp -r ./docs/* ./server/docs/ 2>/dev/null || true
    fi
    if [[ -d "./scripts" ]] && [[ ! -L "./scripts" ]]; then
        cp -r ./scripts/* ./server/scripts/ 2>/dev/null || true
    fi

    echo "✅ Migration complete. Old directories preserved (remove manually when ready)."
    echo "   To clean up: rm -rf ./data ./docs ./scripts"
    echo ""
    echo "📌 From now on, use this command to sync:"
    echo "   bash server/scripts/sync_data.sh"
    echo ""
fi

# --- Download sync settings first (needed for excludes) ---
# Config is managed via the web portal (Data Settings page)
# Stored on server in ~/.sync_settings.yaml
SYNC_CONFIG_LOCAL="/tmp/.sync_settings_$(id -u).yaml"

if [[ -z "$DRY_RUN" ]]; then
    echo "📥 Downloading sync settings from portal..."
    if scp -q data-analyst:~/.sync_settings.yaml "$SYNC_CONFIG_LOCAL" 2>/dev/null; then
        echo "   ✅ Settings loaded"
    else
        echo "   ℹ️  No custom settings, using defaults (Jira disabled)"
        # Create empty/default config
        cat > "$SYNC_CONFIG_LOCAL" << 'DEFAULTS'
datasets:
  jira: false
  jira_attachments: false
  kbc_telemetry_expert: false
DEFAULTS
    fi
    # Download rsync filter for per-table sync
    SYNC_FILTER_LOCAL="/tmp/.sync_rsync_filter_$(id -u)"
    if scp -q data-analyst:~/.sync_rsync_filter "$SYNC_FILTER_LOCAL" 2>/dev/null; then
        echo "   ✅ Filter file loaded"
    else
        # No filter file = no per-table filtering
        rm -f "$SYNC_FILTER_LOCAL"
    fi
    echo ""
else
    # For dry-run, still need settings to show what would happen
    if [[ ! -f "$SYNC_CONFIG_LOCAL" ]]; then
        cat > "$SYNC_CONFIG_LOCAL" << 'DEFAULTS'
datasets:
  jira: false
  jira_attachments: false
  kbc_telemetry_expert: false
DEFAULTS
    fi
    # Download rsync filter for dry-run too
    SYNC_FILTER_LOCAL="/tmp/.sync_rsync_filter_$(id -u)"
    scp -q data-analyst:~/.sync_rsync_filter "$SYNC_FILTER_LOCAL" 2>/dev/null || rm -f "$SYNC_FILTER_LOCAL"
fi

# --- Sync server/ content (read-only from server, --delete removes obsolete files) ---
echo "📋 Syncing documentation and scripts..."

# Build exclude list for docs based on disabled datasets
DOCS_EXCLUDES=""
if ! grep -qE '^\s*jira:\s*true' "$SYNC_CONFIG_LOCAL" 2>/dev/null; then
    DOCS_EXCLUDES="$DOCS_EXCLUDES --exclude=jira_schema.md"
fi
if ! grep -qE '^\s*kbc_telemetry_expert:\s*true' "$SYNC_CONFIG_LOCAL" 2>/dev/null; then
    DOCS_EXCLUDES="$DOCS_EXCLUDES --exclude=datasets/kbc_telemetry_expert*"
fi

# Sync docs with excludes for disabled datasets
if [[ "$USE_RSYNC" == true ]]; then
    rsync_reliable -avz --delete $DOCS_EXCLUDES $DRY_RUN "data-analyst:server/docs/" "./server/docs/"
else
    sync_from_server server/docs ./server/docs
fi

sync_from_server server/scripts ./server/scripts
sync_from_server server/examples ./server/examples
sync_from_server server/metadata ./server/metadata

if [[ -z "$DRY_RUN" ]]; then
    # Regenerate CLAUDE.md from updated template (preserves user's CLAUDE.local.md)
    if [[ -f "./server/docs/setup/claude_md_template.txt" ]]; then
        # Extract username from existing CLAUDE.md, fall back to $USER
        ANALYST_USER="$USER"
        if [[ -f "./CLAUDE.md" ]]; then
            EXISTING_USER=$(grep -oP '\*\*Analyst\*\* \| \K\S+' ./CLAUDE.md 2>/dev/null || true)
            if [[ -n "$EXISTING_USER" ]]; then
                ANALYST_USER="$EXISTING_USER"
            fi
        fi
        sed -e "s/{username}/$ANALYST_USER/g" \
            ./server/docs/setup/claude_md_template.txt > ./CLAUDE.md
        echo "📝 CLAUDE.md updated from latest template"
    fi

    # Update .claude/settings.json (project permissions)
    if [[ -f "./server/docs/setup/claude_settings.json" ]]; then
        mkdir -p ./.claude
        cp ./server/docs/setup/claude_settings.json ./.claude/settings.json
        echo "🔒 .claude/settings.json updated"
    fi
    echo ""
fi

# Sync core parquet data (excludes optional datasets like jira/)
# Optional datasets are synced by sub-scripts based on user config
echo "📦 Syncing core parquet files..."
if [[ "$USE_RSYNC" == true ]]; then
    if [[ -f "$SYNC_FILTER_LOCAL" ]] && grep -q "table_mode: explicit" "$SYNC_FILTER_LOCAL" 2>/dev/null; then
        echo "   Using per-table filter (explicit mode)"
        rsync_reliable -av --delete --progress --filter="merge $SYNC_FILTER_LOCAL" $DRY_RUN data-analyst:server/parquet/ ./server/parquet/
    else
        rsync_reliable -av --delete --progress --exclude='jira/' --exclude='kbc_telemetry_expert/' $DRY_RUN data-analyst:server/parquet/ ./server/parquet/
    fi
else
    sync_from_server server/parquet ./server/parquet
fi

# Create user/ directories if missing
if [[ -z "$DRY_RUN" ]]; then
    mkdir -p ./user/duckdb ./user/notifications ./user/artifacts ./user/scripts ./user/parquet ./user/sessions
fi

# --- Sync optional datasets ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Read config and sync optional datasets
if [[ -f "$SYNC_CONFIG_LOCAL" ]]; then
    # Jira dataset
    if grep -qE '^\s*jira:\s*true' "$SYNC_CONFIG_LOCAL" 2>/dev/null; then
        echo ""
        if [[ -f "${SCRIPT_DIR}/sync_jira.sh" ]]; then
            bash "${SCRIPT_DIR}/sync_jira.sh" $DRY_RUN
        fi
    else
        # Cleanup: remove Jira data if disabled (so Claude doesn't see stale data)
        JIRA_CLEANUP_NEEDED=false
        [[ -d "./server/parquet/jira" ]] && JIRA_CLEANUP_NEEDED=true
        [[ -f "./server/docs/jira_schema.md" ]] && JIRA_CLEANUP_NEEDED=true
        [[ -d "./server/jira_attachments" ]] && JIRA_CLEANUP_NEEDED=true

        if [[ "$JIRA_CLEANUP_NEEDED" == true ]]; then
            echo ""
            if [[ -n "$DRY_RUN" ]]; then
                echo "🧹 [dry-run] Would clean up disabled Jira dataset:"
                [[ -d "./server/parquet/jira" ]] && echo "   Would remove: server/parquet/jira/"
                [[ -f "./server/docs/jira_schema.md" ]] && echo "   Would remove: server/docs/jira_schema.md"
                [[ -d "./server/jira_attachments" ]] && echo "   Would remove: server/jira_attachments/"
            else
                echo "🧹 Cleaning up disabled Jira dataset..."
                if [[ -d "./server/parquet/jira" ]]; then
                    rm -rf ./server/parquet/jira
                    echo "   Removed: server/parquet/jira/"
                fi
                if [[ -f "./server/docs/jira_schema.md" ]]; then
                    rm -f ./server/docs/jira_schema.md
                    echo "   Removed: server/docs/jira_schema.md"
                fi
                if [[ -d "./server/jira_attachments" ]]; then
                    rm -rf ./server/jira_attachments
                    echo "   Removed: server/jira_attachments/"
                fi
            fi
        fi
    fi

    # Telemetry Expert dataset
    if grep -qE '^\s*kbc_telemetry_expert:\s*true' "$SYNC_CONFIG_LOCAL" 2>/dev/null; then
        echo ""
        echo "📊 Syncing Telemetry Expert dataset..."
        if [[ "$USE_RSYNC" == true ]]; then
            rsync_reliable -av --delete --progress $DRY_RUN data-analyst:server/parquet/kbc_telemetry_expert/ ./server/parquet/kbc_telemetry_expert/
        else
            if [[ -n "$DRY_RUN" ]]; then
                echo "  [dry-run] Would sync: kbc_telemetry_expert parquet"
            else
                mkdir -p ./server/parquet/kbc_telemetry_expert
                scp -r "data-analyst:server/parquet/kbc_telemetry_expert/"* ./server/parquet/kbc_telemetry_expert/ 2>/dev/null || true
            fi
        fi
        # Sync dataset docs (.md file + schema directory)
        mkdir -p ./server/docs/datasets
        if [[ "$USE_RSYNC" == true ]]; then
            rsync_reliable -avz $DRY_RUN data-analyst:'server/docs/datasets/kbc_telemetry_expert*' ./server/docs/datasets/
        else
            if [[ -z "$DRY_RUN" ]]; then
                scp -r data-analyst:'server/docs/datasets/kbc_telemetry_expert*' ./server/docs/datasets/ 2>/dev/null || true
            fi
        fi
        if [[ -z "$DRY_RUN" ]]; then
            echo "✅ Telemetry Expert synced"
        fi
    else
        # Cleanup: remove dataset if disabled (so Claude doesn't see stale data)
        KBC_CLEANUP_NEEDED=false
        [[ -d "./server/parquet/kbc_telemetry_expert" ]] && KBC_CLEANUP_NEEDED=true
        ls ./server/docs/datasets/kbc_telemetry_expert* &>/dev/null && KBC_CLEANUP_NEEDED=true

        if [[ "$KBC_CLEANUP_NEEDED" == true ]]; then
            echo ""
            if [[ -n "$DRY_RUN" ]]; then
                echo "🧹 [dry-run] Would clean up disabled Telemetry Expert dataset:"
                [[ -d "./server/parquet/kbc_telemetry_expert" ]] && echo "   Would remove: server/parquet/kbc_telemetry_expert/"
                for f in ./server/docs/datasets/kbc_telemetry_expert*; do
                    [[ -e "$f" ]] && echo "   Would remove: $f"
                done
            else
                echo "🧹 Cleaning up disabled Telemetry Expert dataset..."
                if [[ -d "./server/parquet/kbc_telemetry_expert" ]]; then
                    rm -rf ./server/parquet/kbc_telemetry_expert
                    echo "   Removed: server/parquet/kbc_telemetry_expert/"
                fi
                for f in ./server/docs/datasets/kbc_telemetry_expert*; do
                    if [[ -e "$f" ]]; then
                        rm -rf "$f"
                        echo "   Removed: $f"
                    fi
                done
            fi
        fi
    fi
fi

# --- Backup: collect missed session transcripts ---
# The SessionEnd hook copies transcripts to user/sessions/ automatically.
# This backup catches any missed transcripts (e.g. terminal killed via SIGKILL).
if [[ -z "$DRY_RUN" ]]; then
    # Encode project path the same way Claude Code does:
    # Replaces ALL non-alphanumeric characters with hyphens
    # /Users/john/my_project -> -Users-john-my-project (macOS/Linux)
    # /c/Users/john/project -> -c-Users-john-project (Windows/Git Bash)
    ENCODED_PATH=$(pwd | sed 's|[^a-zA-Z0-9]|-|g; s|^-*||')
    TRANSCRIPT_DIR="$HOME/.claude/projects/-${ENCODED_PATH}"
    if [[ -d "$TRANSCRIPT_DIR" ]]; then
        COLLECTED=0
        for jsonl in "$TRANSCRIPT_DIR"/*.jsonl; do
            [[ -f "$jsonl" ]] || continue
            SESSION_ID=$(basename "$jsonl" .jsonl)
            # Skip if already collected (check by session_id, ignoring date prefix)
            if ls ./user/sessions/*_"${SESSION_ID}".jsonl 1>/dev/null 2>&1; then
                continue
            fi
            # Use the file's actual modification date, not current date
            FILE_DATE=$(date -r "$jsonl" '+%Y-%m-%d')
            TARGET="./user/sessions/${FILE_DATE}_${SESSION_ID}.jsonl"
            cp "$jsonl" "$TARGET" 2>/dev/null && COLLECTED=$((COLLECTED + 1))
        done
        if [[ $COLLECTED -gt 0 ]]; then
            echo "📋 Collected $COLLECTED missed session transcript(s) to user/sessions/"
        fi
    fi
fi

# --- Push user/ to server (backup + runtime for notifications, no --delete to preserve backups) ---
echo ""
echo "📤 Uploading user files to server..."
sync_to_server ./user user

# Backup CLAUDE.local.md to server home (disaster recovery)
if [[ -f "./CLAUDE.local.md" ]]; then
    if [[ -n "$DRY_RUN" ]]; then
        echo "  [dry-run] Would backup: ./CLAUDE.local.md -> data-analyst:~/CLAUDE.local.md"
    else
        scp -q ./CLAUDE.local.md "data-analyst:~/CLAUDE.local.md" && \
            echo "📝 CLAUDE.local.md backed up to server"
    fi
fi

# --- Sync corporate memory rules ---
# Rules are generated server-side based on user votes
echo ""
echo "📚 Syncing corporate memory rules..."
if [[ -z "$DRY_RUN" ]]; then
    mkdir -p .claude/rules
    if scp -rq "data-analyst:~/.claude_rules/"* .claude/rules/ 2>/dev/null; then
        RULES_COUNT=$(ls -1 .claude/rules/km_*.md 2>/dev/null | wc -l)
        echo "   ✅ $RULES_COUNT knowledge rules synced to .claude/rules/"
    else
        echo "   ℹ️  No corporate memory rules yet (upvote items in the portal)"
    fi
else
    echo "  [dry-run] Would sync corporate memory rules to .claude/rules/"
fi

# Sync Python environment to server (only if dependencies changed)
if [[ -z "$DRY_RUN" ]] && [[ -f "./.venv/bin/pip" ]]; then
    echo ""
    LOCAL_REQ="$(mktemp)"
    ./.venv/bin/pip freeze > "$LOCAL_REQ"
    LOCAL_HASH=$(md5sum "$LOCAL_REQ" 2>/dev/null | cut -d' ' -f1 || md5 -q "$LOCAL_REQ" 2>/dev/null || echo "none")
    REMOTE_HASH=$(ssh data-analyst "md5sum ~/.analyst_requirements.txt 2>/dev/null | cut -d' ' -f1 || echo 'missing'" 2>/dev/null || echo "missing")
    if [[ "$LOCAL_HASH" != "$REMOTE_HASH" ]]; then
        echo "Syncing Python environment to server..."
        scp "$LOCAL_REQ" "data-analyst:~/.analyst_requirements.txt"
        ssh data-analyst "test -d ~/.venv || python3 -m venv ~/.venv; ~/.venv/bin/pip install -r ~/.analyst_requirements.txt --quiet 2>&1 | tail -1"
        echo "Server Python environment synced"
    else
        echo "Server Python environment up to date"
    fi
    rm -f "$LOCAL_REQ"
fi

# Only update DuckDB and check freshness if NOT dry-run
if [[ -z "$DRY_RUN" ]]; then
    # Determine script location
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

    # Check if DuckDB is corrupted (can't open), delete if so
    DUCKDB_FILE="user/duckdb/analytics.duckdb"
    if [[ -f "$DUCKDB_FILE" ]]; then
        echo ""
        echo "🔍 Validating DuckDB..."
        if ! python -c "import duckdb; duckdb.connect('$DUCKDB_FILE').execute('SELECT 1')" 2>/dev/null; then
            echo "⚠️  DuckDB corrupted, will recreate from parquet files..."
            rm -f "$DUCKDB_FILE"
        else
            echo "   ✅ DuckDB OK"
        fi
    fi

    # Reinitialize DuckDB views (creates new DB if deleted, or updates views if exists)
    echo ""
    echo "🔄 Updating DuckDB views..."
    bash "${SCRIPT_DIR}/setup_views.sh"

    # Update sync timestamp on server (used by webapp Account card)
    ssh data-analyst "touch ~/server/" 2>/dev/null || true

    echo ""
    echo "✅ Data sync complete!"
    echo ""

else
    echo ""
    echo "🔍 Dry run complete - no changes made"
    echo ""
    echo "💡 To perform actual sync, run: bash server/scripts/sync_data.sh"
fi
