"""Jira partition-format detector for `agnes diagnose`.

Issue #394. The Jira connector has two historical on-disk layouts for its
monthly parquet files:

- **flat** (old):  ``<jira_dir>/data/{table}/YYYY-MM.parquet``
  e.g. ``2025-01.parquet`` sitting directly in the table directory.
- **hive** (new):  ``<jira_dir>/data/{table}/month=YYYY-MM/part-N.parquet``
  i.e. one subdirectory per month following Hive-style partition naming.

Instances that were created before the layout migration may still carry the
old flat layout or a mix of both — the diagnose check surfaces this so an
operator knows whether a migration is needed.

Return value is a dict shaped as a diagnose check entry:

    {
        "name": "jira-partition-format",
        "status": "ok" | "warning" | "info",
        "layout": "new" | "old" | "mixed" | "absent",
        "flat_tables": <int>,
        "hive_tables": <int>,
        "detail": "<human readable string>",
        "audience": "operator",
    }

Statuses:
- ``ok``      — all tables that have data use the hive layout.
- ``warning`` — flat or mixed layout detected; migration recommended.
- ``info``    — no Jira parquet data present on disk.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

# Pattern for the old flat monthly parquet: YYYY-MM.parquet
_FLAT_RE = re.compile(r"^\d{4}-\d{2}\.parquet$")

# Pattern for a hive-style month subdirectory: month=YYYY-MM
_HIVE_DIR_RE = re.compile(r"^month=\d{4}-\d{2}$")

# Table names the Jira connector writes
_JIRA_TABLES = ["issues", "comments", "attachments", "changelog", "issuelinks", "remote_links"]


def _classify_table_dir(table_dir: Path) -> str:
    """Return ``'flat'``, ``'hive'``, ``'mixed'``, or ``'absent'`` for one table dir.

    A table dir may contain both flat parquets and hive subdirectories if a
    partial migration took place — that is ``'mixed'``.
    """
    if not table_dir.is_dir():
        return "absent"

    has_flat = any(_FLAT_RE.match(p.name) for p in table_dir.iterdir() if p.is_file())
    has_hive = any(_HIVE_DIR_RE.match(p.name) for p in table_dir.iterdir() if p.is_dir())

    if has_flat and has_hive:
        return "mixed"
    if has_flat:
        return "flat"
    if has_hive:
        return "hive"
    return "absent"


def detect_jira_partition_layout(
    jira_base_dir: Path,
) -> Dict[str, Any]:
    """Inspect ``<jira_base_dir>/data/`` and classify the Jira partition layout.

    Args:
        jira_base_dir: Root of the Jira extract directory.  The connector
            writes tables under ``<jira_base_dir>/data/{table}/``.  If the
            caller has already resolved the path to the ``data/`` sub-dir,
            passing that works too — the function tries ``data/`` first and
            falls back to treating the path itself as the data root.
    """
    data_dir = jira_base_dir / "data"
    if not data_dir.is_dir():
        # Caller may have passed the data/ dir directly
        data_dir = jira_base_dir

    flat_tables: list[str] = []
    hive_tables: list[str] = []
    mixed_tables: list[str] = []

    for table in _JIRA_TABLES:
        table_dir = data_dir / table
        classification = _classify_table_dir(table_dir)
        if classification == "flat":
            flat_tables.append(table)
        elif classification == "hive":
            hive_tables.append(table)
        elif classification == "mixed":
            mixed_tables.append(table)
        # "absent" — skip; table not present at all

    total_with_data = len(flat_tables) + len(hive_tables) + len(mixed_tables)

    if total_with_data == 0:
        return {
            "name": "jira-partition-format",
            "status": "info",
            "layout": "absent",
            "flat_tables": 0,
            "hive_tables": 0,
            "detail": "no Jira parquet data found on disk — Jira connector not configured or no data synced yet",
            "audience": "operator",
        }

    # Mixed: either a single table dir holds both layouts, OR different
    # tables use different layouts across the extract directory.
    is_mixed = bool(mixed_tables) or (bool(flat_tables) and bool(hive_tables))

    if is_mixed:
        mixed_desc = sorted(mixed_tables + flat_tables)
        tables_str = ", ".join(mixed_desc) if mixed_desc else ", ".join(sorted(hive_tables))
        return {
            "name": "jira-partition-format",
            "status": "warning",
            "layout": "mixed",
            "flat_tables": len(flat_tables) + len(mixed_tables),
            "hive_tables": len(hive_tables) + len(mixed_tables),
            "detail": (
                f"mixed flat/hive layouts detected: flat in ({', '.join(sorted(flat_tables + mixed_tables))}), "
                f"hive in ({', '.join(sorted(hive_tables + mixed_tables))}). "
                "Run the Jira partition migration to convert all tables to the "
                "hive month=*/ layout."
            ),
            "audience": "operator",
        }

    if flat_tables:
        tables_str = ", ".join(sorted(flat_tables))
        return {
            "name": "jira-partition-format",
            "status": "warning",
            "layout": "old",
            "flat_tables": len(flat_tables),
            "hive_tables": 0,
            "detail": (
                f"old flat YYYY-MM.parquet layout detected in table(s): {tables_str}. "
                "Run the Jira partition migration to convert to the hive month=*/ layout."
            ),
            "audience": "operator",
        }

    # All tables with data use the hive layout
    tables_str = ", ".join(sorted(hive_tables))
    return {
        "name": "jira-partition-format",
        "status": "ok",
        "layout": "new",
        "flat_tables": 0,
        "hive_tables": len(hive_tables),
        "detail": f"hive month=*/ partition layout in use ({tables_str})",
        "audience": "operator",
    }
