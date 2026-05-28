#!/usr/bin/env bash
# start.sh — entrypoint for the E2E Agnes container.
#
# Sequence:
#   1. Apply iptables OWNER egress rules so the sandboxed user (uid 1001)
#      can only reach 127.0.0.1 + api.anthropic.com + api.github.com.
#      Requires `--cap-add NET_ADMIN` on the docker-compose service.
#   2. Drop the test instance.yaml into /data/state so config/loader.py
#      picks it up.
#   3. Seed the analytics DuckDB with the SQL fixtures under sample-data/.
#   4. Boot uvicorn.

set -euo pipefail

echo "[start.sh] applying iptables OWNER rules"
/app/tests/e2e/iptables-setup.sh

echo "[start.sh] staging instance.yaml"
mkdir -p /data/state /data/marketplaces /data/analytics
cp /app/tests/e2e/instance.yaml.e2e /data/state/instance.yaml

echo "[start.sh] loading sample data into analytics DuckDB"
/opt/venv/bin/python /app/tests/e2e/load-sample-data.py

echo "[start.sh] starting uvicorn on 0.0.0.0:8000"
exec /opt/venv/bin/uvicorn app.main:app \
    --workers 1 \
    --host 0.0.0.0 \
    --port 8000
