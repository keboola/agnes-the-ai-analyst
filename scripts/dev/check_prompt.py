#!/usr/bin/env python3
"""Scan one or more rendered install-prompt files for banned force-style
phrases and confirm required facts survived.

    .venv/Scripts/python.exe scripts/dev/check_prompt.py rendered.txt [more.txt ...]

Exit 0 when every file is clean (no banned phrase, every required fact
present); exit 1 and print every finding otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from prompt_phrases import BANNED_PHRASES, REQUIRED_FACTS  # noqa: E402


def check_file(path: Path) -> list[str]:
    """Return a list of human-readable findings for `path` (empty = clean)."""
    findings: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for lineno, line in enumerate(lines, start=1):
        for phrase in BANNED_PHRASES:
            if phrase in line:
                findings.append(f"{path}:{lineno}: banned phrase {phrase!r}: {line.strip()}")
    text = "\n".join(lines)
    for fact in REQUIRED_FACTS:
        if fact not in text:
            findings.append(f"{path}: missing required fact {fact!r}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", metavar="PATH", help="rendered prompt file(s) to check")
    args = parser.parse_args()

    all_findings: list[str] = []
    for raw_path in args.files:
        all_findings.extend(check_file(Path(raw_path)))

    if all_findings:
        for finding in all_findings:
            print(finding)
        print(f"\n{len(all_findings)} finding(s).")
        return 1

    print("0 findings — clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
