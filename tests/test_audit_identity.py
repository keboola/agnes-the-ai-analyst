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


def test_v104_backfill_rewrites_email_user_ids(tmp_path):
    """The v104 migration maps audit_log.user_id emails → users.id where the
    email resolves to exactly one account; unresolvable emails stay put."""
    import duckdb as _duckdb

    from src.db import _ensure_schema, _v103_to_v104

    conn = _duckdb.connect(str(tmp_path / "mig.duckdb"))
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO users (id, email) VALUES ('u-1', 'a@b.c'), "
        "('u-2', 'dup@b.c'), ('u-3', 'DUP@b.c')"
    )
    conn.execute(
        "INSERT INTO audit_log (id, timestamp, user_id, action) VALUES "
        "('e1', current_timestamp, 'A@B.C', 'x'), "
        "('e2', current_timestamp, 'ghost@x.y', 'x'), "
        "('e3', current_timestamp, 'u-1', 'x'), "
        "('e4', current_timestamp, 'dup@b.c', 'x')"
    )
    _v103_to_v104(conn)
    rows = dict(conn.execute("SELECT id, user_id FROM audit_log").fetchall())
    assert rows["e1"] == "u-1"  # case-insensitive single match rewritten
    assert rows["e2"] == "ghost@x.y"  # unresolvable email untouched
    assert rows["e3"] == "u-1"  # already a UUID — untouched
    assert rows["e4"] == "dup@b.c"  # ambiguous (two case-variant accounts)
    conn.close()


def test_log_autofills_duration_from_request_context(tmp_path):
    import duckdb as _duckdb

    from src.audit_context import mark_request_start
    from src.db import _ensure_schema
    from src.repositories.audit import AuditRepository

    conn = _duckdb.connect(str(tmp_path / "dur.duckdb"))
    _ensure_schema(conn)
    repo = AuditRepository(conn)
    # outside a request scope → NULL duration (contextvar default)
    repo.log(user_id="u1", action="no.scope")
    mark_request_start()
    repo.log(user_id="u1", action="in.scope")
    rows = dict(conn.execute("SELECT action, duration_ms FROM audit_log").fetchall())
    assert rows["no.scope"] is None
    assert rows["in.scope"] is not None and rows["in.scope"] >= 0
    conn.close()
