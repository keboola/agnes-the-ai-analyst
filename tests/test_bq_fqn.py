"""Tests for the v51 ``bq_fqn`` decoupling work (issue #343).

Covers:

- ``parse_bq_fqn`` unit cases (valid / empty / malformed shapes).
- Extractor honors ``bq_fqn`` in registry rows: dataset/table override
  for same-project rows; cross-project VIEW path works; cross-project
  BASE TABLE skipped with warning; malformed rejected per-row.
- Orchestrator drift sync: ``_remote_attach.url`` mismatch with overlay
  triggers ``rebuild_from_registry``.
- ``validate_bigquery_startup_config`` warning matrix.
- ``RegisterTableRequest`` accepts ``bq_fqn`` field; register handler
  rejects malformed / non-BQ-source bq_fqn at the API boundary.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from connectors.bigquery.extractor import parse_bq_fqn


class _CapturingProxy:
    """Lightweight DuckDB proxy: intercepts BigQuery extension SQL and
    records every CREATE VIEW we would have emitted against the real
    BQ extension. The extension itself isn't loaded (offline tests),
    so view SQL referencing ``bq.*`` or ``bigquery_query(...)`` would
    fail at create-time — the proxy substitutes a no-op CREATE TABLE
    placeholder so downstream INSERT / verification still works.

    Captured SQL is exposed as ``proxy.create_view_sqls`` for tests
    that need to assert on the path the extractor constructed."""

    def __init__(self, real_conn):
        self._real = real_conn
        self.create_view_sqls: list[str] = []

    def execute(self, sql, *args, **kwargs):
        upper = sql.strip().upper()
        if upper.startswith("INSTALL BIGQUERY") or upper.startswith("LOAD BIGQUERY"):
            return MagicMock()
        if upper.startswith("CREATE SECRET") or upper.startswith("CREATE OR REPLACE SECRET"):
            return MagicMock()
        if "ATTACH" in upper and "BIGQUERY" in upper:
            return MagicMock()
        if upper.startswith("DETACH BQ"):
            return MagicMock()
        if upper.startswith("SET BQ_") or upper.startswith("SELECT CURRENT_SETTING"):
            return MagicMock()
        # View bodies that reference the BQ extension (`bq."ds"."t"` for
        # BASE TABLE or `bigquery_query(...)` for VIEW) would error
        # without a live extension. Capture the SQL for the test, then
        # substitute a placeholder TABLE so subsequent INSERT INTO _meta
        # paths keep working.
        if ("FROM BQ." in upper or "BIGQUERY_QUERY(" in upper) and "CREATE" in upper:
            self.create_view_sqls.append(sql)
            m = re.search(r'VIEW\s+"?(\w+)"?', sql, re.IGNORECASE)
            if m:
                self._real.execute(
                    f'CREATE OR REPLACE TABLE "{m.group(1)}" (dummy INTEGER)'
                )
            return MagicMock()
        return self._real.execute(sql, *args, **kwargs)

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


# ----------------------------------------------------------------------
# parse_bq_fqn — pure unit
# ----------------------------------------------------------------------

class TestParseBqFqn:
    def test_none_returns_none(self):
        assert parse_bq_fqn(None) is None

    def test_empty_string_returns_none(self):
        # Treat "" the same as None — the registry persists '' for
        # cleared values in some paths, and the extractor's fallback
        # branch is the right behavior in both cases.
        assert parse_bq_fqn("") is None

    def test_well_formed_three_segments(self):
        assert parse_bq_fqn("my-proj.my_ds.my_tbl") == (
            "my-proj", "my_ds", "my_tbl",
        )

    @pytest.mark.parametrize("bad", [
        "just_a_table",            # one segment
        "ds.table",                # two segments
        "p.d.t.extra",             # four segments
        ".d.t",                    # empty project
        "p..t",                    # empty dataset
        "p.d.",                    # empty table
    ])
    def test_malformed_raises(self, bad):
        with pytest.raises(ValueError, match="malformed bq_fqn"):
            parse_bq_fqn(bad)

    def test_unsafe_project_rejected(self):
        # `_validate_project_id` accepts the canonical BQ project-id
        # grammar (6-30 lowercase letters/digits/dashes). A space
        # would let an attacker break out of the inline backtick path
        # at view-create time; reject upfront.
        with pytest.raises(ValueError, match="project.*grammar"):
            parse_bq_fqn("bad project.ds.tbl")


# ----------------------------------------------------------------------
# Extractor honors bq_fqn
# ----------------------------------------------------------------------

@pytest.fixture
def output_dir(tmp_path):
    d = tmp_path / "extracts" / "bigquery"
    d.mkdir(parents=True)
    return str(d)


def _run_init_extract(output_dir, project_id, tcs, detect_returns):
    """Run init_extract with mocked auth + entity-type detection through
    the capturing proxy. Returns ``(stats, captured_sqls)`` so tests can
    assert on both the per-row outcome AND the SQL the extractor would
    have sent to the live BQ extension."""
    from connectors.bigquery.extractor import init_extract

    detector = (
        detect_returns if callable(detect_returns)
        else (lambda *a, **kw: detect_returns)
    )

    captured: list[str] = []

    def proxy_connect(path=None, **kwargs):
        real_conn = duckdb.connect(path)
        proxy = _CapturingProxy(real_conn)
        proxy.create_view_sqls = captured  # share list across calls
        return proxy

    with patch("connectors.bigquery.extractor.get_metadata_token", lambda: "x"), \
         patch("connectors.bigquery.extractor._detect_table_type", detector), \
         patch("connectors.bigquery.extractor.duckdb") as mock_mod:
        mock_mod.connect = proxy_connect
        result = init_extract(output_dir, project_id, tcs)
    return result, captured


def _meta_rows(output_dir):
    conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"))
    try:
        return conn.execute(
            "SELECT table_name FROM _meta ORDER BY table_name"
        ).fetchall()
    finally:
        conn.close()


class TestExtractorRespectsBqFqn:
    def test_bq_fqn_overrides_bucket_for_same_project_view(self, output_dir):
        """A row with bq_fqn whose project matches the extractor's ATTACH
        project should use the bq_fqn's dataset/table in the inner view.

        Concretely: bucket='Sessions' (UX label) and bq_fqn=
        'my-project.product_analytics.S2_pageviews' — the bigquery_query
        FROM clause should reference product_analytics.S2_pageviews, NOT
        Sessions.S2_pageviews."""
        tcs = [{
            "id": "s2",
            "name": "s2_session_pageviews",
            "source_type": "bigquery",
            "bucket": "Sessions",          # UX label — must NOT leak into BQ path
            "source_table": "ignored_st",  # should also be overridden
            "bq_fqn": "my-project.product_analytics.S2_pageviews",
            "query_mode": "remote",
            "description": "",
        }]
        result, sqls = _run_init_extract(output_dir, "my-project", tcs, "VIEW")
        assert result["tables_registered"] == 1
        joined = "\n".join(sqls)
        assert "product_analytics" in joined, joined
        assert "S2_pageviews" in joined, joined
        # The UX label must not leak into the BQ path
        assert "Sessions" not in joined, joined

    def test_bq_fqn_view_cross_project_succeeds(self, output_dir):
        """VIEW path uses bigquery_query(billing, ...), which can read across
        projects. A bq_fqn with project ≠ extractor project should still
        register the master view (cross-project SA permissions assumed)."""
        tcs = [{
            "id": "rfm",
            "name": "rfm",
            "source_type": "bigquery",
            "bucket": "RFM",
            "source_table": "ignored",
            "bq_fqn": "other-project.revenue.bk_rfm",
            "query_mode": "remote",
            "description": "",
        }]
        result, sqls = _run_init_extract(output_dir, "my-project", tcs, "VIEW")
        assert result["tables_registered"] == 1
        joined = "\n".join(sqls)
        # Verify the FROM clause carries the cross-project FQN
        assert "other-project.revenue.bk_rfm" in joined, joined
        # Billing project for the BQ job is still the ATTACH project
        assert "bigquery_query('my-project'" in joined, joined

    def test_bq_fqn_base_table_cross_project_skipped(self, output_dir):
        """BASE TABLE path goes through the bq ATTACH alias, which is bound
        to the extractor's project. Cross-project BASE TABLE would silently
        route to the wrong project (data not found there) — skip with a
        warning and do NOT insert _meta so the master view isn't created
        against missing data."""
        tcs = [{
            "id": "xp",
            "name": "xp",
            "source_type": "bigquery",
            "bucket": "OtherDs",
            "source_table": "tbl",
            "bq_fqn": "other-project.OtherDs.tbl",
            "query_mode": "remote",
            "description": "",
        }]
        result, _ = _run_init_extract(output_dir, "my-project", tcs, "BASE TABLE")
        assert result["tables_registered"] == 0
        # No _meta row → orchestrator won't create a master view that
        # would resolve to a nonexistent inner view.
        assert _meta_rows(output_dir) == []

    def test_malformed_bq_fqn_records_per_row_error(self, output_dir):
        tcs = [{
            "id": "ok", "name": "ok", "source_type": "bigquery",
            "bucket": "ds", "source_table": "t",
            "query_mode": "remote", "description": "",
        }, {
            "id": "bad", "name": "bad", "source_type": "bigquery",
            "bucket": "ds", "source_table": "t",
            "bq_fqn": "not.enough",          # malformed
            "query_mode": "remote", "description": "",
        }]
        result, _ = _run_init_extract(output_dir, "my-project", tcs, "BASE TABLE")
        # Good row goes through; bad row recorded as per-row error and
        # does NOT abort the whole extract.
        assert result["tables_registered"] == 1
        assert any("malformed bq_fqn" in e["error"] for e in result["errors"])
        # Only the good row landed in _meta
        rows = _meta_rows(output_dir)
        assert rows == [("ok",)]

    def test_no_bq_fqn_falls_back_to_legacy(self, output_dir):
        """A row without bq_fqn must keep using bucket+source_table+
        ATTACH project, exactly as pre-v51. Backwards-compat guarantee."""
        tcs = [{
            "id": "legacy",
            "name": "legacy",
            "source_type": "bigquery",
            "bucket": "legacy_ds",
            "source_table": "legacy_tbl",
            # bq_fqn intentionally absent
            "query_mode": "remote",
            "description": "",
        }]
        result, sqls = _run_init_extract(output_dir, "my-project", tcs, "BASE TABLE")
        assert result["tables_registered"] == 1
        assert any('bq."legacy_ds"."legacy_tbl"' in s for s in sqls), sqls


# ----------------------------------------------------------------------
# Orchestrator drift sync
# ----------------------------------------------------------------------

class TestOrchestratorBqDriftSync:
    def test_drift_triggers_rebuild_from_registry(self, tmp_path, monkeypatch):
        """When extract.duckdb's _remote_attach.url disagrees with the
        overlay's data_source.bigquery.project, the orchestrator's
        pre-pass should call rebuild_from_registry to regenerate the
        extract before the main scan loop."""
        from src.orchestrator import SyncOrchestrator

        bq_dir = tmp_path / "extracts" / "bigquery"
        bq_dir.mkdir(parents=True)
        extract_path = bq_dir / "extract.duckdb"

        # Create a minimal _remote_attach pointing at the OLD project.
        conn = duckdb.connect(str(extract_path))
        try:
            conn.execute(
                "CREATE TABLE _remote_attach ("
                "alias VARCHAR, extension VARCHAR, url VARCHAR, "
                "token_env VARCHAR)"
            )
            conn.execute(
                "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
                ["bq", "bigquery", "project=stale-project", ""],
            )
        finally:
            conn.close()

        # Overlay says the project is now `fresh-project`.
        monkeypatch.setattr(
            "app.instance_config.get_value",
            lambda *a, **kw: "fresh-project" if a[-1] == "project" else "",
        )

        called = []
        monkeypatch.setattr(
            "connectors.bigquery.extractor.rebuild_from_registry",
            lambda *a, **kw: (called.append(1), {"tables_registered": 0, "errors": []})[1],
        )

        orch = SyncOrchestrator(analytics_db_path=str(tmp_path / "analytics.duckdb"))
        orch._sync_bq_remote_attach_with_overlay(tmp_path / "extracts")
        assert called == [1], "drift detected but rebuild_from_registry was not invoked"

    def test_no_drift_is_noop(self, tmp_path, monkeypatch):
        from src.orchestrator import SyncOrchestrator

        bq_dir = tmp_path / "extracts" / "bigquery"
        bq_dir.mkdir(parents=True)
        extract_path = bq_dir / "extract.duckdb"

        conn = duckdb.connect(str(extract_path))
        try:
            conn.execute(
                "CREATE TABLE _remote_attach ("
                "alias VARCHAR, extension VARCHAR, url VARCHAR, "
                "token_env VARCHAR)"
            )
            conn.execute(
                "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
                ["bq", "bigquery", "project=same-project", ""],
            )
        finally:
            conn.close()

        monkeypatch.setattr(
            "app.instance_config.get_value",
            lambda *a, **kw: "same-project" if a[-1] == "project" else "",
        )
        called = []
        monkeypatch.setattr(
            "connectors.bigquery.extractor.rebuild_from_registry",
            lambda *a, **kw: called.append(1) or {},
        )
        orch = SyncOrchestrator(analytics_db_path=str(tmp_path / "analytics.duckdb"))
        orch._sync_bq_remote_attach_with_overlay(tmp_path / "extracts")
        assert called == [], "no drift but rebuild_from_registry was still called"

    def test_missing_extract_is_noop(self, tmp_path, monkeypatch):
        """Pre-pass on an instance with no BQ extract at all must not
        try to read or rewrite anything. Soft-fails silently."""
        from src.orchestrator import SyncOrchestrator
        called = []
        monkeypatch.setattr(
            "connectors.bigquery.extractor.rebuild_from_registry",
            lambda *a, **kw: called.append(1) or {},
        )
        orch = SyncOrchestrator(analytics_db_path=str(tmp_path / "analytics.duckdb"))
        orch._sync_bq_remote_attach_with_overlay(tmp_path / "extracts")
        assert called == []


# ----------------------------------------------------------------------
# validate_bigquery_startup_config
# ----------------------------------------------------------------------

class TestStartupValidation:
    def test_empty_config_no_warnings(self, monkeypatch):
        from connectors.bigquery.access import validate_bigquery_startup_config
        monkeypatch.setattr("app.instance_config.get_value", lambda *a, **kw: "")
        assert validate_bigquery_startup_config() == []

    def test_same_billing_and_data_project_no_warnings(self, monkeypatch):
        from connectors.bigquery.access import validate_bigquery_startup_config

        def fake_get_value(*args, **kwargs):
            key = args[-1]
            return {
                "project": "my-proj",
                "billing_project": "my-proj",
                "location": "",  # location unset is OK when same project
            }.get(key, "")

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)
        assert validate_bigquery_startup_config() == []

    def test_cross_project_without_location_warns(self, monkeypatch):
        from connectors.bigquery.access import validate_bigquery_startup_config

        def fake_get_value(*args, **kwargs):
            key = args[-1]
            return {
                "project": "data-project",
                "billing_project": "billing-project",
                "location": "",
            }.get(key, "")

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)
        warnings = validate_bigquery_startup_config()
        assert len(warnings) == 1
        assert "location is not set" in warnings[0]
        assert "issue #343" in warnings[0]

    def test_warehouse_like_project_without_billing_warns(self, monkeypatch):
        from connectors.bigquery.access import validate_bigquery_startup_config

        def fake_get_value(*args, **kwargs):
            key = args[-1]
            return {
                "project": "prj-grp-dataview-prod-1ff9",
                "billing_project": "",
                "location": "us-central1",
            }.get(key, "")

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)
        warnings = validate_bigquery_startup_config()
        # Only the warehouse-like heuristic fires (cross-project warning
        # is suppressed because effective_billing == project when billing
        # is unset, regardless of location).
        assert any("warehouse" in w or "serviceusage" in w for w in warnings)


# ----------------------------------------------------------------------
# Admin API surface
# ----------------------------------------------------------------------

class TestRegisterRequestAcceptsBqFqn:
    def test_pydantic_accepts_well_formed(self):
        from app.api.admin import RegisterTableRequest
        r = RegisterTableRequest(
            name="t", source_type="bigquery",
            bucket="ds", source_table="t",
            bq_fqn="proj.ds.t",
        )
        assert r.bq_fqn == "proj.ds.t"

    def test_pydantic_accepts_omitted(self):
        from app.api.admin import RegisterTableRequest
        r = RegisterTableRequest(name="t", source_type="bigquery", bucket="ds", source_table="t")
        assert r.bq_fqn is None

    def test_update_request_accepts_bq_fqn(self):
        from app.api.admin import UpdateTableRequest
        u = UpdateTableRequest(bq_fqn="p.d.t")
        assert u.bq_fqn == "p.d.t"
