#!/usr/bin/env bash
# Bootstrap GCP projekt pro Agnes deployment.
# Jednorázové, idempotentní. Spusť jako owner GCP projektu.
#
# Usage: bootstrap-gcp.sh <GCP_PROJECT_ID> [SA_NAME]
#
# Produkuje:
#   - enabled APIs (compute, iam, secretmanager, storage, iamcredentials)
#   - service account <SA_NAME> s rolemi pro TF apply
#   - GCS bucket agnes-<PROJECT_ID>-tfstate (versioned, uniform bucket-level access)
#   - SA JSON key (lokální soubor — paste do GitHub secret GCP_SA_KEY a smazat)
set -euo pipefail

PROJECT_ID="${1:?Usage: $0 <GCP_PROJECT_ID> [SA_NAME=agnes-deploy]}"
SA_NAME="${2:-agnes-deploy}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "=== Bootstrap GCP project: ${PROJECT_ID} ==="
gcloud config set project "${PROJECT_ID}" 1>/dev/null

echo "=== Enable APIs ==="
gcloud services enable \
    compute.googleapis.com \
    iam.googleapis.com \
    iamcredentials.googleapis.com \
    secretmanager.googleapis.com \
    cloudresourcemanager.googleapis.com \
    storage.googleapis.com \
    --project="${PROJECT_ID}"

echo "=== Create deploy service account (if not exists) ==="
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" 2>/dev/null 1>&2; then
    gcloud iam service-accounts create "${SA_NAME}" \
        --display-name="Agnes Terraform deploy" \
        --project="${PROJECT_ID}"
else
    echo "  (SA already exists — skipping creation)"
fi

echo "=== Grant roles ==="
for role in \
    compute.instanceAdmin.v1 \
    compute.securityAdmin \
    compute.networkAdmin \
    iam.serviceAccountUser \
    iam.serviceAccountAdmin \
    secretmanager.admin \
    storage.admin \
    resourcemanager.projectIamAdmin; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="roles/${role}" \
        --condition=None \
        --quiet 1>/dev/null
done

echo "=== Create tfstate bucket (if not exists) ==="
BUCKET="agnes-${PROJECT_ID}-tfstate"
if ! gsutil ls -b "gs://${BUCKET}" 2>/dev/null 1>&2; then
    gsutil mb -p "${PROJECT_ID}" -l europe-west1 -b on "gs://${BUCKET}"
    gsutil versioning set on "gs://${BUCKET}"
else
    echo "  (bucket already exists — skipping creation)"
fi

echo "=== Generate SA key ==="
KEY_FILE="./${SA_NAME}-${PROJECT_ID}-key.json"
gcloud iam service-accounts keys create "${KEY_FILE}" \
    --iam-account="${SA_EMAIL}" \
    --project="${PROJECT_ID}"

echo ""
echo "=== HOTOVO ==="
echo ""
echo "SA email:           ${SA_EMAIL}"
echo "TF state bucket:    gs://${BUCKET}"
echo "SA key file:        ${KEY_FILE}"
echo ""
echo "DALŠÍ KROKY:"
echo "1. Pushni klíč do GitHub secretu privátního infra repa:"
echo "   gh secret set GCP_SA_KEY --repo <owner>/<repo> < ${KEY_FILE}"
echo "2. POTOM smaž klíč z lokálu:"
echo "   rm ${KEY_FILE}"
echo ""
