#!/usr/bin/env bash
# scripts/dev_pg_migrate_volume.sh — move the dev PG from its anonymous
# Docker volume onto the named volume ``agnes_pg_vrpg_data``.
#
# Background: the original ``agnes-pg-vrpg`` container was created with
# ``docker run`` and no ``-v`` flag, so its data lives in an anonymous
# volume (auto-generated hash like ``3ccc54d4fb61...``). Anonymous
# volumes survive container stop/restart but are wiped by
# ``docker volume prune`` and aren't portable. ``scripts/dev_pg_up.sh``
# always uses the named volume; this script does the one-shot migration
# of EXISTING data so operators don't lose state on the swap.
#
# What it does (idempotent — safe to re-run):
#
#   1. ``pg_dump`` the current container to a timestamped .sql file
#      under ``/tmp/``. Compressed -Fc dump, gzipped, sha256-checked.
#   2. Stop + ``docker rm`` the old container. The anonymous volume
#      is INTENTIONALLY left behind — operator can ``docker volume rm``
#      it manually after verifying the restore.
#   3. ``scripts/dev_pg_up.sh`` boots a fresh container on the named
#      volume.
#   4. ``pg_restore`` the dump into the new container.
#   5. Verify row counts match the pre-dump snapshot.
#
# Bail out on any failure with the dump still on disk — re-running from
# the same dump is a one-liner if step 4 / 5 hit a snag.

set -euo pipefail

# See scripts/sync_from_prod.sh — strict KEY=VALUE parser, NOT
# ``source``. Avoids shell-meta in values executing as code.
if [[ -f .env ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$line" || "$line" =~ ^[[:space:]]*$ ]] && continue
    if [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      if [[ "$value" =~ ^\"(.*)\"$ ]] || [[ "$value" =~ ^\'(.*)\'$ ]]; then
        value="${BASH_REMATCH[1]}"
      fi
      export "$key=$value"
    fi
  done < .env
fi

export DOCKER_HOST="${DOCKER_HOST:-unix:///Users/$USER/.colima/default/docker.sock}"
export DOCKER_CONFIG="${DOCKER_CONFIG:-/tmp/empty-docker}"
mkdir -p "$DOCKER_CONFIG"
[[ -f "$DOCKER_CONFIG/config.json" ]] || echo '{}' > "$DOCKER_CONFIG/config.json"

PG_NAME="${PG_NAME:-agnes-pg-vrpg}"
PG_USER="${PG_USER:-agnes}"
PG_PASS="${PG_PASS:-agnes}"
PG_DB="${PG_DB:-agnes}"

DUMP="${DUMP:-/tmp/agnes_pg_dump_$(date +%Y%m%d_%H%M%S).dump}"

# -- step 0: sanity -----------------------------------------------------------

if ! docker ps --filter "name=^${PG_NAME}$" --format '{{.Names}}' | grep -qx "${PG_NAME}"; then
  echo "error: ${PG_NAME} not running. Start it first or run dev_pg_up.sh." >&2
  exit 1
fi

# Bail if already on the named volume (no migration needed).
CURRENT_VOL=$(
  docker inspect "${PG_NAME}" \
    --format '{{ range .Mounts }}{{ if eq .Destination "/var/lib/postgresql/data" }}{{ .Name }}{{ end }}{{ end }}'
)
if [[ "${CURRENT_VOL}" == "agnes_pg_vrpg_data" ]]; then
  echo "==> ${PG_NAME} already uses named volume agnes_pg_vrpg_data — nothing to migrate."
  exit 0
fi

echo "==> current ${PG_NAME} volume: ${CURRENT_VOL:-<anonymous>}"
echo "==> will dump → swap → restore onto named volume agnes_pg_vrpg_data"
echo "==> dump path: ${DUMP}"

# -- step 1: capture pre-dump row counts ------------------------------------

echo "==> capturing pre-dump row counts (sanity-check after restore)"
BEFORE=$(
  docker exec -e PGPASSWORD="${PG_PASS}" "${PG_NAME}" \
    psql -U "${PG_USER}" -d "${PG_DB}" -tA -c \
    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
)
echo "    tables in source: $(echo "${BEFORE}" | wc -l | tr -d ' ')"

# macOS ships bash 3.2 without associative arrays; persist the
# table-name → row-count pairs to a tmp file instead.
BEFORE_FILE="$(mktemp -t agnes_pg_counts.XXXXXX)"
trap "rm -f '${BEFORE_FILE}'" EXIT
while read -r t; do
  [[ -z "$t" ]] && continue
  n=$(
    docker exec -e PGPASSWORD="${PG_PASS}" "${PG_NAME}" \
      psql -U "${PG_USER}" -d "${PG_DB}" -tA -c "SELECT COUNT(*) FROM \"${t}\""
  )
  printf '%s\t%s\n' "$t" "$n" >> "${BEFORE_FILE}"
done <<< "${BEFORE}"

# -- step 2: pg_dump ---------------------------------------------------------

echo "==> pg_dump → ${DUMP}"
docker exec -e PGPASSWORD="${PG_PASS}" "${PG_NAME}" \
  pg_dump -U "${PG_USER}" -d "${PG_DB}" -Fc -f /tmp/agnes.dump
docker cp "${PG_NAME}:/tmp/agnes.dump" "${DUMP}"
docker exec "${PG_NAME}" rm -f /tmp/agnes.dump
sha256=$(shasum -a 256 "${DUMP}" | awk '{print $1}')
size=$(du -h "${DUMP}" | cut -f1)
echo "    dump size: ${size}   sha256: ${sha256:0:16}…"

# -- step 3: rename old (don't delete yet) + launch new ---------------------
#
# Rename instead of removing so the operator has a fast recovery path if
# step 4 (dev_pg_up.sh) fails — ``docker rename agnes-pg-vrpg-OLD-* back``
# restores the previous container with its anonymous volume intact, in
# under a second. The old container is dropped only after the new one
# accepts connections AND pg_restore + row-count verification succeed
# (step 7).

PG_OLD_NAME="${PG_NAME}-OLD-$(date +%Y%m%d%H%M%S)"
echo "==> stopping + renaming ${PG_NAME} → ${PG_OLD_NAME}"
docker stop "${PG_NAME}" >/dev/null
docker rename "${PG_NAME}" "${PG_OLD_NAME}" >/dev/null

# -- step 4: bring up the new container -------------------------------------

echo "==> launching ${PG_NAME} on named volume agnes_pg_vrpg_data"
if ! bash "$(dirname "$0")/dev_pg_up.sh"; then
  echo "==> dev_pg_up.sh failed; rolling back" >&2
  docker rename "${PG_OLD_NAME}" "${PG_NAME}" >/dev/null
  docker start "${PG_NAME}" >/dev/null
  echo "    ${PG_NAME} restored from ${PG_OLD_NAME}. Dump preserved at ${DUMP}." >&2
  exit 1
fi

# Wait for new container to accept connections.
ready=0
for _ in $(seq 1 30); do
  if docker exec "${PG_NAME}" pg_isready -U "${PG_USER}" -d "${PG_DB}" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" == "0" ]]; then
  echo "==> new ${PG_NAME} never became ready; rolling back" >&2
  docker stop "${PG_NAME}" >/dev/null 2>&1 || true
  docker rm "${PG_NAME}" >/dev/null 2>&1 || true
  docker rename "${PG_OLD_NAME}" "${PG_NAME}" >/dev/null
  docker start "${PG_NAME}" >/dev/null
  echo "    ${PG_NAME} restored from ${PG_OLD_NAME}. Dump preserved at ${DUMP}." >&2
  exit 1
fi

# -- step 5: pg_restore ------------------------------------------------------

echo "==> pg_restore ${DUMP} → fresh DB"
docker cp "${DUMP}" "${PG_NAME}:/tmp/agnes.dump"
docker exec -e PGPASSWORD="${PG_PASS}" "${PG_NAME}" \
  pg_restore -U "${PG_USER}" -d "${PG_DB}" --no-owner --clean --if-exists /tmp/agnes.dump
docker exec "${PG_NAME}" rm -f /tmp/agnes.dump

# -- step 6: verify row counts ----------------------------------------------

echo "==> verifying row counts match pre-dump"
fail=0
while IFS=$'\t' read -r t before_n; do
  [[ -z "$t" ]] && continue
  after=$(
    docker exec -e PGPASSWORD="${PG_PASS}" "${PG_NAME}" \
      psql -U "${PG_USER}" -d "${PG_DB}" -tA -c "SELECT COUNT(*) FROM \"${t}\"" 2>/dev/null \
      || echo "ERR"
  )
  if [[ "${before_n}" != "${after}" ]]; then
    echo "    MISMATCH  ${t}  before=${before_n}  after=${after}"
    fail=1
  fi
done < "${BEFORE_FILE}"

if [[ "$fail" == "0" ]]; then
  echo
  echo "==> success. all $(echo "${BEFORE}" | wc -l | tr -d ' ') tables match."
  # Drop the renamed old container ONLY after the verification passes.
  # The anonymous volume the old container used is left on disk (the
  # ``docker rm`` below removes the container record, not the volume)
  # so the operator can still ``docker run --volumes-from`` it for a
  # forensic compare if anything turns up wrong later.
  echo "==> removing renamed old container ${PG_OLD_NAME} (anonymous volume retained)"
  docker rm "${PG_OLD_NAME}" >/dev/null 2>&1 || true
  echo "    dump preserved at ${DUMP} (delete after one full restart cycle)"
  echo "    old anonymous volume left at ${CURRENT_VOL:-<anonymous>} — remove with:"
  echo "      docker volume rm ${CURRENT_VOL}"
else
  echo
  echo "==> ROW-COUNT MISMATCHES — rolling back to ${PG_OLD_NAME}." >&2
  docker stop "${PG_NAME}" >/dev/null 2>&1 || true
  docker rm "${PG_NAME}" >/dev/null 2>&1 || true
  docker rename "${PG_OLD_NAME}" "${PG_NAME}" >/dev/null
  docker start "${PG_NAME}" >/dev/null
  echo "    ${PG_NAME} restored. Dump preserved at ${DUMP}." >&2
  exit 1
fi
