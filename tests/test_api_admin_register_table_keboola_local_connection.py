"""POST /api/admin/register-table rejects Keboola local + connection_id.

Issue #1375: per-connection Keboola batch pull does not exist yet, so mixing
`source_type='keboola'`, `query_mode='local'`, and a `connection_id` would
silently use the default/global Keboola credentials instead of the named
connection. Reject it at registration time.
"""


def test_keboola_local_with_connection_id_is_rejected(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "orders",
            "source_type": "keboola",
            "bucket": "in.c-finance",
            "source_table": "orders",
            "query_mode": "local",
            "connection_id": "conn-123",
        },
    )
    assert r.status_code == 422, r.text
    assert "query_mode='local'" in r.json()["detail"][0]["msg"], r.json()


def test_keboola_local_without_connection_id_is_accepted(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "orders",
            "source_type": "keboola",
            "bucket": "in.c-finance",
            "source_table": "orders",
            "query_mode": "local",
        },
    )
    assert r.status_code == 201, r.text


def test_keboola_remote_with_connection_id_is_accepted(seeded_app):
    from src.db import get_system_db
    from src.repositories.source_connections import SourceConnectionsRepository

    conn = get_system_db()
    try:
        SourceConnectionsRepository(conn).create(
            id="conn-123",
            name="conn-123",
            source_type="keboola",
            config={
                "stack_url": "https://connection.keboola.com",
                "project_id": 123,
                "project_name": "Test Project",
            },
            slug="keboola_123",
            alias="kbc_123",
        )
    finally:
        conn.close()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "orders",
            "source_type": "keboola",
            "bucket": "in.c-finance",
            "source_table": "orders",
            "query_mode": "remote",
            "connection_id": "conn-123",
        },
    )
    assert r.status_code == 201, r.text
