#!/bin/bash

# update.sh - Data synchronization script
#
# This script performs:
# 1. Data synchronization from configured data source
# 2. DuckDB views reinitialization
#
# Note: Git pull and dependency updates are handled by deploy.sh (GitHub Actions)

set -e  # Exit on error

echo "🔄 AI Data Analyst - Data Update"
echo ""

# Check that we're in the correct folder (same check as config.py uses)
if [ ! -f "docs/data_description.md" ]; then
    echo "❌ Run script from project root (folder with docs/data_description.md)"
    exit 1
fi

# Note: Git pull and dependency updates are handled by deploy.sh (GitHub Actions)
# This script focuses only on data synchronization

# Activate virtual environment
# Supports both local (./.venv) and server (/opt/data-analyst/.venv) setups
echo ""
echo "1️⃣  Activating virtual environment..."
if [ -d ".venv" ]; then
    source .venv/bin/activate
    echo "   ✅ Virtual environment activated (local)"
elif [ -d "/opt/data-analyst/.venv" ]; then
    source /opt/data-analyst/.venv/bin/activate
    echo "   ✅ Virtual environment activated (server)"
else
    echo "   ❌ Virtual environment not found. Run init.sh first."
    exit 1
fi

# Data synchronization
echo ""
echo "2️⃣  Synchronizing data..."
echo ""

# Run data sync
if python3 -m src.data_sync; then
    echo ""
    echo "   ✅ Data synchronization complete"
else
    echo ""
    echo "   ❌ Data synchronization failed. Check logs above."
    exit 1
fi

# Generate data profiles (for catalog profiler)
echo ""
echo "3️⃣  Generating data profiles..."
if python3 -m src.profiler; then
    echo "   ✅ Data profiles generated"
else
    echo "   ⚠️  Data profiling failed (non-fatal). Check logs above."
    # Non-fatal: profiling failure should not break the pipeline
fi

# Done
echo ""
echo "✅ Data sync complete!"
echo ""
echo "💡 Parquet files are ready in data/parquet/"
echo "   To setup DuckDB views, run: ./scripts/setup_views.sh"
echo ""
