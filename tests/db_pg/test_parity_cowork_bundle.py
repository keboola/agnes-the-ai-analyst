"""Backend-parity tests for the cowork setup-bundle endpoints.

Cluster: cowork_bundle (app/api/cowork_bundle.py).

Every endpoint in this router resolves its repos by directly instantiating
``SetupTokenRepository(conn)`` / ``AccessTokenRepository(conn)`` /
``UserRepository(conn)`` off a raw DuckDB connection (``Depends(_get_db)``)
instead of going through the backend-aware factory. On a Postgres instance the
state we seed through the factory lands in PG, but the endpoint reads an empty
DuckDB — so the seeded row is invisible.

Discriminator: state is SEEDED THROUGH THE FACTORY (setup_tokens_repo()), then
the endpoint is exercised via ``seeded_app_both``. duck PASS + pg FAIL pinpoints
a backend-split bug at that endpoint.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone


def _auth(seeded_app_both, who="admin"):
    return {"Authorization": f"Bearer {seeded_app_both[f'{who}_token']}"}


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _seed_token(user_id: str, *, raw: str | None = None):
    """Seed a setup token through the factory; return (token_id, raw_token)."""
    from src.repositories import setup_tokens_repo

    token_id = str(uuid.uuid4())
    raw = raw or ("st_" + uuid.uuid4().hex + uuid.uuid4().hex)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    setup_tokens_repo().create(
        id=token_id,
        user_id=user_id,
        token_hash=_hash(raw),
        expires_at=expires_at,
    )
    return token_id, raw


# ---------------------------------------------------------------------------
# GET /api/user/setup-tokens — list active tokens for the caller
# ---------------------------------------------------------------------------

def test_list_setup_tokens_reflects_seeded_token(seeded_app_both):
    token_id, _ = _seed_token("admin1")

    r = seeded_app_both["client"].get(
        "/api/user/setup-tokens", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert token_id in ids, (
        f"[{seeded_app_both['backend']}] seeded setup token missing from "
        f"GET /api/user/setup-tokens: {r.json()} — endpoint reads "
        f"SetupTokenRepository off a raw DuckDB conn instead of setup_tokens_repo()."
    )


# ---------------------------------------------------------------------------
# DELETE /api/user/setup-tokens/{id} — revoke an owned token
#
# The handler 404s when the token is not in the caller's list_active_for_user()
# result. On PG that list is empty (raw-conn read), so a token the owner really
# has gets a 404 instead of a 204.
# ---------------------------------------------------------------------------

def test_revoke_owned_setup_token_succeeds(seeded_app_both):
    token_id, _ = _seed_token("admin1")

    r = seeded_app_both["client"].delete(
        f"/api/user/setup-tokens/{token_id}", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 204, (
        f"[{seeded_app_both['backend']}] DELETE /api/user/setup-tokens/{{id}} "
        f"returned {r.status_code} for a token seeded through the factory and "
        f"owned by the caller — handler reads SetupTokenRepository off a raw "
        f"DuckDB conn instead of setup_tokens_repo(). body={r.text}"
    )


# ---------------------------------------------------------------------------
# POST /api/auth/exchange-setup-token — unauthenticated exchange → PAT
#
# Reads the token by hash via SetupTokenRepository(conn) and the user via
# UserRepository(conn), both off the raw DuckDB conn. On PG the seeded token is
# invisible → 401 instead of 200.
# ---------------------------------------------------------------------------

def test_exchange_setup_token_returns_pat(seeded_app_both):
    _, raw = _seed_token("admin1")

    r = seeded_app_both["client"].post(
        "/api/auth/exchange-setup-token",
        json={"setup_token": raw},
    )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] POST /api/auth/exchange-setup-token "
        f"returned {r.status_code} for a token seeded through the factory — "
        f"handler reads SetupTokenRepository/UserRepository off a raw DuckDB "
        f"conn instead of setup_tokens_repo()/users_repo(). body={r.text}"
    )
    body = r.json()
    assert body.get("access_token"), f"no access_token in exchange response: {body}"
    assert body.get("user_email") == "admin@test.com", body
