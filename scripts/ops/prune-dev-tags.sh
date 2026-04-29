#!/usr/bin/env bash
# Prune two flavors of dev/stable image identity from git + GHCR:
#
# 1. **Legacy CalVer** — git tags + GHCR image versions of the form
#      dev-YYYY.MM.N      e.g. dev-2026.04.475
#      stable-YYYY.MM.N   e.g. stable-2026.04.474
#    Produced by the old per-build claim-tag race loop, dropped from
#    release.yml in 2026-04 (replaced by github.run_number-based tags).
#    Retention: KEEP_MONTHS (default 1) keeps the current month + the
#    previous KEEP_MONTHS months. On 2026-04-29 with KEEP_MONTHS=1 we
#    keep `*-2026.04.*` and `*-2026.03.*`, prune older.
#
# 2. **New run-number scheme** — GHCR image versions only (no git tags)
#      dev-N      e.g. dev-475
#      stable-N   e.g. stable-475
#    Produced by current release.yml. Retention: KEEP_RECENT (default 50)
#    keeps the newest N per channel, prunes older. Versions carrying any
#    floating alias (:stable, :dev, *-latest) are NEVER pruned, even
#    when their run-number tag is below the cutoff — this protects the
#    currently-deployed image and per-developer aliases.
#
# Dry-run via PRUNE_DRY_RUN=1 (or workflow input) — lists what would be
# pruned without acting.
#
# Idempotent: re-running with no eligible tags exits 0.

set -euo pipefail

KEEP_MONTHS="${KEEP_MONTHS:-1}"
KEEP_RECENT="${KEEP_RECENT:-50}"
DRY_RUN="${PRUNE_DRY_RUN:-0}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY env var must be set (e.g. keboola/agnes-the-ai-analyst)}"

cd "$(git rev-parse --show-toplevel)"

# Compute the set of YYYY.MM strings to KEEP.
# Walking back KEEP_MONTHS+1 months from today. Uses GNU date if available,
# falls back to BSD date (macOS) — both cover ubuntu-latest and dev macs.
TODAY_YEAR=$(date +%Y)
TODAY_MONTH=$(date +%m)
TODAY_MONTH_NUM=$((10#$TODAY_MONTH))  # strip leading zero for arithmetic

KEEP_YYYY_MM=()
for i in $(seq 0 "$KEEP_MONTHS"); do
  Y=$TODAY_YEAR
  M=$((TODAY_MONTH_NUM - i))
  while [ "$M" -lt 1 ]; do
    M=$((M + 12))
    Y=$((Y - 1))
  done
  KEEP_YYYY_MM+=("$(printf '%04d.%02d' "$Y" "$M")")
done

echo "Retention window (YYYY.MM): ${KEEP_YYYY_MM[*]}"

# Collect candidate tags. Match `dev-YYYY.MM.N` and `stable-YYYY.MM.N`
# strictly — the new short-form `dev-N` / `stable-N` (Phase 5 onward) is
# a different shape and won't match.
LEGACY_TAGS=$(git tag -l 'dev-*' 'stable-*' \
  | grep -E '^(dev|stable)-[0-9]{4}\.[0-9]{2}\.[0-9]+$' \
  || true)

# Filter: keep tags whose YYYY.MM is in the keep window; everything else prunes.
# Always populate TO_PRUNE (possibly empty) so the rest of the script can
# fall through to Section 2 even when Section 1 has nothing to do.
TO_PRUNE=()
if [ -n "$LEGACY_TAGS" ]; then
  while IFS= read -r TAG; do
    [ -z "$TAG" ] && continue
    # Extract YYYY.MM from `<channel>-YYYY.MM.N`
    TAG_YM=$(echo "$TAG" | sed -E 's/^(dev|stable)-([0-9]{4}\.[0-9]{2})\.[0-9]+$/\2/')
    KEEP=0
    for KEEP_YM in "${KEEP_YYYY_MM[@]}"; do
      if [ "$TAG_YM" = "$KEEP_YM" ]; then KEEP=1; break; fi
    done
    if [ "$KEEP" = "0" ]; then
      TO_PRUNE+=("$TAG")
    fi
  done <<< "$LEGACY_TAGS"
fi

# Track whether Section 1 has any work — affects Section 2 fall-through
SECTION1_HAS_WORK=0

if [ -z "$LEGACY_TAGS" ]; then
  echo "No legacy CalVer tags found — Section 1 nothing to prune."
elif [ "${#TO_PRUNE[@]}" -eq 0 ]; then
  echo "All legacy tags are within retention window — Section 1 nothing to prune."
else
  SECTION1_HAS_WORK=1
  echo "Will prune ${#TO_PRUNE[@]} tags older than the retention window:"
  printf '  %s\n' "${TO_PRUNE[@]}" | head -20
  [ "${#TO_PRUNE[@]}" -gt 20 ] && echo "  ... (and $((${#TO_PRUNE[@]} - 20)) more)"
fi

if [ "$SECTION1_HAS_WORK" = "1" ] && [ "$DRY_RUN" = "1" ]; then
  echo "(dry-run — no deletions in Section 1)"
  # Don't exit — fall through to Section 2 dry-run too.
  SECTION1_HAS_WORK=0    # disables the actual deletion blocks below
fi

# Section 1 actual deletion (only if real run AND there's work to do)
if [ "$SECTION1_HAS_WORK" = "1" ]; then
  # Delete git tags (remote first; local fallback is harmless).
  for TAG in "${TO_PRUNE[@]}"; do
    echo "  deleting tag: $TAG"
    git push origin --delete "$TAG" 2>/dev/null || echo "    (already gone on remote)"
    git tag -d "$TAG" 2>/dev/null || true
  done

  # Delete GHCR image versions. Requires GH_TOKEN with packages:delete.
  # `gh api` paginates org packages; we look up the version-id for each tag.
  if [ -n "${GH_TOKEN:-}" ]; then
    ORG=$(echo "$REPO" | cut -d/ -f1)
    PKG_NAME=$(echo "$REPO" | cut -d/ -f2)
    echo "Deleting matching GHCR image versions in org $ORG / package $PKG_NAME ..."
    for TAG in "${TO_PRUNE[@]}"; do
      VERSION_ID=$(gh api \
        "/orgs/${ORG}/packages/container/${PKG_NAME}/versions" \
        --paginate \
        --jq ".[] | select(.metadata.container.tags[]? == \"$TAG\") | .id" \
        | head -1 || true)
      if [ -n "$VERSION_ID" ]; then
        echo "  deleting GHCR image $TAG (version $VERSION_ID)"
        gh api -X DELETE \
          "/orgs/${ORG}/packages/container/${PKG_NAME}/versions/${VERSION_ID}" \
          2>/dev/null \
          || echo "    (DELETE failed — check packages:write scope, GitHub API rate limits, or version already gone)"
      fi
    done
  else
    echo "GH_TOKEN not set — skipping GHCR image deletion (git tags pruned above)."
  fi
fi

# ============================================================================
# Section 2: prune new run-number scheme — GHCR image versions only.
# ============================================================================
# Tags `dev-N` / `stable-N` (purely numeric N) are produced by the current
# release.yml using ${{ github.run_number }}. They exist as GHCR image
# versions only (no git tags). Retention: keep newest KEEP_RECENT per channel.
# Versions carrying ANY floating alias (:stable, :dev, *-latest) are SKIPPED
# regardless of their position — protects the currently-deployed image.

if [ -z "${GH_TOKEN:-}" ]; then
  echo ""
  echo "GH_TOKEN not set — skipping new-scheme GHCR prune."
  exit 0
fi

echo ""
echo "=== Section 2: prune new-scheme GHCR images (KEEP_RECENT=$KEEP_RECENT per channel) ==="

ORG="${ORG:-$(echo "$REPO" | cut -d/ -f1)}"
PKG_NAME="${PKG_NAME:-$(echo "$REPO" | cut -d/ -f2)}"

# A version is "protected" (never pruned) if any of its tags match these
# patterns — :stable, :dev, :keboola-deploy-latest, :dev-<prefix>-latest.
PROTECTED_TAG_RE='^(stable|dev|keboola-deploy-latest|dev-.+-latest)$'

for CHANNEL in stable dev; do
  echo ""
  echo "Channel: $CHANNEL"

  # List versions whose run-number tag matches `<channel>-N`, paired with
  # the numeric run number. Versions with any protected tag are excluded
  # at jq filter time. Output: "<run_num> <version_id>" lines.
  CANDIDATES=$(gh api \
    "/orgs/${ORG}/packages/container/${PKG_NAME}/versions" \
    --paginate \
    --jq "
      .[]
      | . as \$v
      | (\$v.metadata.container.tags // []) as \$tags
      | (\$tags | map(select(test(\"^${CHANNEL}-[0-9]+\$\"))) | .[0]) as \$rn_tag
      | select(\$rn_tag != null)
      | select(\$tags | map(test(\"${PROTECTED_TAG_RE}\")) | any | not)
      | (\$rn_tag | sub(\"^${CHANNEL}-\"; \"\") | tonumber) as \$num
      | \"\(\$num) \(\$v.id)\"
    " \
    || true)

  if [ -z "$CANDIDATES" ]; then
    echo "  no prunable $CHANNEL-N versions (none, or all protected by floating alias)."
    continue
  fi

  # Sort by run number descending, drop the top KEEP_RECENT, prune the rest.
  # `tail -n +K` outputs lines starting at line K (1-indexed), so K=KEEP_RECENT+1
  # drops the first KEEP_RECENT entries.
  TO_DELETE=$(echo "$CANDIDATES" | sort -k1,1 -nr | tail -n +"$((KEEP_RECENT + 1))")

  if [ -z "$TO_DELETE" ]; then
    echo "  $(echo "$CANDIDATES" | wc -l | tr -d ' ') $CHANNEL-N versions — all within KEEP_RECENT=$KEEP_RECENT."
    continue
  fi

  COUNT=$(echo "$TO_DELETE" | wc -l | tr -d ' ')
  echo "  Will prune $COUNT old $CHANNEL-N versions (keeping newest $KEEP_RECENT):"
  echo "$TO_DELETE" | head -10 | awk '{ printf "    %s-%s (version %s)\n", "'$CHANNEL'", $1, $2 }'
  [ "$COUNT" -gt 10 ] && echo "    ... (and $((COUNT - 10)) more)"

  if [ "$DRY_RUN" = "1" ]; then
    echo "  (dry-run — no deletions)"
    continue
  fi

  echo "$TO_DELETE" | while read -r RUN_NUM VERSION_ID; do
    [ -z "$VERSION_ID" ] && continue
    echo "  deleting ${CHANNEL}-${RUN_NUM} (version $VERSION_ID)"
    gh api -X DELETE \
      "/orgs/${ORG}/packages/container/${PKG_NAME}/versions/${VERSION_ID}" \
      2>/dev/null \
      || echo "    (DELETE failed — check packages:write scope, rate limits, or version already gone)"
  done
done
