#!/usr/bin/env python3
"""Collect Claude Code session transcript to user/sessions/.

This script is invoked by Claude Code's SessionEnd hook.
It reads JSON from stdin containing session_id, transcript_path, and cwd,
then copies the transcript JSONL file to user/sessions/ with a date prefix.

Design principles:
- Stdlib only (no external dependencies)
- Must NEVER crash or produce non-zero exit - Claude Code expects clean exit
- Uses shutil.copy2 (not move) - Claude Code still references the transcript
- UTC date for consistency across timezones
"""

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)
        session_id = data.get("session_id", "")
        transcript_path = data.get("transcript_path", "")
        cwd = data.get("cwd", "")

        if not transcript_path or not session_id:
            return

        source = Path(transcript_path)
        if not source.exists() or not source.is_file():
            return

        # Determine target directory: cwd/user/sessions/
        if not cwd:
            return

        target_dir = Path(cwd) / "user" / "sessions"

        # Only collect if we're inside a project that has user/ directory
        user_dir = Path(cwd) / "user"
        if not user_dir.is_dir():
            return

        target_dir.mkdir(parents=True, exist_ok=True)

        # Format: YYYY-MM-DD_{full_session_id}.jsonl
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target_file = target_dir / f"{date_str}_{session_id}.jsonl"

        # Skip if already collected (check by session_id suffix to handle date changes)
        for existing in target_dir.glob(f"*_{session_id}.jsonl"):
            return

        shutil.copy2(str(source), str(target_file))

    except Exception:
        # Must never crash - Claude Code depends on clean exit
        pass


if __name__ == "__main__":
    main()
