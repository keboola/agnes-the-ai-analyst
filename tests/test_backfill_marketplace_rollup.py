"""Backfill of usage_marketplace_item_daily + _window from existing usage_events."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import datetime, timezone, timedelta

import duckdb


def _fresh_db(tmp_path, monkeypatch) -> duckdb.DuckDBPyConnection:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    db_module._system_db_conn = None
    db_module._system_db_path = None
    return db_module.get_system_db()


def _seed_curated_plugin(conn, plugin_name: str, marketplace_id: str = "mp"):
    conn.execute(
        "INSERT OR IGNORE INTO marketplace_registry (id, name, url) "
        "VALUES (?, ?, 'https://example.test/repo.git')",
        [marketplace_id, marketplace_id.upper()],
    )
    conn.execute(
        "INSERT OR IGNORE INTO marketplace_plugins (marketplace_id, name) VALUES (?, ?)",
        [marketplace_id, plugin_name],
    )


def _seed_event(conn, *, occurred_at, skill_name, event_id):
    conn.execute(
        """
        INSERT OR IGNORE INTO usage_events
            (id, session_id, session_file, username, user_id, event_uuid, event_type,
             tool_name, skill_name, is_error, source, ref_id, occurred_at, processor_version)
        VALUES (?, 's1', 's1.jsonl', 'alice', 'uid-alice', NULL, 'tool_use',
                'Skill', ?, FALSE, 'builtin', NULL, ?, 5)
        """,
        [event_id, skill_name, occurred_at],
    )


def test_backfill_populates_daily_and_window_from_historic_events(tmp_path, monkeypatch):
    """Events older than the 7-day incremental window still land in the
    daily fact table after the backfill script runs (it scans from the
    oldest occurred_at, not the default 7-day cutoff)."""
    conn = _fresh_db(tmp_path, monkeypatch)
    _seed_curated_plugin(conn, "myplug")

    now = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    # One old event (15 days ago, outside the default 7-day incremental
    # window) and one recent (1 day ago).
    _seed_event(
        conn, occurred_at=now - timedelta(days=15),
        skill_name="myplug:old-skill", event_id="e-old",
    )
    _seed_event(
        conn, occurred_at=now - timedelta(days=1),
        skill_name="myplug:new-skill", event_id="e-new",
    )

    # Run backfill in-process so the test inherits the same tmp DB.
    from scripts.backfill_marketplace_rollup import main as backfill_main
    rc = backfill_main()
    assert rc == 0

    # Daily fact has rows for both days.
    daily_count = conn.execute(
        "SELECT COUNT(*) FROM usage_marketplace_item_daily "
        "WHERE source='curated' AND type='skill'"
    ).fetchone()[0]
    assert daily_count == 2

    # Window 30d aggregates both into the plugin-level row.
    plugin_row = conn.execute(
        "SELECT invocations FROM usage_marketplace_item_window "
        "WHERE period_label='last_30d' AND type='plugin' AND name='myplug'"
    ).fetchone()
    assert plugin_row is not None
    assert plugin_row[0] == 2
