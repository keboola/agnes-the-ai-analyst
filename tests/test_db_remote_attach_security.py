"""Issue #81 Group A — query-path (db.py) trust-boundary tests.

Mirror of `tests/test_orchestrator_remote_attach_security.py` for
`src/db.py:_reattach_remote_extensions`. The query path runs on every
`/api/query` request via `get_analytics_db_readonly()`; it must enforce
the same allowlists as the rebuild path or the security guarantee is
hollow.

Setup is real-DuckDB (not mock-conn) because db.py introspects via
`information_schema.tables`/`duckdb_databases()` rather than just
executing whatever SQL we hand it. We feed it a real extract.duckdb
with a programmable `_remote_attach` row, ATTACH it, then call the
function and assert which `LOAD/ATTACH` SQL fired (or didn't) by
sniffing connection state.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pytest

from src.db import _reattach_remote_extensions


def _make_extract_with_remote_attach(path: Path, alias: str, extension: str, url: str, token_env: str) -> None:
    """Create a tiny extract.duckdb whose _remote_attach table has one row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    wal = path.with_suffix(".duckdb.wal")
    if wal.exists():
        wal.unlink()
    c = duckdb.connect(str(path))
    c.execute("CREATE TABLE _remote_attach (alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR)")
    c.execute(
        "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
        [alias, extension, url, token_env],
    )
    c.close()


def _attach_and_call(extracts_dir: Path, source_name: str):
    """ATTACH the source's extract.duckdb to a fresh memory conn, run the
    function, return the conn (so the test can introspect attached_dbs)."""
    conn = duckdb.connect()
    conn.execute(f"ATTACH '{extracts_dir / source_name / 'extract.duckdb'}' AS {source_name} (READ_ONLY)")
    _reattach_remote_extensions(conn, extracts_dir)
    return conn


def _attached(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}


class TestQueryPathExtensionAllowlist:
    def test_refuses_unknown_extension(self, tmp_path, caplog):
        """A connector that requested `httpfs` (not on allowlist) is
        refused — `httpfs` does not appear among attached databases."""
        _make_extract_with_remote_attach(
            tmp_path / "extracts" / "src1" / "extract.duckdb",
            alias="ext_alias",
            extension="httpfs",
            url="https://x",
            token_env="",
        )
        with caplog.at_level(logging.ERROR):
            conn = _attach_and_call(tmp_path / "extracts", "src1")
        assert "ext_alias" not in _attached(conn)
        assert any("not in allowlist" in r.message for r in caplog.records)


class TestQueryPathTokenEnvAllowlist:
    def test_refuses_session_secret(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("SESSION_SECRET", "shouldnt-leak")
        _make_extract_with_remote_attach(
            tmp_path / "extracts" / "src1" / "extract.duckdb",
            alias="kbc",
            extension="keboola",
            url="https://x",
            token_env="SESSION_SECRET",
        )
        with caplog.at_level(logging.ERROR):
            conn = _attach_and_call(tmp_path / "extracts", "src1")
        assert "kbc" not in _attached(conn)
        assert any("token_env" in r.message and "not in allowlist" in r.message for r in caplog.records)

    def test_refuses_jwt_secret_key(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("JWT_SECRET_KEY", "x" * 64)
        _make_extract_with_remote_attach(
            tmp_path / "extracts" / "src1" / "extract.duckdb",
            alias="kbc",
            extension="keboola",
            url="https://x",
            token_env="JWT_SECRET_KEY",
        )
        with caplog.at_level(logging.ERROR):
            conn = _attach_and_call(tmp_path / "extracts", "src1")
        assert "kbc" not in _attached(conn)


class TestQueryPathInstallStrategy:
    def test_no_install_on_query_path(self, tmp_path, monkeypatch, caplog):
        """The query path must NOT issue INSTALL FROM community — it runs
        on every read request and shouldn't touch the network. LOAD
        without prior INSTALL fails for missing extensions, which is the
        documented behaviour (operator is told to trigger a rebuild)."""
        # We can't easily verify "no INSTALL" without intercepting SQL;
        # instead, verify the query path doesn't hit the community
        # registry by setting an extension that's NOT on the default
        # allowlist nor pre-installed. The function should refuse
        # before any LOAD attempt.
        _make_extract_with_remote_attach(
            tmp_path / "extracts" / "src1" / "extract.duckdb",
            alias="bad",
            extension="some_other_community_ext",
            url="https://x",
            token_env="",
        )
        conn = _attach_and_call(tmp_path / "extracts", "src1")
        assert "bad" not in _attached(conn)


class TestQueryPathOverride:
    def test_override_replaces_default(self, tmp_path, monkeypatch):
        """Setting AGNES_REMOTE_ATTACH_TOKEN_ENVS=MY_TOKEN replaces the
        default — KBC_TOKEN no longer accepted. (Operator-typo defense
        contract; mirrored from rebuild-path tests.)"""
        monkeypatch.setenv("AGNES_REMOTE_ATTACH_TOKEN_ENVS", "MY_TOKEN")
        monkeypatch.setenv("MY_TOKEN", "value")
        _make_extract_with_remote_attach(
            tmp_path / "extracts" / "src1" / "extract.duckdb",
            alias="kbc",
            extension="keboola",
            url="https://x",
            token_env="KBC_TOKEN",  # NOT in the override set
        )
        conn = _attach_and_call(tmp_path / "extracts", "src1")
        assert "kbc" not in _attached(conn)


class TestQueryPathBqSecretRefreshOnLongLivedConnection:
    """Wave-2G task 4: `src.ducklake_session.get_ducklake_read()` calls
    `_reattach_remote_extensions` repeatedly on the SAME long-lived
    connection (unlike the legacy per-request path, which always gets a
    brand-new connection with nothing attached yet). Once the `bq` alias
    is attached, the function must still refresh the short-lived BQ
    ACCESS_TOKEN secret on every subsequent call — only the now-redundant
    ATTACH statement should be skipped. Without this, a long-lived reader's
    BQ queries would start failing once the first GCE metadata token
    expired (~1h) — see docs/superpowers/sdd/task-4-report.md."""

    def test_refreshes_bq_secret_on_repeat_call_when_already_attached(self, tmp_path, monkeypatch):
        _make_extract_with_remote_attach(
            tmp_path / "extracts" / "src1" / "extract.duckdb",
            alias="bq",
            extension="bigquery",
            url="project=proj",
            token_env="",
        )
        conn = duckdb.connect()
        # This test exercises the real BQ secret-refresh branch, which runs
        # only after `LOAD bigquery` succeeds. `_reattach_remote_extensions`
        # is LOAD-only (no INSTALL) by design, so the extension must already
        # be on disk. Under CI's randomized ordering that is NOT guaranteed —
        # nothing else in this shard need have installed it first — so install
        # it here to make the test self-sufficient and order-independent.
        # Skip (rather than fail) if the extension genuinely can't be fetched
        # (offline runner).
        # Mirror production's install incantation (orchestrator rebuild path,
        # connectors/bigquery): bigquery is a *community* extension.
        try:
            conn.execute("INSTALL bigquery FROM community")
            conn.execute("LOAD bigquery")
        except duckdb.Error as e:  # pragma: no cover - offline CI only
            pytest.skip(f"bigquery DuckDB extension unavailable: {e}")
        # Simulate "already attached from an earlier call on this same
        # long-lived connection" without needing real BQ credentials —
        # any physical attach under the `bq` alias satisfies the
        # `alias in attached_dbs` check `_reattach_remote_extensions` uses.
        conn.execute("ATTACH ':memory:' AS bq")
        conn.execute(f"ATTACH '{tmp_path / 'extracts' / 'src1' / 'extract.duckdb'}' AS src1 (READ_ONLY)")

        calls = {"n": 0}

        def fake_get_metadata_token():
            calls["n"] += 1
            return f"token-{calls['n']}"

        monkeypatch.setattr("src.db.get_metadata_token", fake_get_metadata_token)

        from src.db import _reattach_remote_extensions

        _reattach_remote_extensions(conn, tmp_path / "extracts")
        _reattach_remote_extensions(conn, tmp_path / "extracts")

        assert calls["n"] == 2, "BQ metadata token must be re-fetched on every call, not just the first"
        secret_names = {r[0] for r in conn.execute("SELECT name FROM duckdb_secrets()").fetchall()}
        assert "bq_secret_bq" in secret_names
        # The fake `bq` alias (a plain :memory: attach, not a real BQ
        # catalog) is still present exactly once — proof the function
        # never tried (and failed) to re-ATTACH it.
        db_names = [r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()]
        assert db_names.count("bq") == 1
