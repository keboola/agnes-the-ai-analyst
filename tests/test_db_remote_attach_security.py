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


def _make_extract_with_remote_attach(
    path: Path, alias: str, extension: str, url: str, token_env: str
) -> None:
    """Create a tiny extract.duckdb whose _remote_attach table has one row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    wal = path.with_suffix(".duckdb.wal")
    if wal.exists():
        wal.unlink()
    c = duckdb.connect(str(path))
    c.execute(
        "CREATE TABLE _remote_attach ("
        "alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR)"
    )
    c.execute(
        "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
        [alias, extension, url, token_env],
    )
    c.close()


def _attach_and_call(extracts_dir: Path, source_name: str):
    """ATTACH the source's extract.duckdb to a fresh memory conn, run the
    function, return the conn (so the test can introspect attached_dbs)."""
    conn = duckdb.connect()
    conn.execute(
        f"ATTACH '{extracts_dir / source_name / 'extract.duckdb'}' "
        f"AS {source_name} (READ_ONLY)"
    )
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
            alias="ext_alias", extension="httpfs",
            url="https://x", token_env="",
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
            alias="kbc", extension="keboola",
            url="https://x", token_env="SESSION_SECRET",
        )
        with caplog.at_level(logging.ERROR):
            conn = _attach_and_call(tmp_path / "extracts", "src1")
        assert "kbc" not in _attached(conn)
        assert any(
            "token_env" in r.message and "not in allowlist" in r.message
            for r in caplog.records
        )

    def test_refuses_jwt_secret_key(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("JWT_SECRET_KEY", "x" * 64)
        _make_extract_with_remote_attach(
            tmp_path / "extracts" / "src1" / "extract.duckdb",
            alias="kbc", extension="keboola",
            url="https://x", token_env="JWT_SECRET_KEY",
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
            alias="bad", extension="some_other_community_ext",
            url="https://x", token_env="",
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
            alias="kbc", extension="keboola",
            url="https://x", token_env="KBC_TOKEN",  # NOT in the override set
        )
        conn = _attach_and_call(tmp_path / "extracts", "src1")
        assert "kbc" not in _attached(conn)
