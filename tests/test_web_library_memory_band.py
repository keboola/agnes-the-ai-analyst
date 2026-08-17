"""tests/test_web_library_memory_band.py — the Library Memory band absorbs
what only /corporate-memory cards had: item counts and empty-domain hiding.
Rail-only (topnav serves library_legacy.html before this pipeline runs).

Seed helpers are verbatim copies from tests/test_web_memory_domain_detail.py
— same schema, same junction table.
"""

from __future__ import annotations

import uuid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_domain(slug: str, name: str) -> str:
    from src.db import get_system_db
    from src.repositories.memory_domains import MemoryDomainsRepository

    conn = get_system_db()
    try:
        return MemoryDomainsRepository(conn).create(
            slug=slug,
            name=name,
            description=f"{name} desc",
            icon="🎯",
            color="#dcfce7",
            created_by="test",
        )
    finally:
        conn.close()


def _make_item(item_id: str, title: str, domain_id: str, is_required: bool = False):
    from src.db import get_system_db
    from src.repositories.knowledge import KnowledgeRepository

    conn = get_system_db()
    try:
        KnowledgeRepository(conn).create(
            id=item_id,
            title=title,
            content=f"# {title}\n\nbody",
            category="workflow",
            status="approved",
            is_required=is_required,
            source_user="contrib@example.com",
        )
        conn.execute(
            "INSERT INTO knowledge_item_domains(item_id, domain_id, added_by) VALUES (?, ?, 'test')",
            [item_id, domain_id],
        )
    finally:
        conn.close()


def _grant_domain(group_name: str, domain_id: str, requirement: str = "available", users: list[str] | None = None):
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        gid_row = conn.execute("SELECT id FROM user_groups WHERE name = ?", [group_name]).fetchone()
        if not gid_row:
            return
        group_id = gid_row[0]
        # seeded_app creates users via the repo, which does NOT auto-join
        # Everyone (that happens at login) — memberships must be explicit.
        for u in users or []:
            try:
                UserGroupMembersRepository(conn).add_member(u, group_id, source="test")
            except Exception:
                pass
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'memory_domain', ?, ?, CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), group_id, domain_id, requirement],
        )
    finally:
        conn.close()


def test_memory_row_meta_carries_counts(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    dom = _make_domain("lib-ops", "Lib Ops")
    _make_item("lib_ops_1", "Runbook", dom)
    _make_item("lib_ops_2", "Escalation", dom, is_required=True)
    _grant_domain("Everyone", dom, users=["analyst1"])
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "Lib Ops" in body
    assert "2 items · 1 required" in body


def test_empty_optional_domain_hidden(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    empty = _make_domain("lib-empty", "Lib Empty")
    _grant_domain("Everyone", empty, users=["analyst1"])
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert "Lib Empty" not in resp.text


def test_empty_required_domain_stays_visible(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    dom = _make_domain("lib-empty-req", "Lib Empty Required")
    _grant_domain("Everyone", dom, requirement="required", users=["analyst1"])
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert "Lib Empty Required" in resp.text


def _subscribe(user_id: str, domain_id: str):
    from src.db import get_system_db
    from src.repositories.user_stack_subscriptions import UserStackSubscriptionsRepository

    conn = get_system_db()
    try:
        UserStackSubscriptionsRepository(conn).subscribe(user_id, "memory_domain", domain_id)
    finally:
        conn.close()


def test_classic_self_subscription_is_removable_not_locked(seeded_app, monkeypatch):
    """A classic-mode subscription the caller created themselves must render
    the REMOVE control, exactly as /catalog offers for the same membership.
    The locked pill's claim ("only an admin can remove it") is false for a
    self-subscription — the lock is driven by droppability, and this row IS
    droppable. Real confusion: a user added a domain, read the lock as
    'required by admin', and found the removable truth only on /catalog."""
    # Self-subscription only exists under the classic subscribe model; under
    # auto-membership (the default since Wave 0, 2026-08) the grant IS the
    # membership and the row is locked by design —
    # test_auto_membership_grant_stays_locked below is that case.
    monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "0")
    dom = _make_domain("lib-selfsub", "Lib SelfSub")
    _make_item("lib_selfsub_1", "Note", dom)
    _grant_domain("Everyone", dom, users=["analyst1"])
    _subscribe("analyst1", dom)
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    row_at = body.index("Lib SelfSub")
    row = body[row_at : row_at + 4000]
    assert f'data-remove-from-stack="{dom}"' in row
    assert f'data-stack-remove-endpoint="/api/stack/subscription/memory_domain/{dom}"' in row
    assert "only an admin can remove it" not in row


def test_required_membership_stays_locked(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    dom = _make_domain("lib-req-lock", "Lib ReqLock")
    _make_item("lib_reqlock_1", "Note", dom)
    _grant_domain("Everyone", dom, requirement="required", users=["analyst1"])
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    body = resp.text
    row_at = body.index("Lib ReqLock")
    row = body[row_at : row_at + 4000]
    assert "lib-instack--locked" in row
    assert f'data-remove-from-stack="{dom}"' not in row


def test_auto_membership_grant_stays_locked(seeded_app, monkeypatch):
    """Under auto-membership the grant IS the membership — there is no
    subscription to drop, so the locked pill (and its admin wording) is the
    truth there."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")
    dom = _make_domain("lib-auto-lock", "Lib AutoLock")
    _make_item("lib_autolock_1", "Note", dom)
    _grant_domain("Everyone", dom, users=["analyst1"])
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    body = resp.text
    row_at = body.index("Lib AutoLock")
    row = body[row_at : row_at + 4000]
    assert "lib-instack--locked" in row
    assert f'data-remove-from-stack="{dom}"' not in row
