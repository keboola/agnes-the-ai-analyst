"""/api/admin/server-config exposes materialize.lock_ttl_seconds and
accepts updates. Default is 86400 (24h).

Fixture `seeded_app` is auto-discovered from `tests/conftest.py` —
DO NOT import. It returns a dict: `{"client": TestClient,
"admin_token": str, ...}`. Auth helper `_auth(token)` mirrors the
project's local pattern (also used in test_api_admin_materialized.py).

Behaviour contract:
  - GET returns `materialize` section in `sections` (empty dict when no
    override is set, since the endpoint surfaces every editable section).
  - GET also exposes the known_fields registry entry for `materialize`
    with `lock_ttl_seconds` spec (kind=int, default=86400).
  - POST with a valid value persists it and GET returns the new value.
  - POST with lock_ttl_seconds < 60 or > 604800 is rejected with 422.
"""
from __future__ import annotations

import pytest
import yaml


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# GET — default state
# ---------------------------------------------------------------------------


def test_get_returns_materialize_in_editable_sections(seeded_app):
    """materialize must appear in editable_sections."""
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    resp = client.get("/api/admin/server-config", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "materialize" in body["editable_sections"]


def test_get_returns_materialize_section_key(seeded_app):
    """materialize key appears in sections (empty dict when no override set)."""
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    resp = client.get("/api/admin/server-config", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    # The endpoint surfaces every editable section so the UI can render it.
    assert "materialize" in body["sections"]


def test_get_returns_materialize_known_fields(seeded_app):
    """known_fields must have a materialize.lock_ttl_seconds entry."""
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    resp = client.get("/api/admin/server-config", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    mat_fields = body.get("known_fields", {}).get("materialize", {})
    assert "lock_ttl_seconds" in mat_fields, body.get("known_fields", {})
    spec = mat_fields["lock_ttl_seconds"]
    assert spec["kind"] == "int"
    assert spec["default"] == 86400


# ---------------------------------------------------------------------------
# POST — update and read back
# ---------------------------------------------------------------------------


def test_put_updates_materialize_lock_ttl(seeded_app, tmp_path, monkeypatch):
    """POST with a valid value persists; GET reflects the new value."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    import app.instance_config as ic
    ic._instance_config = None
    try:
        client = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"materialize": {"lock_ttl_seconds": 3600}}},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        # Verify on disk.
        loaded = yaml.safe_load((state / "instance.yaml").read_text())
        assert loaded["materialize"]["lock_ttl_seconds"] == 3600

        # Verify GET reflects the new value.
        ic._instance_config = None
        resp2 = client.get("/api/admin/server-config", headers=headers)
        assert resp2.json()["sections"]["materialize"]["lock_ttl_seconds"] == 3600
    finally:
        ic._instance_config = None


# ---------------------------------------------------------------------------
# POST — validation
# ---------------------------------------------------------------------------


def test_invalid_lock_ttl_below_min_rejected(seeded_app):
    """lock_ttl_seconds < 60 is rejected with 422."""
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"materialize": {"lock_ttl_seconds": -5}}},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


def test_invalid_lock_ttl_zero_rejected(seeded_app):
    """lock_ttl_seconds=0 is rejected with 422 (below the 60s floor)."""
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"materialize": {"lock_ttl_seconds": 0}}},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


def test_invalid_lock_ttl_above_max_rejected(seeded_app):
    """lock_ttl_seconds > 604800 (1 week) is rejected with 422."""
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"materialize": {"lock_ttl_seconds": 604801}}},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


def test_valid_lock_ttl_boundary_min_accepted(seeded_app, tmp_path, monkeypatch):
    """lock_ttl_seconds=60 (minimum) is accepted."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    import app.instance_config as ic
    ic._instance_config = None
    try:
        client = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"materialize": {"lock_ttl_seconds": 60}}},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
    finally:
        ic._instance_config = None


def test_valid_lock_ttl_boundary_max_accepted(seeded_app, tmp_path, monkeypatch):
    """lock_ttl_seconds=604800 (maximum) is accepted."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    import app.instance_config as ic
    ic._instance_config = None
    try:
        client = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"materialize": {"lock_ttl_seconds": 604800}}},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
    finally:
        ic._instance_config = None
