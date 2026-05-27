#!/bin/bash
# Host-side daemon. Reads /data/state/db-state-target.flag and
# /data/state/instance.yaml to determine desired compose lifecycle;
# applies docker compose changes. Runs every 30s via systemd timer.
#
# Behavior:
#   - flag = "duckdb"            → ensure postgres container NOT in COMPOSE_FILE
#   - flag = "side-car-enabled"  → ensure postgres.yml + postgres-host-mount.yml
#                                   in COMPOSE_FILE; docker compose up -d postgres
#   - flag = "cloud-only"        → remove postgres.yml from COMPOSE_FILE;
#                                   docker compose stop postgres + docker rm postgres
#
# Idempotent: if current state matches desired, no-op.
set -euo pipefail

FLAG=/data/state/db-state-target.flag
COMPOSE_DIR=/opt/agnes

if [ ! -f "$FLAG" ]; then
    # No flag yet → default duckdb (no postgres overlay)
    exit 0
fi

TARGET="$(tr -d '[:space:]' < "$FLAG")"

cd "$COMPOSE_DIR"
# shellcheck disable=SC1091
set -a; . "$COMPOSE_DIR/.env"; set +a

NEW_COMPOSE_FILE=""

case "$TARGET" in
    duckdb|cloud-only)
        # Strip postgres.yml + postgres-host-mount.yml from COMPOSE_FILE
        NEW_COMPOSE_FILE=$(echo "$COMPOSE_FILE" | tr ':' '\n' | \
            grep -vE 'docker-compose\.(postgres|postgres-host-mount)\.yml$' | \
            tr '\n' ':' | sed 's/:$//')

        if [ "$TARGET" = "cloud-only" ]; then
            docker compose stop postgres 2>/dev/null || true
            docker compose rm -f postgres 2>/dev/null || true
        fi
        ;;
    side-car-enabled)
        NEW_COMPOSE_FILE="$COMPOSE_FILE"
        if ! echo "$COMPOSE_FILE" | grep -q "docker-compose.postgres.yml"; then
            NEW_COMPOSE_FILE="${NEW_COMPOSE_FILE}:docker-compose.postgres.yml"
        fi
        if ! echo "$COMPOSE_FILE" | grep -q "docker-compose.postgres-host-mount.yml"; then
            NEW_COMPOSE_FILE="${NEW_COMPOSE_FILE}:docker-compose.postgres-host-mount.yml"
        fi

        # Ensure /data/postgres exists with uid 70 ownership
        mkdir -p /data/postgres
        chown 70:70 /data/postgres
        chmod 700 /data/postgres

        # Update .env COMPOSE_FILE line
        sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$NEW_COMPOSE_FILE|" "$COMPOSE_DIR/.env"

        export COMPOSE_FILE="$NEW_COMPOSE_FILE"
        docker compose up -d postgres
        ;;
    *)
        logger -t agnes-state-applier "Unknown target: $TARGET — ignoring"
        exit 0
        ;;
esac

# If COMPOSE_FILE changed, also recreate app + scheduler
if [ "${COMPOSE_FILE:-}" != "${NEW_COMPOSE_FILE:-}" ]; then
    sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$NEW_COMPOSE_FILE|" "$COMPOSE_DIR/.env"
    export COMPOSE_FILE="$NEW_COMPOSE_FILE"
    docker compose up -d --force-recreate app scheduler
fi
