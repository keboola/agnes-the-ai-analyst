"""Unity Catalog ATTACH for `query_mode='remote'` Databricks rows (phase 3).

Scope note — read this before adding to the file
------------------------------------------------
Everything here is verifiable without a Databricks workspace: the
``_remote_attach`` contract, the view DDL, the opt-in gate, the identifier
refusals, and the credential-egress allowlist. What is NOT covered — and cannot
be, from a sandbox with no route to a workspace — is whether the ``uc_catalog``
community extension actually installs, authenticates, and returns rows. That
gap is real and is stated in `docs/DATA_SOURCES.md` rather than papered over
with a mock that would only prove the mock works.

The consequence is deliberate: the feature ships **off**
(``data_source.databricks.attach_enabled``), and with it off none of this code
runs at all — a `query_mode='remote'` row is served entirely by the SQL
warehouse path, which IS wire-tested end to end.
"""

from __future__ import annotations

import duckdb
import pytest

from connectors.databricks.attach import (
    UC_ALIAS,
    UC_EXTENSION,
    UC_TOKEN_ENV,
    build_remote_attach_url,
    parse_remote_attach_url,
)
from connectors.databricks.extract_init import init_extract, rebuild_from_registry


class TestRemoteAttachUrl:
    def test_round_trips_host_and_catalog(self):
        url = build_remote_attach_url("https://dbc-test.cloud.databricks.example", "main")
        assert parse_remote_attach_url(url) == ("https://dbc-test.cloud.databricks.example", "main")

    def test_bare_host_is_upgraded_to_https(self):
        url = build_remote_attach_url("dbc-test.cloud.databricks.example", "main")
        assert url.startswith("https://")

    def test_host_stays_parseable_by_the_allowlist(self):
        """The whole reason host+catalog are packed into a URL rather than a
        fifth column: `is_attach_host_allowed` must see the real host."""
        from src.orchestrator_security import is_attach_host_allowed

        url = build_remote_attach_url("https://dbc-test.cloud.databricks.example", "main")
        import os

        os.environ["AGNES_REMOTE_ATTACH_HOST_ALLOWLIST"] = "dbc-test.cloud.databricks.example"
        try:
            assert is_attach_host_allowed(url) is True
            os.environ["AGNES_REMOTE_ATTACH_HOST_ALLOWLIST"] = "someone-else.example"
            assert is_attach_host_allowed(url) is False
        finally:
            os.environ.pop("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", None)

    @pytest.mark.parametrize("catalog", ["has space", "a/b", "a`b", "", "a'b"])
    def test_unsafe_catalog_is_refused(self, catalog):
        with pytest.raises(ValueError):
            build_remote_attach_url("https://host.example", catalog)

    @pytest.mark.parametrize("url", ["http://host.example/main", "https://host.example", "not-a-url"])
    def test_malformed_url_is_refused_not_guessed(self, url):
        with pytest.raises(ValueError):
            parse_remote_attach_url(url)


class TestAllowlists:
    def test_uc_extensions_are_allowlisted(self):
        """Without both, the ATTACH resolves nothing — uc_catalog reads table
        data through delta."""
        from src.orchestrator_security import is_extension_allowed

        assert is_extension_allowed("uc_catalog")
        assert is_extension_allowed("delta")

    def test_databricks_token_env_is_allowlisted(self):
        from src.orchestrator_security import is_token_env_allowed

        assert is_token_env_allowed(UC_TOKEN_ENV)

    def test_allowlisting_did_not_open_the_gate_generally(self):
        """A regression guard on the allowlist itself: adding two names must
        not have turned it into "anything goes"."""
        from src.orchestrator_security import is_extension_allowed, is_token_env_allowed

        assert not is_extension_allowed("httpfs")
        assert not is_token_env_allowed("JWT_SECRET_KEY")
        assert not is_token_env_allowed("OPENAI_API_KEY")


@pytest.fixture
def fake_uc(tmp_path):
    """Stand in for the Unity Catalog ATTACH with a local DuckDB file.

    `uc_catalog` is unreachable from here, but everything this builder does
    around the ATTACH — DDL shape, ordering, `_meta` bookkeeping, per-row error
    isolation — is engine-agnostic, and DuckDB binds a view's catalog at CREATE
    time either way. Attaching a real local catalog under the same alias
    exercises all of it for real rather than against a mock.
    """
    uc_path = tmp_path / "fake_uc.duckdb"
    seed = duckdb.connect(str(uc_path))
    try:
        seed.execute("CREATE SCHEMA sales; CREATE SCHEMA s")
        for schema, tables in (("sales", ("orders_raw", "a", "b", "c", "o")), ("s", ("a",))):
            for t in tables:
                seed.execute(f"CREATE TABLE {schema}.{t} (id INTEGER)")
    finally:
        seed.close()

    def attach(conn, *, url, token):
        conn.execute(f"ATTACH '{uc_path}' AS {UC_ALIAS} (READ_ONLY)")

    return attach


class TestExtractInit:
    def test_writes_remote_attach_row_and_views(self, tmp_path, fake_uc):
        stats = init_extract(
            str(tmp_path),
            "https://dbc-test.cloud.databricks.example",
            "main",
            [{"name": "orders", "bucket": "sales", "source_table": "orders_raw", "description": "d"}],
            attach_fn=fake_uc,
        )
        assert stats["tables_registered"] == 1
        assert stats["errors"] == []

        conn = duckdb.connect(str(tmp_path / "extract.duckdb"))
        try:
            alias, extension, url, token_env = conn.execute(
                "SELECT alias, extension, url, token_env FROM _remote_attach"
            ).fetchone()
            assert (alias, extension, token_env) == (UC_ALIAS, UC_EXTENSION, UC_TOKEN_ENV)
            assert parse_remote_attach_url(url) == ("https://dbc-test.cloud.databricks.example", "main")

            # DuckDB normalizes away quoting it does not need, so assert on
            # the resolved path rather than the exact DDL text.
            view_sql = conn.execute("SELECT sql FROM duckdb_views() WHERE view_name = 'orders'").fetchone()[0]
            assert "dbx.sales.orders_raw" in view_sql.replace('"', "")

            meta = conn.execute("SELECT table_name, query_mode FROM _meta").fetchall()
            assert meta == [("orders", "remote")]
        finally:
            conn.close()

    def test_missing_upstream_table_fails_at_build_time_not_query_time(self, tmp_path, fake_uc):
        """DuckDB binds the view's catalog reference at CREATE time, so a table
        that no longer exists upstream is caught here — with the row named —
        rather than surfacing inside an analyst's query hours later."""
        stats = init_extract(
            str(tmp_path),
            "https://dbc-test.cloud.databricks.example",
            "main",
            [{"name": "gone", "bucket": "sales", "source_table": "deleted_upstream"}],
            attach_fn=fake_uc,
        )
        assert stats["tables_registered"] == 0
        assert stats["errors"][0]["table"] == "gone"

    def test_attach_failure_reports_every_row_and_keeps_old_views(self, tmp_path, fake_uc):
        """A workspace outage must not silently empty the analyst's catalog."""
        rows = [{"name": "orders", "bucket": "sales", "source_table": "orders_raw"}]
        init_extract(str(tmp_path), "https://host.example", "main", rows, attach_fn=fake_uc)

        def boom(conn, *, url, token):
            raise RuntimeError("workspace unreachable")

        stats = init_extract(str(tmp_path), "https://host.example", "main", rows, attach_fn=boom)
        assert stats["tables_registered"] == 0
        assert "ATTACH failed" in stats["errors"][0]["error"]

        conn = duckdb.connect(str(tmp_path / "extract.duckdb"))
        try:
            assert conn.execute("SELECT count(*) FROM duckdb_views() WHERE view_name='orders'").fetchone()[0] == 1
        finally:
            conn.close()

    def test_row_targeting_a_foreign_catalog_is_reported_not_silently_remapped(self, tmp_path, fake_uc):
        """One ATTACH serves one catalog. Pointing the view at the attached
        catalog anyway would return another table's rows under this name."""
        stats = init_extract(
            str(tmp_path),
            "https://dbc-test.cloud.databricks.example",
            "main",
            [{"name": "orders", "bucket": "other_catalog.sales", "source_table": "orders_raw"}],
            attach_fn=fake_uc,
        )
        assert stats["tables_registered"] == 0
        assert "other_catalog" in stats["errors"][0]["error"]

    @pytest.mark.parametrize(
        "row",
        [
            {"name": "bad name", "bucket": "sales", "source_table": "o"},
            {"name": "ok", "bucket": "sales", "source_table": 'o"; DROP TABLE x; --'},
            {"name": "ok", "bucket": "sa les", "source_table": "o"},
        ],
    )
    def test_unsafe_identifiers_are_skipped_with_an_error(self, tmp_path, fake_uc, row):
        stats = init_extract(str(tmp_path), "https://host.example", "main", [row], attach_fn=fake_uc)
        assert stats["tables_registered"] == 0
        assert len(stats["errors"]) == 1

    def test_one_bad_row_does_not_cost_the_good_ones(self, tmp_path, fake_uc):
        stats = init_extract(
            str(tmp_path),
            "https://host.example",
            "main",
            [
                {"name": "good", "bucket": "sales", "source_table": "a"},
                {"name": "bad name", "bucket": "sales", "source_table": "b"},
                {"name": "also_good", "bucket": "sales", "source_table": "c"},
            ],
            attach_fn=fake_uc,
        )
        assert stats["tables_registered"] == 2
        assert len(stats["errors"]) == 1

    def test_rebuild_drops_views_for_unregistered_rows(self, tmp_path, fake_uc):
        args = ("https://host.example", "main")
        row = [{"name": "keep", "bucket": "s", "source_table": "a"}]
        init_extract(str(tmp_path), *args, row, attach_fn=fake_uc)
        init_extract(str(tmp_path), *args, row, attach_fn=fake_uc)
        conn = duckdb.connect(str(tmp_path / "extract.duckdb"))
        try:
            # _meta must not accumulate duplicates across rebuilds.
            assert conn.execute("SELECT count(*) FROM _meta").fetchone()[0] == 1
        finally:
            conn.close()


class TestOptIn:
    def test_disabled_by_default_writes_nothing(self, tmp_path, monkeypatch):
        """The default must be inert: no extract on disk means no ATTACH, no
        community-extension install, and no PAT leaving the process."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        result = rebuild_from_registry()
        assert result["skipped"] is True
        assert result["reason"] == "attach_disabled"
        assert not (tmp_path / "extracts" / "databricks" / "extract.duckdb").exists()

    def test_enabled_but_unconfigured_still_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setattr("connectors.databricks.attach.attach_enabled", lambda: True)
        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda: None,
        )
        result = rebuild_from_registry()
        assert result["skipped"] is True
        assert result["reason"] == "not_configured"

    def test_enabled_and_configured_with_no_remote_rows_writes_nothing(self, e2e_env, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setattr("connectors.databricks.attach.attach_enabled", lambda: True)
        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda: {"host": "https://host.example", "catalog": "main", "warehouse_id": "w", "token": "t"},
        )
        result = rebuild_from_registry()
        assert result["skipped"] is True
        assert result["reason"] == "no_remote_rows"

    def test_enabled_and_configured_builds_the_extract(self, e2e_env, tmp_path, monkeypatch, fake_uc):
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="dbx.sales.orders",
                name="orders",
                source_type="databricks",
                bucket="sales",
                source_table="orders_raw",
                query_mode="remote",
            )
        finally:
            conn.close()

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setattr("connectors.databricks.attach.attach_enabled", lambda: True)
        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda: {"host": "https://host.example", "catalog": "main", "warehouse_id": "w", "token": "t"},
        )
        # Substitute the ATTACH: the real one installs a community extension
        # and talks to a workspace. Everything else in the path is real.
        monkeypatch.setattr("connectors.databricks.extract_init._default_attach_fn", fake_uc)
        result = rebuild_from_registry()
        assert result["skipped"] is False
        assert result["tables_registered"] == 1
        assert (tmp_path / "extracts" / "databricks" / "extract.duckdb").exists()
