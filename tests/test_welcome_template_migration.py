"""v20 → v21 historically added the `welcome_template` singleton table.

v28 consolidated welcome_template + claude_md_template into the generic
`instance_templates(key, content, ...)` table; the legacy `welcome_template`
table is dropped during the v27 → v28 migration. The historical assertion
"a v20 DB upgrades and welcome_template exists" is no longer valid because the
ladder runs through v28 and consolidates the table away.

This file is preserved so the welcome-template migration history stays
covered in the test suite, but the assertion is reshaped to match v28
reality: post-migration the welcome content lives at
`instance_templates WHERE key = 'welcome'`.
"""

from pathlib import Path

import duckdb

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


def _open(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path))


def test_welcome_content_landing_post_v28(tmp_path):
    """A fresh-install ladder lands the welcome row in instance_templates,
    not in a separate welcome_template table (consolidated in v28)."""
    db_path = tmp_path / "system.duckdb"
    conn = _open(db_path)
    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION

    # Legacy welcome_template table is gone post-v28.
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "welcome_template" not in tables, (
        "welcome_template should be consolidated into instance_templates by v28"
    )

    # Welcome row lives in instance_templates keyed 'welcome' with NULL content
    # (= use shipped default).
    row = conn.execute(
        "SELECT key, content FROM instance_templates WHERE key = 'welcome'"
    ).fetchone()
    assert row is not None
    assert row[0] == "welcome"
    assert row[1] is None
    conn.close()
