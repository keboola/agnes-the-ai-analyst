#!/usr/bin/env bash
# agnes-statusline.sh — Claude Code status line.
#
# Reads ~/.agnes/refresh.status (written by `agnes refresh-marketplace`
# whenever it actually installed/updated something) and surfaces a short
# one-line indicator at the bottom of the Claude Code UI.
#
# Contract:
#   stdin  — Claude Code session JSON (model, context window %, …).
#            Ignored by this script.
#   stdout — single line of text (ANSI colors OK, multi-line OK but we
#            keep it to one line). Empty output = no status shown.
#   exit   — always 0 so a missing/garbled state file never breaks
#            Claude Code's UI.
#
# Auto-hide: status entries are silently dropped after AGNES_STATUS_TTL_S
# seconds (default 1800 = 30 min). The ttl exists so a stale "installed
# X" message doesn't linger indefinitely; once the user has had time to
# act on it (typically /exit + restart), the line falls off naturally
# instead of needing an explicit dismissal.
#
# This script is materialized by `agnes init` (see cli/lib/statusline.py)
# and removed by `scripts/dev/agnes-client-reset.sh`. Hand-edit at your
# own risk — `agnes init` will preserve a hand-edited script (it only
# writes the file when absent), but a fresh-machine reset re-materializes
# the canonical version.

set -u

# Drain stdin so Claude Code doesn't block on the write side.
cat > /dev/null

STATUS_FILE="$HOME/.agnes/refresh.status"
[ -f "$STATUS_FILE" ] || exit 0

TTL_S="${AGNES_STATUS_TTL_S:-1800}"

# Pure-bash JSON peek — we only need three fields and the file is
# written by us (always one-object, always ASCII keys, no nested
# structures), so grep/sed is enough. Avoids a python startup on every
# Claude Code message tick.
TS="$(grep -o '"timestamp": *[0-9]*' "$STATUS_FILE" 2>/dev/null | grep -o '[0-9]*' | head -1)"
[ -z "$TS" ] && exit 0

NOW="$(date +%s)"
AGE=$((NOW - TS))
[ "$AGE" -gt "$TTL_S" ] && exit 0

SUMMARY="$(grep -o '"summary": *"[^"]*"' "$STATUS_FILE" 2>/dev/null | sed 's/^"summary": *"//; s/"$//')"
[ -z "$SUMMARY" ] && exit 0

NEEDS_RESTART_FLAG="$(grep -o '"needs_restart": *\(true\|false\)' "$STATUS_FILE" 2>/dev/null | grep -o '\(true\|false\)' | head -1)"

SUFFIX=""
if [ "$NEEDS_RESTART_FLAG" = "true" ]; then
    SUFFIX=" · /exit + restart to load"
fi

# Single line, no newline at end (Claude Code joins multi-line output).
printf 'agnes \xe2\x9f\xb3 %s%s' "$SUMMARY" "$SUFFIX"
