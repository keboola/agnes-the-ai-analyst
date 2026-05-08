"""Pure utilities used by the runner and individual processors. No DB, no
side effects beyond logging."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_jsonl(path: Path) -> list[dict]:
    """Parse a Claude Code session jsonl into a list of event dicts.

    Malformed lines are logged and skipped — a single corrupt row mustn't
    abort processing of the rest of the session. Lifted verbatim from the
    pre-refactor verification_detector.detector.parse_session so the
    behavior is identical."""
    turns: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    turns.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed JSONL line in %s", path)
    return turns


def compute_file_hash(path: Path) -> str:
    """MD5 of the file content. Used to invalidate session_processor_state
    rows when a jsonl grows (Claude Code appending to an active session)."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
