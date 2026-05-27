#!/usr/bin/env bash
# scripts/sync_from_prod.sh — repeatable prod → local-PG snapshot.
#
# What it does, end-to-end:
#
#   1. gcloud compute scp from foundryai-production:/data/state/system.duckdb
#      → /tmp/prod_system.duckdb  (via IAP tunnel; no public access path)
#   2. (Optional) back up the existing local data/state/system.duckdb so a
#      bad migration can be reverted by hand.
#   3. ``alembic upgrade head`` so the local PG schema matches the latest
#      migration (no-op if already up-to-date).
#   4. ``python -m scripts.migrate_duckdb_to_pg`` against the snapshot,
#      scoped to the table list the user actually cares about (data
#      tables, curated marketplace, flea market, knowledge memory, plus
#      the v49+ catalog cluster). Idempotent on the PG primary keys
#      (``ON CONFLICT DO NOTHING``).
#
# Usage:
#
#   bash scripts/sync_from_prod.sh                # full sync, default scope
#   bash scripts/sync_from_prod.sh --scope=minimal # only table_registry + marketplaces
#   bash scripts/sync_from_prod.sh --no-scp        # reuse /tmp/prod_system.duckdb
#
# Authorization: each operator gates the gcloud IAP path with their own
# Workforce Pool identity. The script does NOT cache or print credentials.
#
# Safety: this script is READ-ONLY against production — only the
# ``gcloud compute scp`` step touches the prod host, and only to pull
# the live snapshot. No writes, no SSH commands beyond the implicit
# file-read needed for the scp.

set -euo pipefail

# Pull AGNES_DB_URL + colima/PG creds from the project ``.env`` so each
# step (alembic, migrate) connects to the same PG without the operator
# having to remember the export incantation.
#
# Do NOT ``source .env`` — that runs the file as bash and any shell
# meta in a value (``#``, ``&``, spaces, ``$()``, backticks) truncates
# the variable, backgrounds a process, or executes a subshell. Use a
# strict KEY=VALUE parser instead: only lines that match
# ``^[A-Z_][A-Z0-9_]*=`` are exported, the value is taken verbatim with
# no expansion, comments + blank lines are dropped.
if [[ -f .env ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$line" || "$line" =~ ^[[:space:]]*$ ]] && continue
    if [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      # Strip surrounding double or single quotes if present, no
      # expansion inside (no $VAR / $() / backticks).
      if [[ "$value" =~ ^\"(.*)\"$ ]] || [[ "$value" =~ ^\'(.*)\'$ ]]; then
        value="${BASH_REMATCH[1]}"
      fi
      export "$key=$value"
    fi
  done < .env
fi

# -- knobs -------------------------------------------------------------------

PROD_VM="${PROD_VM:-foundryai-production}"
PROD_PROJECT="${PROD_PROJECT:-prj-grp-foundryai-dev-7c37}"
PROD_ZONE="${PROD_ZONE:-us-central1-a}"
PROD_DUCKDB_PATH="${PROD_DUCKDB_PATH:-/data/state/system.duckdb}"
LOCAL_SNAPSHOT="${LOCAL_SNAPSHOT:-/tmp/prod_system.duckdb}"

# -- argparse ----------------------------------------------------------------

scope="default"
do_scp=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope=*) scope="${1#*=}"; shift ;;
    --no-scp) do_scp=0; shift ;;
    -h|--help)
      sed -n '2,30p' "$0"
      exit 0
      ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

# -- scope → --only list ------------------------------------------------------

case "$scope" in
  minimal)
    ONLY=(
      table_registry
      marketplace_registry marketplace_plugins
      store_entities store_submissions
    )
    ;;
  default)
    ONLY=(
      # ops/registry
      table_registry
      # curated marketplace
      marketplace_registry marketplace_plugins
      # flea market
      store_entities store_submissions
      # knowledge memory + v49 catalog cluster (must follow table_registry
      # + knowledge_items because data_package_tables and
      # knowledge_item_domains have FKs into them).
      knowledge_items knowledge_contradictions knowledge_item_relations
      knowledge_votes knowledge_item_user_dismissed
      data_packages data_package_tables
      memory_domains knowledge_item_domains
      memory_domain_suggestions recipes
      user_stack_subscriptions
    )
    ;;
  full)
    # Migrate every task the script knows about. Includes audit_log,
    # usage_*, and the rest of the per-instance state. Heavy.
    ONLY=()
    ;;
  *)
    echo "unknown --scope=$scope (valid: minimal|default|full)" >&2
    exit 2
    ;;
esac

# -- step 1: scp from prod ----------------------------------------------------

if [[ "$do_scp" == "1" ]]; then
  echo "==> snapshotting ${PROD_VM}:${PROD_DUCKDB_PATH} → ${LOCAL_SNAPSHOT}"
  gcloud compute scp \
    --project="${PROD_PROJECT}" \
    --zone="${PROD_ZONE}" \
    --tunnel-through-iap \
    "${PROD_VM}:${PROD_DUCKDB_PATH}" \
    "${LOCAL_SNAPSHOT}"
else
  echo "==> reusing existing snapshot ${LOCAL_SNAPSHOT} (--no-scp)"
fi

[[ -f "${LOCAL_SNAPSHOT}" ]] || { echo "no snapshot at ${LOCAL_SNAPSHOT}" >&2; exit 1; }
echo "    snapshot size: $(du -h "${LOCAL_SNAPSHOT}" | cut -f1)"

# -- step 2: alembic upgrade head --------------------------------------------

echo "==> alembic upgrade head"
.venv/bin/alembic upgrade head

# -- step 3: migrate the snapshot --------------------------------------------

echo "==> migrating snapshot → local PG"
ONLY_ARGS=()
for t in "${ONLY[@]}"; do ONLY_ARGS+=("--only" "$t"); done

.venv/bin/python -m scripts.migrate_duckdb_to_pg \
  --duckdb-path "${LOCAL_SNAPSHOT}" \
  "${ONLY_ARGS[@]}"

echo
echo "==> done. counts:"
.venv/bin/python - <<'PY'
import os
import sqlalchemy as sa
os.environ.setdefault(
    "AGNES_DB_URL",
    open(".env").read().split("AGNES_DB_URL=", 1)[1].split("\n", 1)[0],
)
from src.db_pg import get_engine
TABLES = (
    "table_registry",
    "marketplace_registry", "marketplace_plugins",
    "store_entities", "store_submissions",
    "knowledge_items", "knowledge_item_domains",
    "data_packages", "data_package_tables",
    "memory_domains", "memory_domain_suggestions", "recipes",
    "user_stack_subscriptions",
)
with get_engine().connect() as c:
    for t in TABLES:
        try:
            n = c.execute(sa.text(f"SELECT COUNT(*) FROM {t}")).scalar()
            print(f"    {t:<32s} {n}")
        except Exception as e:  # noqa: BLE001
            print(f"    {t:<32s} ERR {e}")
PY
