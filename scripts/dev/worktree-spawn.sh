#!/usr/bin/env bash
# Spawn an isolated worktree for a parallel Claude Code session.
#
# Usage:
#   scripts/dev/worktree-spawn.sh <branch-name> [base-branch]
#
# Example:
#   scripts/dev/worktree-spawn.sh fix/auth-redirect main
#
# What it does:
#   1. Creates .worktrees/<slug>/ off <base-branch> (default: current HEAD).
#   2. Symlinks user/, .venv/, .env, data/  -> main checkout (single-writer state stays put).
#   3. Prints next steps (cd, COMPOSE_PROJECT_NAME hint).

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <branch-name> [base-branch]" >&2
  exit 2
fi

BRANCH="$1"
BASE="${2:-HEAD}"

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

SLUG="${BRANCH//\//-}"
WT_DIR=".worktrees/$SLUG"

if [[ -e "$WT_DIR" ]]; then
  echo "error: $WT_DIR already exists" >&2
  exit 1
fi

git worktree add "$WT_DIR" -b "$BRANCH" "$BASE"

# Symlink shared, single-writer state. Worktree gets isolated source files
# but shares the heavy/stateful bits with the main checkout.
for target in user .venv .env data; do
  if [[ -e "$REPO_ROOT/$target" ]]; then
    ln -s "$REPO_ROOT/$target" "$WT_DIR/$target"
    echo "  linked $target"
  fi
done

cat <<EOF

Worktree ready: $WT_DIR
Branch:         $BRANCH (off $BASE)

Next:
  cd $WT_DIR
  # launch claude code from inside the worktree

Caveats for parallel sessions:
  - DuckDB (user/duckdb/, data/state/system.duckdb) is single-writer.
    Don't run 'da sync' or migrations from two worktrees at once.
  - For parallel docker compose stacks, set:
      export COMPOSE_PROJECT_NAME=agnes-$SLUG
  - Cleanup when done:
      git worktree remove $WT_DIR
      git branch -d $BRANCH    # or -D if not merged
EOF
