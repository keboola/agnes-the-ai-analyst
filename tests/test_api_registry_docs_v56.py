"""API tests for v56 ``table_registry`` extended-doc PATCH endpoint.

The existing ``PATCH /api/admin/registry/{table_id}/docs`` (v52) accepts
``sample_questions``, ``things_to_know``, ``pairs_well_with``. v56
extends it to also accept ``grain``, ``platforms``, ``partition_col``,
``history``, ``gotchas``.

GET ``/api/admin/registry/{table_id}`` must echo the new fields.
"""

from __future__ import annotations

import uuid

import pytest

from src.db import get_system_db


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_table(table_id: str = "tbl_t1") -> str:
    conn = get_system_db()
    conn.execute(
        "INSERT INTO table_registry(id, name, source_type, query_mode) "
        "VALUES (?, ?, 'keboola', 'local')",
        [table_id, f"name_{table_id}"],
    )
    conn.close()
    return table_id


class TestPatchExtendedDocs:
    def test_patch_grain(self, seeded_app):
        tid = _seed_table(f"tbl_{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].patch(
            f"/api/admin/registry/{tid}/docs",
            json={"grain": "1 row per session"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json().get("grain") == "1 row per session"

    def test_patch_platforms(self, seeded_app):
        tid = _seed_table(f"tbl_{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].patch(
            f"/api/admin/registry/{tid}/docs",
            json={"platforms": ["MBNXT", "Legacy"]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert r.json().get("platforms") == ["MBNXT", "Legacy"]

    def test_patch_partition_col(self, seeded_app):
        tid = _seed_table(f"tbl_{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].patch(
            f"/api/admin/registry/{tid}/docs",
            json={"partition_col": "event_date"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert r.json().get("partition_col") == "event_date"

    def test_patch_history(self, seeded_app):
        tid = _seed_table(f"tbl_{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].patch(
            f"/api/admin/registry/{tid}/docs",
            json={"history": "Rolling 15 months"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert r.json().get("history") == "Rolling 15 months"

    def test_patch_gotchas_array(self, seeded_app):
        tid = _seed_table(f"tbl_{uuid.uuid4().hex[:8]}")
        gotchas = [
            {"key": True, "body": "Always filter mbnxt"},
            {"key": False, "body": "Country goes on S1"},
        ]
        r = seeded_app["client"].patch(
            f"/api/admin/registry/{tid}/docs",
            json={"gotchas": gotchas},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert r.json().get("gotchas") == gotchas


class TestValidation:
    def test_rejects_too_many_gotchas(self, seeded_app):
        tid = _seed_table(f"tbl_{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].patch(
            f"/api/admin/registry/{tid}/docs",
            json={"gotchas": [
                {"key": False, "body": f"g{i}"} for i in range(20)
            ]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422

    def test_rejects_gotcha_missing_body(self, seeded_app):
        tid = _seed_table(f"tbl_{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].patch(
            f"/api/admin/registry/{tid}/docs",
            json={"gotchas": [{"key": True}]},  # body required
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422

    def test_rejects_too_many_platforms(self, seeded_app):
        tid = _seed_table(f"tbl_{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].patch(
            f"/api/admin/registry/{tid}/docs",
            json={"platforms": [f"P{i}" for i in range(20)]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422


class TestAtomicityAndAccess:
    def test_patch_v56_and_v52_fields_together(self, seeded_app):
        tid = _seed_table(f"tbl_{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].patch(
            f"/api/admin/registry/{tid}/docs",
            json={
                "grain": "1 row per session",
                "sample_questions": ["What is X?"],
                "things_to_know": "Some legacy notes",
                "gotchas": [{"key": True, "body": "Big one"}],
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        body = r.json()
        assert body.get("grain") == "1 row per session"
        assert body.get("sample_questions") == ["What is X?"]
        assert body.get("things_to_know") == "Some legacy notes"
        assert body.get("gotchas")[0]["body"] == "Big one"

    def test_non_admin_cannot_patch_docs(self, seeded_app):
        tid = _seed_table(f"tbl_{uuid.uuid4().hex[:8]}")
        r = seeded_app["client"].patch(
            f"/api/admin/registry/{tid}/docs",
            json={"grain": "x"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 403
