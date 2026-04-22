"""Tests for #12 — personal access tokens (PAT)."""

import os
import tempfile
import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def test_schema_v6_creates_pat_table(fresh_db):
    from src.db import get_system_db, get_schema_version, close_system_db
    conn = get_system_db()
    try:
        cols = conn.execute("PRAGMA table_info(personal_access_tokens)").fetchall()
        col_names = [c[1] for c in cols]
        for expected in ("id", "user_id", "name", "token_hash", "prefix",
                         "scopes", "created_at", "expires_at", "last_used_at", "revoked_at"):
            assert expected in col_names
        assert get_schema_version(conn) >= 6
    finally:
        conn.close()
        close_system_db()


def test_schema_v7_adds_last_used_ip_column(fresh_db):
    """Schema v7: personal_access_tokens has last_used_ip column."""
    from src.db import get_system_db, get_schema_version, close_system_db
    conn = get_system_db()
    try:
        cols = conn.execute("PRAGMA table_info(personal_access_tokens)").fetchall()
        col_names = [c[1] for c in cols]
        assert "last_used_ip" in col_names
        assert get_schema_version(conn) >= 7
    finally:
        conn.close()
        close_system_db()


def test_access_token_repo_create_and_lookup(fresh_db):
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.access_tokens import AccessTokenRepository

    conn = get_system_db()
    try:
        repo = AccessTokenRepository(conn)
        token_id = str(uuid.uuid4())
        raw = "abcdefgh" + "x" * 32
        repo.create(
            id=token_id,
            user_id="u1",
            name="laptop",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            prefix=raw[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        row = repo.get_by_id(token_id)
        assert row is not None
        assert row["name"] == "laptop"
        assert row["prefix"] == "abcdefgh"
        assert row["revoked_at"] is None

        rows = repo.list_for_user("u1")
        assert len(rows) == 1

        repo.revoke(token_id)
        assert repo.get_by_id(token_id)["revoked_at"] is not None
    finally:
        conn.close()
        close_system_db()


def test_access_token_repo_mark_used(fresh_db):
    import hashlib, uuid
    from datetime import datetime, timezone
    from src.db import get_system_db, close_system_db
    from src.repositories.access_tokens import AccessTokenRepository

    conn = get_system_db()
    try:
        repo = AccessTokenRepository(conn)
        tid = str(uuid.uuid4())
        repo.create(id=tid, user_id="u1", name="x",
                    token_hash=hashlib.sha256(b"r").hexdigest(), prefix="rrrrrrrr")
        assert repo.get_by_id(tid)["last_used_at"] is None
        repo.mark_used(tid)
        assert repo.get_by_id(tid)["last_used_at"] is not None
    finally:
        conn.close()
        close_system_db()


def test_pat_token_carries_typ_claim(fresh_db):
    from app.auth.jwt import create_access_token, verify_token
    token = create_access_token(
        user_id="u1", email="u@test", role="analyst",
        token_id="deadbeef-1234", typ="pat",
    )
    payload = verify_token(token)
    assert payload["typ"] == "pat"
    assert payload["jti"] == "deadbeef-1234"


def test_session_token_defaults_typ(fresh_db):
    from app.auth.jwt import create_access_token, verify_token
    token = create_access_token(user_id="u1", email="u@test", role="analyst")
    payload = verify_token(token)
    # Default typ is "session".
    assert payload.get("typ") == "session"


def test_revoked_pat_is_rejected(fresh_db, monkeypatch):
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        token_id = str(uuid.uuid4())
        raw = "secretXX" + "a" * 32
        AccessTokenRepository(conn).create(
            id=token_id, user_id=uid, name="ci",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            prefix=raw[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        jwt_token = create_access_token(
            user_id=uid, email="u@t", role="admin", token_id=token_id, typ="pat",
        )
        # Revoke
        AccessTokenRepository(conn).revoke(token_id)
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {jwt_token}", "Accept": "application/json"},
    )
    assert resp.status_code == 401


def test_expired_pat_is_rejected_from_db(fresh_db):
    """A PAT with a past expires_at in DB is rejected even if JWT exp is in future."""
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        tid = str(uuid.uuid4())
        # Past-dated expiry in DB
        AccessTokenRepository(conn).create(
            id=tid, user_id=uid, name="stale",
            token_hash=hashlib.sha256(b"whatever").hexdigest(), prefix=tid.replace("-","")[:8],
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        # JWT with much longer TTL so signature-level `exp` would pass
        pat = create_access_token(
            user_id=uid, email="u@t", role="admin",
            token_id=tid, typ="pat",
            expires_delta=timedelta(days=365),
        )
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"},
    )
    assert resp.status_code == 401


def test_create_pat_returns_raw_once(fresh_db):
    from fastapi.testclient import TestClient
    import uuid
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        sess_token = create_access_token(user_id=uid, email="u@t", role="admin")  # typ=session
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.post(
        "/auth/tokens",
        headers={"Authorization": f"Bearer {sess_token}"},
        json={"name": "laptop", "expires_in_days": 30},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "laptop"
    assert "token" in data and data["token"]  # raw token returned exactly once

    # Listing returns prefix, never raw.
    # Prefix is derived from the token id (jti), not the JWT string, to avoid
    # all tokens having the useless "eyJhbGci" JWT-header prefix.
    list_resp = client.get(
        "/auth/tokens", headers={"Authorization": f"Bearer {sess_token}"},
    )
    assert list_resp.status_code == 200
    rows = list_resp.json()
    assert len(rows) == 1
    assert "token" not in rows[0]
    assert rows[0]["prefix"] == data["prefix"]
    assert len(rows[0]["prefix"]) == 8
    assert not data["prefix"].startswith("eyJ")  # regression: not the JWT header


def test_pat_cannot_create_pat(fresh_db):
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        tid = str(uuid.uuid4())
        # Create the JWT first so we can store its sha256 as token_hash (otherwise
        # the defense-in-depth check in get_current_user would reject it with 401
        # before require_session_token ever runs).
        pat = create_access_token(user_id=uid, email="u@t", role="admin", token_id=tid, typ="pat")
        AccessTokenRepository(conn).create(
            id=tid, user_id=uid, name="x",
            token_hash=hashlib.sha256(pat.encode()).hexdigest(),
            prefix=tid.replace("-", "")[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.post(
        "/auth/tokens",
        headers={"Authorization": f"Bearer {pat}"},
        json={"name": "bad", "expires_in_days": 30},
    )
    assert resp.status_code == 403


def test_profile_page_redirects_to_tokens(fresh_db):
    """/profile was unified under /tokens in feat/unify-tokens-fullwidth;
    the route now 302-redirects to /tokens."""
    from fastapi.testclient import TestClient
    import uuid
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="analyst")
        token = create_access_token(user_id=uid, email="u@t", role="analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    # Redirect is unauthenticated (no auth guard on the redirect itself)
    resp = client.get("/profile", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/tokens"

    # Following the redirect with a valid session lands on the unified page.
    resp = client.get(
        "/tokens",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "My tokens" in resp.text  # non-admin title
    assert 'id="new-token-btn"' in resp.text  # non-admin CTA


def test_pat_first_use_from_new_ip_audits(fresh_db):
    """Using a PAT from a different IP than last time emits an audit entry."""
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        tid = str(uuid.uuid4())
        pat = create_access_token(
            user_id=uid, email="u@t", role="admin", token_id=tid, typ="pat",
            expires_delta=timedelta(days=90),
        )
        repo = AccessTokenRepository(conn)
        repo.create(
            id=tid, user_id=uid, name="ci",
            token_hash=hashlib.sha256(pat.encode()).hexdigest(),
            prefix=tid.replace("-", "")[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        # Simulate a prior use from 1.1.1.1 so the upcoming call is a "new IP".
        repo.mark_used(tid, ip="1.1.1.1")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/api/users",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json",
            "X-Forwarded-For": "2.2.2.2",
        },
    )
    assert resp.status_code == 200, resp.text

    conn = get_system_db()
    try:
        rows = conn.execute(
            "SELECT params FROM audit_log WHERE action = 'token.first_use_new_ip' AND user_id = ?",
            [uid],
        ).fetchall()
        assert len(rows) == 1, f"expected 1 audit row, got {len(rows)}"
        params = rows[0][0]
        # params is stored as JSON text; check the IP appears
        assert "2.2.2.2" in str(params)
    finally:
        conn.close()
        close_system_db()


def test_pat_same_ip_does_not_audit(fresh_db):
    """Using a PAT from the same IP as last time does NOT emit an audit entry."""
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        tid = str(uuid.uuid4())
        pat = create_access_token(
            user_id=uid, email="u@t", role="admin", token_id=tid, typ="pat",
            expires_delta=timedelta(days=90),
        )
        repo = AccessTokenRepository(conn)
        repo.create(
            id=tid, user_id=uid, name="ci",
            token_hash=hashlib.sha256(pat.encode()).hexdigest(),
            prefix=tid.replace("-", "")[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        repo.mark_used(tid, ip="3.3.3.3")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/api/users",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json",
            "X-Forwarded-For": "3.3.3.3",
        },
    )
    assert resp.status_code == 200, resp.text

    conn = get_system_db()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'token.first_use_new_ip' AND user_id = ?",
            [uid],
        ).fetchone()[0]
        assert count == 0
    finally:
        conn.close()
        close_system_db()


def test_pat_can_list_own_tokens(fresh_db):
    """A PAT must be allowed to list its owner's tokens — `da auth token list`
    CLI flow. Previously this returned 403 because require_session_token
    blocked all PATs uniformly."""
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="analyst")
        tid = str(uuid.uuid4())
        pat = create_access_token(
            user_id=uid, email="u@t", role="analyst", token_id=tid, typ="pat",
            expires_delta=timedelta(days=90),
        )
        AccessTokenRepository(conn).create(
            id=tid, user_id=uid, name="laptop",
            token_hash=hashlib.sha256(pat.encode()).hexdigest(),
            prefix=tid.replace("-", "")[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/auth/tokens",
        headers={"Authorization": f"Bearer {pat}"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert any(r["id"] == tid for r in rows)


def test_pat_can_revoke_own_token(fresh_db):
    """A PAT must be allowed to revoke its owner's own tokens —
    `da auth token revoke` CLI flow."""
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="analyst")
        # Token A — the PAT used to authenticate this call.
        tid_a = str(uuid.uuid4())
        pat_a = create_access_token(
            user_id=uid, email="u@t", role="analyst", token_id=tid_a, typ="pat",
            expires_delta=timedelta(days=90),
        )
        AccessTokenRepository(conn).create(
            id=tid_a, user_id=uid, name="primary",
            token_hash=hashlib.sha256(pat_a.encode()).hexdigest(),
            prefix=tid_a.replace("-", "")[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        # Token B — the one we'll revoke with A.
        tid_b = str(uuid.uuid4())
        AccessTokenRepository(conn).create(
            id=tid_b, user_id=uid, name="old-ci",
            token_hash=hashlib.sha256(b"whatever").hexdigest(),
            prefix=tid_b.replace("-", "")[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.delete(
        f"/auth/tokens/{tid_b}",
        headers={"Authorization": f"Bearer {pat_a}"},
    )
    assert resp.status_code == 204, resp.text

    # Confirm B is now revoked.
    conn = get_system_db()
    try:
        row = AccessTokenRepository(conn).get_by_id(tid_b)
        assert row["revoked_at"] is not None
    finally:
        conn.close()
        close_system_db()


def test_create_token_rejects_expires_in_days_above_cap(fresh_db):
    """expires_in_days > 3650 must return 400 (not 500 via datetime overflow)."""
    from fastapi.testclient import TestClient
    import uuid
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        sess_token = create_access_token(user_id=uid, email="u@t", role="admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    # Just above the cap — must be 400, not 500.
    resp = client.post(
        "/auth/tokens",
        headers={"Authorization": f"Bearer {sess_token}"},
        json={"name": "laptop", "expires_in_days": 3651},
    )
    assert resp.status_code == 400, resp.text
    assert "3650" in resp.text

    # Huge value that would previously overflow datetime.max — still 400.
    resp = client.post(
        "/auth/tokens",
        headers={"Authorization": f"Bearer {sess_token}"},
        json={"name": "laptop", "expires_in_days": 10_000_000_000},
    )
    assert resp.status_code == 400, resp.text


def test_pat_first_ever_use_does_not_audit(fresh_db):
    """The first-ever use of a PAT (no prior last_used_at) does NOT emit an audit entry."""
    from fastapi.testclient import TestClient
    import hashlib, uuid
    from datetime import datetime, timezone, timedelta
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        tid = str(uuid.uuid4())
        pat = create_access_token(
            user_id=uid, email="u@t", role="admin", token_id=tid, typ="pat",
            expires_delta=timedelta(days=90),
        )
        AccessTokenRepository(conn).create(
            id=tid, user_id=uid, name="ci",
            token_hash=hashlib.sha256(pat.encode()).hexdigest(),
            prefix=tid.replace("-", "")[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        # No mark_used call → first-ever use
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/api/users",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json",
            "X-Forwarded-For": "4.4.4.4",
        },
    )
    assert resp.status_code == 200, resp.text

    conn = get_system_db()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'token.first_use_new_ip' AND user_id = ?",
            [uid],
        ).fetchone()[0]
        assert count == 0
    finally:
        conn.close()
        close_system_db()
