"""GET /api/data/{table_id}/download must write to audit_log."""
from src.db import get_system_db


def _grant_table(conn, user_id: str, table_id: str) -> str:
    """Stack-gated RBAC: wrap table in auto data_package + grant package."""
    from tests.conftest import grant_table_via_package
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    pkg_id = grant_table_via_package(
        conn, table_id, user_id, group_name=f"dl-{user_id}",
    )
    grp = UserGroupsRepository(conn).get_by_name(f"dl-{user_id}")
    existing = next(
        g for g in ResourceGrantsRepository(conn)
            .list_for_groups([grp["id"]], "data_package")
        if g["resource_id"] == pkg_id
    )
    return existing["id"]


def _setup(seeded_app, mock_extract_factory, table_name="dl_test_tbl"):
    """Register table + create extract on disk + grant analyst access."""
    c = seeded_app["client"]
    admin_hdrs = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    resp = c.post(
        "/api/admin/register-table",
        json={"name": table_name, "source_type": "keboola", "query_mode": "local"},
        headers=admin_hdrs,
    )
    assert resp.status_code == 201

    mock_extract_factory(
        "keboola",
        [{"name": table_name, "data": [{"a": "1", "b": "2"}]}],
    )

    conn = get_system_db()
    try:
        _grant_table(conn, "analyst1", table_name)
    finally:
        conn.close()


def test_data_download_writes_audit_log(seeded_app, analyst_user, mock_extract_factory):
    """Successful parquet download must write exactly one audit_log row."""
    table_name = "dl_test_tbl"
    _setup(seeded_app, mock_extract_factory, table_name)

    c = seeded_app["client"]
    conn = get_system_db()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='data.download'"
    ).fetchone()[0]
    conn.close()

    resp = c.get(f"/api/data/{table_name}/download", headers=analyst_user)
    assert resp.status_code == 200

    conn = get_system_db()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='data.download'"
    ).fetchone()[0]
    row = conn.execute(
        "SELECT user_id, action, resource, result, client_kind "
        "FROM audit_log WHERE action='data.download' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert after == before + 1
    assert row[0] == "analyst1"
    assert row[1] == "data.download"
    assert "dl_test_tbl" in row[2]
    assert row[3] == "success"
    # Session JWT → 'web'; PAT → 'cli'. analyst_user fixture uses a session token.
    assert row[4] == "web"
