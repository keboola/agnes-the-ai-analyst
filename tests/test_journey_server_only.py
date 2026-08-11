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


@pytest.mark.journey
def test_register_bq_live_server_only_rejected_post_coercion(seeded_app, bq_instance):
    """#630 review: a live BQ registration defaults query_mode to 'local',
    sails past the Pydantic validator, and is coerced to 'remote' inside
    _validate_bigquery_register_payload — the post-coercion check must still
    reject server_only=true instead of persisting the incoherent row."""
    c = seeded_app["client"]
    resp = c.post(
        "/api/admin/register-table",
        json={
            "name": "bq_live_so",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "orders",
            "server_only": True,
        },
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 422, resp.text
    assert "server_only" in resp.text


@pytest.mark.journey
def test_put_bq_coercion_cannot_bypass_server_only(seeded_app, bq_instance):
    """#630 review, PUT path: updating a materialized BQ row with
    query_mode='local' + server_only=true passes the pre-coercion merged
    check, but the synthetic re-validation coerces to 'remote' — the row
    must reject with 422, not persist server_only on a remote row."""
    c = seeded_app["client"]
    resp = c.post(
        "/api/admin/register-table",
        json={
            "name": "bq_mat_so",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "orders",
            "query_mode": "materialized",
        },
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 201, resp.text
    table_id = resp.json()["id"]

    resp = c.put(
        f"/api/admin/registry/{table_id}",
        json={"query_mode": "local", "server_only": True},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 422, resp.text
    assert "server_only" in resp.text


@pytest.mark.journey
def test_server_only_table_is_not_downloadable(seeded_app, mock_extract_factory):
    """`server_only` must be a SERVER-side gate, not advice `agnes pull` is
    trusted to follow.

    The flag's whole purpose is "this parquet is not distributed". `agnes pull`
    honours it client-side (`cli/lib/pull.py`), but the bytes were still one
    authenticated GET away: `/api/data/{id}/download` gated on
    `can_access_table` and nothing else, and on Caddy deployments
    `forward_auth` → `check-access` → `file_server` serves the file without
    the app ever seeing the request — so `check-access` is the only place that
    can close that path too.

    Deliberately asserted with the ADMIN token: this is not an authorization
    question. The table is undistributed for everyone, exactly as the manifest
    reports it to everyone.
    """
    c = seeded_app["client"]
    env = seeded_app["env"]
    hdrs = _auth(seeded_app["admin_token"])

    for name, server_only in (("dist_tbl", False), ("nodist_tbl", True)):
        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": name,
                "source_type": "keboola",
                "query_mode": "local",
                "server_only": server_only,
            },
            headers=hdrs,
        )
        assert resp.status_code == 201, resp.text

    mock_extract_factory(
        "keboola",
        [
            {"name": "dist_tbl", "data": [{"id": "1"}]},
            {"name": "nodist_tbl", "data": [{"id": "1"}]},
        ],
    )
    from src.orchestrator import SyncOrchestrator
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    # The distributed table is unaffected — both surfaces still serve it.
    assert c.get("/api/data/dist_tbl/check-access", headers=hdrs).status_code == 204
    assert c.get("/api/data/dist_tbl/download", headers=hdrs).status_code == 200

    # The server_only one is refused on BOTH, and the refusal names the flag
    # so an operator staring at a failed download knows why.
    resp = c.get("/api/data/nodist_tbl/download", headers=hdrs)
    assert resp.status_code == 403, resp.text
    assert "server_only" in resp.text

    resp = c.get("/api/data/nodist_tbl/check-access", headers=hdrs)
    assert resp.status_code == 403, resp.text
    assert "server_only" in resp.text
