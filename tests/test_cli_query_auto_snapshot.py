"""Tests for `agnes query --remote --auto-snapshot` (issue #616).

Opt-in client-side auto-recovery from the 5 GB `remote_scan_too_large`
cap on BigQuery VIEW targets. With the flag OFF, behavior is byte-for-byte
unchanged (the structured 400 re-raises through the shared renderer).
"""

import json
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.commands.query import _auto_snapshot_id, _normalize_sql

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path / "local"))
    (tmp_path / "config").mkdir()
    (tmp_path / "local").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


def _over_cap_400(view_targets):
    """A structured remote_scan_too_large 400 body (server shape)."""
    return {
        "detail": {
            "reason": "remote_scan_too_large",
            "scan_bytes": 8_000_000_000,
            "limit_bytes": 5_368_709_120,
            "tables": ["ds.web_view"],
            "view_targets": view_targets,
            "suggestion": "use snapshot create ...",
        }
    }


class TestFlagOff:
    def test_over_cap_400_reraises_unchanged_without_flag(self):
        """ACCEPTANCE: without --auto-snapshot, a remote_scan_too_large 400
        re-raises through the shared renderer exactly as today (rc=1)."""
        body = _over_cap_400(["web_view"])
        with patch("cli.client.api_post", return_value=_resp(400, body)):
            result = runner.invoke(
                app, ["query", "SELECT country FROM web_view", "--remote"]
            )
        assert result.exit_code == 1
        # Shared renderer surfaces the structured reason.
        assert "remote_scan_too_large" in result.output

    def test_physical_table_query_identical_with_and_without_flag(self):
        """ACCEPTANCE: a --remote query against a PHYSICAL table (no 400)
        behaves identically with and without the flag."""
        payload = {"columns": ["id"], "rows": [[1]], "truncated": False}
        with patch("cli.client.api_post", return_value=_resp(200, payload)):
            r_off = runner.invoke(
                app, ["query", "SELECT id FROM phys", "--remote", "--json"]
            )
        with patch("cli.client.api_post", return_value=_resp(200, payload)):
            r_on = runner.invoke(
                app,
                ["query", "SELECT id FROM phys", "--remote", "--json", "--auto-snapshot"],
            )
        assert r_off.exit_code == 0
        assert r_on.exit_code == 0
        assert r_off.output == r_on.output

    def test_non_view_over_cap_reraises_even_with_flag(self):
        """A remote_scan_too_large 400 with EMPTY view_targets (physical-table
        over-cap) must re-raise even when --auto-snapshot is on — the fallback
        only applies to VIEW targets."""
        body = _over_cap_400([])  # no view targets
        with patch("cli.client.api_post", return_value=_resp(400, body)):
            result = runner.invoke(
                app,
                ["query", "SELECT * FROM huge_table", "--remote", "--auto-snapshot"],
            )
        assert result.exit_code == 1
        assert "remote_scan_too_large" in result.output

    def test_multi_view_over_cap_materializes_per_view_snapshot(self, tmp_config, monkeypatch):
        """Regression — Devin Review ANALYSIS_0001 on #619.

        A remote_scan_too_large 400 with MULTIPLE view targets now creates
        ONE snapshot per view (each keyed on the view name, each
        materialized as `SELECT * FROM <view>`). The rewritten SQL
        substitutes both views with their respective snapshot IDs and runs
        locally. The previous behaviour skipped multi-view entirely; the
        bug that risked silent-self-join (one snapshot reused across all
        views) was BUG_0001 — both fixes ship together so the multi-view
        path is now correct, not skipped."""
        import duckdb

        snap_a = _auto_snapshot_id("view_a")
        snap_b = _auto_snapshot_id("view_b")
        assert snap_a != snap_b, "per-view IDs must differ"

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute(f'CREATE TABLE "{snap_a}" (id INTEGER, country VARCHAR)')
        conn.execute(f"INSERT INTO \"{snap_a}\" VALUES (1, 'CZ'), (2, 'US')")
        conn.execute(f'CREATE TABLE "{snap_b}" (id INTEGER, amount INTEGER)')
        conn.execute(f"INSERT INTO \"{snap_b}\" VALUES (1, 100), (2, 200)")
        conn.close()

        created = []

        def fake_create(*, view_target, snapshot_id, ttl):
            created.append({"view_target": view_target, "snapshot_id": snapshot_id})
            return None

        body = _over_cap_400(["view_a", "view_b"])
        with patch("cli.client.api_post", return_value=_resp(400, body)), \
             patch("cli.commands.query._create_auto_snapshot", side_effect=fake_create):
            result = runner.invoke(
                app,
                [
                    "query",
                    "SELECT country, amount FROM view_a JOIN view_b USING (id)",
                    "--remote",
                    "--auto-snapshot",
                    "--format", "json",
                ],
            )
        assert result.exit_code == 0, result.output
        # Per-view snapshot creation: each view gets its own (id, materialize).
        assert len(created) == 2
        assert {c["view_target"] for c in created} == {"view_a", "view_b"}
        assert {c["snapshot_id"] for c in created} == {snap_a, snap_b}
        # Verify the JOIN ran on the substituted snapshots (correct rows).
        data = json.loads(result.stdout if hasattr(result, "stdout") else result.output)
        assert {(r["country"], r["amount"]) for r in data} == {("CZ", 100), ("US", 200)}

    def test_other_400_reraises_with_flag(self):
        """A 400 whose reason is NOT remote_scan_too_large re-raises even with
        the flag on."""
        body = {"detail": {"reason": "bq_path_not_registered", "path": "bq.x.y"}}
        with patch("cli.client.api_post", return_value=_resp(400, body)):
            result = runner.invoke(
                app, ["query", "SELECT 1 FROM x", "--remote", "--auto-snapshot"]
            )
        assert result.exit_code == 1
        assert "bq_path_not_registered" in result.output


class TestIdDerivation:
    def test_id_is_per_view_and_deterministic(self):
        """Same view → same auto_<sha8> id; different views → different ids."""
        a = _auto_snapshot_id("web_view")
        a2 = _auto_snapshot_id("WEB_VIEW")  # canonical-case normalization
        b = _auto_snapshot_id("orders_view")
        assert a == a2
        assert a != b
        assert a.startswith("auto_")
        # auto_ + 8 hex chars
        assert len(a) == len("auto_") + 8

    def test_normalize_sql_collapses_whitespace_and_lowercases(self):
        assert _normalize_sql("  SELECT  a\n FROM  t ") == "select a from t"


class TestAutoSnapshotFallback:
    def test_creates_snapshot_then_runs_locally(self, tmp_config, monkeypatch):
        """ACCEPTANCE: --auto-snapshot on an over-cap VIEW completes via ONE
        command — client creates auto_<sha8> via --from-query (mocked), then
        re-runs the SQL with the view substituted to the snapshot locally.
        """
        import duckdb

        sql = "SELECT country FROM web_view"
        snap_id = _auto_snapshot_id("web_view")

        # Local DuckDB the rewritten query runs against. The auto snapshot view
        # is registered there (simulating what `snapshot create` does).
        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute(f'CREATE TABLE "{snap_id}" (country VARCHAR)')
        conn.execute(f"INSERT INTO \"{snap_id}\" VALUES ('CZ'), ('US'), ('CZ')")
        conn.close()

        created = {}

        def fake_create(*, view_target, snapshot_id, ttl):
            created["called"] = True
            created["snapshot_id"] = snapshot_id
            created["view_target"] = view_target
            # Snapshot is assumed registered already (we did it above).
            return None

        body = _over_cap_400(["web_view"])
        with patch("cli.client.api_post", return_value=_resp(400, body)), \
             patch("cli.commands.query._create_auto_snapshot", side_effect=fake_create):
            result = runner.invoke(
                app,
                ["query", sql, "--remote", "--auto-snapshot", "--format", "json"],
            )

        assert created.get("called"), "expected the auto-snapshot to be created"
        assert created["snapshot_id"] == snap_id
        assert created["view_target"] == "web_view"
        assert result.exit_code == 0, result.output
        # The rewritten SQL ran locally against the snapshot → grouped data is
        # the raw rows; the original SQL is `SELECT country FROM web_view`, so
        # output is the 3 rows from the snapshot.
        data = json.loads(result.stdout if hasattr(result, "stdout") else result.output)
        assert {r["country"] for r in data} == {"CZ", "US"}

    def test_reuses_fresh_snapshot_no_recreate(self, tmp_config, monkeypatch):
        """ACCEPTANCE: repeat invocation within the TTL reuses the snapshot
        (no rebuild). A fresh auto_<sha8> already present → _create not called.
        """
        import duckdb
        from datetime import datetime, timedelta, timezone
        from cli.snapshot_meta import SnapshotMeta, write_meta

        sql = "SELECT country FROM web_view"
        snap_id = _auto_snapshot_id("web_view")

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute(f'CREATE TABLE "{snap_id}" (country VARCHAR)')
        conn.execute(f"INSERT INTO \"{snap_id}\" VALUES ('CZ')")
        conn.close()

        # Write a FRESH snapshot meta (expires 23h in the future).
        snap_dir = tmp_config / "local" / "user" / "snapshots"
        snap_dir.mkdir(parents=True)
        future = (datetime.now(timezone.utc) + timedelta(hours=23)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        write_meta(snap_dir, SnapshotMeta(
            name=snap_id, table_id="web_view", select=None, where=None,
            limit=None, order_by=None, fetched_at=now, effective_as_of=now,
            rows=1, bytes_local=10, estimated_scan_bytes_at_fetch=0,
            result_hash_md5="x", expires_at=future,
        ))

        body = _over_cap_400(["web_view"])
        with patch("cli.client.api_post", return_value=_resp(400, body)), \
             patch("cli.commands.query._create_auto_snapshot") as mock_create:
            result = runner.invoke(
                app,
                ["query", sql, "--remote", "--auto-snapshot", "--format", "json"],
            )
        assert result.exit_code == 0, result.output
        mock_create.assert_not_called()

    def test_end_to_end_real_create_chain(self, tmp_config, monkeypatch):
        """Full chain WITHOUT mocking _create_auto_snapshot: the only mock is
        the network (api_post for /api/query and api_post_arrow for the
        snapshot materialize). Proves _create_auto_snapshot → _create_snapshot
        → view registration → local re-run actually works (#616).
        """
        import duckdb
        import pyarrow as pa

        sql = "SELECT country FROM web_view"
        snap_id = _auto_snapshot_id("web_view")

        # Local DuckDB must exist for the snapshot-create fetch-path guard.
        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        duckdb.connect(str(db_dir / "analytics.duckdb")).close()

        # The materialize returns the view's rows as Arrow.
        arrow_table = pa.table({"country": ["CZ", "US", "CZ"]})
        body = _over_cap_400(["web_view"])

        with patch("cli.client.api_post", return_value=_resp(400, body)), \
             patch("cli.commands.snapshot.api_post_arrow", return_value=arrow_table):
            result = runner.invoke(
                app,
                ["query", sql, "--remote", "--auto-snapshot", "--format", "json"],
            )

        assert result.exit_code == 0, result.output
        # The snapshot view was registered and the rewritten query ran locally.
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"), read_only=True)
        n = conn.execute(f'SELECT COUNT(*) FROM "{snap_id}"').fetchone()[0]
        conn.close()
        assert n == 3
        data = json.loads(result.stdout if hasattr(result, "stdout") else result.output)
        assert {r["country"] for r in data} == {"CZ", "US"}

    def test_stale_snapshot_is_recreated(self, tmp_config, monkeypatch):
        """An EXPIRED auto_<sha8> is rebuilt (TTL elapsed)."""
        import duckdb
        from datetime import datetime, timedelta, timezone
        from cli.snapshot_meta import SnapshotMeta, write_meta

        sql = "SELECT country FROM web_view"
        snap_id = _auto_snapshot_id("web_view")

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute(f'CREATE TABLE "{snap_id}" (country VARCHAR)')
        conn.execute(f"INSERT INTO \"{snap_id}\" VALUES ('CZ')")
        conn.close()

        snap_dir = tmp_config / "local" / "user" / "snapshots"
        snap_dir.mkdir(parents=True)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        write_meta(snap_dir, SnapshotMeta(
            name=snap_id, table_id="web_view", select=None, where=None,
            limit=None, order_by=None, fetched_at=now, effective_as_of=now,
            rows=1, bytes_local=10, estimated_scan_bytes_at_fetch=0,
            result_hash_md5="x", expires_at=past,
        ))

        body = _over_cap_400(["web_view"])
        with patch("cli.client.api_post", return_value=_resp(400, body)), \
             patch("cli.commands.query._create_auto_snapshot") as mock_create:
            result = runner.invoke(
                app,
                ["query", sql, "--remote", "--auto-snapshot", "--format", "json"],
            )
        assert result.exit_code == 0, result.output
        mock_create.assert_called_once()


class TestDevinFindings:
    """Regression tests pinning the Devin Review fixes on #620.

    BUG_0001 — auto-snapshot must materialize the RAW VIEW (`SELECT *
    FROM <view>`), not the user's full query, or every transformation in
    the original SQL (WHERE, GROUP BY, DISTINCT, COUNT, …) would
    double-apply when the rewritten query runs against the snapshot.

    BUG_0002 — `_substitute_view` must be case-insensitive: the registry
    canonical ID stored in `view_targets` may differ from the casing the
    user typed in their SQL, and a case-sensitive substitution would
    leave the original view name in the rewritten SQL and explode locally
    with "table not found".
    """

    def test_snapshot_materializes_raw_view_not_full_query(self, tmp_config, monkeypatch):
        """The `from_query` sent to `_create_snapshot` must be `SELECT *
        FROM <view_target>`, NOT the user's full SQL. Otherwise the
        snapshot contains the already-transformed result and the rewritten
        local query double-applies every transformation."""
        sql = "SELECT country, COUNT(*) AS n FROM web_view GROUP BY 1"
        captured = {}

        def fake_create_snapshot(*, table_id, from_query, as_name, ttl, force, quiet):
            captured["from_query"] = from_query

        # Need a local DuckDB present + the snapshot table so the rewritten
        # query can run without erroring (the fake just intercepts the call).
        import duckdb
        snap_id = _auto_snapshot_id("web_view")
        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute(f'CREATE TABLE "{snap_id}" (country VARCHAR)')
        conn.execute(f"INSERT INTO \"{snap_id}\" VALUES ('CZ'), ('CZ'), ('US')")
        conn.close()

        body = _over_cap_400(["web_view"])
        with patch("cli.client.api_post", return_value=_resp(400, body)), \
             patch("cli.commands.snapshot._create_snapshot", side_effect=fake_create_snapshot):
            result = runner.invoke(
                app,
                ["query", sql, "--remote", "--auto-snapshot", "--format", "json"],
            )

        # CRITICAL: the snapshot must materialize the RAW view, not the
        # user's full query with its GROUP BY / aggregations.
        assert captured.get("from_query") == "SELECT * FROM web_view"
        assert "GROUP BY" not in (captured.get("from_query") or "")
        assert "COUNT" not in (captured.get("from_query") or "")
        # And the rewritten local query produced the correct aggregation
        # (counts grouped by country — CZ=2, US=1 — not double-applied).
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout if hasattr(result, "stdout") else result.output)
        rows = {(r["country"], r["n"]) for r in data}
        assert rows == {("CZ", 2), ("US", 1)}

    def test_substitute_view_is_case_insensitive(self):
        """The user typed `Web_View` but the registry canonical ID is
        `web_view` — substitution must still rewrite the SQL or the local
        re-run dies with "table not found"."""
        from cli.commands.query import _substitute_view

        sql = "SELECT * FROM Web_View WHERE id > 10"
        rewritten = _substitute_view(sql, "web_view", "auto_abc12345")
        # Original casing replaced regardless of how the user typed it.
        assert "auto_abc12345" in rewritten
        assert "Web_View" not in rewritten
