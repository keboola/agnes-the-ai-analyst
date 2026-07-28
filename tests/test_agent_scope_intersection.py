"""compute_agent_intersection: owner ∩ agent scope, fail-closed (V1d)."""

import pytest


def _agent(**over):
    row = {
        "id": "a1",
        "owner_user_id": "u1",
        "tables_mode": "all",
        "plugins_mode": "all",
        "connections_mode": "all",
        "memory_mode": "all",
    }
    row.update(over)
    return row


def test_all_mode_returns_owner_set_verbatim(monkeypatch):
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset({"t1", "t2"}))
    monkeypatch.setattr(mod, "_agent_scope_ids", lambda aid, it, conn=None: frozenset())
    out = mod.compute_agent_intersection("u1", _agent())
    assert out["table"] == frozenset({"t1", "t2"})


def test_selected_mode_narrows_to_subset(monkeypatch):
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset({"t1", "t2", "t3"}))
    monkeypatch.setattr(
        mod, "_agent_scope_ids", lambda aid, it, conn=None: frozenset({"t2"}) if it == "table" else frozenset()
    )
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="selected"))
    assert out["table"] == frozenset({"t2"})


def test_agent_can_never_widen_beyond_owner(monkeypatch):
    """A scope row naming a table the OWNER lacks must not appear."""
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset({"t1"}))
    monkeypatch.setattr(mod, "_agent_scope_ids", lambda aid, it, conn=None: frozenset({"t1", "SECRET"}))
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="selected"))
    assert out["table"] == frozenset({"t1"})
    assert "SECRET" not in out["table"]


def test_unrecognized_mode_fails_closed(monkeypatch):
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset({"t1"}))
    monkeypatch.setattr(mod, "_agent_scope_ids", lambda aid, it, conn=None: frozenset({"t1"}))
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="bogus"))
    assert out.get("table", frozenset()) == frozenset()


@pytest.mark.parametrize("owner,agent_row", [("", _agent()), ("u1", None), ("u1", {})])
def test_missing_inputs_deny_everything(owner, agent_row):
    from src.agent_scope_intersection import compute_agent_intersection

    assert compute_agent_intersection(owner, agent_row) == {}


def test_agent_narrows_flag():
    from src.agent_scope_intersection import agent_narrows

    assert agent_narrows(_agent()) is False
    assert agent_narrows(_agent(plugins_mode="selected")) is True
