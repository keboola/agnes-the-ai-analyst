"""GET /api/admin/usage/export — csv/json/parquet streaming."""
from __future__ import annotations

import csv
import io
import json

import pytest


def _seed_events(conn, n=3):
    """Insert n usage_events rows with deterministic shape."""
    from datetime import datetime, timezone

    for i in range(n):
        conn.execute(
            """INSERT INTO usage_events
            (id, session_id, session_file, username, event_uuid, parent_uuid,
             event_type, tool_name, skill_name, subagent_type, command_name,
             is_error, source, ref_id, model, cwd, occurred_at, processor_version)
            VALUES (?, 'sess-1', 'alice/file.jsonl', 'alice',
                    ?, NULL, 'tool_use', 'Bash', NULL, NULL, NULL,
                    false, 'builtin', NULL, 'claude-x', '/tmp', ?, 1)""",
            [
                f"event-{i}",
                f"uuid-{i}",
                datetime(2026, 5, 10 + i, 10, 0, tzinfo=timezone.utc),
            ],
        )


def test_export_csv_default(seeded_app, admin_user):
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    _seed_events(conn, n=3)
    conn.close()
    close_system_db()

    resp = seeded_app["client"].get("/api/admin/usage/export?format=csv", headers=admin_user)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    text = resp.text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][0] == "id"
    assert len(rows) == 4  # header + 3 rows


def test_export_json_ndjson(seeded_app, admin_user):
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    _seed_events(conn, n=2)
    conn.close()
    close_system_db()

    resp = seeded_app["client"].get("/api/admin/usage/export?format=json", headers=admin_user)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = [l for l in resp.text.splitlines() if l.strip()]
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert "id" in rec
    assert rec["tool_name"] == "Bash"


def test_export_parquet(seeded_app, admin_user, tmp_path):
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    _seed_events(conn, n=2)
    conn.close()
    close_system_db()

    resp = seeded_app["client"].get("/api/admin/usage/export?format=parquet", headers=admin_user)
    assert resp.status_code == 200
    out = tmp_path / "out.parquet"
    out.write_bytes(resp.content)
    # Read back via duckdb
    import duckdb as ddb

    rows = ddb.connect().execute(f"SELECT * FROM read_parquet('{out}')").fetchall()
    assert len(rows) == 2


def test_export_filters_by_since(seeded_app, admin_user):
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    _seed_events(conn, n=3)  # events on 2026-05-10, 2026-05-11, 2026-05-12
    conn.close()
    close_system_db()

    resp = seeded_app["client"].get(
        "/api/admin/usage/export?format=csv&since=2026-05-11", headers=admin_user
    )
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert len(rows) - 1 == 2  # 2 events on 2026-05-11 and later


def test_export_filters_by_user(seeded_app, admin_user):
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    _seed_events(conn, n=3)
    conn.close()
    close_system_db()

    resp = seeded_app["client"].get(
        "/api/admin/usage/export?format=csv&user_id=nobody", headers=admin_user
    )
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert len(rows) == 1  # header only


def test_export_filters_by_source(seeded_app, admin_user):
    """source=curated returns only curated events; builtin events excluded."""
    from datetime import datetime, timezone
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    # Seed 2 builtin + 1 curated
    _seed_events(conn, n=2)
    conn.execute(
        """INSERT INTO usage_events
        (id, session_id, session_file, username, event_uuid, parent_uuid,
         event_type, tool_name, skill_name, subagent_type, command_name,
         is_error, source, ref_id, model, cwd, occurred_at, processor_version)
        VALUES ('curated-1', 'sess-1', 'alice/file.jsonl', 'alice',
                'uuid-c1', NULL, 'tool_use', 'Bash', NULL, NULL, NULL,
                false, 'curated', NULL, 'claude-x', '/tmp', ?, 1)""",
        [datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc)],
    )
    conn.close()
    close_system_db()

    resp = seeded_app["client"].get(
        "/api/admin/usage/export?format=csv&source=curated", headers=admin_user
    )
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert len(rows) - 1 == 1  # only the curated row


def test_export_writes_audit_log(seeded_app, admin_user):
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='usage.export'"
    ).fetchone()[0]
    conn.close()
    close_system_db()

    seeded_app["client"].get("/api/admin/usage/export?format=csv", headers=admin_user)

    conn = get_system_db()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='usage.export'"
    ).fetchone()[0]
    row = conn.execute(
        "SELECT params FROM audit_log WHERE action='usage.export' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    close_system_db()

    assert after == before + 1
    params = json.loads(row[0])
    assert params["format"] == "csv"
    assert "row_count" in params


def test_export_rejects_invalid_format(seeded_app, admin_user):
    resp = seeded_app["client"].get(
        "/api/admin/usage/export?format=xml", headers=admin_user
    )
    assert resp.status_code == 422  # FastAPI Literal validation


def test_export_rejects_invalid_since(seeded_app, admin_user):
    resp = seeded_app["client"].get(
        "/api/admin/usage/export?format=csv&since=not-a-date", headers=admin_user
    )
    assert resp.status_code == 400


def test_export_admin_only(seeded_app, analyst_user):
    resp = seeded_app["client"].get(
        "/api/admin/usage/export?format=csv", headers=analyst_user
    )
    assert resp.status_code in (401, 403)
