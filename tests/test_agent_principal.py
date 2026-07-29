"""AgentPrincipal is a frozen, restricted auth subject (V1d)."""

import pytest


def test_agent_principal_is_frozen_and_carries_intersection():
    from app.auth.session_principal import AgentPrincipal

    p = AgentPrincipal(
        session_id="c1",
        agent_id="a1",
        owner_user_id="u1",
        owner_email="owner@example.com",
        intersection={"table": frozenset({"t1"})},
    )
    assert p.intersection["table"] == frozenset({"t1"})
    with pytest.raises(Exception):  # frozen dataclass
        p.agent_id = "a2"  # type: ignore[misc]


def test_principal_union_covers_both():
    from app.auth.session_principal import AgentPrincipal, Principal, SessionPrincipal

    agent = AgentPrincipal("c1", "a1", "u1", "o@example.com", {})
    co = SessionPrincipal("c2", ["u1"], ["o@example.com"], {})
    for p in (agent, co):
        assert isinstance(p, Principal.__args__)  # both members of the union
