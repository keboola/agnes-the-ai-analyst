import pytest


def test_mint_co_session_jwt_has_no_participant_identity(e2e_env):
    # e2e_env sets a 32-char JWT_SECRET_KEY; verify with the same module path.
    from app.auth.access import mint_co_session_jwt
    from app.auth.jwt import verify_token
    token = mint_co_session_jwt("chat_42", ttl=3600)
    payload = verify_token(token)
    assert payload is not None
    assert payload["typ"] == "co_session"
    assert payload["chat_session_id"] == "chat_42"
    assert payload["sub"] == "session:chat_42"  # synthetic, never a user UUID
    assert "participants" not in payload
    assert payload.get("email") == ""  # no real identity baked in


@pytest.fixture
def co_fixture(e2e_env):
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface
    c = get_system_db()
    UserRepository(c).create(id="ua", email="a@example.com", name="A")
    UserRepository(c).create(id="ub", email="b@example.com", name="B")
    repo = ChatRepository(c)
    s0 = repo.create_session(user_email="a@example.com", surface=Surface.WEB)
    s1 = repo.fork_session_as_co_session(
        s0.id,
        owner_email="a@example.com", owner_user_id="ua",
        invitee_email="b@example.com", invitee_user_id="ub",
    )
    yield c, s1.id
    c.close()


def test_co_session_token_resolves_live_principal(co_fixture):
    conn, co_id = co_fixture
    from app.auth.access import mint_co_session_jwt
    from app.auth.pat_resolver import resolve_token_to_user
    from app.auth.session_principal import SessionPrincipal
    subj, reason = resolve_token_to_user(conn, mint_co_session_jwt(co_id))
    assert reason is None
    assert isinstance(subj, SessionPrincipal)
    assert set(subj.participant_emails) == {"a@example.com", "b@example.com"}


def test_single_user_token_against_co_session_fails_closed(co_fixture):
    conn, co_id = co_fixture
    from app.auth.jwt import create_access_token
    from app.auth.pat_resolver import resolve_token_to_user
    tok = create_access_token("ua", "a@example.com", extra_claims={"chat_session_id": co_id})
    subj, reason = resolve_token_to_user(conn, tok)
    assert subj is None
    assert reason == "invalid_token"
