"""Server-side tests for /api/v2/scan `from_query` mode (issue #616).

The `from_query` field lets the snapshot path materialize a raw SELECT
(the analyst's original --remote SQL) against BigQuery, reusing the SAME
RBAC + registry-gating that /api/query enforces, but WITHOUT the
remote_scan_too_large cap (the analyst explicitly opted into the
snapshot via --auto-snapshot).
"""

import importlib
from unittest.mock import patch

import pyarrow as pa
import pytest


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _ensure_admin1(conn):
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.users import UserRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    if UserRepository(conn).get_by_id('admin1') is None:
        UserRepository(conn).create(id='admin1', email='admin1@test.com', name='Admin')
    admin_gid = conn.execute(
        'SELECT id FROM user_groups WHERE name = ?', [SYSTEM_ADMIN_GROUP]
    ).fetchone()
    if admin_gid:
        UserGroupMembersRepository(conn).add_member(
            'admin1', admin_gid[0], source='system_seed',
        )


def _seed(conn):
    _ensure_admin1(conn)
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="web_view", name="web_view", source_type="bigquery",
        bucket="ds", source_table="web_view", query_mode="remote",
    )


def _bq(billing="billing-proj", data="data-proj"):
    from connectors.bigquery.access import BqAccess, BqProjects
    return BqAccess(BqProjects(billing=billing, data=data))


def test_from_query_materializes_raw_sql_no_cap(reload_db, monkeypatch):
    """`from_query` runs the raw SELECT and materializes the full Arrow
    result with NO remote_scan_too_large cap."""
    from app.api import v2_scan

    fake_table = pa.table({"country": ["CZ", "US", "CZ"]})

    # The shared run-from-query path materializes via the query.py reuse.
    # Patch the materialize core so we don't need a live BQ.
    monkeypatch.setattr(
        "app.api.query.run_remote_select_to_arrow",
        lambda conn, user, sql, bq, quota: fake_table,
    )

    conn = reload_db.get_system_db()
    try:
        _seed(conn)
        user = {"id": "admin1", "email": "a@x.com"}
        req = {
            "from_query": "SELECT country FROM web_view",
            "as": "auto_deadbeef",
        }
        tracker = v2_scan._build_quota_tracker()
        ipc = v2_scan.run_scan(conn, user, req, bq=_bq(), quota=tracker)
    finally:
        conn.close()

    from app.api.v2_arrow import parse_ipc_bytes
    out = parse_ipc_bytes(ipc)
    assert out.num_rows == 3
    assert out.column("country").to_pylist() == ["CZ", "US", "CZ"]


def test_from_query_and_table_id_select_are_mutually_exclusive(reload_db):
    """`from_query` cannot be combined with select/where (server guard)."""
    from app.api import v2_scan

    conn = reload_db.get_system_db()
    try:
        _seed(conn)
        user = {"id": "admin1", "email": "a@x.com"}
        req = {
            "table_id": "web_view",
            "from_query": "SELECT country FROM web_view",
            "select": ["country"],
        }
        tracker = v2_scan._build_quota_tracker()
        with pytest.raises(ValueError):
            v2_scan.run_scan(conn, user, req, bq=_bq(), quota=tracker)
    finally:
        conn.close()
