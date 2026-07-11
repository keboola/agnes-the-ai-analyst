"""Backend-routing coverage for GET /api/admin/telemetry/export.

The repository export methods have cross-engine contract tests, but a
regression back to ``Depends(_get_db)`` (always DuckDB) at the *endpoint*
layer would still pass those and export zero / stale rows after a Postgres
cutover. This exercises the route boundary on BOTH backends: seed a
telemetry event through the factory, hit the endpoint with admin auth, and
assert the seeded event is in the exported bytes.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration


def _admin_headers(s):
    return {"Authorization": f"Bearer {s['admin_token']}"}


def _seed_event(event_id: str, username: str = "alice") -> None:
    from src.repositories import usage_repo

    usage_repo().upsert_events(
        [
            {
                "id": event_id,
                "session_id": "sess-1",
                "session_file": f"{username}/x.jsonl",
                "username": username,
                "event_type": "tool_use",
                "tool_name": "Bash",
                "is_error": False,
                "source": "builtin",
                "occurred_at": datetime(2026, 5, 12, tzinfo=timezone.utc),
            }
        ],
        processor_version=1,
    )


def test_export_csv_routes_through_active_backend(seeded_app_both):
    """The seeded event must appear in the CSV export on DuckDB *and* Postgres."""
    _seed_event("evt-routing-1")

    resp = seeded_app_both["client"].get(
        "/api/admin/telemetry/export?format=csv", headers=_admin_headers(seeded_app_both)
    )
    assert resp.status_code == 200, seeded_app_both["backend"]
    rows = list(csv.reader(io.StringIO(resp.text)))
    header = rows[0]
    assert "id" in header
    id_col = header.index("id")
    ids = {r[id_col] for r in rows[1:]}
    assert "evt-routing-1" in ids, (
        f"seeded event missing from export on backend={seeded_app_both['backend']} "
        "— endpoint likely read the wrong (always-DuckDB) connection"
    )


def test_export_ndjson_routes_through_active_backend(seeded_app_both):
    """Same guarantee for the NDJSON format path."""
    import json

    _seed_event("evt-routing-2")

    resp = seeded_app_both["client"].get(
        "/api/admin/telemetry/export?format=json", headers=_admin_headers(seeded_app_both)
    )
    assert resp.status_code == 200, seeded_app_both["backend"]
    ids = {json.loads(line)["id"] for line in resp.text.splitlines() if line.strip()}
    assert "evt-routing-2" in ids, seeded_app_both["backend"]
