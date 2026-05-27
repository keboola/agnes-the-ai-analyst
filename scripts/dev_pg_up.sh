#!/usr/bin/env bash
# scripts/dev_pg_up.sh — bring up the local dev stack.
#
# What this provisions:
#
#   1. ``agnes-pg-vrpg`` (postgres:16-alpine) — bound to a NAMED Docker
#      volume ``agnes_pg_vrpg_data`` so the DB survives ``docker rm``.
#      The earlier anonymous-volume form (``docker run`` with no -v) is
#      one ``docker volume prune`` away from a silent wipe.
#   2. ``agnes-pgweb`` (sosedoff/pgweb:latest) — web browser at
#      http://127.0.0.1:8081 pointed at the host's PG via
#      host.docker.internal.
#
# Idempotent: if either container is already running, it is reused.
# If the named volume already exists, the data inside is preserved
# (PG re-init only happens when the volume is empty).
#
# Pairs with ``scripts/sync_from_prod.sh`` for repeatable bootstrap:
#
#     bash scripts/dev_pg_up.sh
#     .venv/bin/alembic upgrade head
#     bash scripts/sync_from_prod.sh    # optional: seed from prod
#     .venv/bin/uvicorn app.main:app --port 8001

set -euo pipefail

# Use colima's docker daemon explicitly. Skips host Docker.app +
# avoids the osxkeychain credstore prompt some operators hit.
export DOCKER_HOST="${DOCKER_HOST:-unix:///Users/$USER/.colima/default/docker.sock}"
export DOCKER_CONFIG="${DOCKER_CONFIG:-/tmp/empty-docker}"
mkdir -p "$DOCKER_CONFIG"
[[ -f "$DOCKER_CONFIG/config.json" ]] || echo '{}' > "$DOCKER_CONFIG/config.json"

PG_NAME="${PG_NAME:-agnes-pg-vrpg}"
PG_VOLUME="${PG_VOLUME:-agnes_pg_vrpg_data}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-agnes}"
PG_PASS="${PG_PASS:-agnes}"
PG_DB="${PG_DB:-agnes}"

PGWEB_NAME="${PGWEB_NAME:-agnes-pgweb}"
PGWEB_PORT="${PGWEB_PORT:-8081}"

is_running() {
  docker ps --filter "name=^${1}$" --format '{{.Names}}' | grep -qx "$1"
}

is_present() {
  docker ps -a --filter "name=^${1}$" --format '{{.Names}}' | grep -qx "$1"
}

PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"

echo "==> ensuring named volume ${PG_VOLUME}"
docker volume inspect "${PG_VOLUME}" >/dev/null 2>&1 || \
  docker volume create "${PG_VOLUME}" >/dev/null

# Verify an existing container matches expectations before reusing it.
# Three failure modes the adversarial review surfaced:
#   1) different image tag (postgres:15 vs 16 → data_directory layout
#      drift, silent corruption when started against PG16's expectations).
#   2) different volume mount (anonymous volume from an earlier
#      ``docker run`` → bypasses the named-volume durability story).
#   3) different bound port (operator manually moved 5432→5433 → .env's
#      AGNES_DB_URL stops resolving without a clear error).
# Bail loudly with the swap command instead of pretending nothing's wrong.
_assert_container_matches() {
  local name="$1"
  local actual_image actual_volume actual_port
  actual_image=$(docker inspect "$name" --format '{{ .Config.Image }}' 2>/dev/null || echo "")
  actual_volume=$(
    docker inspect "$name" --format \
      '{{ range .Mounts }}{{ if eq .Destination "/var/lib/postgresql/data" }}{{ .Name }}{{ end }}{{ end }}' \
      2>/dev/null || echo ""
  )
  actual_port=$(
    docker inspect "$name" --format \
      '{{ (index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort }}' \
      2>/dev/null || echo ""
  )

  local fail=0
  if [[ "$actual_image" != "$PG_IMAGE" ]]; then
    echo "    image mismatch: container=$actual_image expected=$PG_IMAGE" >&2
    fail=1
  fi
  if [[ -n "$actual_volume" && "$actual_volume" != "$PG_VOLUME" ]]; then
    echo "    volume mismatch: container=$actual_volume expected=$PG_VOLUME" >&2
    fail=1
  fi
  if [[ -n "$actual_port" && "$actual_port" != "$PG_PORT" ]]; then
    echo "    port mismatch: container=$actual_port expected=$PG_PORT" >&2
    fail=1
  fi
  if [[ "$fail" == "1" ]]; then
    cat >&2 <<EOF

==> ${name} exists but doesn't match the expected shape.
    If you want to swap it onto the named volume + current image,
    run ``scripts/dev_pg_migrate_volume.sh`` (which pg_dumps → rm →
    pg_restore). If you want the existing container as-is, set
    PG_IMAGE / PG_VOLUME / PG_PORT in the environment to match.
EOF
    exit 1
  fi
}

if is_running "${PG_NAME}"; then
  echo "==> ${PG_NAME} already running — verifying image/volume/port"
  _assert_container_matches "${PG_NAME}"
  echo "    matches expectations — reusing"
else
  if is_present "${PG_NAME}"; then
    echo "==> ${PG_NAME} exists but stopped — verifying image/volume/port"
    _assert_container_matches "${PG_NAME}"
    echo "    matches — starting"
    docker start "${PG_NAME}" >/dev/null
  else
    echo "==> launching ${PG_NAME} (image ${PG_IMAGE})"
    docker run -d \
      --name "${PG_NAME}" \
      -e "POSTGRES_USER=${PG_USER}" \
      -e "POSTGRES_PASSWORD=${PG_PASS}" \
      -e "POSTGRES_DB=${PG_DB}" \
      -v "${PG_VOLUME}:/var/lib/postgresql/data" \
      -p "127.0.0.1:${PG_PORT}:5432" \
      --restart unless-stopped \
      "${PG_IMAGE}" >/dev/null
  fi
fi

# Wait for PG to accept connections before launching pgweb.
echo "==> waiting for PG to be ready"
for _ in $(seq 1 30); do
  if docker exec "${PG_NAME}" pg_isready -U "${PG_USER}" -d "${PG_DB}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if is_running "${PGWEB_NAME}"; then
  echo "==> ${PGWEB_NAME} already running — reusing"
else
  if is_present "${PGWEB_NAME}"; then
    echo "==> ${PGWEB_NAME} exists but stopped — starting"
    docker start "${PGWEB_NAME}" >/dev/null
  else
    echo "==> launching ${PGWEB_NAME}"
    docker run -d \
      --name "${PGWEB_NAME}" \
      -p "127.0.0.1:${PGWEB_PORT}:8081" \
      --add-host=host.docker.internal:host-gateway \
      sosedoff/pgweb:latest \
      pgweb --bind=0.0.0.0 --listen=8081 \
      --url="postgresql://${PG_USER}:${PG_PASS}@host.docker.internal:${PG_PORT}/${PG_DB}?sslmode=disable" >/dev/null
  fi
fi

echo
echo "==> ready"
echo "    PG:     127.0.0.1:${PG_PORT}  (volume: ${PG_VOLUME} — persisted)"
echo "    pgweb:  http://127.0.0.1:${PGWEB_PORT}"
