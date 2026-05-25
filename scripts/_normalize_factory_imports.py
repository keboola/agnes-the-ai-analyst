"""Move every ``from src.repositories import (...)`` block to the top
of its file (right after the docstring / module-level top imports).

Handles the case where the previous mechanical swap injected the
factory import inside a string literal, after the last `import` keyword
in the file (which can be a string-literal embedded `import` rather
than a real one).

Idempotent — running again on a file already in the right shape is a
no-op.
"""
from __future__ import annotations

import re
from pathlib import Path


_BLOCK_RE = re.compile(
    r"(?m)^from src\.repositories import \(\n(?:    [a-z_]+,\n)+\)\n",
)


def _normalize(path: Path) -> bool:
    text = path.read_text()
    blocks = _BLOCK_RE.findall(text)
    if not blocks:
        return False
    if len(blocks) == 1 and text.lstrip().startswith("from src.repositories import (") and text.count(blocks[0]) == 1:
        # Already at the top
        return False

    # Remove every factory block from its current location.
    stripped = _BLOCK_RE.sub("", text)

    # Merge all factory blocks into a single deduplicated one.
    factories: set[str] = set()
    for block in blocks:
        for line in block.splitlines():
            line = line.strip().rstrip(",")
            if line and line not in ("from src.repositories import (", ")"):
                factories.add(line)
    if not factories:
        return False

    merged = (
        "from src.repositories import (\n    "
        + ",\n    ".join(sorted(factories))
        + ",\n)\n"
    )

    # Insert merged block right after the LAST top-level real import
    # (lines starting with ``from `` or ``import `` BEFORE the first
    # non-import line at column 0).
    out_lines: list[str] = []
    inserted = False
    in_docstring = False
    docstring_seen = False
    for i, line in enumerate(stripped.splitlines(keepends=True)):
        stripped_line = line.lstrip()
        # Crude docstring tracker — count the first """ as opening, second as closing
        if not docstring_seen and stripped_line.startswith('"""'):
            if stripped_line.endswith('"""\n') and len(stripped_line) > 4:
                # one-line docstring
                docstring_seen = True
                in_docstring = False
            else:
                docstring_seen = True
                in_docstring = True
            out_lines.append(line)
            continue
        if in_docstring:
            out_lines.append(line)
            if '"""' in stripped_line:
                in_docstring = False
            continue

        if not inserted:
            # Look for first non-import top-level statement
            if line.startswith(("from ", "import ", "#", "\n")) or stripped_line == "":
                out_lines.append(line)
                continue
            # First non-import: insert here
            out_lines.append(merged)
            out_lines.append(line)
            inserted = True
        else:
            out_lines.append(line)

    if not inserted:
        # File was all imports/comments; append at the end
        out_lines.append(merged)

    new_text = "".join(out_lines)
    if new_text == text:
        return False
    path.write_text(new_text)
    return True


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    changed = 0
    for root in ("app", "services", "cli"):
        for path in (repo_root / root).rglob("*.py"):
            if _normalize(path):
                changed += 1
                print(f"  normalized {path.relative_to(repo_root)}")
    print(f"\nNormalized {changed} files.")


if __name__ == "__main__":
    main()
