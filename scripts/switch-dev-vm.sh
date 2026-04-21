#!/usr/bin/env bash
# switch-dev-vm.sh — point the shared hackathon dev VM at the caller's branch image.
#
# Usage:
#   scripts/switch-dev-vm.sh <branch-slug>
#   scripts/switch-dev-vm.sh hack-zs-metrics
#
# Prerequisite: your branch has been pushed and the release.yml workflow has completed,
# producing ghcr.io/keboola/agnes-the-ai-analyst:dev-<slug>.
#
# The slug is derived from your branch name by stripping the leading "feature/" and
# replacing non-alphanumeric chars with "-". For branch "feature/hack-zs-metrics" the slug
# is "hack-zs-metrics".
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <branch-slug>" >&2
  echo "Example: $0 hack-zs-metrics" >&2
  exit 2
fi

SLUG="$1"
VM="agnes-dev"
ZONE="europe-west1-b"
TAG="dev-$SLUG"
IMAGE="ghcr.io/keboola/agnes-the-ai-analyst:$TAG"

echo "[1/4] Verifying $IMAGE exists on GHCR..."
docker manifest inspect "$IMAGE" > /dev/null || {
  echo "ERROR: $IMAGE not found on GHCR. Did your release.yml run finish?" >&2
  echo "Check: gh run list --branch feature/$SLUG --workflow release.yml" >&2
  exit 1
}

echo "[2/4] Updating AGNES_TAG on $VM to $TAG..."
gcloud compute ssh "$VM" --zone="$ZONE" --quiet --command "\
  sudo sed -i 's|^AGNES_TAG=.*|AGNES_TAG=$TAG|' /opt/agnes/.env && \
  sudo grep -E '^AGNES_TAG=' /opt/agnes/.env"

echo "[3/4] Triggering auto-upgrade..."
gcloud compute ssh "$VM" --zone="$ZONE" --quiet --command \
  "sudo /usr/local/bin/agnes-auto-upgrade.sh 2>&1 | tail -10"

echo "[4/4] Waiting for app to become healthy..."
for i in $(seq 1 30); do
  STATUS=$(curl -s --max-time 5 http://34.77.94.14:8000/api/health | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status","down"))' 2>/dev/null || echo down)
  echo "  [$i/30] status=$STATUS"
  if [ "$STATUS" = "healthy" ] || [ "$STATUS" = "degraded" ]; then
    echo "OK — agnes-dev now running $TAG. Open http://34.77.94.14:8000"
    exit 0
  fi
  sleep 3
done
echo "ERROR: agnes-dev did not become healthy in 90s. SSH in and check: docker compose logs" >&2
exit 1
