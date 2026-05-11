"""GET /api/data/{table_id}/download must write to audit_log."""
from src.db import get_system_db


def _grant_table(conn, user_id: str, table_id: str) -> str:
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    grp = UserGroupsRepository(conn).get_by_name(f"dl-{user_id}")
    if not grp:
        grp = UserGroupsRepository(conn).create(
            name=f"dl-{user_id}", description="download-test", created_by="test",
        )
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "table", table_id):
        return grants.create(
            group_id=grp["id"], resource_type="table", resource_id=table_id,
            assigned_by="test",
        )
    existing = next(
        g for g in grants.list_for_groups([grp["id"]], "table")
        if g["resource_id"] == table_id
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
    assert row[4] == "cli"
