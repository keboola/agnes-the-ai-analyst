import pytest


@pytest.fixture
def rbac_conn(e2e_env):
    from src.db import get_system_db
    c = get_system_db()
    yield c
    c.close()


def test_can_access_table_with_session_principal(rbac_conn):
    from src.rbac import can_access_table
    from app.auth.session_principal import SessionPrincipal
    p = SessionPrincipal(
        session_id="chat_1",
        participant_user_ids=["ua", "ub"],
        participant_emails=["a@example.com", "b@example.com"],
        intersection={"table": frozenset({"t2"})},
    )
    assert can_access_table(p, "t2", rbac_conn) is True
    assert can_access_table(p, "t1", rbac_conn) is False


def test_can_access_table_principal_never_admin_short_circuits(rbac_conn, monkeypatch):
    from src.rbac import can_access_table
    from app.auth.session_principal import SessionPrincipal
    monkeypatch.setattr("app.auth.access.is_user_admin", lambda *a, **k: pytest.fail("admin"))
    p = SessionPrincipal("chat_1", ["ua"], ["a@example.com"], {"table": frozenset()})
    assert can_access_table(p, "t2", rbac_conn) is False


def test_get_accessible_tables_with_principal_returns_list_not_none(rbac_conn):
    from src.rbac import get_accessible_tables
    from app.auth.session_principal import SessionPrincipal
    p = SessionPrincipal("chat_1", ["ua"], ["a@example.com"], {"table": frozenset({"t2"})})
    result = get_accessible_tables(p, rbac_conn)
    assert result is not None  # never "all" for a principal
    assert "t2" in result
    from connectors.internal.access import INTERNAL_TABLES
    for t in INTERNAL_TABLES:
        assert t.registry_id in result
