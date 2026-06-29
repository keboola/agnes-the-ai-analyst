"""Tests for the contributed-skill REST API triple-surface.

Covers:
- Non-admin (analyst) gets 403 on all three endpoints
- Admin: POST with valid skill_md → 200 with metadata
- Admin: GET lists contributed plugins → 200 with list
- Admin: DELETE → 204
- Admin: DELETE non-existent → 404
"""

from __future__ import annotations


_SKILL_MD = """\
---
name: Test Skill
description: A test skill for unit tests
---
# Test skill body
"""


def test_analyst_cannot_post(seeded_app):
    client = seeded_app["client"]
    token = seeded_app["analyst_token"]
    r = client.post(
        "/api/admin/contributed-skills",
        json={"skill_md": _SKILL_MD},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


def test_analyst_cannot_get(seeded_app):
    client = seeded_app["client"]
    token = seeded_app["analyst_token"]
    r = client.get(
        "/api/admin/contributed-skills",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


def test_analyst_cannot_delete(seeded_app):
    client = seeded_app["client"]
    token = seeded_app["analyst_token"]
    r = client.delete(
        "/api/admin/contributed-skills/test-skill",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


def test_admin_post_returns_skill_metadata(seeded_app):
    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.post(
        "/api/admin/contributed-skills",
        json={"skill_md": _SKILL_MD, "grant_group": "Admin"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["skill_name"] == "Test Skill"
    assert data["plugin_name"] == "test-skill"
    assert data["granted_group"] == "Admin"


def test_admin_get_lists_plugins(seeded_app):
    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    client.post(
        "/api/admin/contributed-skills",
        json={"skill_md": _SKILL_MD, "grant_group": "Admin"},
        headers={"Authorization": f"Bearer {token}"},
    )
    r = client.get(
        "/api/admin/contributed-skills",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "plugins" in data
    names = [p["name"] for p in data["plugins"]]
    assert "test-skill" in names


def test_admin_delete_returns_204(seeded_app):
    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    client.post(
        "/api/admin/contributed-skills",
        json={"skill_md": _SKILL_MD, "grant_group": "Admin"},
        headers={"Authorization": f"Bearer {token}"},
    )
    r = client.delete(
        "/api/admin/contributed-skills/test-skill",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204


def test_admin_delete_nonexistent_returns_404(seeded_app):
    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = client.delete(
        "/api/admin/contributed-skills/does-not-exist",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
