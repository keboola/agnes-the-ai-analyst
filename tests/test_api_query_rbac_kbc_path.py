"""POST /api/query gates direct `kbc_alias."<bucket>"."<table>"` references.

Issue #1375: per-connection Keboola aliases mean the legacy single `kbc`
catalog is now one of many. Direct catalog paths must resolve to a registered
`query_mode='remote'` Keboola table and, for non-admins, be within the caller's
access grant set.
"""

from fastapi.testclient import TestClient

from src.db import get_system_db
from src.repositories.source_connections import SourceConnectionsRepository
from src.repositories.table_registry import TableRegistryRepository


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_keboola_connection(
    conn_id: str,
    alias: str,
    is_default: bool = False,
    project_id: int = 123,
    project_name: str = "Test Project",
) -> None:
    conn = get_system_db()
    try:
        SourceConnectionsRepository(conn).create(
            id=conn_id,
            name=f"conn-{conn_id}",
            source_type="keboola",
            config={
                "stack_url": "https://connection.keboola.com",
                "project_id": project_id,
                "project_name": project_name,
            },
            is_default=is_default,
            slug=alias,
            alias=alias,
        )
    finally:
        conn.close()


def _register_remote_kbc_table(
    name: str,
    bucket: str,
    source_table: str,
    conn_id: str,
    alias: str,
) -> None:
    _create_keboola_connection(conn_id, alias, is_default=(alias == "kbc"))
    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=f"{alias}.{bucket}.{source_table}",
            name=name,
            source_type="keboola",
            bucket=bucket,
            source_table=source_table,
            query_mode="remote",
            connection_id=conn_id,
        )
    finally:
        conn.close()


def _kbc_reason(response) -> str | None:
    detail = response.json().get("detail")
    if isinstance(detail, dict):
        return detail.get("reason")
    return None


def test_unregistered_default_kbc_path_rejected_with_403(seeded_app):
    c: TestClient = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        headers=_auth(token),
        json={"sql": 'SELECT * FROM kbc."in.c-finance"."orders"'},
    )
    assert r.status_code == 403, r.text
    assert _kbc_reason(r) == "kbc_path_not_registered", r.json()


def test_registered_default_kbc_path_passes_gate(seeded_app):
    _register_remote_kbc_table("orders", "in.c-finance", "orders", "conn-default", "kbc")
    c: TestClient = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        headers=_auth(token),
        json={"sql": 'SELECT * FROM kbc."in.c-finance"."orders"'},
    )
    assert _kbc_reason(r) != "kbc_path_not_registered"
    assert _kbc_reason(r) != "kbc_path_access_denied"
    # The KBC extension is not installed in tests; we only assert the gate
    # did not reject for registry/access reasons. Execution failures are OK.


def test_unregistered_per_connection_alias_rejected_with_403(seeded_app):
    # Alias exists in source_connections but the bucket/table is not registered.
    _create_keboola_connection("conn-proj1", "kbc_proj1")
    c: TestClient = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        headers=_auth(token),
        json={"sql": 'SELECT * FROM kbc_proj1."in.c-finance"."orders"'},
    )
    assert r.status_code == 403, r.text
    assert _kbc_reason(r) == "kbc_path_not_registered", r.json()


def test_registered_per_connection_alias_passes_gate(seeded_app):
    _register_remote_kbc_table(
        "orders_proj1",
        "in.c-finance",
        "orders",
        "conn-proj1",
        "kbc_proj1",
    )
    c: TestClient = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        headers=_auth(token),
        json={"sql": 'SELECT * FROM kbc_proj1."in.c-finance"."orders"'},
    )
    assert _kbc_reason(r) != "kbc_path_not_registered"
    assert _kbc_reason(r) != "kbc_path_access_denied"
