"""Repair multi-line imports broken by ``_swap_repos_to_factory.py``.

The factory-import injection sometimes landed *inside* an open
``from X import (`` parenthesised block. This script finds and untangles
those — moves the misplaced ``from src.repositories import (...)`` block
out to a position AFTER the closing ``)`` of whichever import it landed in.
"""
from __future__ import annotations

import re
from pathlib import Path


_FACTORY_BLOCK = re.compile(
    r"from src\.repositories import \(\n(?:    [a-z_]+,\n)+\)\n",
    re.MULTILINE,
)


def _fix_file(path: Path) -> bool:
    text = path.read_text()
    if "from src.repositories import (" not in text:
        return False

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    pending_factory: list[str] = []
    in_other_paren = 0  # depth of any other import using ( ... )
    i = 0
    changed = False
    while i < len(lines):
        line = lines[i]
        # Detect start of a multi-line ``from X import (`` BUT not the factory itself
        if (
            line.startswith("from ")
            and "import (" in line
            and "from src.repositories import (" not in line
            and ")" not in line  # not single-line
        ):
            # We're starting a multi-line import block.
            out.append(line)
            in_other_paren += 1
            i += 1
            continue

        # If we're INSIDE a non-factory multi-line import and we see the start
        # of a factory import block, capture it and skip until its closing ).
        if in_other_paren > 0 and line.startswith("from src.repositories import ("):
            captured: list[str] = [line]
            i += 1
            while i < len(lines):
                captured.append(lines[i])
                if lines[i].rstrip() == ")":
                    i += 1
                    break
                i += 1
            pending_factory.append("".join(captured))
            changed = True
            continue

        # Closing of the outer import.
        if in_other_paren > 0 and ")" in line and "(" not in line:
            out.append(line)
            in_other_paren -= 1
            i += 1
            # If we just closed and have pending factory imports, drop them now.
            if in_other_paren == 0 and pending_factory:
                out.append("\n")
                out.extend(pending_factory)
                pending_factory = []
            continue

        out.append(line)
        i += 1

    if changed:
        path.write_text("".join(out))
    return changed


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    fixed = 0
    for root in ("app", "services", "cli"):
        for path in (repo_root / root).rglob("*.py"):
            if _fix_file(path):
                fixed += 1
                print(f"  fixed {path.relative_to(repo_root)}")
    print(f"\nFixed {fixed} files.")


if __name__ == "__main__":
    main()
