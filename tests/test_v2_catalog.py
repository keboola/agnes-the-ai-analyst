import importlib
import pytest


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed_two_tables(conn):
    from src.repositories.table_registry import TableRegistryRepository
    repo = TableRegistryRepository(conn)
    repo.register(
        id="orders", name="orders", source_type="keboola",
        bucket="sales", source_table="orders", query_mode="local",
    )
    repo.register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
    )


def _seed_server_only(conn):
    from src.repositories.table_registry import TableRegistryRepository
    repo = TableRegistryRepository(conn)
    # Materialized on the server but NOT distributed to analyst laptops
    # (`agnes pull` skips server_only rows), so only `agnes query --remote`
    # can query it — local `agnes query` fails with "table does not exist".
    repo.register(
        id="server_metric", name="server_metric", source_type="keboola",
        bucket="usage", source_table="server_metric",
        query_mode="materialized", server_only=True,
    )
    # A BigQuery-materialized table can also be server_only.
    repo.register(
        id="bq_server_metric", name="bq_server_metric", source_type="bigquery",
        bucket="ds", source_table="bq_server_metric",
        query_mode="materialized", server_only=True,
    )


def _make_admin(conn) -> dict:
    """Create an admin user + grant Admin-group membership; return user dict."""
    import uuid
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    uid = "u-" + uuid.uuid4().hex[:8]
    UserRepository(conn).create(id=uid, email=f"{uid}@x.com", name="A")
    admin_gid = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]
    UserGroupMembersRepository(conn).add_member(uid, admin_gid, source="system_seed")
    return {"id": uid, "email": f"{uid}@x.com"}


class TestCatalogShape:
    def test_admin_sees_both_tables(self, reload_db):
        from app.api.v2_catalog import build_catalog
        conn = reload_db.get_system_db()
        try:
            _seed_two_tables(conn)
            admin = _make_admin(conn)
            data = build_catalog(conn, admin)
            ids = {t["id"] for t in data["tables"]}
            assert {"orders", "bq_view"} <= ids
        finally:
            conn.close()

    def test_local_table_has_duckdb_flavor(self, reload_db):
        from app.api.v2_catalog import build_catalog
        conn = reload_db.get_system_db()
        try:
            _seed_two_tables(conn)
            admin = _make_admin(conn)
            data = build_catalog(conn, admin)
            row = next(t for t in data["tables"] if t["id"] == "orders")
            assert row["sql_flavor"] == "duckdb"
            assert row["query_mode"] == "local"
        finally:
            conn.close()

    def test_bq_table_has_bigquery_flavor(self, reload_db):
        from app.api.v2_catalog import build_catalog
        conn = reload_db.get_system_db()
        try:
            _seed_two_tables(conn)
            admin = _make_admin(conn)
            data = build_catalog(conn, admin)
            row = next(t for t in data["tables"] if t["id"] == "bq_view")
            assert row["sql_flavor"] == "bigquery"
            assert row["query_mode"] == "remote"
            assert "where_examples" in row
            assert "fetch_via" in row
        finally:
            conn.close()


class TestServerOnlyCatalog:
    """A ``server_only=true`` table is materialized on the server but not
    synced to analyst laptops. Its catalog metadata must say so: expose the
    ``server_only`` flag and point ``fetch_via`` at ``agnes query --remote``,
    not the misleading "already local" / snapshot-create hints."""

    def test_server_only_keboola_table_points_to_remote(self, reload_db):
        from app.api import v2_catalog
        conn = reload_db.get_system_db()
        try:
            v2_catalog._table_rows_cache.clear()
            _seed_server_only(conn)
            admin = _make_admin(conn)
            data = v2_catalog.build_catalog(conn, admin)
            row = next(t for t in data["tables"] if t["id"] == "server_metric")
            assert "server_only" in row and row["server_only"] is True
            assert "--remote" in row["fetch_via"], row["fetch_via"]
            assert "already local" not in row["fetch_via"]
        finally:
            conn.close()
            v2_catalog._table_rows_cache.clear()

    def test_server_only_bigquery_table_points_to_remote_not_snapshot(self, reload_db):
        from app.api import v2_catalog
        conn = reload_db.get_system_db()
        try:
            v2_catalog._table_rows_cache.clear()
            _seed_server_only(conn)
            admin = _make_admin(conn)
            data = v2_catalog.build_catalog(conn, admin)
            row = next(t for t in data["tables"] if t["id"] == "bq_server_metric")
            assert "server_only" in row and row["server_only"] is True
            assert "--remote" in row["fetch_via"], row["fetch_via"]
            assert "snapshot create" not in row["fetch_via"]
        finally:
            conn.close()
            v2_catalog._table_rows_cache.clear()

    def test_regular_local_table_still_reports_already_local(self, reload_db):
        # No over-correction: a normally-distributed local table keeps its
        # "already local" hint and reports server_only=False.
        from app.api import v2_catalog
        conn = reload_db.get_system_db()
        try:
            v2_catalog._table_rows_cache.clear()
            _seed_two_tables(conn)
            admin = _make_admin(conn)
            data = v2_catalog.build_catalog(conn, admin)
            row = next(t for t in data["tables"] if t["id"] == "orders")
            assert row.get("server_only") is False
            assert "already local" in row["fetch_via"]
        finally:
            conn.close()
            v2_catalog._table_rows_cache.clear()


class TestCatalogCacheRbac:
    """Regression: the per-user payload cache used to leave revoked users
    seeing tables for up to TTL. Cache the underlying rows globally; enforce
    RBAC fresh per request. Same pattern as v2_schema.py / v2_sample.py."""

    def test_rbac_decision_is_fresh_per_call_not_cached(self, reload_db, monkeypatch):
        from app.api import v2_catalog

        conn = reload_db.get_system_db()
        try:
            _seed_two_tables(conn)
            # Use a real (non-admin) user id; the stub can_access_table below
            # decides what they actually see, but build_catalog still needs a
            # real id for the is_user_admin lookup it does internally.
            import uuid
            from src.repositories.users import UserRepository
            uid = "u-" + uuid.uuid4().hex[:8]
            UserRepository(conn).create(id=uid, email=f"{uid}@x.com", name="A")
            user = {"id": uid, "email": f"{uid}@x.com"}

            # First call: a fake can_access_table that grants both tables.
            calls = []

            def grant_all(_user, table_id, _conn):
                calls.append(("grant", table_id))
                return True

            monkeypatch.setattr(v2_catalog, "can_access_table", grant_all)
            data1 = v2_catalog.build_catalog(conn, user)
            ids1 = {t["id"] for t in data1["tables"]}
            assert {"orders", "bq_view"} <= ids1

            # Second call (cache HIT on raw rows): can_access_table now denies
            # `orders`. The user must NOT see it any more — RBAC re-evaluates.
            def deny_orders(_user, table_id, _conn):
                calls.append(("eval", table_id))
                return table_id != "orders"

            monkeypatch.setattr(v2_catalog, "can_access_table", deny_orders)
            data2 = v2_catalog.build_catalog(conn, user)
            ids2 = {t["id"] for t in data2["tables"]}
            assert "orders" not in ids2, \
                f"revoked table 'orders' still visible — cache leaked stale RBAC: {ids2}"
            assert "bq_view" in ids2

            # And RBAC ran on the second call (the eval calls are present).
            assert any(kind == "eval" for kind, _ in calls), \
                "RBAC was not re-evaluated on cached call"
        finally:
            conn.close()
            v2_catalog._table_rows_cache.clear() if hasattr(
                v2_catalog._table_rows_cache, "clear"
            ) else None
