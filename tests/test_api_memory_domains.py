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

    def test_create_seeds_first_knowledge_item(self, seeded_app):
        """The builder can finally carry the knowledge itself.

        `/admin/studio/corporate-memory` posts here, and the endpoint used to
        accept only name/slug/description — so a page whose subtitle promises
        "Distill reusable knowledge into a memory domain" produced an empty
        container every time, and the content had to be added by an admin
        through a different surface.
        """
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/memory-domains",
            json={
                "name": "Month-end close",
                "slug": "month-end-close",
                "content": "Never report the latest month before the FX rate lands.",
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["item_id"], "the seeded item's id must come back"
        # Authoring is not approving: the item lands pending like every other
        # route into corporate memory, and the response says which state it is
        # in so the builder can tell the author. (Devin Review on #1263.)
        assert body["item_status"] == "pending"

        conn = get_system_db()
        item = KnowledgeRepository(conn).get_by_id(body["item_id"])
        assert item is not None, "the seeded item must exist"
        assert item["content"] == "Never report the latest month before the FX rate lands."
        assert item["domain"] == "month-end-close", "item must land in the new domain"

        rows = _audit_actions_for_resource(f"memory_domain:{body['id']}")
        assert "memory_domain.seed_item" in [r["action"] for r in rows]

    def test_create_without_content_still_makes_an_empty_domain(self, seeded_app):
        """Seeding is opt-in — the old shape must keep working untouched."""
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/memory-domains",
            json={"name": "Empty", "slug": "empty-on-purpose"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201
        assert resp.json()["item_id"] is None

    def test_blank_content_is_not_an_item(self, seeded_app):
        """Whitespace is not knowledge — a textarea the author tabbed through
        must not produce an empty memory item."""
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/memory-domains",
            json={"name": "Blank", "slug": "blank-content", "content": "   \n  "},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201
        assert resp.json()["item_id"] is None

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


class TestSeededItemsDoNotGrowATaxonomy:
    """Devin Review on #1263: `category` feeds the admin page's dropdowns.

    They are built with `SELECT DISTINCT category`, so one category per domain
    would grow a long tail of single-item entries there. The domain is already
    recorded in `domain`; the category says where the item came from.
    """

    def test_the_category_is_one_stable_label(self, seeded_app):
        from app.api.memory_domains import SEEDED_ITEM_CATEGORY

        c = seeded_app["client"]
        made = []
        for slug in ("taxo-one", "taxo-two"):
            resp = c.post(
                "/api/admin/memory-domains",
                json={"name": slug, "slug": slug, "content": "some knowledge"},
                headers=_auth(seeded_app["admin_token"]),
            )
            assert resp.status_code == 201, resp.text
            made.append(resp.json()["item_id"])

        conn = get_system_db()
        repo = KnowledgeRepository(conn)
        cats = {repo.get_by_id(i)["category"] for i in made}
        conn.close()
        assert cats == {SEEDED_ITEM_CATEGORY}, cats


class TestAFailedSeedLeavesNoDomainOnThisPathEither:
    """Devin Review on #1263: I fixed the queue path and left this one.

    The admin endpoint commits the domain before saving the knowledge, so a
    failure there returned a 500 with the empty domain still in place — and
    every retry was refused for the duplicate slug.
    """

    def test_the_domain_is_rolled_back_and_the_retry_works(self, seeded_app):
        from unittest.mock import patch

        from src.repositories import memory_domains_repo

        c = seeded_app["client"]
        body = {"name": "Rollback admin", "slug": "rollback-admin", "content": "knowledge"}

        # TestClient re-raises server exceptions rather than returning 500;
        # what matters here is the state left behind either way.
        with pytest.raises(RuntimeError):
            with patch("app.api.memory_domains.seed_domain_item", side_effect=RuntimeError("boom")):
                c.post("/api/admin/memory-domains", json=body, headers=_auth(seeded_app["admin_token"]))
        assert not any(d["slug"] == "rollback-admin" for d in memory_domains_repo().list())

        retry = c.post("/api/admin/memory-domains", json=body, headers=_auth(seeded_app["admin_token"]))
        assert retry.status_code == 201, retry.text
        assert retry.json()["item_id"]


def test_the_assistant_is_told_about_the_content_field():
    """Devin Review on #1263: the corporate-memory assistant was still
    describing the three-field body, so it could not offer the one thing this
    change added."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "app" / "chat" / "profiles.py").read_text(encoding="utf-8")
    i = text.index("POST /api/admin/memory-domains`")
    assert "content" in text[i : i + 420], text[i : i + 420]
