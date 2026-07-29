"""My Workspace — the unified resource surface (rail layout).

My Workspace folds the old My Stack + Catalog + Marketplace into one
state-driven inventory: every resource the caller's AI can reach, in one
table, distinguished only by state (Required / Enabled / Available) and
type (Data / Plugins / Memory / Uploads). These tests pin the Required
(org-managed, locked) state and the folded-nav contract; the state
transition (Available → Enabled) is covered in
tests/test_web_stack_auto_membership.py, and the redirect + tab/chip
structure in tests/test_ui_layout_theme.py.
"""

from __future__ import annotations

import re
import uuid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _grant_required_package(conn, *, slug: str, name: str, user_id: str) -> str:
    from src.repositories.data_packages import DataPackagesRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    pkg_id = DataPackagesRepository(conn).create(
        name=name,
        slug=slug,
        description="d",
        icon=None,
        color=None,
        created_by="test",
    )
    gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()[0]
    UserGroupMembersRepository(conn).add_member(user_id, gid, source="test")
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, 'data_package', ?, 'required', CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), gid, pkg_id],
    )
    return pkg_id


class TestRequiredState:
    def test_required_package_is_locked(self, seeded_app, monkeypatch):
        """An org-required grant renders In Workspace as a Required row: the
        Required badge, a Locked action, and NO add/remove toggle (it can't be
        removed)."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        from src.db import get_system_db

        conn = get_system_db()
        _grant_required_package(conn, slug="req-pkg", name="Governed Revenue Data", user_id="analyst1")
        conn.close()

        c = seeded_app["client"]
        resp = c.get("/workspace", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        body = resp.text
        assert "Governed Revenue Data" in body

        # The package renders In Workspace as a Required row with a Locked
        # action.
        row = re.search(
            r'<tr data-stack-row[^>]*data-scope="in_workspace" data-state="required"[^>]*>.*?Governed Revenue Data.*?</tr>',
            body,
            re.DOTALL,
        )
        assert row, "org-required package should render as an In-Workspace Required row"
        row_html = row.group(0)
        assert 'class="ws-badge ws-badge--required"' in row_html
        assert ">Required" in row_html
        assert "ws-act-btn--locked" in row_html
        assert ">Locked</span>" in row_html
        # A required resource carries no add/remove toggle.
        assert "data-stack-toggle" not in row_html


class TestWorkspaceNav:
    def test_workspace_is_the_single_resource_destination(self, seeded_app, monkeypatch):
        """The rail exposes one resource home — My Workspace — and no
        separate Marketplace/Catalog destination."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        c = seeded_app["client"]
        resp = c.get("/workspace", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert 'id="nav-workspace"' in text
        assert 'class="rail-i-label">My Workspace<' in text
        # No standalone Marketplace/Catalog rail item.
        assert 'class="rail-i-label">Marketplace<' not in text
        assert 'id="nav-catalog"' not in text
