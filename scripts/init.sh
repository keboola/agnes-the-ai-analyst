#!/bin/bash

# init.sh - Initialization script for AI Data Analyst Tool
# 
# This script sets up the project for first use:
# 1. Creates Python virtual environment
# 2. Installs dependencies
# 3. Creates necessary folders
# 4. Copies .env template

set -e  # Exit on error

echo "Initializing AI Data Analyst..."
echo ""

# Check Python version
echo "1️⃣  Checking Python..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Install Python 3.8 or newer."
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "   ✅ Python version: $PYTHON_VERSION"

# Create virtual environment
echo ""
echo "2️⃣  Creating virtual environment..."
if [ -d ".venv" ]; then
    echo "   ⚠️  .venv already exists, skipping..."
else
    python3 -m venv .venv
    echo "   ✅ Virtual environment created"
fi

# Activate venv
echo ""
echo "3️⃣  Activating virtual environment..."
source .venv/bin/activate
echo "   ✅ Virtual environment activated"

# Upgrade pip
echo ""
echo "4️⃣  Upgrading pip..."
pip install --upgrade pip --quiet
echo "   ✅ pip upgraded"

# Install dependencies
echo ""
echo "5️⃣  Installing dependencies from requirements.txt..."
pip install -r requirements.txt --quiet
echo "   ✅ Dependencies installed"

# Create folders
echo ""
echo "6️⃣  Creating data folders..."

# Load DATA_DIR from .env (default: ./data)
if [ -f ".env" ]; then
    DATA_DIR=$(grep -E "^DATA_DIR=" .env | cut -d'=' -f2 | tr -d '"' | tr -d "'" || echo "./data")
fi
DATA_DIR="${DATA_DIR:-./data}"

mkdir -p "${DATA_DIR}/parquet"
mkdir -p "${DATA_DIR}/metadata"
mkdir -p "${DATA_DIR}/staging"
mkdir -p "${DATA_DIR}/duckdb"
echo "   ✅ Folders created in ${DATA_DIR}:"
echo "      - parquet/ (Parquet files - subfolders created automatically)"
echo "      - metadata/ (sync state, cache)"
echo "      - staging/ (temporary files during sync)"
echo "      - duckdb/ (DuckDB database)"

# Copy .env template
echo ""
echo "7️⃣  Setting up .env file..."
if [ -f ".env" ]; then
    echo "   ⚠️  .env already exists, keeping it..."
else
    cp config/.env.template .env
    echo "   ✅ .env created from template"
    echo ""
    echo "   How to set up:"
    echo "   1. Copy config/instance.yaml.example to config/instance.yaml"
    echo "   2. Fill in your data source credentials in .env"
    echo "   3. Configure your instance settings in config/instance.yaml"
    echo ""
fi

# Test configuration
echo ""
echo "8️⃣  Testing configuration..."
if python3 -c "from src.config import get_config; get_config()" 2>/dev/null; then
    echo "   ✅ Configuration is valid"
else
    echo "   ⚠️  Configuration not complete yet (expected - fill in .env)"
fi

# Done
echo ""
echo "✅ Initialization complete!"
echo ""
echo "Next steps:"
echo "   1. Configure config/instance.yaml and .env"
echo "   2. Run './scripts/update.sh' to sync data"
echo "   3. Use Claude Code to analyze the data!"
echo ""
echo "💡 Tip: Activate virtual environment with 'source .venv/bin/activate'"
echo ""
