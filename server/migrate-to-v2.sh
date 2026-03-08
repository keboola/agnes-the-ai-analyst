#!/bin/bash
# One-time migration script: old directory structure -> server/ + user/ layout
#
# What it does:
# 1. Renames /data/user_scripts -> /data/scripts (if needed)
# 2. For each analyst user:
#    - Removes old symlinks (~/data, ~/docs, ~/user_scripts, ~/KICKOFF.md)
#    - Creates ~/server/ with symlinks to shared /data/
#    - Creates ~/user/ real directories (writable)
#    - Moves ~/workspace/notifications/* -> ~/user/notifications/
#    - Builds per-user DuckDB from shared parquet
#
# Usage: sudo bash server/migrate-to-v2.sh [--dry-run]
#
# Run this AFTER deploy.sh has installed new scripts to /data/scripts/

set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "=== DRY RUN MODE - no changes will be made ==="
    echo ""
fi

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (use sudo)"
    exit 1
fi

run_cmd() {
    if [[ "$DRY_RUN" == true ]]; then
        echo "  [dry-run] $*"
    else
        "$@"
    fi
}

echo "=== Migration to v2 directory structure ==="
echo ""

# Step 1: Rename /data/user_scripts -> /data/scripts
if [[ -d /data/user_scripts ]] && [[ ! -d /data/scripts ]]; then
    echo "Step 1: Renaming /data/user_scripts -> /data/scripts"
    run_cmd mv /data/user_scripts /data/scripts
elif [[ -d /data/scripts ]]; then
    echo "Step 1: /data/scripts already exists (OK)"
else
    echo "Step 1: WARNING - neither /data/user_scripts nor /data/scripts found"
    echo "         Make sure deploy.sh has run first."
fi
echo ""

# Step 2: Migrate user homes
echo "Step 2: Migrating user home directories"
echo ""

for user_home in /home/*/; do
    username=$(basename "$user_home")

    # Skip system/deploy users
    [[ "$username" == "deploy" ]] && continue
    [[ "$username" == "lost+found" ]] && continue

    # Skip if not a real user
    id "$username" &>/dev/null || continue

    echo "--- Migrating $username ---"

    # Check if already migrated
    if [[ -d "${user_home}server" ]] && [[ -d "${user_home}user" ]]; then
        echo "  Already migrated (server/ and user/ exist), skipping"
        echo ""
        continue
    fi

    # Remove old symlinks
    for old_link in "${user_home}data" "${user_home}docs" "${user_home}user_scripts" "${user_home}KICKOFF.md"; do
        if [[ -e "$old_link" ]] || [[ -L "$old_link" ]]; then
            echo "  Removing old: $old_link"
            run_cmd rm -f "$old_link"
        fi
    done

    # Create server/ directory with symlinks
    echo "  Creating server/ symlinks"
    run_cmd mkdir -p "${user_home}server"
    run_cmd ln -sf /data/docs "${user_home}server/docs"
    run_cmd ln -sf /data/scripts "${user_home}server/scripts"
    run_cmd ln -sf /data/docs/examples "${user_home}server/examples"
    run_cmd ln -sf /data/src_data/parquet "${user_home}server/parquet"
    run_cmd ln -sf /data/src_data/metadata "${user_home}server/metadata"

    # Create user/ real directories
    echo "  Creating user/ directories"
    run_cmd mkdir -p "${user_home}user/notifications"
    run_cmd mkdir -p "${user_home}user/artifacts"
    run_cmd mkdir -p "${user_home}user/scripts"
    run_cmd mkdir -p "${user_home}user/parquet"
    run_cmd mkdir -p "${user_home}user/duckdb"

    # Move existing workspace/notifications content
    if [[ -d "${user_home}workspace/notifications" ]]; then
        notification_count=$(find "${user_home}workspace/notifications" -name "*.py" 2>/dev/null | wc -l)
        if [[ "$notification_count" -gt 0 ]]; then
            echo "  Moving $notification_count notification script(s) from workspace/ to user/"
            run_cmd cp -r "${user_home}workspace/notifications/"* "${user_home}user/notifications/" 2>/dev/null || true
        fi
    fi

    # Fix ownership
    echo "  Setting ownership"
    run_cmd chown -R "${username}:${username}" "${user_home}server" "${user_home}user"

    # Build per-user DuckDB
    if [[ "$DRY_RUN" == false ]] && [[ -x /data/scripts/setup_views.sh ]]; then
        echo "  Building DuckDB database..."
        sudo -u "$username" bash -c "cd ${user_home} && /data/scripts/setup_views.sh" 2>&1 | sed 's/^/    /' || true
    else
        echo "  [dry-run] Would build DuckDB for $username"
    fi

    echo "  Done: $username"
    echo ""
done

# Step 3: Cleanup old workspace dirs (informational only)
echo "=== Migration complete ==="
echo ""
echo "Old ~/workspace/ directories were NOT removed (safety measure)."
echo "After verifying everything works, you can clean them up:"
echo ""
for user_home in /home/*/; do
    username=$(basename "$user_home")
    [[ "$username" == "deploy" ]] && continue
    id "$username" &>/dev/null || continue
    if [[ -d "${user_home}workspace" ]]; then
        echo "  rm -rf ${user_home}workspace"
    fi
done
echo ""
echo "Verification commands:"
echo "  ls -la /home/*/server/       # Check symlinks"
echo "  ls -la /home/*/user/duckdb/  # Check DuckDB per user"
echo "  ls -la /data/scripts/        # Check renamed scripts dir"
