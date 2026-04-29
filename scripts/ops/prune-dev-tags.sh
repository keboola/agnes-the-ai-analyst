#!/usr/bin/env bash
# Prune legacy CalVer git tags + GHCR image versions of the form
#   dev-YYYY.MM.N      e.g. dev-2026.04.475
#   stable-YYYY.MM.N   e.g. stable-2026.04.474
#
# These were produced by the old per-build claim-tag race loop, dropped
# from release.yml in 2026-04 (replaced by github.run_number-based image
# tags). The new tag scheme (`dev-475`, `stable-475`) is NOT touched by
# this script — those image versions are kept until the operator
# explicitly removes them.
#
# Retention: KEEP_MONTHS=1 (default) keeps the current month + the
# previous KEEP_MONTHS months. So with KEEP_MONTHS=1 on 2026-04-29 we
# keep tags matching `*-2026.04.*` and `*-2026.03.*`, prune older.
#
# Dry-run via PRUNE_DRY_RUN=1 (or workflow input) — lists what would be
# pruned without acting.
#
# Idempotent: re-running with no eligible tags exits 0.

set -euo pipefail

KEEP_MONTHS="${KEEP_MONTHS:-1}"
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

if [ -z "$LEGACY_TAGS" ]; then
  echo "No legacy CalVer tags found — nothing to prune."
  exit 0
fi

# Filter: keep tags whose YYYY.MM is in the keep window; everything else prunes.
TO_PRUNE=()
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

if [ "${#TO_PRUNE[@]}" -eq 0 ]; then
  echo "All legacy tags are within retention window — nothing to prune."
  exit 0
fi

echo "Will prune ${#TO_PRUNE[@]} tags older than the retention window:"
printf '  %s\n' "${TO_PRUNE[@]}" | head -20
[ "${#TO_PRUNE[@]}" -gt 20 ] && echo "  ... (and $((${#TO_PRUNE[@]} - 20)) more)"

if [ "$DRY_RUN" = "1" ]; then
  echo "(dry-run — no deletions)"
  exit 0
fi

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
