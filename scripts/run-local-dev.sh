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

# Default LOCAL_DEV_GROUPS so /profile and group-aware code see *something* on
# first boot. Operators can override (LOCAL_DEV_GROUPS='[...]' make local-dev)
# or disable (LOCAL_DEV_GROUPS= make local-dev). See docs/local-development.md.
#
# Indirection through DEFAULT_LOCAL_DEV_GROUPS dodges the parameter-expansion
# gotcha where a literal `}` inside `${VAR:=default}` closes the expansion
# early — silently truncating the JSON to the first group and producing an
# unparseable value. The single-quoted variable holds the JSON intact.
#
# `${VAR-DEFAULT}` (no `:`) substitutes only when VAR is *unset*, not when it
# is set-but-empty. The empty-value path is documented as the "disable" knob —
# `LOCAL_DEV_GROUPS= make local-dev` must reach the parser as "" so the
# get_local_dev_groups() short-circuit returns []. The `:-` form would
# silently substitute the default on empty, breaking that contract.
DEFAULT_LOCAL_DEV_GROUPS='[{"id":"local-dev-engineers@example.com","name":"Local Dev Engineers"},{"id":"local-dev-admins@example.com","name":"Local Dev Admins"}]'
export LOCAL_DEV_GROUPS="${LOCAL_DEV_GROUPS-$DEFAULT_LOCAL_DEV_GROUPS}"

exec docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.local-dev.yml \
  up "$@"
