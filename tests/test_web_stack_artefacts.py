"""Add Artefacts to My Stack.

Artefacts (file_corpora uploads) are available to the *user* the moment
they're owned or shared with them; they only become available to the
*default agent* once added to My Stack. This reuses the generic
``user_stack_subscriptions`` model (resource_type='collection') — no new
table, no StackResolver — via three dedicated endpoints
(POST/DELETE/GET /api/stack/artefacts*), a new Artefacts tab on /stack, and
stack-awareness on /artefacts.
"""

from __future__ import annotations

import io


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(seeded_app, name: str, token: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _upload(seeded_app, cid: str, filename: str, content: bytes, ctype: str, token: str):
    return seeded_app["client"].post(
        f"/api/collections/{cid}/files",
        files={"files": (filename, io.BytesIO(content), ctype)},
        headers=_auth(token),
    )


def _share_collection_with_user(collection_id: str, user_id: str, group_name: str = "af-stack-share-grp") -> None:
    """Add ``user_id`` to a group and grant that group the collection."""
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name) or groups.create(name=group_name, description="test", created_by="test")
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "collection", collection_id):
        grants.create(group_id=grp["id"], resource_type="collection", resource_id=collection_id, assigned_by="test")
    conn.close()


def _publish_workspace(collection_id: str, user_id: str) -> None:
    """Grant ``collection_id`` to the Everyone system group + add ``user_id``
    as an explicit Everyone member (implicit membership was removed)."""
    from src.db import get_system_db, SYSTEM_EVERYONE_GROUP
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    conn = get_system_db()
    everyone = UserGroupsRepository(conn).get_by_name(SYSTEM_EVERYONE_GROUP)
    assert everyone is not None, "Everyone system group must be seeded"
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, everyone["id"]):
        members.add_member(user_id, everyone["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([everyone["id"]], "collection", collection_id):
        grants.create(
            group_id=everyone["id"], resource_type="collection", resource_id=collection_id, assigned_by="test"
        )
    conn.close()


class TestArtefactStackApi:
    def test_add_and_remove_roundtrip(self, seeded_app):
        col = _create(seeded_app, "Roadmap", seeded_app["analyst_token"])
        c = seeded_app["client"]

        r = c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["added"] is True
        assert body["card"]["title"] == "Roadmap"
        assert body["card"]["action"]["mode"] == "stack"
        assert body["card"]["action"]["remove_url"] == f"/api/stack/artefacts/{col['id']}"

        r = c.delete(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 204

    def test_add_is_idempotent_no_duplicate_membership(self, seeded_app):
        col = _create(seeded_app, "Dup Test", seeded_app["analyst_token"])
        c = seeded_app["client"]
        for _ in range(2):
            r = c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
            assert r.status_code == 200

        from src.repositories import user_stack_subscriptions_repo

        ids = user_stack_subscriptions_repo().list_for_user("analyst1", "collection")
        assert ids.count(col["id"]) == 1

    def test_add_requires_access(self, seeded_app):
        col = _create(seeded_app, "Admin Private", seeded_app["admin_token"])
        c = seeded_app["client"]
        r = c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_add_nonexistent_404(self, seeded_app):
        c = seeded_app["client"]
        r = c.post("/api/stack/artefacts/col_doesnotexist", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 404

    def test_remove_does_not_delete_the_artefact(self, seeded_app):
        col = _create(seeded_app, "Keep Me", seeded_app["analyst_token"])
        c = seeded_app["client"]
        c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        r = c.delete(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 204

        # Still owned, still reachable on /artefacts — only Stack access dropped.
        get_r = c.get(f"/api/collections/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert get_r.status_code == 200
        artefacts_text = c.get("/library", headers=_auth(seeded_app["analyst_token"])).text
        assert "Keep Me" in artefacts_text

    def test_candidates_excludes_inaccessible_and_already_in_stack(self, seeded_app):
        mine = _create(seeded_app, "Mine Candidate", seeded_app["analyst_token"])
        _create(seeded_app, "Not Mine", seeded_app["admin_token"])
        c = seeded_app["client"]

        r = c.get("/api/stack/artefacts/candidates", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        body = r.json()
        titles = {it["title"] for it in body["items"]}
        assert "Mine Candidate" in titles
        assert "Not Mine" not in titles  # not owned, not shared → invisible

        # Add it → no longer a candidate; total_accessible stays stable.
        c.post(f"/api/stack/artefacts/{mine['id']}", headers=_auth(seeded_app["analyst_token"]))
        r2 = c.get("/api/stack/artefacts/candidates", headers=_auth(seeded_app["analyst_token"]))
        titles2 = {it["title"] for it in r2.json()["items"]}
        assert "Mine Candidate" not in titles2


class TestMyStackArtefactsTab:
    def test_tab_renders_with_count_and_row_details(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        col = _create(seeded_app, "Board Notes", seeded_app["analyst_token"])
        c = seeded_app["client"]
        c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))

        resp = c.get("/stack", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert 'data-kind="artefacts"' in text
        assert 'id="stk-count-artefacts"' in text
        assert "Board Notes" in text
        assert "Remove from My Stack" in text
        # Private (owned, not shared) → the Source column shows "Private".
        assert "Private" in text

    def test_workspace_visibility_label(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        col = _create(seeded_app, "Company Handbook", seeded_app["analyst_token"])
        _publish_workspace(col["id"], "analyst1")
        c = seeded_app["client"]
        c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))

        text = c.get("/stack", headers=_auth(seeded_app["analyst_token"])).text
        assert "Company Handbook" in text
        assert "Workspace" in text

    def test_shared_with_me_shows_owner_name(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        col = _create(seeded_app, "Owner Deck", seeded_app["admin_token"])
        _share_collection_with_user(col["id"], "analyst1")
        c = seeded_app["client"]
        r = c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200, r.text

        text = c.get("/stack", headers=_auth(seeded_app["analyst_token"])).text
        assert "Owner Deck" in text
        assert "Shared by Admin" in text

    def test_removed_artefact_stays_in_artefacts_but_leaves_stack(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        col = _create(seeded_app, "Ephemeral", seeded_app["analyst_token"])
        c = seeded_app["client"]
        c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert "Ephemeral" in c.get("/stack", headers=_auth(seeded_app["analyst_token"])).text

        c.delete(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        stack_text = c.get("/stack", headers=_auth(seeded_app["analyst_token"])).text
        assert "Ephemeral" not in stack_text
        artefacts_text = c.get("/library", headers=_auth(seeded_app["analyst_token"])).text
        assert "Ephemeral" in artefacts_text

    def test_unavailable_badge_when_access_revoked(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        from src.db import get_system_db
        from src.repositories.resource_grants import ResourceGrantsRepository

        col = _create(seeded_app, "Revocable Deck", seeded_app["admin_token"])
        _share_collection_with_user(col["id"], "analyst1", group_name="revoke-grp")
        c = seeded_app["client"]
        r = c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200, r.text

        # Revoke the grant — the artefact stays in the caller's Stack (never
        # dropped silently) but is no longer accessible.
        conn = get_system_db()
        grants = ResourceGrantsRepository(conn)
        for g in grants.list_all(resource_type="collection"):
            if g["resource_id"] == col["id"]:
                grants.delete(g["id"])
        conn.close()

        text = c.get("/stack", headers=_auth(seeded_app["analyst_token"])).text
        assert "Revocable Deck" in text
        assert "Unavailable" in text
        assert "You no longer have access to this artefact." in text
        # Remove-from-Stack must still be available.
        assert "Remove from My Stack" in text

    def test_empty_state_copy_present(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        # analyst1 has zero artefacts in their Stack in a fresh seeded_app.
        text = seeded_app["client"].get("/stack", headers=_auth(seeded_app["analyst_token"])).text
        assert "No artefacts in your Stack" in text
        assert "Add artefacts you want the default agent to access and use." in text

    def test_add_artefacts_button_and_picker_markup_present(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        text = seeded_app["client"].get("/stack", headers=_auth(seeded_app["analyst_token"])).text
        assert 'id="stk-add-artefacts-btn"' in text
        assert 'id="stk-af-picker"' in text
        assert "Add artefacts to Stack" in text
        assert "Select artefacts the default agent should be able to access." in text
        assert ">Add to Stack<" in text
        # Picker empty-state copy (rendered client-side from JSON, but the
        # literal strings live in the page's own script block).
        assert "No artefacts exist" in text
        assert "Create an artefact first, then add it to your Stack." in text
        assert "Go to Artefacts" in text
        assert "All artefacts are already in your Stack" in text
        assert "No artefacts match your search" in text


class TestArtefactsPageStackAwareness:
    def test_not_in_stack_shows_add_button(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        col = _create(seeded_app, "Fresh Upload", seeded_app["analyst_token"])
        text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
        assert f'data-add-to-stack="{col["id"]}"' in text
        assert f'data-stack-badge="{col["id"]}"' not in text

    def test_in_stack_shows_quiet_badge_not_button(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        col = _create(seeded_app, "Already Added", seeded_app["analyst_token"])
        c = seeded_app["client"]
        c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))

        text = c.get("/library", headers=_auth(seeded_app["analyst_token"])).text
        assert f'data-stack-badge="{col["id"]}"' in text
        assert f'data-add-to-stack="{col["id"]}"' not in text
        assert "In Stack" in text
