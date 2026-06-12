"""#607 — end-to-end HTTP journey for the server_only distribution flag.

Register a query_mode='local' table with server_only=true → it appears in
the RBAC-filtered manifest with server_only:true (so `agnes catalog` still
discovers it) while a normal local table sits alongside with server_only
false. The admin-API validator rejects server_only=true + query_mode='remote'.
"""
import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.journey
def test_register_server_only_appears_in_manifest(seeded_app, mock_extract_factory):
    c = seeded_app["client"]
    env = seeded_app["env"]

    # Register one normal local table + one server_only local table.
    for name, server_only in (("normal_tbl", False), ("so_tbl", True)):
        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": name,
                "source_type": "keboola",
                "query_mode": "local",
                "server_only": server_only,
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text

    mock_extract_factory(
        "keboola",
        [
            {"name": "normal_tbl", "data": [{"id": "1"}]},
            {"name": "so_tbl", "data": [{"id": "1"}]},
        ],
    )
    from src.orchestrator import SyncOrchestrator
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    # Admin reads the manifest (admin god-mode short-circuit → both listed).
    resp = c.get("/api/sync/manifest", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200, resp.text
    tables = resp.json()["tables"]
    assert "so_tbl" in tables, f"server_only table must still be listed: {list(tables)}"
    assert tables["so_tbl"]["server_only"] is True
    assert tables["normal_tbl"]["server_only"] is False


@pytest.mark.journey
def test_register_server_only_remote_rejected(seeded_app):
    c = seeded_app["client"]
    resp = c.post(
        "/api/admin/register-table",
        json={
            "name": "bad_remote",
            "source_type": "keboola",
            "query_mode": "remote",
            "server_only": True,
        },
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 422, resp.text
    assert "server_only" in resp.text
