#!/bin/bash
# Remote Query - wrapper for src.remote_query
#
# Runs DuckDB queries spanning local Parquet + remote BigQuery tables.
# Sets up the correct environment (PYTHONPATH, CONFIG_DIR, .env) automatically.
#
# Usage (via SSH from analyst machine):
#   ssh <alias> 'bash ~/server/scripts/remote_query.sh \
#     --register-bq "traffic=SELECT ... FROM \`project.dataset.table\` WHERE ... GROUP BY ..." \
#     --sql "SELECT * FROM traffic ORDER BY ..." \
#     --format table'
#
# All arguments are passed directly to python -m src.remote_query.
# See: python -m src.remote_query --help

set -e

APP_DIR="/opt/data-analyst"

# Load environment (BQ project, location, etc.)
set -a
source "${APP_DIR}/.env"
set +a

# Run remote_query with correct paths
cd "${APP_DIR}"
PYTHONPATH="${APP_DIR}/repo" \
CONFIG_DIR="${APP_DIR}/instance/config" \
exec "${APP_DIR}/.venv/bin/python3" -m src.remote_query "$@"
