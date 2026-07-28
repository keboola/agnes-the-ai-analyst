"""Tests for KeboolaAccess facade."""
import os
import pytest
from connectors.keboola.access import KeboolaAccess


def test_access_session_yields_attached_duckdb(tmp_path, monkeypatch):
    """Mock-mode test: the facade should accept a token, install+load
    the Keboola extension, and ATTACH it as 'kbc'. We verify the SQL
    issued by intercepting the duckdb.connect call.
    """
    issued = []
    class FakeConn:
        def execute(self, sql, *args, **kwargs):
            issued.append(sql)
            class R:
                def fetchall(s): return []
                def fetchone(s): return (0,)
            return R()
        def close(self): pass

    import duckdb
    monkeypatch.setattr(duckdb, "connect", lambda *a, **kw: FakeConn())

    acc = KeboolaAccess(
        url="https://connection.keboola.com/",
        token="fake-token-xyz",
    )
    with acc.duckdb_session() as conn:
        assert conn is not None
    # Verify the install + load + attach sequence happened.
    joined = "\n".join(issued)
    assert "INSTALL keboola" in joined
    assert "LOAD keboola" in joined
    assert "ATTACH" in joined and "TYPE keboola" in joined
    # Token must be escaped for embedding in the ATTACH literal.
    assert "fake-token-xyz" in joined


def test_access_escapes_single_quote_in_token(monkeypatch):
    """Defense against a token containing a single quote breaking the
    ATTACH literal. SQL injection here is non-trivial because the token
    is admin-supplied at instance config time, but escape it anyway."""
    issued = []
    class FakeConn:
        def execute(self, sql, *args, **kwargs):
            issued.append(sql)
            class R:
                def fetchall(s): return []
                def fetchone(s): return (0,)
            return R()
        def close(self): pass
    import duckdb
    monkeypatch.setattr(duckdb, "connect", lambda *a, **kw: FakeConn())

    acc = KeboolaAccess(url="x", token="bad'token")
    with acc.duckdb_session() as conn:
        pass
    attach_sql = next(s for s in issued if "ATTACH" in s)
    # Doubled single-quote per SQL string-literal escaping.
    assert "bad''token" in attach_sql


def test_access_real_attach_when_creds_present(tmp_path):
    """Smoke when KBC_TEST_URL + KBC_TEST_TOKEN are present."""
    url = os.environ.get("KBC_TEST_URL")
    token = os.environ.get("KBC_TEST_TOKEN")
    if not (url and token):
        pytest.skip("Keboola creds not provided")
    acc = KeboolaAccess(url=url, token=token)
    with acc.duckdb_session() as conn:
        # ATTACH must have succeeded — querying duckdb_databases() should
        # show the 'kbc' alias.
        rows = [r[0] for r in conn.execute("SELECT name FROM duckdb_databases()").fetchall()]
        assert "kbc" in rows
