"""One-shot cleanup for ``table_registry.description`` rows corrupted by shell-quoting.

Background
----------
Some operators registered tables via shell/curl invocations whose quoting
injected literal backslash escapes into the JSON payload — e.g. ``Don\\'t
confuse...``, ``it\\'s...``, and embedded ``\\n`` instead of real newlines.
The backend stored those bytes verbatim and the admin UI rendered them
verbatim too. ``app/api/admin.py`` now applies ``_unescape_shell_quoting``
on register/update so newly-saved descriptions are clean, but rows that
were registered before that fix landed still hold the corrupted text.

This script rewrites every affected ``table_registry.description`` to its
unescaped form. Idempotent — once normalized, a second run is a no-op
because the helper has nothing left to substitute.

Usage
-----
    # 1) Preview the changes that would be made (default).
    python scripts/fix_description_escapes.py

    # 2) Apply for real once the diff looks right.
    python scripts/fix_description_escapes.py --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path so ``src`` is importable when invoked as a script.
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.logging_config import setup_logging  # noqa: E402
from src.db import get_system_db  # noqa: E402

setup_logging(__name__)
logger = logging.getLogger(__name__)


def _unescape_shell_quoting(s: str | None) -> str | None:
    """Mirror of ``app.api.admin._unescape_shell_quoting``.

    Kept inline (rather than imported) so this script stays runnable as a
    standalone one-shot even if ``app.api.admin`` grows imports that an
    operator's cleanup environment can't satisfy.
    """
    if not s:
        return s
    SENTINEL = "\x00"
    return (
        s.replace("\\\\", SENTINEL)
         .replace("\\n", "\n")
         .replace("\\r", "\r")
         .replace("\\t", "\t")
         .replace("\\'", "'")
         .replace('\\"', '"')
         .replace(SENTINEL, "\\")
    )


def _preview(text: str, width: int = 80) -> str:
    """Single-line preview of a possibly multi-line description."""
    flat = text.replace("\n", " \\n ").replace("\r", " ").replace("\t", " ")
    if len(flat) > width:
        flat = flat[: width - 1] + "…"
    return flat


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fix table_registry.description rows corrupted by shell-quoting "
            "backslash-escapes. Defaults to dry-run; pass --apply to write."
        )
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Print the diff but do not write (default).",
    )
    group.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="Apply the UPDATE statements.",
    )
    args = parser.parse_args()

    conn = get_system_db()
    try:
        rows = conn.execute(
            "SELECT id, name, description FROM table_registry "
            "WHERE description IS NOT NULL"
        ).fetchall()
    finally:
        # get_system_db returns a cursor over a shared connection; closing
        # the cursor is safe and does not close the underlying handle.
        try:
            conn.close()
        except Exception:
            pass

    changed = 0
    for table_id, name, description in rows:
        normalized = _unescape_shell_quoting(description)
        if normalized == description:
            continue
        changed += 1
        print(f"{table_id} | {name} | {_preview(normalized or '')}")

        if not args.dry_run:
            write_conn = get_system_db()
            try:
                write_conn.execute(
                    "UPDATE table_registry SET description = ? WHERE id = ?",
                    [normalized, table_id],
                )
            finally:
                try:
                    write_conn.close()
                except Exception:
                    pass

    if changed == 0:
        print("No rows need normalization.")
    else:
        action = "would update" if args.dry_run else "updated"
        print(f"\n{action} {changed} row(s).")
        if args.dry_run:
            print("Re-run with --apply to write the changes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
