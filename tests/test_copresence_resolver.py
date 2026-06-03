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
