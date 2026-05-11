"""AuditRepository v40 — new kwargs (params_before, client_ip, client_kind,
correlation_id) round-trip; legacy callers compile-time-unbroken."""
import duckdb
import pytest
from src.db import _ensure_schema as init_database
from src.repositories.audit import AuditRepository


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.duckdb"
    c = duckdb.connect(str(db_path))
    init_database(c)
    yield c
    c.close()


def test_log_accepts_new_kwargs(conn):
    repo = AuditRepository(conn)
    entry_id = repo.log(
        user_id="u1",
        action="registry.update",
        resource="table:web_sessions",
        params={"after": {"cron": "*/15 * * * *"}},
        params_before={"cron": "0 */1 * * *"},
        client_ip="10.0.0.42",
        client_kind="web",
        correlation_id="corr-123",
    )
    row = conn.execute("SELECT params_before, client_ip, client_kind, correlation_id FROM audit_log WHERE id=?", [entry_id]).fetchone()
    assert row[0] is not None  # JSON
    assert row[1] == "10.0.0.42"
    assert row[2] == "web"
    assert row[3] == "corr-123"


def test_log_legacy_signature_still_works(conn):
    """The original kwargs-only call site (used by 30+ existing endpoints)
    must keep working unchanged."""
    repo = AuditRepository(conn)
    entry_id = repo.log(user_id="u1", action="auth.login")
    row = conn.execute("SELECT user_id, action, params_before FROM audit_log WHERE id=?", [entry_id]).fetchone()
    assert row == ("u1", "auth.login", None)
