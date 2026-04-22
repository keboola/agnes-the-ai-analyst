#!/usr/bin/env bash
# Run Agnes locally with auth bypass + dev-mode magic links.
#
# Stacks three compose files:
#   1. docker-compose.yml          — base services
#   2. docker-compose.override.yml — hot-reload + source bind mount (dev default)
#   3. docker-compose.local-dev.yml — LOCAL_DEV_MODE=1, drops .env requirement
#
# After startup visit http://localhost:8000 — you'll land on /dashboard
# logged in as dev@localhost (role=admin). No login screen, no email delivery needed.
set -euo pipefail

cd "$(dirname "$0")/.."

# Ensure docker-compose.yml does not require a .env file. We override env_file in the
# local-dev overlay, but compose still touches the file path during config validation.
if [[ ! -f .env ]]; then
  touch .env
fi

exec docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.local-dev.yml \
  up "$@"
