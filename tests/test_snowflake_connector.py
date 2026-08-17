"""Tests for the Snowflake connector: attach, extract, settings, admin registration, sync dispatch, and query guardrail."""

from unittest.mock import MagicMock

import duckdb
import pytest

from connectors.bigquery.extractor import MaterializeBudgetError
from connectors.snowflake.attach import (
    SF_ALIAS,
    SF_EXTENSION,
    SF_TOKEN_ENV,
    attach_snowflake,
    build_remote_attach_url,
    parse_remote_attach_url,
)
from connectors.snowflake.extract_init import init_extract
from connectors.snowflake.extractor import (
    full_table_sql,
    materialize_query,
    split_bucket,
)
from connectors.snowflake.settings import resolve_snowflake_settings


SF_SETTINGS = {
    "account": "xy12345",
    "user": "alice",
    "password": "secret",
    "database": "analytics",
    "warehouse": "compute_wh",
    "role": "analyst",
    "token_env": "SNOWFLAKE_PASSWORD",
}

SF_HOST = "xy12345.snowflakecomputing.com"


@pytest.fixture
def snowflake_instance(monkeypatch):
    """Patch instance.yaml so Snowflake settings resolve from env."""
    cfg = {
        "data_source": {
            "type": "snowflake",
            "snowflake": {
                "account": SF_SETTINGS["account"],
                "user": SF_SETTINGS["user"],
                "database": SF_SETTINGS["database"],
                "warehouse": SF_SETTINGS["warehouse"],
                "role": SF_SETTINGS["role"],
                "token_env": SF_SETTINGS["token_env"],
            },
        },
    }
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda: cfg, raising=False)
    monkeypatch.setenv(SF_SETTINGS["token_env"], SF_SETTINGS["password"])
    from app.instance_config import reset_cache

    reset_cache()
    yield cfg
    reset_cache()


@pytest.fixture
def snowflake_settings():
    return SF_SETTINGS.copy()


# --- attach.py -----------------------------------------------------------------


def test_build_and_parse_remote_attach_url():
    url = build_remote_attach_url(
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
        SF_SETTINGS["role"],
    )
    assert url.startswith(f"https://{SF_HOST}?")
    assert parse_remote_attach_url(url) == {
        "account": "xy12345",
        "database": "analytics",
        "warehouse": "compute_wh",
        "user": "alice",
        "role": "analyst",
    }


def test_build_remote_attach_url_allows_full_host():
    url = build_remote_attach_url(
        "xy12345.snowflakecomputing.com",
        "analytics",
        "compute_wh",
        "alice",
    )
    assert url.startswith("https://xy12345.snowflakecomputing.com?")


def test_build_remote_attach_url_rejects_unsafe_account():
    with pytest.raises(ValueError):
        build_remote_attach_url("evil;drop", "analytics", "compute_wh", "alice")


def test_parse_remote_attach_url_requires_https():
    with pytest.raises(ValueError):
        parse_remote_attach_url("http://xy12345.snowflakecomputing.com?database=analytics")


def test_attach_snowflake_issues_secret_and_attach(monkeypatch):
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    conn = MagicMock()
    url = build_remote_attach_url(
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
        SF_SETTINGS["role"],
    )
    attach_snowflake(conn, alias=SF_ALIAS, url=url, token="secret")
    calls = [c[0][0] for c in conn.execute.call_args_list]
    assert any("CREATE OR REPLACE SECRET" in c for c in calls)
    assert any("ATTACH '' AS sf" in c for c in calls)
    secret_call = next(c for c in calls if "CREATE OR REPLACE SECRET" in c)
    assert "ACCOUNT 'xy12345'" in secret_call
    assert "USER 'alice'" in secret_call
    assert "PASSWORD 'secret'" in secret_call
    assert "DATABASE 'analytics'" in secret_call
    assert "WAREHOUSE 'compute_wh'" in secret_call
    assert "ROLE 'analyst'" in secret_call


def test_attach_snowflake_respects_host_allowlist(monkeypatch):
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", "other.example.com")
    conn = MagicMock()
    url = build_remote_attach_url(
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
    )
    with pytest.raises(ValueError, match="AGNES_REMOTE_ATTACH_HOST_ALLOWLIST"):
        attach_snowflake(conn, alias=SF_ALIAS, url=url, token="secret")


# --- extractor.py ----------------------------------------------------------------


def test_split_bucket_default_and_dotted():
    assert split_bucket("public", "analytics") == ("analytics", "public")
    assert split_bucket("analytics.public", "analytics") == ("analytics", "public")


def test_split_bucket_rejects_traversal():
    with pytest.raises(ValueError):
        split_bucket("../public", "analytics")
    with pytest.raises(ValueError):
        split_bucket("analytics/../public", "analytics")


def test_full_table_sql():
    assert full_table_sql("public", "orders") == 'SELECT * FROM "sf"."public"."orders"'


def test_full_table_sql_rejects_unsafe():
    with pytest.raises(ValueError):
        full_table_sql('public"', "orders")
    with pytest.raises(ValueError):
        full_table_sql("public", "orders;drop")


def _make_stub_duckdb_conn():
    """Return a stub connection that no-ops extension/secret/attach but runs real DuckDB for COPY."""
    real = duckdb.connect(":memory:")
    real.execute("ATTACH ':memory:' AS sf")
    real.execute("CREATE SCHEMA sf.public")
    real.execute("CREATE TABLE sf.public.orders AS SELECT 'EU' AS region, 100 AS revenue")

    class Stub:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *args):
            upper = sql.strip().upper()
            if (
                upper.startswith("INSTALL")
                or upper.startswith("LOAD")
                or upper.startswith("CREATE SECRET")
                or upper.startswith("CREATE OR REPLACE SECRET")
                or upper.startswith("ATTACH")
            ):
                return MagicMock()
            return self._real.execute(sql, *args)

        def close(self):
            self._real.close()

    return Stub(real)


def test_materialize_query_writes_parquet(tmp_path, monkeypatch, snowflake_settings):
    out = tmp_path / "extracts" / "snowflake"
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    monkeypatch.setattr(
        "connectors.snowflake.extractor._open_duckdb",
        lambda path, **kw: _make_stub_duckdb_conn(),
    )
    stats = materialize_query(
        "orders_summary",
        output_dir=str(out),
        source_query='SELECT * FROM "sf"."public"."orders"',
        settings=snowflake_settings,
        max_bytes=None,
    )
    parquet_path = out / "data" / "orders_summary.parquet"
    assert parquet_path.exists()
    assert stats["query_mode"] == "materialized"
    assert stats["rows"] == 1
    assert stats["size_bytes"] > 0
    assert stats["hash"]


def test_materialize_query_enforces_max_bytes(tmp_path, monkeypatch, snowflake_settings):
    out = tmp_path / "extracts" / "snowflake"
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    monkeypatch.setattr(
        "connectors.snowflake.extractor._open_duckdb",
        lambda path, **kw: _make_stub_duckdb_conn(),
    )
    with pytest.raises(MaterializeBudgetError):
        materialize_query(
            "orders_summary",
            output_dir=str(out),
            source_query='SELECT * FROM "sf"."public"."orders"',
            settings=snowflake_settings,
            max_bytes=1,
        )


def test_materialize_query_host_allowlist_blocks(tmp_path, monkeypatch, snowflake_settings):
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", "other.example.com")
    out = tmp_path / "extracts" / "snowflake"
    with pytest.raises(ValueError, match="AGNES_REMOTE_ATTACH_HOST_ALLOWLIST"):
        materialize_query(
            "orders_summary",
            output_dir=str(out),
            source_query='SELECT * FROM "sf"."public"."orders"',
            settings=snowflake_settings,
            max_bytes=None,
        )


# --- extract_init.py --------------------------------------------------------------


def test_init_extract_creates_meta_and_views(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", SF_HOST)
    out = tmp_path / "extracts" / "snowflake"
    attach_calls = []

    def fake_attach(conn, *, url, token):
        attach_calls.append((url, token))
        conn.execute("ATTACH ':memory:' AS sf")
        conn.execute("CREATE SCHEMA sf.public")
        conn.execute("CREATE TABLE sf.public.orders (id INTEGER)")

    stats = init_extract(
        str(out),
        SF_SETTINGS["account"],
        SF_SETTINGS["database"],
        SF_SETTINGS["warehouse"],
        SF_SETTINGS["user"],
        SF_SETTINGS["role"],
        [{"name": "orders", "bucket": "public", "source_table": "orders", "description": ""}],
        token="secret",
        attach_fn=fake_attach,
    )
    assert stats["tables_registered"] == 1
    assert stats["errors"] == []
    assert len(attach_calls) == 1

    extract_db = duckdb.connect(str(out / "extract.duckdb"))
    try:
        meta = extract_db.execute(
            "SELECT table_name, query_mode FROM _meta"
        ).fetchall()
        assert meta == [("orders", "remote")]
        attach_rows = extract_db.execute(
            "SELECT alias, extension, token_env FROM _remote_attach"
        ).fetchall()
        assert attach_rows == [("sf", "snowflake", "SNOWFLAKE_PASSWORD")]
        views = extract_db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'orders'"
        ).fetchall()
        assert views == [("orders",)]
    finally:
        extract_db.close()


# --- settings.py ------------------------------------------------------------------


def test_resolve_snowflake_settings_from_env(snowflake_instance):
    settings = resolve_snowflake_settings()
    assert settings == SF_SETTINGS


def test_resolve_snowflake_settings_missing(monkeypatch):
    cfg = {
        "data_source": {
            "type": "snowflake",
            "snowflake": {
                "account": "xy",
                "user": "u",
                "database": "db",
                "warehouse": "wh",
            },
        },
    }
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda: cfg, raising=False)
    monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)
    from app.instance_config import reset_cache

    reset_cache()
    try:
        assert resolve_snowflake_settings() is None
    finally:
        reset_cache()


# --- admin API --------------------------------------------------------------------


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _sf_payload(**overrides):
    p = {
        "name": "orders",
        "source_type": "snowflake",
        "bucket": "public",
        "source_table": "orders",
        "query_mode": "remote",
    }
    p.update(overrides)
    return p


@pytest.fixture
def stub_snowflake_extract(monkeypatch):
    rebuild = MagicMock(return_value={"tables_registered": 1, "errors": [], "skipped": False})
    monkeypatch.setattr("connectors.snowflake.extract_init.rebuild_from_registry", rebuild)
    return rebuild


def test_admin_precheck_snowflake_valid(seeded_app, snowflake_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table/precheck",
        json=_sf_payload(),
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["table"]["source_type"] == "snowflake"


def test_admin_precheck_missing_bucket(seeded_app, snowflake_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table/precheck",
        json=_sf_payload(bucket=""),
        headers=_auth(token),
    )
    assert resp.status_code == 422
    assert "bucket" in resp.json()["detail"].lower()


def test_admin_precheck_unsafe_view_name(seeded_app, snowflake_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table/precheck",
        json=_sf_payload(name="orders-2026"),
        headers=_auth(token),
    )
    assert resp.status_code in (400, 422)


def test_admin_precheck_unconfigured_instance(seeded_app, monkeypatch):
    """When Snowflake settings are absent, precheck must fail fast."""
    cfg = {"data_source": {"type": "snowflake", "snowflake": {}}}
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda: cfg, raising=False)
    monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)
    from app.instance_config import reset_cache

    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table/precheck",
            json=_sf_payload(),
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "not configured" in resp.json()["detail"].lower()
    finally:
        reset_cache()


def test_admin_register_snowflake_remote(seeded_app, snowflake_instance, stub_snowflake_extract):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table",
        json=_sf_payload(),
        headers=_auth(token),
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "registered"
    stub_snowflake_extract.assert_called_once()


def test_admin_register_snowflake_materialized(seeded_app, snowflake_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/register-table",
        json=_sf_payload(
            query_mode="materialized",
            source_query='SELECT * FROM "sf"."public"."orders"',
        ),
        headers=_auth(token),
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "registered"


# --- sync dispatch ----------------------------------------------------------------


def test_run_materialized_pass_dispatches_snowflake(monkeypatch, tmp_path, snowflake_settings):
    from app.api.sync import _run_materialized_pass

    monkeypatch.setattr("app.api.sync._get_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr("app.api.sync.is_table_due", lambda schedule, last: True)
    monkeypatch.setattr("connectors.snowflake.settings.resolve_snowflake_settings", lambda: snowflake_settings)

    sf_materialize = MagicMock(
        return_value={"rows": 5, "size_bytes": 100, "hash": "abc", "query_mode": "materialized"}
    )
    monkeypatch.setattr("connectors.snowflake.extractor.materialize_query", sf_materialize)

    registry = MagicMock()
    registry.list_all.return_value = [
        {
            "name": "orders_summary",
            "id": "orders_summary",
            "source_type": "snowflake",
            "query_mode": "materialized",
            "source_query": "SELECT region, revenue FROM sf.public.orders",
            "bucket": "public",
            "source_table": "orders",
            "sync_schedule": None,
        }
    ]
    state = MagicMock()
    state.get_last_sync.return_value = None
    monkeypatch.setattr("app.api.sync.table_registry_repo", lambda: registry)
    monkeypatch.setattr("app.api.sync.sync_state_repo", lambda: state)

    summary = _run_materialized_pass(None, None, source_type="snowflake")
    assert summary["materialized"] == ["orders_summary"]
    assert summary["errors"] == []
    assert summary["skipped"] == []
    sf_materialize.assert_called_once()
    call_kwargs = sf_materialize.call_args.kwargs
    assert call_kwargs["table_id"] == "orders_summary"
    assert call_kwargs["settings"] == snowflake_settings


# --- query guardrail --------------------------------------------------------------


def _patch_guardrail(monkeypatch, rows, admin=False):
    repo = MagicMock()
    repo.list_by_source.return_value = rows
    monkeypatch.setattr("src.repositories.table_registry_repo", lambda: repo)
    monkeypatch.setattr("app.api.query._caller_is_unrestricted_admin", lambda *a, **kw: admin)


def test_sf_guardrail_registered_and_accessible(monkeypatch):
    from app.api.query import _sf_guardrail_inputs

    _patch_guardrail(
        monkeypatch,
        [{"id": "t1", "bucket": "public", "source_table": "orders", "name": "orders"}],
    )
    result = _sf_guardrail_inputs(
        'SELECT * FROM sf."public"."orders"',
        'select * from sf."public"."orders"',
        None,
        {},
        ["t1"],
    )
    assert result is None


def test_sf_guardrail_unregistered_path(monkeypatch):
    from app.api.query import _sf_guardrail_inputs

    _patch_guardrail(monkeypatch, [])
    result = _sf_guardrail_inputs(
        'SELECT * FROM sf."public"."missing"',
        'select * from sf."public"."missing"',
        None,
        {},
        [],
    )
    assert result is not None
    assert result["reason"] == "sf_path_not_registered"


def test_sf_guardrail_access_denied(monkeypatch):
    from app.api.query import _sf_guardrail_inputs

    _patch_guardrail(
        monkeypatch,
        [{"id": "t1", "bucket": "public", "source_table": "orders", "name": "orders"}],
    )
    result = _sf_guardrail_inputs(
        'SELECT * FROM sf."public"."orders"',
        'select * from sf."public"."orders"',
        None,
        {},
        [],
    )
    assert result is not None
    assert result["reason"] == "sf_path_access_denied"
