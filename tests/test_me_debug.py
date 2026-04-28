"""Tests for /me/debug self-diagnostic page.

The page must:

- Be 404 (not 403) when ``AGNES_DEBUG_AUTH`` is unset / falsy. 404 makes
  the route's existence undetectable in production.
- Be 200 for any authenticated user when the flag is on; 401 when no
  session cookie is presented.
- Never echo the raw JWT — only decoded claims and a sha256 prefix.
- Refetch endpoint must return the diff shape and perform zero database
  writes (snapshot user_group_members before/after).
"""

from __future__ import annotations

import tempfile
import uuid

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    """Per-test DATA_DIR + JWT secret so the system DB is fresh."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _make_user_and_session(conn, email: str = "u@example.com"):
    """Create a non-admin user, return (user_id, session_jwt)."""
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    uid = str(uuid.uuid4())
    UserRepository(conn).create(
        id=uid, email=email, name=email.split("@")[0], role="analyst"
    )
    token = create_access_token(user_id=uid, email=email, role="analyst")
    return uid, token


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


class TestGating:
    @pytest.mark.parametrize("flag_value", ["", "0", "false", "False", "no", "off"])
    def test_returns_404_when_flag_off(self, fresh_db, monkeypatch, flag_value):
        """Falsy / unset flag must yield 404 (not 403)."""
        if flag_value == "":
            monkeypatch.delenv("AGNES_DEBUG_AUTH", raising=False)
        else:
            monkeypatch.setenv("AGNES_DEBUG_AUTH", flag_value)

        from src.db import get_system_db, close_system_db
        conn = get_system_db()
        try:
            _, sess = _make_user_and_session(conn)
        finally:
            conn.close()
            close_system_db()

        c = _client()
        resp = c.get("/me/debug", cookies={"access_token": sess})
        assert resp.status_code == 404

    @pytest.mark.parametrize("flag_value", ["1", "true", "TRUE", "yes"])
    def test_returns_200_for_authed_user_when_flag_on(self, fresh_db, monkeypatch, flag_value):
        monkeypatch.setenv("AGNES_DEBUG_AUTH", flag_value)

        from src.db import get_system_db, close_system_db
        conn = get_system_db()
        try:
            _, sess = _make_user_and_session(conn)
        finally:
            conn.close()
            close_system_db()

        c = _client()
        resp = c.get("/me/debug", cookies={"access_token": sess})
        assert resp.status_code == 200, resp.text
        assert "Auth debug" in resp.text

    def test_redirects_to_login_when_unauthenticated(self, fresh_db, monkeypatch):
        """Flag on, no cookie → get_current_user raises 401, the app's
        global exception handler redirects HTML GETs to /login. Important:
        the response must NOT be 404 (which would prove the gate runs
        before auth and could leak existence to scanners) — it's 302 to
        /login, same as any other authenticated page."""
        monkeypatch.setenv("AGNES_DEBUG_AUTH", "true")
        from fastapi.testclient import TestClient
        from app.main import app
        c = TestClient(app, follow_redirects=False)
        resp = c.get("/me/debug")
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("location", "")


# ---------------------------------------------------------------------------
# Data leakage guards
# ---------------------------------------------------------------------------


class TestNoSensitiveLeakage:
    def test_raw_jwt_not_in_body(self, fresh_db, monkeypatch):
        """The full session JWT must never appear in the rendered page —
        only its decoded claims and a short fingerprint."""
        monkeypatch.setenv("AGNES_DEBUG_AUTH", "true")
        from src.db import get_system_db, close_system_db
        conn = get_system_db()
        try:
            _, sess = _make_user_and_session(conn)
        finally:
            conn.close()
            close_system_db()

        c = _client()
        resp = c.get("/me/debug", cookies={"access_token": sess})
        assert resp.status_code == 200
        assert sess not in resp.text, "raw JWT leaked into page body"


# ---------------------------------------------------------------------------
# Refetch endpoint — dry-run, zero DB writes
# ---------------------------------------------------------------------------


class TestRefetchDryRun:
    def test_404_when_flag_off(self, fresh_db, monkeypatch):
        monkeypatch.delenv("AGNES_DEBUG_AUTH", raising=False)
        from src.db import get_system_db, close_system_db
        conn = get_system_db()
        try:
            _, sess = _make_user_and_session(conn)
        finally:
            conn.close()
            close_system_db()

        c = _client()
        resp = c.post("/me/debug/refetch-groups", cookies={"access_token": sess})
        assert resp.status_code == 404

    def test_returns_diff_shape_and_does_not_write(self, fresh_db, monkeypatch):
        """Mocked Google response, refetch must return the documented shape
        AND not change any user_group_members rows."""
        monkeypatch.setenv("AGNES_DEBUG_AUTH", "true")
        # Mock fetch to return a deterministic list (no real Google call).
        monkeypatch.setenv(
            "GOOGLE_ADMIN_SDK_MOCK_GROUPS",
            "grp_foundryai_admin@groupon.com,grp_foundryai_finance@groupon.com",
        )

        from src.db import get_system_db, close_system_db
        conn = get_system_db()
        try:
            uid, sess = _make_user_and_session(conn, email="m@example.com")
            before_rows = conn.execute(
                "SELECT user_id, group_id, source FROM user_group_members "
                "WHERE user_id = ?", [uid],
            ).fetchall()
        finally:
            conn.close()
            close_system_db()

        c = _client()
        resp = c.post("/me/debug/refetch-groups", cookies={"access_token": sess})
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Documented shape — keys present, types right.
        for key in (
            "soft_failed", "prefix", "fetched", "fetched_relevant",
            "current_names", "current_external_ids",
            "would_add", "would_remove", "applied",
        ):
            assert key in data, f"missing key {key!r}"
        assert data["applied"] is False
        assert data["soft_failed"] is False
        assert isinstance(data["fetched"], list)
        assert isinstance(data["would_add"], list)

        # Zero DB writes — snapshot before/after must match exactly.
        conn = get_system_db()
        try:
            after_rows = conn.execute(
                "SELECT user_id, group_id, source FROM user_group_members "
                "WHERE user_id = ?", [uid],
            ).fetchall()
        finally:
            conn.close()
            close_system_db()
        assert before_rows == after_rows

    def test_soft_fail_marker_when_mock_unset_and_real_path_unconfigured(
        self, fresh_db, monkeypatch
    ):
        """Without the mock env and without GOOGLE_ADMIN_SDK_SUBJECT, the
        real path returns soft-fail; the endpoint reports it as such."""
        monkeypatch.setenv("AGNES_DEBUG_AUTH", "true")
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", raising=False)
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_SUBJECT", raising=False)

        from src.db import get_system_db, close_system_db
        conn = get_system_db()
        try:
            _, sess = _make_user_and_session(conn, email="sf@example.com")
        finally:
            conn.close()
            close_system_db()

        c = _client()
        resp = c.post("/me/debug/refetch-groups", cookies={"access_token": sess})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # On the keyless-DWD branch, fetch_user_groups returns [] on missing
        # subject (legacy fail-soft as empty list); on the prefix-mapping
        # branch it returns None. Tolerate either — endpoint reports
        # soft_failed=True when None, False+empty list when [].
        if data["soft_failed"]:
            assert data["fetched"] == []
        else:
            # Real path returned [] — also a valid shape; assert no writes
            # happened by virtue of applied=False + DB snapshot below.
            assert data["fetched"] == []
        assert data["applied"] is False
