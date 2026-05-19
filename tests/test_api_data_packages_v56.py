"""API tests for v56 ``data_packages`` extended-content endpoints.

Covers:
  * PUT / POST writes for all new fields
  * field-level validation (counts, lengths)
  * GET response includes new fields
  * GET response includes the virtual ``badge`` (`curated` / `new` /
    None) derived server-side from creator membership + age
"""

from __future__ import annotations

import uuid

import pytest

from src.db import get_system_db


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_pkg(seeded_app, **fields) -> str:
    body = {"name": "Sales", "slug": fields.pop("slug", f"s{uuid.uuid4().hex[:8]}"),
            "description": "x"}
    body.update(fields)
    r = seeded_app["client"].post(
        "/api/admin/data-packages", json=body,
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


class TestPutWritesNewFields:
    def test_owner_fields(self, seeded_app):
        pid = _create_pkg(seeded_app)
        r = seeded_app["client"].put(
            f"/api/admin/data-packages/{pid}",
            json={"owner_name": "Jane Doe", "owner_team": "Sales Ops"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["owner_name"] == "Jane Doe"
        assert body["owner_team"] == "Sales Ops"

    def test_tags(self, seeded_app):
        pid = _create_pkg(seeded_app)
        r = seeded_app["client"].put(
            f"/api/admin/data-packages/{pid}",
            json={"tags": ["Finance", "Revenue", "Margin"]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert r.json()["tags"] == ["Finance", "Revenue", "Margin"]

    def test_long_description(self, seeded_app):
        pid = _create_pkg(seeded_app)
        body = "Multi-line\n\n- bullet"
        r = seeded_app["client"].put(
            f"/api/admin/data-packages/{pid}",
            json={"long_description": body},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert r.json()["long_description"] == body

    def test_when_to_use_and_when_not_to_use(self, seeded_app):
        pid = _create_pkg(seeded_app)
        r = seeded_app["client"].put(
            f"/api/admin/data-packages/{pid}",
            json={
                "when_to_use": ["You need X", "You compute Y"],
                "when_not_to_use": ["You only need session counts"],
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["when_to_use"] == ["You need X", "You compute Y"]
        assert body["when_not_to_use"] == ["You only need session counts"]

    def test_example_questions(self, seeded_app):
        pid = _create_pkg(seeded_app)
        qs = ["What was revenue last week?", "Top 10 customers by spend."]
        r = seeded_app["client"].put(
            f"/api/admin/data-packages/{pid}",
            json={"example_questions": qs},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert r.json()["example_questions"] == qs


class TestValidation:
    def test_rejects_too_many_tags(self, seeded_app):
        pid = _create_pkg(seeded_app)
        r = seeded_app["client"].put(
            f"/api/admin/data-packages/{pid}",
            json={"tags": [f"tag{i}" for i in range(20)]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422

    def test_rejects_too_long_tag(self, seeded_app):
        pid = _create_pkg(seeded_app)
        r = seeded_app["client"].put(
            f"/api/admin/data-packages/{pid}",
            json={"tags": ["A" * 100]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422

    def test_rejects_too_long_long_description(self, seeded_app):
        pid = _create_pkg(seeded_app)
        r = seeded_app["client"].put(
            f"/api/admin/data-packages/{pid}",
            json={"long_description": "x" * 5000},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422

    def test_rejects_too_many_when_to_use_bullets(self, seeded_app):
        pid = _create_pkg(seeded_app)
        r = seeded_app["client"].put(
            f"/api/admin/data-packages/{pid}",
            json={"when_to_use": [f"bullet {i}" for i in range(20)]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422

    def test_rejects_too_long_bullet(self, seeded_app):
        pid = _create_pkg(seeded_app)
        r = seeded_app["client"].put(
            f"/api/admin/data-packages/{pid}",
            json={"when_to_use": ["X" * 500]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422

    def test_rejects_too_many_example_questions(self, seeded_app):
        pid = _create_pkg(seeded_app)
        r = seeded_app["client"].put(
            f"/api/admin/data-packages/{pid}",
            json={"example_questions": [f"q{i}?" for i in range(20)]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422


class TestGetIncludesNewFields:
    def test_get_returns_all_new_fields(self, seeded_app):
        pid = _create_pkg(
            seeded_app,
            owner_name="Jane", owner_team="Ops",
            tags=["A", "B"], long_description="body",
            when_to_use=["x"], when_not_to_use=["y"],
            example_questions=["q?"],
        )
        r = seeded_app["client"].get(
            f"/api/admin/data-packages/{pid}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["owner_name"] == "Jane"
        assert body["owner_team"] == "Ops"
        assert body["tags"] == ["A", "B"]
        assert body["long_description"] == "body"
        assert body["when_to_use"] == ["x"]
        assert body["when_not_to_use"] == ["y"]
        assert body["example_questions"] == ["q?"]

    def test_unset_fields_return_empty_list_or_null(self, seeded_app):
        pid = _create_pkg(seeded_app)
        r = seeded_app["client"].get(
            f"/api/admin/data-packages/{pid}",
            headers=_auth(seeded_app["admin_token"]),
        )
        body = r.json()
        assert body["tags"] == []
        assert body["when_to_use"] == []
        assert body["when_not_to_use"] == []
        assert body["example_questions"] == []
        assert body.get("owner_name") is None
        assert body.get("long_description") is None


class TestBadgeDerivation:
    def test_badge_curated_when_admin_created(self, seeded_app):
        """Created_by points to an Admin-group member → badge=='curated'."""
        pid = _create_pkg(seeded_app)
        r = seeded_app["client"].get(
            f"/api/admin/data-packages/{pid}",
            headers=_auth(seeded_app["admin_token"]),
        )
        body = r.json()
        assert "curated" in (body.get("badges") or [])

    def test_badge_new_when_recently_created(self, seeded_app):
        """Any package created in the last 30 days → badge includes 'new'.
        Fresh test → always within window."""
        pid = _create_pkg(seeded_app)
        r = seeded_app["client"].get(
            f"/api/admin/data-packages/{pid}",
            headers=_auth(seeded_app["admin_token"]),
        )
        body = r.json()
        assert "new" in (body.get("badges") or [])

    def test_badge_omits_new_after_threshold(self, seeded_app):
        """Backdate the created_at past the 30-day window → no 'new'.
        'curated' still present because creator is admin."""
        from datetime import datetime, timedelta, timezone

        pid = _create_pkg(seeded_app)
        conn = get_system_db()
        conn.execute(
            "UPDATE data_packages SET created_at = ? WHERE id = ?",
            [datetime.now(timezone.utc) - timedelta(days=120), pid],
        )
        conn.close()
        r = seeded_app["client"].get(
            f"/api/admin/data-packages/{pid}",
            headers=_auth(seeded_app["admin_token"]),
        )
        badges = r.json().get("badges") or []
        assert "new" not in badges
        assert "curated" in badges
