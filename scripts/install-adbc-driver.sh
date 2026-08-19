#!/usr/bin/env bash
# Install the ADBC Snowflake driver into DuckDB's extension directory.
#
# DuckDB's ``snowflake`` community extension needs the Apache Arrow ADBC
# Snowflake shared library on disk, but it searches only the DuckDB extension
# directory (``~/.duckdb/extensions/v<version>/<platform>/``) and a few
# system library paths. The ``adbc-driver-snowflake`` Python package ships
# the correct library, so this script copies it to the directory DuckDB
# actually searches.
#
# Run from the repo root, or pass the repo root as the first argument.
set -euo pipefail

REPO_ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON="${AGNES_PYTHON:-}"

if [ -z "$PYTHON" ] && [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
fi

if [ -z "$PYTHON" ]; then
    PYTHON="$(command -v python3 || true)"
fi

if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
    echo "ERROR: Python interpreter not found. Set AGNES_PYTHON or create a .venv." >&2
    exit 1
fi

# Make adbc-driver-snowflake available before we import it.
if ! "$PYTHON" -c "import adbc_driver_snowflake" 2>/dev/null; then
    echo "adbc-driver-snowflake not found; installing it..." >&2
    "$PYTHON" -m pip install adbc-driver-snowflake
fi

PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}" "$PYTHON" - <<'PY'
from connectors.snowflake.attach import install_snowflake_adbc_driver
install_snowflake_adbc_driver(missing_ok=False)
print("ADBC Snowflake driver installed in DuckDB extension directory.")
PY
