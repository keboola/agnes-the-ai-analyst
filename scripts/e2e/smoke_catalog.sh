#!/usr/bin/env bash
# Smoke check: /catalog renders, tabs are clickable, no JS errors blow up
# the page load. Catches regressions in the unified-stack work (#L98).
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-./e2e-artifacts}"
mkdir -p "$ARTIFACTS_DIR"

# Bail early if agent-browser isn't installed — surfaces a clearer error
# than the daemon-init failure that follows.
if ! command -v agent-browser >/dev/null 2>&1; then
  echo "::error::agent-browser CLI missing — run 'npm i -g agent-browser && agent-browser install'."
  exit 2
fi

# Use a temporary, isolated session so two parallel scripts can't clobber
# each other's cookies.
SESSION="agnes-e2e-$$"
trap 'agent-browser --session "$SESSION" close >/dev/null 2>&1 || true' EXIT

# Sign the session in before hitting a protected page — /catalog otherwise
# 401-redirects to /login.
source "$(dirname "$0")/_login.sh"

echo "→ open ${BASE_URL}/catalog"
agent-browser --session "$SESSION" open "${BASE_URL}/catalog"
agent-browser --session "$SESSION" wait --load networkidle

echo "→ screenshot landing"
agent-browser --session "$SESSION" screenshot "$ARTIFACTS_DIR/catalog-landing.png"

echo "→ snapshot landing — look for Browse tab"
SNAPSHOT="$(agent-browser --session "$SESSION" snapshot -i)"
if ! grep -qi 'Browse' <<<"$SNAPSHOT"; then
  echo "::error::Browse tab missing from /catalog snapshot."
  echo "$SNAPSHOT" | head -40
  exit 1
fi

# Press the "2" hotkey added in v55 (L71) — should switch to My Stack.
echo "→ press 2 (kbd shortcut → My Stack tab)"
agent-browser --session "$SESSION" press 2
agent-browser --session "$SESSION" wait 500

echo "→ verify My Stack view shown"
SNAPSHOT="$(agent-browser --session "$SESSION" snapshot -i)"
if ! grep -qiE 'My Stack|Your stack' <<<"$SNAPSHOT"; then
  echo "::error::Pressing '2' didn't activate the My Stack tab."
  echo "$SNAPSHOT" | head -40
  exit 1
fi

echo "✓ /catalog smoke passed."
