#!/bin/bash

# setup_views.sh - DuckDB views initialization script
#
# This script performs:
# 1. DuckDB views initialization from Parquet files
#
# Run this after syncing Parquet files (either via update.sh or rsync)

set -e  # Exit on error

echo "🦆 DuckDB Views Setup"
echo ""

# Check that we're in the correct folder
# Support both local (server/docs/) and server (~/ with server/ symlinks) layouts
if [ ! -f "server/docs/data_description.md" ] && [ ! -f "docs/data_description.md" ]; then
    echo "❌ Run script from project root (folder with server/docs/data_description.md)"
    exit 1
fi

# Activate virtual environment
echo "1️⃣  Activating virtual environment..."
if [ -d ".venv" ]; then
    # Try Unix-style activation first
    if [ -f ".venv/bin/activate" ]; then
        source .venv/bin/activate
    # Windows-style activation
    elif [ -f ".venv/Scripts/activate" ]; then
        source .venv/Scripts/activate
    else
        echo "   ❌ Virtual environment activation script not found."
        exit 1
    fi
    echo "   ✅ Virtual environment activated"
elif [ -d "venv" ]; then
    # Legacy support for old venv name
    if [ -f "venv/bin/activate" ]; then
        source venv/bin/activate
    elif [ -f "venv/Scripts/activate" ]; then
        source venv/Scripts/activate
    else
        echo "   ❌ Virtual environment activation script not found."
        exit 1
    fi
    echo "   ✅ Virtual environment activated (legacy venv)"
else
    echo "   ❌ Virtual environment not found (.venv or venv)."
    echo "   Run bootstrap setup first."
    exit 1
fi

# Initialize DuckDB views
echo ""
echo "2️⃣  Initializing DuckDB views..."
echo ""

# Use python3 if available, otherwise python (Windows compatibility)
if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD=python3
else
    PYTHON_CMD=python
fi

# Determine scripts location (local: server/scripts/, server deploy: same dir as this script)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DUCKDB_MANAGER="${SCRIPT_DIR}/duckdb_manager.py"

if $PYTHON_CMD "$DUCKDB_MANAGER" --reinit; then
    echo ""
    echo "   ✅ DuckDB views initialized"
else
    echo ""
    echo "   ❌ DuckDB initialization failed. Check logs above."
    exit 1
fi

# Optional dataset views (each sync script manages its own views)
if [ -d "server/parquet/jira" ]; then
    JIRA_SCRIPT="${SCRIPT_DIR}/sync_jira.sh"
    if [ -f "$JIRA_SCRIPT" ]; then
        echo ""
        echo "3️⃣  Creating Jira views..."
        bash "$JIRA_SCRIPT" --views-only
    fi
fi

# Display sync state
echo ""
echo "📊 Data state:"
# Check for sync state in either location
SYNC_STATE=""
if [ -f "server/metadata/sync_state.json" ]; then
    SYNC_STATE="server/metadata/sync_state.json"
elif [ -f "data/metadata/sync_state.json" ]; then
    SYNC_STATE="data/metadata/sync_state.json"
fi

if [ -n "$SYNC_STATE" ]; then
    $PYTHON_CMD << PYTHON
import json
from datetime import datetime

with open("$SYNC_STATE", "r") as f:
    state = json.load(f)

print(f"\n   Last update: {state.get('last_updated', 'N/A')}")
print(f"   Tables: {len(state.get('tables', {}))}\n")

for table_id, table_state in state.get('tables', {}).items():
    table_name = table_state.get('table_name', table_id)
    rows = table_state.get('rows', 0)
    size_mb = table_state.get('file_size_mb', 0)
    strategy = table_state.get('strategy', 'N/A')

    print(f"   - {table_name}: {rows:,} rows, {size_mb:.2f} MB ({strategy})")
PYTHON
fi

# Done
echo ""
echo "✅ Setup complete!"
echo ""
echo "💡 Data is ready for analysis with Claude Code"
echo "   DuckDB database: user/duckdb/analytics.duckdb"
echo ""
