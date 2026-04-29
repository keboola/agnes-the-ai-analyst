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
        from tests.helpers.auth import grant_admin
        grant_admin(conn, uid)
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
        from tests.helpers.auth import grant_admin
        grant_admin(conn, uid)
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
        from tests.helpers.auth import grant_admin
        grant_admin(conn, uid)
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
        from tests.helpers.auth import grant_admin
        grant_admin(conn, uid)
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


# NOTE: test_profile_page_redirects_to_tokens removed — /profile no longer
# redirects to /tokens; it renders a real profile page including Google
# Workspace groups (cherry-pick of Zdeněk's 4f7e4cd). The /tokens render
# checks (My tokens title, new-token-btn) survive in the test_admin_tokens_ui
# suite.


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
        from tests.helpers.auth import grant_admin
        grant_admin(conn, uid)
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
        from tests.helpers.auth import grant_admin
        grant_admin(conn, uid)
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
        from tests.helpers.auth import grant_admin
        grant_admin(conn, uid)
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
        from tests.helpers.auth import grant_admin
        grant_admin(conn, uid)
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


def test_pat_null_expiry_jwt_has_no_exp_claim(fresh_db):
    """PAT with `expires_in_days=null` (user-requested "never") must not
    carry an `exp` claim at all — the DB `expires_at=NULL` is the source
    of truth. The previous ~100y `exp` claim was a misleading silent expiry."""
    from fastapi.testclient import TestClient
    import uuid
    import jwt as pyjwt
    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    from app.main import app

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="u@t", name="U", role="admin")
        from tests.helpers.auth import grant_admin
        grant_admin(conn, uid)
        sess_token = create_access_token(user_id=uid, email="u@t", role="admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.post(
        "/auth/tokens",
        headers={"Authorization": f"Bearer {sess_token}"},
        json={"name": "forever", "expires_in_days": None},
    )
    assert resp.status_code == 201, resp.text
    raw_pat = resp.json()["token"]
    # Decode without signature verification — we're inspecting claims only.
    claims = pyjwt.decode(raw_pat, options={"verify_signature": False})
    assert "exp" not in claims, f"expected no exp claim, got: {claims.get('exp')}"
    # But the other PAT claims are still present.
    assert claims.get("typ") == "pat"
    assert claims.get("sub") == uid
    assert "jti" in claims

    # DB row mirrors this: expires_at is NULL.
    assert resp.json()["expires_at"] is None


def test_pat_with_null_expiry_is_accepted_by_verify_token(fresh_db):
    """A claim-less JWT (no `exp`) must round-trip through verify_token without
    raising ExpiredSignatureError and without falling back to a wall-clock
    cap. The DB-level expiry check in dependencies.py remains authoritative."""
    from app.auth.jwt import create_access_token, verify_token

    raw = create_access_token(
        user_id="u-1", email="u@t", role="admin",
        token_id="tid-1", typ="pat", omit_exp=True,
    )
    payload = verify_token(raw)
    assert payload is not None
    assert "exp" not in payload
    assert payload["typ"] == "pat"
    assert payload["jti"] == "tid-1"


def test_pat_null_expiry_end_to_end_allows_authenticated_request(fresh_db):
    """Create a PAT with `expires_in_days=null`, then use it to call an
    authenticated endpoint. Previously relied on the 36500-day `exp`;
    now relies on the DB row. Regression guard for the switch."""
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
        from tests.helpers.auth import grant_admin
        grant_admin(conn, uid)
        sess_token = create_access_token(user_id=uid, email="u@t", role="admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    created = client.post(
        "/auth/tokens",
        headers={"Authorization": f"Bearer {sess_token}"},
        json={"name": "forever", "expires_in_days": None},
    )
    assert created.status_code == 201, created.text
    pat = created.json()["token"]

    # Use the PAT to list tokens (any authenticated endpoint).
    listed = client.get("/auth/tokens", headers={"Authorization": f"Bearer {pat}"})
    assert listed.status_code == 200, listed.text
    assert any(row["name"] == "forever" for row in listed.json())


class TestPATMalformedToken:
    """Tests for malformed and edge-case PAT tokens."""

    def test_malformed_jwt_rejected(self, fresh_db):
        """A completely malformed JWT string must be rejected with 401."""
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        resp = client.get(
            "/api/users",
            headers={"Authorization": "Bearer not.a.valid.jwt", "Accept": "application/json"},
        )
        assert resp.status_code == 401

    def test_random_string_rejected(self, fresh_db):
        """A random string (not JWT format) must be rejected with 401."""
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        resp = client.get(
            "/api/users",
            headers={"Authorization": "Bearer totally-random-garbage", "Accept": "application/json"},
        )
        assert resp.status_code == 401

    def test_empty_bearer_rejected(self, fresh_db):
        """An empty Bearer token must be rejected with 401."""
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        resp = client.get(
            "/api/users",
            headers={"Authorization": "Bearer ", "Accept": "application/json"},
        )
        assert resp.status_code in (401, 403)

    def test_pat_last_used_ip_updated(self, fresh_db):
        """Successful PAT use must update last_used_ip in the DB."""
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
            UserRepository(conn).create(id=uid, email="ip@t", name="IP", role="admin")
            from tests.helpers.auth import grant_admin
            grant_admin(conn, uid)
            tid = str(uuid.uuid4())
            pat = create_access_token(
                user_id=uid, email="ip@t", role="admin", token_id=tid, typ="pat",
                expires_delta=timedelta(days=90),
            )
            AccessTokenRepository(conn).create(
                id=tid, user_id=uid, name="ip-test",
                token_hash=hashlib.sha256(pat.encode()).hexdigest(),
                prefix=tid.replace("-", "")[:8],
                expires_at=datetime.now(timezone.utc) + timedelta(days=90),
            )
        finally:
            conn.close()
            close_system_db()

        client = TestClient(app)
        resp = client.get(
            "/api/users",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/json",
                "X-Forwarded-For": "10.20.30.40",
            },
        )
        assert resp.status_code == 200, resp.text

        conn = get_system_db()
        try:
            row = AccessTokenRepository(conn).get_by_id(tid)
            assert row["last_used_ip"] == "10.20.30.40", "last_used_ip should be updated"
        finally:
            conn.close()
            close_system_db()
