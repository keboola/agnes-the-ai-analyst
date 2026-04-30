# tests/test_v2_sample.py
import asyncio
import importlib
from unittest.mock import MagicMock, patch
import pytest
from fastapi import HTTPException


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed(conn):
    _ensure_admin1(conn)
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
    )


def _ensure_admin1(conn):
    """Seed an admin user with id='admin1' + Admin group membership so
    {"id": "admin1", ...} dicts pass the can_access admin shortcut."""
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


def _bq(billing="billing-proj", data="data-proj"):
    """Build a BqAccess wired to default factories. For tests that monkeypatch
    `_fetch_bq_sample` whole, the inner factories are never called."""
    from connectors.bigquery.access import BqAccess, BqProjects
    return BqAccess(BqProjects(billing=billing, data=data))


class TestSampleEndpoint:
    def test_returns_n_rows_for_bq_table(self, reload_db, monkeypatch):
        from app.api import v2_sample
        monkeypatch.setattr(
            v2_sample, "_fetch_bq_sample",
            lambda bq, dataset, table, n: [
                {"event_date": "2026-04-27", "country_code": "CZ"},
                {"event_date": "2026-04-26", "country_code": "SK"},
            ],
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            data = v2_sample.build_sample(conn, user, "bq_view", n=2, bq=_bq())
        finally:
            conn.close()
        assert data["table_id"] == "bq_view"
        assert len(data["rows"]) == 2

    def test_caps_n_at_100(self, reload_db, monkeypatch):
        from app.api import v2_sample
        captured = {}
        def fake_fetch(bq, dataset, table, n):
            captured["n"] = n
            return []
        monkeypatch.setattr(v2_sample, "_fetch_bq_sample", fake_fetch)
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            v2_sample.build_sample(conn, user, "bq_view", n=999, bq=_bq())
        finally:
            conn.close()
        assert captured["n"] == 100

    def test_rbac_check_runs_before_cache(self, reload_db, monkeypatch):
        """Regression: cache check used to come before RBAC, leaking sample rows
        cached by an authorized user to subsequent unauthorized callers."""
        from app.api import v2_sample
        monkeypatch.setattr(
            v2_sample, "_fetch_bq_sample",
            lambda *a, **kw: [{"col": "secret"}],
        )
        monkeypatch.setattr(
            "app.api.v2_sample.can_access_table",
            lambda user, tid, conn: user.get("id") == "admin1",
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            admin = {"id": "admin1", "email": "admin@x.com"}
            v2_sample.build_sample(conn, admin, "bq_view", n=2, bq=_bq())
            other = {"id": "viewer1", "email": "viewer@x.com"}
            with pytest.raises(PermissionError):
                v2_sample.build_sample(conn, other, "bq_view", n=2, bq=_bq())
        finally:
            conn.close()


class TestBqAccessErrors:
    """Issue #134: structured 502 translation on BQ errors in sample path.

    These tests exercise the REAL translation path through `BqAccess` +
    `translate_bq_error` by injecting a duckdb_session whose execute() raises
    the Google API exception. That's the production path — Phase 1
    monkeypatches of `_fetch_bq_sample` whole would skip the translation logic
    and only test the outer wrap (which has been removed in Phase 2).

    Key difference from /scan: /sample SQL is server-constructed (validated
    identifiers + LIMIT n), so a BadRequest from BQ means registry corruption,
    NOT user input → translates to `bq_upstream_error` (HTTP 502), not 400.
    """

    @pytest.fixture(autouse=True)
    def _clear_sample_cache(self):
        """The sample-result TTL cache is module-level; clear it between
        tests so cached payloads from a sibling test don't mask call paths."""
        from app.api import v2_sample
        v2_sample._sample_cache.clear()
        yield
        v2_sample._sample_cache.clear()

    def test_sample_returns_502_on_bq_forbidden_serviceusage(self, reload_db, bq_access):
        """When the BQ extension raises Forbidden mentioning serviceusage,
        the endpoint must translate to HTTP 502 with a structured body
        whose `error` is `cross_project_forbidden` and whose hint mentions
        `billing_project`."""
        from app.api import v2_sample
        from google.api_core.exceptions import Forbidden

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Forbidden(
            "Permission denied: serviceusage.services.use on project foo"
        )
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}

            # Endpoint is async — drive it directly. dependency_overrides only
            # fires through TestClient/HTTP, so pass `bq=bq` explicitly.
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(v2_sample.sample(
                    table_id="bq_view", n=5, user=user, conn=conn, bq=bq,
                ))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "cross_project_forbidden"
        assert "billing_project" in detail["details"]["hint"].lower()

    def test_sample_returns_502_on_bq_forbidden_non_serviceusage(self, reload_db, bq_access):
        """A Forbidden that is NOT about serviceusage (e.g. dataset-level ACL)
        still becomes a 502, but with `bq_forbidden` (no billing_project hint)."""
        from app.api import v2_sample
        from google.api_core.exceptions import Forbidden

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Forbidden(
            "Access Denied: Table foo.bar.baz: User does not have permission"
        )
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}

            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(v2_sample.sample(
                    table_id="bq_view", n=5, user=user, conn=conn, bq=bq,
                ))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"] == "bq_forbidden"

    def test_sample_returns_502_on_bq_bad_request(self, reload_db, bq_access):
        """`/sample` SQL is server-constructed (validated identifiers + LIMIT n),
        so a BQ BadRequest means registry corruption, not user input. Must
        surface as HTTP 502 with `bq_upstream_error` (NOT 400 / `bq_bad_request`
        like /scan does — that's the key difference from Task 2.7)."""
        from app.api import v2_sample
        from google.api_core.exceptions import BadRequest

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = BadRequest(
            "Syntax error: unexpected token at line 1, column 5"
        )
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}

            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(v2_sample.sample(
                    table_id="bq_view", n=5, user=user, conn=conn, bq=bq,
                ))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "bq_upstream_error"
        assert "Syntax error" in detail["message"]

    def test_sample_passes_billing_project_to_bigquery_query(self, reload_db, bq_access):
        """Regression guard: bq.projects.billing must be passed to bigquery_query()
        as the billing project (positional arg 0). Verifies the migration didn't
        regress the original cross-project bug fix."""
        from app.api import v2_sample

        captured = {}

        def _fake_execute(sql, params):
            # Capture the bigquery_query() call args.
            if "bigquery_query" in sql:
                captured["billing_project"] = params[0]
                captured["bq_sql"] = params[1]
            result = MagicMock()
            result.fetchdf.return_value.to_dict.return_value = []
            return result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            asyncio.run(v2_sample.sample(
                table_id="bq_view", n=5, user=user, conn=conn, bq=bq,
            ))
        finally:
            conn.close()

        assert captured["billing_project"] == "billing-proj"
        # FROM clause uses data project (where the table actually lives)
        assert "`data-proj.ds.bq_view`" in captured["bq_sql"]
