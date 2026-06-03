import pytest

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
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.session_id = "other"  # type: ignore[misc]


def test_can_access_session_membership_only():
    from app.auth.access import can_access_session
    p = SessionPrincipal(
        session_id="chat_1",
        participant_user_ids=["u1", "u2"],
        participant_emails=["a@example.com", "b@example.com"],
        intersection={"table": frozenset({"t2"})},
    )
    assert can_access_session(p, "table", "t2") is True
    assert can_access_session(p, "table", "t1") is False
    assert can_access_session(p, "slack_channel", "C1") is False


def test_can_access_session_does_not_call_is_user_admin_or_can_access(monkeypatch):
    import app.auth.access as access
    from app.auth.access import can_access_session
    monkeypatch.setattr(access, "is_user_admin", lambda *a, **k: pytest.fail("admin called"))
    monkeypatch.setattr(access, "can_access", lambda *a, **k: pytest.fail("can_access called"))
    p = SessionPrincipal("chat_1", ["u1"], ["a@example.com"], {"table": frozenset({"t2"})})
    assert can_access_session(p, "table", "t2") is True
