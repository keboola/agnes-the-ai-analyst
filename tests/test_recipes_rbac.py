"""RBAC tests for /api/recipes — admin sees all, analyst sees only
recipes their groups have a ``resource_grants`` row for (v55).

Mirrors the analogous RBAC behavior on /api/data-packages: default
visibility is *closed* — with no grant the recipe is hidden even for
status='prod' rows. Admin short-circuits the check.
"""

from __future__ import annotations

import uuid

from src.db import get_system_db


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_recipe(slug: str, title: str, status: str = "prod") -> str:
    from src.repositories.recipes import RecipesRepository

    conn = get_system_db()
    rid = RecipesRepository(conn).create(
        slug=slug,
        title=title,
        description=None,
        icon=None,
        color=None,
        sql_template=None,
        related_table_ids=None,
        status=status,
        created_by="test",
    )
    conn.close()
    return rid


def _group_with(user_id: str, name: str) -> str:
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    g = UserGroupsRepository(conn).create(name=name, description="", created_by="test")
    gid = g["id"] if isinstance(g, dict) else g
    UserGroupMembersRepository(conn).add_member(user_id, gid, source="test")
    conn.close()
    return gid


def _grant(group_id: str, recipe_id: str, requirement: str = "available") -> None:
    conn = get_system_db()
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, 'recipe', ?, ?, CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), group_id, recipe_id, requirement],
    )
    conn.close()


class TestRecipeListRbac:
    def test_admin_sees_every_recipe(self, seeded_app):
        _create_recipe("r-pub", "Public")
        _create_recipe("r-prv", "Private")
        resp = seeded_app["client"].get(
            "/api/recipes",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        slugs = {r["slug"] for r in resp.json()["items"]}
        assert {"r-pub", "r-prv"}.issubset(slugs)

    def test_analyst_without_grant_sees_nothing(self, seeded_app):
        _create_recipe("r-secret", "Secret")
        resp = seeded_app["client"].get(
            "/api/recipes",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        slugs = {r["slug"] for r in resp.json()["items"]}
        # The recipe exists but the analyst has no grant for it →
        # default-closed behavior hides it.
        assert "r-secret" not in slugs

    def test_analyst_with_grant_sees_recipe(self, seeded_app):
        rid = _create_recipe("r-shared", "Shared")
        gid = _group_with("analyst1", "Analysts-shared")
        _grant(gid, rid, "available")
        resp = seeded_app["client"].get(
            "/api/recipes",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        slugs = {r["slug"] for r in resp.json()["items"]}
        assert "r-shared" in slugs

    def test_analyst_with_grant_does_not_see_draft(self, seeded_app):
        # A grant on a draft recipe doesn't flip the status gate —
        # drafts remain admin-only regardless of grants.
        rid = _create_recipe("r-draft", "Draft", status="draft")
        gid = _group_with("analyst1", "Analysts-draft")
        _grant(gid, rid, "available")
        resp = seeded_app["client"].get(
            "/api/recipes",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        slugs = {r["slug"] for r in resp.json()["items"]}
        assert "r-draft" not in slugs

    def test_recipe_access_resolved_once_not_per_row(self, seeded_app, monkeypatch):
        # N+1 regression guard: the accessible-RECIPE set must be resolved
        # exactly once per request (via get_accessible_ids), regardless of
        # how many recipes exist — not once per row via can_access.
        for i in range(5):
            rid = f"r-bulk-{i}"
            _create_recipe(rid, f"Bulk {i}")
        gid = _group_with("analyst1", "Analysts-bulk")
        # Grant a couple of them so the filter has real work to do.
        conn = get_system_db()
        rows = conn.execute("SELECT id FROM recipes WHERE slug LIKE 'r-bulk-%'").fetchall()
        conn.close()
        for (rid,) in rows[:2]:
            _grant(gid, rid, "available")

        calls = {"n": 0}
        import app.api.recipes as recipes_module

        original = recipes_module.get_accessible_ids

        def _counting_get_accessible_ids(*args, **kwargs):
            calls["n"] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(recipes_module, "get_accessible_ids", _counting_get_accessible_ids)

        resp = seeded_app["client"].get(
            "/api/recipes",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        assert calls["n"] == 1


class TestRecipeGetBySlugRbac:
    def test_admin_can_get_any(self, seeded_app):
        _create_recipe("r-anything", "Anything")
        resp = seeded_app["client"].get(
            "/api/recipes/r-anything",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["slug"] == "r-anything"

    def test_analyst_without_grant_gets_404(self, seeded_app):
        # 404 (not 403) so unprivileged callers can't probe for the
        # existence of recipes they aren't allowed to know about.
        _create_recipe("r-hidden", "Hidden")
        resp = seeded_app["client"].get(
            "/api/recipes/r-hidden",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 404

    def test_analyst_with_grant_gets_recipe(self, seeded_app):
        rid = _create_recipe("r-visible", "Visible")
        gid = _group_with("analyst1", "Analysts-visible")
        _grant(gid, rid, "available")
        resp = seeded_app["client"].get(
            "/api/recipes/r-visible",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["slug"] == "r-visible"


class TestRecipeResourceTypeSpec:
    """The new ResourceType.RECIPE registration must surface on
    /api/admin/resource-types so the admin /access UI can list it."""

    def test_recipe_listed_in_resource_types(self, seeded_app):
        resp = seeded_app["client"].get(
            "/api/admin/resource-types",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        types = {t["key"] for t in resp.json()}
        assert "recipe" in types

    def test_recipe_blocks_projection(self, seeded_app):
        # The list_blocks projection should return one synthetic block
        # holding the recipe items — same shape as memory_domain. Surfaced
        # via /api/admin/access-overview as the `resources[*]` array.
        _create_recipe("r-block-1", "First")
        _create_recipe("r-block-2", "Second")
        resp = seeded_app["client"].get(
            "/api/admin/access-overview",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        recipe_section = next(
            (r for r in body["resources"] if r["type_key"] == "recipe"),
            None,
        )
        assert recipe_section is not None
        # One synthetic "Recipes" block holding both items.
        assert len(recipe_section["blocks"]) == 1
        names = {item["name"] for item in recipe_section["blocks"][0]["items"]}
        assert {"First", "Second"}.issubset(names)

    def test_recipe_blocks_skip_soft_deleted(self, seeded_app):
        # Soft-deleted recipes are filtered out of the admin grant list
        # so an admin can't accidentally hand out access to rows the
        # Recipes tab can no longer show.
        from src.repositories.recipes import RecipesRepository

        rid = _create_recipe("r-dropped", "Dropped")
        conn = get_system_db()
        RecipesRepository(conn).delete(rid)
        conn.close()
        resp = seeded_app["client"].get(
            "/api/admin/access-overview",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        recipe_section = next(
            (r for r in body["resources"] if r["type_key"] == "recipe"),
            None,
        )
        if recipe_section is None or not recipe_section["blocks"]:
            return  # nothing to assert if no live recipes at all
        names = {item["name"] for item in recipe_section["blocks"][0]["items"]}
        assert "Dropped" not in names
