#!/usr/bin/env bash
# Stáhne secrets z GCP Secret Manageru a vytvoří .env pro Agnes.
# Spouští se na VM pod uživatelem, který má gcloud přístup k Secret Manageru
# (typicky přes VM service account s roles/secretmanager.secretAccessor).
#
# Usage: ./fetch-env-from-secrets.sh [APP_DIR]
# Default APP_DIR: /home/deploy/app
set -euo pipefail

APP_DIR="${1:-${APP_DIR:-/home/deploy/app}}"
ENV_FILE="${APP_DIR}/.env"

# Non-secret config (override via environment or hardcoded defaults)
DATA_SOURCE="${DATA_SOURCE:-keboola}"
KEBOOLA_STACK_URL="${KEBOOLA_STACK_URL:-https://connection.us-east4.gcp.keboola.com/}"
SEED_ADMIN_EMAIL="${SEED_ADMIN_EMAIL:?SEED_ADMIN_EMAIL must be set}"
LOG_LEVEL="${LOG_LEVEL:-info}"
DATA_DIR="${DATA_DIR:-/data}"
AGNES_TAG="${AGNES_TAG:-stable}"

echo "Fetching secrets from Secret Manager..."
JWT_KEY=$(gcloud secrets versions access latest --secret=jwt-secret-key)
KEBOOLA_TOKEN=""
if [ "$DATA_SOURCE" = "keboola" ]; then
    KEBOOLA_TOKEN=$(gcloud secrets versions access latest --secret=keboola-storage-token)
fi

echo "Writing ${ENV_FILE}..."
cat > "${ENV_FILE}" <<EOF
JWT_SECRET_KEY=${JWT_KEY}
DATA_DIR=${DATA_DIR}
DATA_SOURCE=${DATA_SOURCE}
KEBOOLA_STORAGE_TOKEN=${KEBOOLA_TOKEN}
KEBOOLA_STACK_URL=${KEBOOLA_STACK_URL}
SEED_ADMIN_EMAIL=${SEED_ADMIN_EMAIL}
LOG_LEVEL=${LOG_LEVEL}
AGNES_TAG=${AGNES_TAG}
EOF

chmod 600 "${ENV_FILE}"
# chown is best-effort — ignore if the script isn't running as root.
chown deploy:deploy "${ENV_FILE}" 2>/dev/null || true

echo "Done. ${ENV_FILE} has $(wc -l < "${ENV_FILE}") lines, chmod 600."
