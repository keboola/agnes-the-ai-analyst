#!/usr/bin/env bash
# Smoke check: /catalog renders, kind tabs are clickable, no JS errors blow
# up the page load. Catches regressions in the unified-stack work (#L98).
#
# The assertions track `catalog_unified.html`, which /catalog has rendered
# since the unified-catalog cutover: kind tabs (Data · Plugins · Memory ·
# Recipes) over one card grid, switched by click. Data and Memory are
# `hidden` when they have no cards, so a seeded-empty instance shows only
# Plugins and Recipes — those two are the pair worth asserting on.
#
# Do NOT reintroduce a "Browse"/"My Stack" tab check here: those tabs, and
# the numeric hotkeys that select them, belong to /marketplace
# (`marketplace.html`), a different page. Asserting them against /catalog is
# what made this smoke fail nightly rather than catch anything.
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

echo "→ snapshot landing — look for the kind tabs"
SNAPSHOT="$(agent-browser --session "$SESSION" snapshot -i)"
for TAB in Plugins Recipes; do
  if ! grep -qi "$TAB" <<<"$SNAPSHOT"; then
    echo "::error::${TAB} tab missing from /catalog snapshot."
    echo "$SNAPSHOT" | head -40
    exit 1
  fi
done

# Kind tabs are click-driven (catalog_unified.html has no keyboard handler),
# so drive the switch the way a user does. Selected by data-kind rather than
# by label: the tab's visible text carries a live count next to it.
echo "→ click the Recipes tab"
agent-browser --session "$SESSION" click '.uc-kindtab[data-kind=recipes]'
agent-browser --session "$SESSION" wait 500

echo "→ verify the Recipes tab is now the selected one"
# Assert on the accessibility snapshot's own selected marker
# (`- tab "Recipes 0" [selected, ref=...]`), NOT on the word "recipe"
# appearing somewhere: the tab's own label contains it either way, so a
# bare text match would pass even if the click did nothing.
SNAPSHOT="$(agent-browser --session "$SESSION" snapshot -i)"
if ! grep -qiE 'tab "Recipes[^"]*"[^]]*\[[^]]*selected' <<<"$SNAPSHOT"; then
  echo "::error::Clicking the Recipes tab didn't select it."
  echo "$SNAPSHOT" | head -40
  exit 1
fi

echo "✓ /catalog smoke passed."
