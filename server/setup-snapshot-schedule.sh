#!/bin/bash
# Set up GCP automatic snapshot schedules for data-disk and home-disk
#
# Run once from local machine (requires gcloud auth):
#   ./server/setup-snapshot-schedule.sh
#
# This creates a daily snapshot policy with 14-day retention
# and attaches it to both persistent disks.

set -euo pipefail

PROJECT="kids-ai-data-analysis"
REGION="europe-north1"
ZONE="${REGION}-a"
POLICY_NAME="daily-backup"
RETENTION_DAYS=14
START_TIME="02:00"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# Check gcloud is available
if ! command -v gcloud &>/dev/null; then
    echo "ERROR: gcloud CLI not found. Install it from https://cloud.google.com/sdk"
    exit 1
fi

# Step 1: Create snapshot schedule policy
log "Creating snapshot schedule policy '$POLICY_NAME'..."
if gcloud compute resource-policies describe "$POLICY_NAME" \
    --project="$PROJECT" --region="$REGION" &>/dev/null; then
    log "Policy '$POLICY_NAME' already exists, skipping creation"
else
    gcloud compute resource-policies create snapshot-schedule "$POLICY_NAME" \
        --project="$PROJECT" \
        --region="$REGION" \
        --max-retention-days="$RETENTION_DAYS" \
        --daily-schedule \
        --start-time="$START_TIME" \
        --description="Daily snapshots for data broker disks, ${RETENTION_DAYS}-day retention"
    log "Policy created"
fi

# Step 2: Attach policy to disks
for DISK in data-disk home-disk; do
    log "Attaching policy to $DISK..."
    if gcloud compute disks add-resource-policies "$DISK" \
        --project="$PROJECT" \
        --zone="$ZONE" \
        --resource-policies="$POLICY_NAME" 2>/dev/null; then
        log "  $DISK: policy attached"
    else
        log "  $DISK: policy may already be attached (or disk not found)"
    fi
done

log ""
log "Done! Verify in GCP Console:"
log "  https://console.cloud.google.com/compute/snapshots?project=$PROJECT"
log ""
log "Snapshots will be taken daily at $START_TIME UTC"
log "Retention: $RETENTION_DAYS days (older snapshots auto-deleted)"
