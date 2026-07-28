"""Audit identity discipline: user_id must be users.id, never a raw email."""

import pytest


@pytest.fixture(autouse=True)
def _clear_email_cache():
    from app.chat import audit as chat_audit

    chat_audit._EMAIL_ID_CACHE.clear()
    yield
    chat_audit._EMAIL_ID_CACHE.clear()


class _FakeAuditRepo:
    def __init__(self, sink):
        self._sink = sink

    def log(self, **kw):
        self._sink.update(kw)


def test_write_audit_resolves_email_to_user_id(monkeypatch):
    from app.chat import audit as chat_audit

    logged: dict = {}

    class FakeUsers:
        def get_by_email(self, email):
            return {"id": "uuid-1", "email": email}

    monkeypatch.setattr("src.repositories.audit_repo", lambda: _FakeAuditRepo(logged))
    monkeypatch.setattr("src.repositories.users_repo", lambda: FakeUsers())
    chat_audit.write_audit(user_email="a@b.c", action="chat.x", details={})
    assert logged["user_id"] == "uuid-1"


def test_write_audit_falls_back_to_email_when_unresolvable(monkeypatch):
    from app.chat import audit as chat_audit

    logged: dict = {}

    class FakeUsers:
        def get_by_email(self, email):
            return None

    monkeypatch.setattr("src.repositories.audit_repo", lambda: _FakeAuditRepo(logged))
    monkeypatch.setattr("src.repositories.users_repo", lambda: FakeUsers())
    chat_audit.write_audit(user_email="ghost@b.c", action="chat.x", details={})
    assert logged["user_id"] == "ghost@b.c"


def test_write_audit_explicit_user_id_skips_lookup(monkeypatch):
    from app.chat import audit as chat_audit

    logged: dict = {}

    class ExplodingUsers:
        def get_by_email(self, email):  # pragma: no cover - must not be called
            raise AssertionError("lookup must be skipped")

    monkeypatch.setattr("src.repositories.audit_repo", lambda: _FakeAuditRepo(logged))
    monkeypatch.setattr("src.repositories.users_repo", lambda: ExplodingUsers())
    chat_audit.write_audit(user_email="a@b.c", action="chat.x", details={}, user_id="uuid-9")
    assert logged["user_id"] == "uuid-9"


def test_write_audit_caches_email_resolution(monkeypatch):
    from app.chat import audit as chat_audit

    logged: dict = {}
    calls = {"n": 0}

    class CountingUsers:
        def get_by_email(self, email):
            calls["n"] += 1
            return {"id": "uuid-1", "email": email}

    monkeypatch.setattr("src.repositories.audit_repo", lambda: _FakeAuditRepo(logged))
    monkeypatch.setattr("src.repositories.users_repo", lambda: CountingUsers())
    chat_audit.write_audit(user_email="a@b.c", action="chat.x", details={})
    chat_audit.write_audit(user_email="a@b.c", action="chat.y", details={})
    assert calls["n"] == 1


class TestRunWritersStampClientKind:
    def test_run_session_processor_audit_row_carries_client_kind(self, seeded_app):
        """run_* scheduler-tick writers must classify their source — a JWT
        admin trigger stamps 'web'; NULL client_kind lands the row in the
        'other' facet bucket (the pre-fix bug)."""
        from unittest.mock import patch

        from src.db import get_system_db

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        from services.session_processors import _build_registry

        _build_registry.cache_clear()
        fake_stats = {
            "processor": "usage",
            "scanned": 0,
            "processed": 0,
            "skipped": 0,
            "errors": 0,
            "items_extracted": 0,
            "errors_detail": [],
        }
        with patch(
            "services.session_pipeline.runner.run_processor",
            return_value=fake_stats,
        ):
            resp = c.post(
                "/api/admin/run-session-processor?processor=usage",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200, resp.text

        conn = get_system_db()
        try:
            row = conn.execute(
                "SELECT client_kind FROM audit_log "
                "WHERE action = 'run_session_processor:usage' "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "web"
