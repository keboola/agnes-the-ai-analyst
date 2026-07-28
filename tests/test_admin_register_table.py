"""POST /api/admin/register-table — connection_id acceptance and validation.

Tests the optional ``connection_id`` field added to the register-table endpoint
and the CLI command.  Covers:

- registering without connection_id → 201 (baseline, no regression)
- registering with a valid connection_id → 201
- registering with an unknown connection_id → 400
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _base_payload(name: str = "my_table") -> dict:
    return {
        "name": name,
        "source_type": "keboola",
        "bucket": "in.c-main",
        "source_table": "events",
        "query_mode": "local",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _conn_id(seeded_app) -> str:
    """Create a source connection and return its id."""
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    cid = "conn-test-abc123"
    repo.create(
        id=cid,
        name="test-keboola-conn",
        source_type="keboola",
        config={"stack_url": "https://connection.keboola.com"},
        token_env="KEBOOLA_STORAGE_TOKEN",
        is_default=False,
    )
    return cid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_register_table_without_connection_id(seeded_app):
    """Baseline: register without connection_id still returns 201."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post(
        "/api/admin/register-table",
        json=_base_payload("no_conn_table"),
        headers=_auth(token),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["id"] == "no_conn_table"


def test_register_table_with_valid_connection_id(seeded_app, _conn_id):
    """Providing a connection_id that exists → 201; id round-trips in response."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    payload = _base_payload("with_conn_table")
    payload["connection_id"] = _conn_id

    r = c.post(
        "/api/admin/register-table",
        json=payload,
        headers=_auth(token),
    )
    assert r.status_code == 201
    assert r.json()["id"] == "with_conn_table"


def test_register_table_with_unknown_connection_id(seeded_app):
    """Providing a connection_id that does not exist → 400."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    payload = _base_payload("bad_conn_table")
    payload["connection_id"] = "does-not-exist"

    r = c.post(
        "/api/admin/register-table",
        json=payload,
        headers=_auth(token),
    )
    assert r.status_code == 400
    assert "does-not-exist" in r.json().get("detail", "")
