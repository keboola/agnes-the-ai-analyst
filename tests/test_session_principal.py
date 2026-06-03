from app.auth.session_principal import SessionPrincipal


def test_session_principal_is_frozen_and_holds_intersection():
    p = SessionPrincipal(
        session_id="chat_1",
        participant_user_ids=["u1", "u2"],
        participant_emails=["a@example.com", "b@example.com"],
        intersection={"table": frozenset({"t1"})},
    )
    assert p.session_id == "chat_1"
    assert p.intersection["table"] == frozenset({"t1"})
    import dataclasses
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.session_id = "other"  # type: ignore[misc]
