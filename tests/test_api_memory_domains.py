"""Tests for /api/admin/memory-domains (Task 6.2)."""

from __future__ import annotations

import json
import uuid

import pytest

from src.db import get_system_db
from src.repositories.knowledge import KnowledgeRepository


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_knowledge_item(title: str = "T") -> str:
    conn = get_system_db()
    item_id = "ki_" + uuid.uuid4().hex[:8]
    KnowledgeRepository(conn).create(
        id=item_id,
        title=title,
        content="x",
        category="engineering",
        status="approved",
    )
    conn.close()
    return item_id


def _audit_actions_for_resource(resource: str) -> list[dict]:
    conn = get_system_db()
    rows = conn.execute(
        "SELECT action, params FROM audit_log WHERE resource = ? "
        "ORDER BY timestamp",
        [resource],
    ).fetchall()
    conn.close()
    return [
        {"action": a, "params": json.loads(p) if p else None}
        for a, p in rows
    ]


class TestMemoryDomainsList:
    def test_admin_list_includes_seeded(self, seeded_app):
        # v49 migration seeds md_finance / md_engineering etc.
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/memory-domains",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        slugs = [d["slug"] for d in resp.json()]
        assert "finance" in slugs
        assert "engineering" in slugs

    def test_non_admin_gets_403(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/memory-domains",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestMemoryDomainsCreate:
    def test_create_audits(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/memory-domains",
            json={
                "name": "Sales Playbook",
                "slug": "sales-playbook",
                "icon": "🎯",
                "color": "#dcfce7",
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201
        domain_id = resp.json()["id"]
        assert domain_id.startswith("md_")

        rows = _audit_actions_for_resource(f"memory_domain:{domain_id}")
        actions = [r["action"] for r in rows]
        assert "memory_domain.create" in actions

    def test_duplicate_slug_409(self, seeded_app):
        c = seeded_app["client"]
        c.post(
            "/api/admin/memory-domains",
            json={"name": "A", "slug": "dup"},
            headers=_auth(seeded_app["admin_token"]),
        )
        resp = c.post(
            "/api/admin/memory-domains",
            json={"name": "A2", "slug": "dup"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409


class TestMemoryDomainsDetail:
    def test_get_returns_items(self, seeded_app):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        domain_id = c.post(
            "/api/admin/memory-domains",
            json={"name": "D", "slug": "d-detail"},
            headers=headers,
        ).json()["id"]
        item_id = _create_knowledge_item()
        c.post(
            f"/api/admin/memory-domains/{domain_id}/items",
            json={"item_id": item_id},
            headers=headers,
        )
        resp = c.get(f"/api/admin/memory-domains/{domain_id}", headers=headers)
        assert resp.status_code == 200
        assert any(it["id"] == item_id for it in resp.json()["items"])


class TestMemoryDomainsUpdate:
    def test_update_audits_diff(self, seeded_app):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        domain_id = c.post(
            "/api/admin/memory-domains",
            json={"name": "Old", "slug": "u-update"},
            headers=headers,
        ).json()["id"]
        resp = c.put(
            f"/api/admin/memory-domains/{domain_id}",
            json={"name": "New", "icon": "🆕"},
            headers=headers,
        )
        assert resp.status_code == 200
        rows = _audit_actions_for_resource(f"memory_domain:{domain_id}")
        upd = next(r for r in rows if r["action"] == "memory_domain.update")
        assert upd["params"]["after"]["name"] == "New"


class TestMemoryDomainsDelete:
    def test_delete_audits_items_count(self, seeded_app):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        domain_id = c.post(
            "/api/admin/memory-domains",
            json={"name": "D", "slug": "del-domain"},
            headers=headers,
        ).json()["id"]
        item_id = _create_knowledge_item()
        c.post(
            f"/api/admin/memory-domains/{domain_id}/items",
            json={"item_id": item_id},
            headers=headers,
        )
        resp = c.delete(
            f"/api/admin/memory-domains/{domain_id}",
            headers=headers,
        )
        assert resp.status_code == 204
        rows = _audit_actions_for_resource(f"memory_domain:{domain_id}")
        dl = next(r for r in rows if r["action"] == "memory_domain.delete")
        assert dl["params"]["items_count"] == 1


class TestMemoryDomainsJunction:
    def test_add_remove_item_audited(self, seeded_app):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        domain_id = c.post(
            "/api/admin/memory-domains",
            json={"name": "J", "slug": "j-junction"},
            headers=headers,
        ).json()["id"]
        item_id = _create_knowledge_item()
        add = c.post(
            f"/api/admin/memory-domains/{domain_id}/items",
            json={"item_id": item_id},
            headers=headers,
        )
        assert add.status_code == 200
        assert add.json()["added"] is True

        again = c.post(
            f"/api/admin/memory-domains/{domain_id}/items",
            json={"item_id": item_id},
            headers=headers,
        )
        assert again.json()["added"] is False

        rem = c.delete(
            f"/api/admin/memory-domains/{domain_id}/items/{item_id}",
            headers=headers,
        )
        assert rem.status_code == 204

        rows = _audit_actions_for_resource(f"memory_domain:{domain_id}")
        actions = [r["action"] for r in rows]
        assert "memory_domain.add_item" in actions
        assert "memory_domain.remove_item" in actions

    def test_add_unknown_item_404(self, seeded_app):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        domain_id = c.post(
            "/api/admin/memory-domains",
            json={"name": "U", "slug": "u-no-item"},
            headers=headers,
        ).json()["id"]
        resp = c.post(
            f"/api/admin/memory-domains/{domain_id}/items",
            json={"item_id": "does-not-exist"},
            headers=headers,
        )
        assert resp.status_code == 404
