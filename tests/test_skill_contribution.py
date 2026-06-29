"""Tests for ``src.skill_contribution.contribute_skill``.

``contribute_skill`` is the receiver for an external "Load skill to Agnes"
button: it takes a pasted ``SKILL.md`` and publishes it as a one-skill plugin
into the local, sync-immune "Agnes Contributed" marketplace. These tests pin:

- the happy path writes the on-disk plugin tree and the registry/cache rows,
- the published plugin actually reaches an Admin-group member through the RBAC
  consumption path (``list_granted_for_groups``),
- re-publishing the same skill is idempotent (one plugin, not duplicates),
- empty / frontmatter-less input is rejected with ``SkillContributionError``.

They use the ``e2e_env`` fixture so the repository factory resolves to a fresh,
isolated DuckDB system DB (schema + system groups auto-seeded by
``get_system_db``).
"""

from __future__ import annotations

import pytest

from src.skill_contribution import (
    CONTRIBUTED_MARKETPLACE_SLUG,
    SkillContributionError,
    _slugify,
    contribute_skill,
)

_SKILL = """---
name: Revenue Booking - Customer ARR Classification
description: Classify customers by quarterly ARR movement (upsell/downsell/stall/new/churned).
---

# Revenue Booking — Customer ARR Classification

Operator reviews quarterly ARR and classifies each customer.
"""


def test_slugify_yields_valid_marketplace_slug():
    from src.marketplace import is_valid_slug

    slug = _slugify("Revenue Booking – Customer ARR!!")
    assert slug == "revenue-booking-customer-arr"
    assert is_valid_slug(slug)


def test_contribute_publishes_files_and_grants(e2e_env):
    from app.utils import get_marketplaces_dir
    from src.repositories import (
        marketplace_plugins_repo,
        marketplace_registry_repo,
        user_groups_repo,
    )

    res = contribute_skill(_SKILL, registered_by="tester@test", grant_group="Admin")

    pname = res["plugin_name"]
    assert pname == "revenue-booking-customer-arr-classification"
    assert res["granted_group"] == "Admin"
    assert res["detail_url"] == f"/marketplace/curated/{CONTRIBUTED_MARKETPLACE_SLUG}/{pname}"

    # On-disk plugin tree.
    root = get_marketplaces_dir() / CONTRIBUTED_MARKETPLACE_SLUG
    assert (root / ".claude-plugin" / "marketplace.json").is_file()
    assert (root / "plugins" / pname / ".claude-plugin" / "plugin.json").is_file()
    assert (root / "plugins" / pname / "skills" / pname / "SKILL.md").is_file()

    # Registry row is sync-immune (is_builtin → nightly git-sync skips it).
    reg = marketplace_registry_repo().get(CONTRIBUTED_MARKETPLACE_SLUG)
    assert reg is not None and reg["is_builtin"]

    # Plugin is cached and actually served to an Admin-group member.
    cached = {p["name"] for p in marketplace_plugins_repo().list_for_marketplace(CONTRIBUTED_MARKETPLACE_SLUG)}
    assert pname in cached
    admin = user_groups_repo().get_by_name("Admin")
    served = marketplace_plugins_repo().list_granted_for_groups([admin["id"]])
    assert (CONTRIBUTED_MARKETPLACE_SLUG, pname) in {(g["marketplace_id"], g["name"]) for g in served}


def test_contribute_is_idempotent(e2e_env):
    from src.repositories import marketplace_plugins_repo

    contribute_skill(_SKILL, grant_group="Admin")
    contribute_skill(_SKILL, grant_group="Admin")

    plugins = marketplace_plugins_repo().list_for_marketplace(CONTRIBUTED_MARKETPLACE_SLUG)
    assert len(plugins) == 1


def test_contribute_rejects_missing_frontmatter_name(e2e_env):
    with pytest.raises(SkillContributionError):
        contribute_skill("# Just a heading, no YAML frontmatter")


def test_contribute_rejects_empty(e2e_env):
    with pytest.raises(SkillContributionError):
        contribute_skill("   \n  ")


def test_rest_delete_removes_plugin_and_grants(e2e_env):
    from app.auth.jwt import create_access_token
    from app.main import create_app
    from app.utils import get_marketplaces_dir
    from fastapi.testclient import TestClient
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories import resource_grants_repo
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="del_admin1", email="deladmin@test.com", name="DelAdmin")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("del_admin1", admin_gid, source="system_seed")
    conn.close()

    result = contribute_skill(_SKILL, registered_by="tester@test", grant_group="Admin")
    pname = result["plugin_name"]

    root = get_marketplaces_dir() / CONTRIBUTED_MARKETPLACE_SLUG
    assert (root / "plugins" / pname).exists()

    token = create_access_token("del_admin1", "deladmin@test.com")
    client = TestClient(create_app())
    r = client.delete(
        f"/api/admin/contributed-skills/{pname}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204

    assert not (root / "plugins" / pname).exists()
    grants = resource_grants_repo().list_all(resource_type="marketplace_plugin")
    resource_ids = {g["resource_id"] for g in grants}
    assert f"{CONTRIBUTED_MARKETPLACE_SLUG}/{pname}" not in resource_ids
