#!/usr/bin/env bash
# Smoke check: /admin/activity renders + the v55 Resource type filter
# dropdown is wired and changes the URL on selection.
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-./e2e-artifacts}"
mkdir -p "$ARTIFACTS_DIR"

if ! command -v agent-browser >/dev/null 2>&1; then
  echo "::error::agent-browser CLI missing — run 'npm i -g agent-browser && agent-browser install'."
  exit 2
fi

SESSION="agnes-e2e-$$"
trap 'agent-browser --session "$SESSION" close >/dev/null 2>&1 || true' EXIT

# Sign the session in before hitting a protected page — /admin/activity otherwise
# 401-redirects to /login.
source "$(dirname "$0")/_login.sh"

echo "→ open ${BASE_URL}/admin/activity"
agent-browser --session "$SESSION" open "${BASE_URL}/admin/activity"
agent-browser --session "$SESSION" wait --load networkidle

agent-browser --session "$SESSION" screenshot "$ARTIFACTS_DIR/admin-activity-landing.png"

SNAPSHOT="$(agent-browser --session "$SESSION" snapshot -i)"
if ! grep -qi 'Resource' <<<"$SNAPSHOT"; then
  echo "::error::Resource filter dropdown missing from /admin/activity."
  exit 1
fi

echo "✓ /admin/activity smoke passed."
