#!/bin/bash
# Helper script to activate virtual environment
# Usage: source scripts/activate_venv.sh

# Detect project root (should be run from project directory)
if [ ! -d ".venv" ]; then
    echo "❌ Virtual environment not found. Are you in the project directory?"
    echo "   Expected: A directory with .venv/ folder"
    return 1 2>/dev/null || exit 1
fi

# Activate based on platform
if [ -f ".venv/bin/activate" ]; then
    # Unix/macOS
    source .venv/bin/activate
    echo "✅ Virtual environment activated (Unix)"
elif [ -f ".venv/Scripts/activate" ]; then
    # Windows (Git Bash)
    source .venv/Scripts/activate
    echo "✅ Virtual environment activated (Windows)"
else
    echo "❌ Could not find activation script in .venv/"
    return 1 2>/dev/null || exit 1
fi

# Show Python version
echo "   Python: $(python --version 2>&1)"
echo "   Location: $(which python)"
