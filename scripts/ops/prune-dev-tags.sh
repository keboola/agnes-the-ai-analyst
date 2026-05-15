#!/usr/bin/env bash
# Prune legacy CalVer dev/stable image identity from git + GHCR:
#
# Git tags + GHCR image versions of the form
#   dev-YYYY.MM.N      e.g. dev-2026.04.475
#   stable-YYYY.MM.N   e.g. stable-2026.04.474
# accumulate one per CI build. Retention: KEEP_MONTHS (default 1) keeps
# the current month + the previous KEEP_MONTHS months; older tags +
# images are pruned.
#
# Dry-run via PRUNE_DRY_RUN=1 (or workflow input) — lists what would be
# pruned without acting.
#
# Idempotent: re-running with no eligible tags exits 0.

set -euo pipefail

KEEP_MONTHS="${KEEP_MONTHS:-1}"
[[ "$KEEP_MONTHS" =~ ^[0-9]+$ ]] || { echo "KEEP_MONTHS must be a non-negative integer (got: '$KEEP_MONTHS')"; exit 1; }
DRY_RUN="${PRUNE_DRY_RUN:-0}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY env var must be set (e.g. keboola/agnes-the-ai-analyst)}"

cd "$(git rev-parse --show-toplevel)"

# Compute the set of YYYY.MM strings to KEEP — walk back KEEP_MONTHS+1
# months from today.
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

# Collect candidate tags — strictly `dev-YYYY.MM.N` / `stable-YYYY.MM.N`.
LEGACY_TAGS=$(git tag -l 'dev-*' 'stable-*' \
  | grep -E '^(dev|stable)-[0-9]{4}\.[0-9]{2}\.[0-9]+$' \
  || true)

# Filter: keep tags whose YYYY.MM is in the keep window; prune the rest.
TO_PRUNE=()
if [ -n "$LEGACY_TAGS" ]; then
  while IFS= read -r TAG; do
    [ -z "$TAG" ] && continue
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

SECTION1_HAS_WORK=0

if [ -z "$LEGACY_TAGS" ]; then
  echo "No legacy CalVer tags found — nothing to prune."
elif [ "${#TO_PRUNE[@]}" -eq 0 ]; then
  echo "All legacy tags are within retention window — nothing to prune."
else
  SECTION1_HAS_WORK=1
  echo "Will prune ${#TO_PRUNE[@]} tags older than the retention window:"
  # Array slice instead of `printf … | head` — under `set -o pipefail`,
  # head closing the pipe early can SIGPIPE printf (exit 141) and abort
  # the script before any deletion runs. The slice avoids the pipeline.
  printf '  %s\n' "${TO_PRUNE[@]:0:20}"
  [ "${#TO_PRUNE[@]}" -gt 20 ] && echo "  ... (and $((${#TO_PRUNE[@]} - 20)) more)"
fi

if [ "$SECTION1_HAS_WORK" = "1" ] && [ "$DRY_RUN" = "1" ]; then
  echo "(dry-run — no deletions)"
  SECTION1_HAS_WORK=0
fi

if [ "$SECTION1_HAS_WORK" = "1" ]; then
  # Delete git tags (remote first; local fallback is harmless).
  for TAG in "${TO_PRUNE[@]}"; do
    echo "  deleting tag: $TAG"
    git push origin --delete "$TAG" \
      || echo "    (remote push failed — tag may already be gone, or the push was blocked)"
    git tag -d "$TAG" 2>/dev/null || true
  done

  # Delete GHCR image versions. Requires GH_TOKEN with packages:write.
  if [ -n "${GH_TOKEN:-}" ]; then
    ORG=$(echo "$REPO" | cut -d/ -f1)
    PKG_NAME=$(echo "$REPO" | cut -d/ -f2)
    echo "Fetching GHCR image versions for $ORG/$PKG_NAME ..."

    # One paginated fetch up-front, then per-tag lookups against the
    # cached result. Avoids O(N × pages) API calls on a multi-month
    # backlog (legacy CalVer tag counts run ~500/month per channel).
    VERSIONS_JSON=$(gh api \
      "/orgs/${ORG}/packages/container/${PKG_NAME}/versions" \
      --paginate \
      || echo "[]")

    # CRITICAL: GHCR's DELETE-version drops the entire manifest, taking
    # EVERY tag on it (including `:stable`, `:dev`, `dev-<user>-latest`).
    # After a rollback re-tag, the previous-known-good version carries
    # both `:stable` and its CalVer tag — pruning that CalVer tag would
    # vaporize `:stable`. So skip any version that also carries a
    # floating alias. The jq filter applies that exclusion up-front.
    TAG_TO_ID=$(echo "$VERSIONS_JSON" | jq -r '
      .[]
      | select(
          (.metadata.container.tags | index("stable") // false | not) and
          (.metadata.container.tags | index("dev")    // false | not) and
          ((.metadata.container.tags | map(endswith("-latest")) | any) | not)
        )
      | . as $v
      | .metadata.container.tags[] as $t
      | "\($t)\t\($v.id)"
    ' || echo "")

    echo "Deleting matching GHCR image versions ..."
    for TAG in "${TO_PRUNE[@]}"; do
      VERSION_ID=$(echo "$TAG_TO_ID" | awk -v t="$TAG" '$1==t {print $2; exit}')
      if [ -n "$VERSION_ID" ]; then
        echo "  deleting GHCR image $TAG (version $VERSION_ID)"
        gh api -X DELETE \
          "/orgs/${ORG}/packages/container/${PKG_NAME}/versions/${VERSION_ID}" \
          || echo "    (DELETE failed — check packages:write scope, rate limits, or version already gone)"
      else
        echo "  skipping GHCR image $TAG — no eligible version (already gone, or shares a manifest with :stable/:dev/*-latest)"
      fi
    done
  else
    echo "GH_TOKEN not set — skipping GHCR image deletion (git tags pruned above)."
  fi
fi

echo "Prune complete."
