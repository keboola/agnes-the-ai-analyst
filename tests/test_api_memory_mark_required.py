"""Tests for /api/memory/items/{id}/mark-(un)mandatory endpoints (Task 6.4).

Covers admin guard, is_required toggle, and ``memory_item.set_required``
audit row shape per Section 9.1 of the unified stack design.
"""

from __future__ import annotations

import json
import uuid

import pytest

from src.db import get_system_db
from src.repositories.knowledge import KnowledgeRepository


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_item(is_required: bool = False) -> str:
    conn = get_system_db()
    item_id = "ki_" + uuid.uuid4().hex[:8]
    KnowledgeRepository(conn).create(
        id=item_id,
        title="T",
        content="x",
        category="engineering",
        status="approved",
        is_required=is_required,
    )
    conn.close()
    return item_id


def _audit_rows_for(item_id: str) -> list[dict]:
    conn = get_system_db()
    rows = conn.execute(
        "SELECT action, params FROM audit_log "
        "WHERE resource = ? AND action = 'memory_item.set_required' "
        "ORDER BY timestamp",
        [f"knowledge_item:{item_id}"],
    ).fetchall()
    conn.close()
    return [
        {"action": a, "params": json.loads(p) if p else None}
        for a, p in rows
    ]


def _get_item(item_id: str) -> dict:
    conn = get_system_db()
    item = KnowledgeRepository(conn).get_by_id(item_id)
    conn.close()
    return item


class TestMarkMandatory:
    def test_mark_mandatory_flips_flag_and_audits(self, seeded_app):
        item_id = _create_item(is_required=False)
        resp = seeded_app["client"].post(
            f"/api/memory/items/{item_id}/mark-mandatory",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["is_required"] is True
        assert _get_item(item_id)["is_required"] is True
        rows = _audit_rows_for(item_id)
        assert rows[-1]["params"] == {"new_value": True}

    def test_mark_mandatory_requires_admin(self, seeded_app):
        item_id = _create_item()
        resp = seeded_app["client"].post(
            f"/api/memory/items/{item_id}/mark-mandatory",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestMarkUnmandatory:
    def test_mark_unmandatory_flips_flag_and_audits(self, seeded_app):
        item_id = _create_item(is_required=True)
        resp = seeded_app["client"].post(
            f"/api/memory/items/{item_id}/mark-unmandatory",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["is_required"] is False
        assert _get_item(item_id)["is_required"] is False
        rows = _audit_rows_for(item_id)
        assert rows[-1]["params"] == {"new_value": False}

    def test_mark_unmandatory_requires_admin(self, seeded_app):
        item_id = _create_item(is_required=True)
        resp = seeded_app["client"].post(
            f"/api/memory/items/{item_id}/mark-unmandatory",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_mark_unmandatory_unknown_item_404(self, seeded_app):
        resp = seeded_app["client"].post(
            "/api/memory/items/ki_nope/mark-unmandatory",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404
