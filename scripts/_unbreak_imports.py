"""Find files where ``from src.repositories import (...)`` was injected
inside another ``from X import (`` block. Hoist the factory block out
to its own statement immediately after the surrounding import closes.
"""
from __future__ import annotations

import re
from pathlib import Path


def _unbreak(path: Path) -> bool:
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    changed = False
    while i < len(lines):
        line = lines[i]
        # Detect: a line that starts a multi-line import (open paren no close)
        if (
            re.match(r"^from\s+[A-Za-z_.]+\s+import\s+\(\s*$", line)
            and not line.startswith("from src.repositories")
        ):
            # Now scan ahead — if the next line starts a factory import,
            # we have the broken pattern.
            if i + 1 < len(lines) and lines[i + 1].startswith(
                "from src.repositories import ("
            ):
                # Capture factory block fully (until its closing ')')
                factory_lines = [lines[i + 1]]
                j = i + 2
                while j < len(lines):
                    factory_lines.append(lines[j])
                    if lines[j].rstrip() == ")":
                        j += 1
                        break
                    j += 1
                # Now lines[j:] continues the OUTER import block — let's keep
                # the outer import opening, skip the factory block, and continue.
                out.append(line)
                # Continue scanning original block from j (skipping factory)
                while j < len(lines):
                    out.append(lines[j])
                    if lines[j].rstrip() == ")":
                        j += 1
                        break
                    j += 1
                # After the outer ) emit the factory block on its own
                out.append("\n")
                out.extend(factory_lines)
                i = j
                changed = True
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
            if _unbreak(path):
                fixed += 1
                print(f"  unbroke {path.relative_to(repo_root)}")
    print(f"\nUnbroke {fixed} files.")


if __name__ == "__main__":
    main()
