"""Admin endpoints for the flea-market guardrail submissions surface.

Covers:
  * Listing — non-admin gets 403, admin gets the table
  * Override — flips status + entity visibility, writes audit row
  * Retry — re-queues a review_error / blocked_llm submission
  * Delete — wipes both submission row and entity bundle
  * Override edge: inline-blocked submissions without an entity_id
    cannot be overridden (refused with 409)
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.utils import get_store_dir
from src.db import close_system_db, get_system_db
from src.repositories.users import UserRepository


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    close_system_db()
    from app.main import create_app
    app = create_app()
    yield TestClient(app)
    close_system_db()


def _create_user(client, email, password="UserPass1!"):
    ph = PasswordHasher()
    conn = get_system_db()
    user_id = email.split("@")[0]
    UserRepository(conn).create(
        id=user_id, email=email, name=user_id, password_hash=ph.hash(password),
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, {"access_token": r.json()["access_token"]}


def _create_admin(client, email="admin@x.com"):
    from tests.helpers.auth import grant_admin
    user_id, cookies = _create_user(client, email, password="AdminPass1!")
    conn = get_system_db()
    grant_admin(conn, user_id)
    conn.close()
    return user_id, cookies


def _make_skill_zip(skill_name: str = "probe") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: A clean test skill bundle for reviews.\n---\n\n"
            + ("Body that is intentionally long enough to clear quality thresholds. " * 6),
        )
    return buf.getvalue()


def _make_eval_skill_zip(skill_name: str = "bad") -> bytes:
    """A skill with a bash-eval script — guaranteed to fail static_security."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: A bad test skill bundle for reviews.\n---\n\n"
            + ("Body. " * 50),
        )
        zf.writestr(f"{skill_name}/run.sh", "#!/bin/sh\neval $1\n")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# /api/admin/store/submissions — listing
# ---------------------------------------------------------------------------


class TestAdminListing:
    def test_non_admin_forbidden(self, web_client):
        _, user_cookies = _create_user(web_client, "user@x.com")
        r = web_client.get("/api/admin/store/submissions", cookies=user_cookies)
        assert r.status_code == 403

    def test_admin_sees_blocked_inline_submission(self, web_client):
        # Bad upload from a regular user → inline-blocked → submission row.
        _, user_cookies = _create_user(web_client, "u@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("bad"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        assert c.status_code == 422
        # 422 detail must include both submission_id AND entity_id so
        # the upload-page JS can redirect the submitter to the detail
        # page (same UX as a successful upload — they land on the
        # quarantine banner instead of staying stuck on /store/new).
        detail = c.json()["detail"]
        assert detail["code"] == "submission_blocked"
        assert detail["submission_id"]
        assert detail["entity_id"], (
            "422 body must carry entity_id so the uploader can be "
            "redirected to /marketplace/flea/{entity_id}"
        )

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/api/admin/store/submissions?status=blocked_inline",
            cookies=admin_cookies,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1
        assert any(s["status"] == "blocked_inline" for s in body["items"])


# ---------------------------------------------------------------------------
# Override
# ---------------------------------------------------------------------------


class TestAdminOverride:
    def test_override_inline_blocked_publishes_entity(self, web_client):
        """v30: inline-blocked submissions now persist the bundle + entity
        row at visibility=hidden, so override flips them to approved
        identically to blocked_llm."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        _, user_cookies = _create_user(web_client, "u@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("bad"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        assert c.status_code == 422
        sub_id = c.json()["detail"]["submission_id"]

        # Confirm v30 invariants: submission carries entity_id + sha + size,
        # entity row exists at visibility=hidden.
        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["entity_id"] is not None
        assert sub["bundle_sha256"] and len(sub["bundle_sha256"]) == 64
        assert sub["file_size"] and sub["file_size"] > 0
        ent = StoreEntitiesRepository(conn).get(sub["entity_id"])
        assert ent and ent["visibility_status"] == "hidden"
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sub_id}/override",
            json={"reason": "false positive — internal-only"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "overridden"
        ent = StoreEntitiesRepository(conn).get(sub["entity_id"])
        assert ent["visibility_status"] == "approved"
        conn.close()

    def test_override_blocked_llm_publishes_entity(self, web_client):
        """Manually stage a blocked_llm row + entity, then override — the
        entity must flip to visibility_status='approved' and the
        submission to 'overridden'."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        user_id, _ = _create_user(web_client, "submitter@x.com")
        conn = get_system_db()
        ents = StoreEntitiesRepository(conn)
        ents.create(
            id="ent-blk", owner_user_id=user_id, owner_username="submitter",
            type="skill", name="blocked-thing", description="x" * 30,
            category=None, version="1.0.0", file_size=10,
            visibility_status="pending",
        )
        subs = StoreSubmissionsRepository(conn)
        sid = subs.create(
            submitter_id=user_id, submitter_email="submitter@x.com",
            type="skill", name="blocked-thing", version="1.0.0",
            status="blocked_llm", entity_id="ent-blk",
            llm_findings={"risk_level": "high", "summary": "exfil"},
        )
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/override",
            json={"reason": "false positive — internal-only constants"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get("ent-blk")
        assert ent["visibility_status"] == "approved"
        sub = StoreSubmissionsRepository(conn).get(sid)
        assert sub["status"] == "overridden"
        assert sub["override_reason"].startswith("false positive")
        conn.close()

    def test_override_short_reason_rejected(self, web_client):
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        user_id, _ = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        StoreEntitiesRepository(conn).create(
            id="e1", owner_user_id=user_id, owner_username="u",
            type="skill", name="x", description="x" * 30, category=None,
            version="1.0.0", file_size=10, visibility_status="pending",
        )
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=user_id, submitter_email="u@x.com",
            type="skill", name="x", version="1.0.0",
            status="blocked_llm", entity_id="e1",
        )
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/override",
            json={"reason": "ok"},  # 2 chars < min_length=4
            cookies=admin_cookies,
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestAdminDelete:
    def test_delete_clears_submission_and_entity(self, web_client):
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        user_id, _ = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        StoreEntitiesRepository(conn).create(
            id="e2", owner_user_id=user_id, owner_username="u",
            type="skill", name="y", description="x" * 30, category=None,
            version="1.0.0", file_size=10, visibility_status="approved",
        )
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=user_id, submitter_email="u@x.com",
            type="skill", name="y", version="1.0.0",
            status="approved", entity_id="e2",
        )
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.delete(
            f"/api/admin/store/submissions/{sid}",
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        assert StoreEntitiesRepository(conn).get("e2") is None
        assert StoreSubmissionsRepository(conn).get(sid) is None
        conn.close()


# ---------------------------------------------------------------------------
# List filters + pagination
# ---------------------------------------------------------------------------


def _seed_submissions(submitters):
    """Stage rows directly through the repo. Returns list of submission ids
    in insertion order so tests can reference rows."""
    from src.repositories.store_submissions import StoreSubmissionsRepository
    conn = get_system_db()
    ids = []
    subs = StoreSubmissionsRepository(conn)
    for u_id, u_email, type_, name, version, status in submitters:
        ids.append(subs.create(
            submitter_id=u_id, submitter_email=u_email,
            type=type_, name=name, version=version, status=status,
            entity_id=f"ent-{u_id}-{name}",
        ))
    conn.close()
    return ids


class TestAdminListFilters:
    def test_filter_by_submitter(self, web_client):
        u1, _ = _create_user(web_client, "alice@x.com")
        u2, _ = _create_user(web_client, "bob@x.com")
        _seed_submissions([
            (u1, "alice@x.com", "skill", "thing", "1.0", "approved"),
            (u1, "alice@x.com", "skill", "other", "1.0", "blocked_llm"),
            (u2, "bob@x.com",   "skill", "third", "1.0", "approved"),
        ])
        _, admin_cookies = _create_admin(web_client)

        r = web_client.get(f"/api/admin/store/submissions?submitter={u1}", cookies=admin_cookies)
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_filter_by_type(self, web_client):
        u, _ = _create_user(web_client, "u@x.com")
        _seed_submissions([
            (u, "u@x.com", "skill",  "a", "1.0", "approved"),
            (u, "u@x.com", "agent",  "b", "1.0", "approved"),
            (u, "u@x.com", "plugin", "c", "1.0", "approved"),
        ])
        _, admin_cookies = _create_admin(web_client)

        r = web_client.get("/api/admin/store/submissions?type=agent", cookies=admin_cookies)
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["type"] == "agent"

    def test_invalid_type_400(self, web_client):
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get("/api/admin/store/submissions?type=bogus", cookies=admin_cookies)
        assert r.status_code == 400

    def test_filter_by_name_substring(self, web_client):
        u, _ = _create_user(web_client, "u@x.com")
        _seed_submissions([
            (u, "u@x.com", "skill", "summarizer-pro",   "1.0", "approved"),
            (u, "u@x.com", "skill", "summarizer-alpha", "1.0", "approved"),
            (u, "u@x.com", "skill", "totally-other",    "1.0", "approved"),
        ])
        _, admin_cookies = _create_admin(web_client)

        r = web_client.get("/api/admin/store/submissions?name=summarizer", cookies=admin_cookies)
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_filter_by_version_substring(self, web_client):
        u, _ = _create_user(web_client, "u@x.com")
        _seed_submissions([
            (u, "u@x.com", "skill", "a", "1.0.0", "approved"),
            (u, "u@x.com", "skill", "b", "1.0.1", "approved"),
            (u, "u@x.com", "skill", "c", "2.0.0", "approved"),
        ])
        _, admin_cookies = _create_admin(web_client)

        r = web_client.get("/api/admin/store/submissions?version=1.0", cookies=admin_cookies)
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_combined_filters(self, web_client):
        u1, _ = _create_user(web_client, "alice@x.com")
        u2, _ = _create_user(web_client, "bob@x.com")
        _seed_submissions([
            (u1, "alice@x.com", "skill", "x", "1.0", "blocked_llm"),
            (u1, "alice@x.com", "agent", "y", "1.0", "blocked_llm"),
            (u2, "bob@x.com",   "skill", "z", "1.0", "blocked_llm"),
        ])
        _, admin_cookies = _create_admin(web_client)

        r = web_client.get(
            f"/api/admin/store/submissions?submitter={u1}&type=skill",
            cookies=admin_cookies,
        )
        assert r.json()["total"] == 1


class TestAdminListPagination:
    def test_skip_limit_and_total(self, web_client):
        u, _ = _create_user(web_client, "u@x.com")
        _seed_submissions([
            (u, "u@x.com", "skill", f"thing-{i}", "1.0", "approved")
            for i in range(7)
        ])
        _, admin_cookies = _create_admin(web_client)

        r = web_client.get("/api/admin/store/submissions?limit=3&skip=0", cookies=admin_cookies)
        b = r.json()
        assert b["total"] == 7 and len(b["items"]) == 3

        r = web_client.get("/api/admin/store/submissions?limit=3&skip=3", cookies=admin_cookies)
        b = r.json()
        assert b["total"] == 7 and len(b["items"]) == 3

        r = web_client.get("/api/admin/store/submissions?limit=3&skip=6", cookies=admin_cookies)
        b = r.json()
        assert b["total"] == 7 and len(b["items"]) == 1

    def test_limit_clamped(self, web_client):
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get("/api/admin/store/submissions?limit=999999", cookies=admin_cookies)
        # Endpoint clamps to 500. No assertion on content; smoke test.
        assert r.status_code == 200
        assert r.json()["limit"] == 500


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------


class TestAdminDetailPage:
    def test_detail_renders_for_existing_submission(self, web_client):
        u, _ = _create_user(web_client, "u@x.com")
        ids = _seed_submissions([
            (u, "u@x.com", "skill", "thing", "1.0", "approved"),
        ])
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(f"/admin/store/submissions/{ids[0]}", cookies=admin_cookies)
        assert r.status_code == 200
        body = r.text
        assert ids[0] in body
        assert "Back to all submissions" in body

    def test_detail_404_on_missing(self, web_client):
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get("/admin/store/submissions/does-not-exist", cookies=admin_cookies)
        assert r.status_code == 404

    def test_detail_non_admin_forbidden(self, web_client):
        u, user_cookies = _create_user(web_client, "u@x.com")
        ids = _seed_submissions([
            (u, "u@x.com", "skill", "thing", "1.0", "approved"),
        ])
        r = web_client.get(f"/admin/store/submissions/{ids[0]}", cookies=user_cookies)
        assert r.status_code in (302, 401, 403)


# ---------------------------------------------------------------------------
# Rescan
# ---------------------------------------------------------------------------


def _stage_entity_with_bundle(tmp_root, owner_id, name, body=None):
    """Create a real on-disk plugin tree under DATA_DIR/store/<entity_id>/plugin
    so the rescan endpoint sees a bundle to scan."""
    from pathlib import Path
    from src.repositories.store_entities import StoreEntitiesRepository
    import uuid
    entity_id = uuid.uuid4().hex
    plugin_dir = Path(tmp_root) / "store" / entity_id / "plugin" / "skills" / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "SKILL.md").write_text(
        body or (
            "---\nname: " + name + "\ndescription: rescan probe skill\n---\n\n"
            + ("Long body to satisfy quality threshold. " * 8)
        ),
        encoding="utf-8",
    )
    conn = get_system_db()
    StoreEntitiesRepository(conn).create(
        id=entity_id, owner_user_id=owner_id, owner_username=owner_id,
        type="skill", name=name, description="x" * 30, category=None,
        version="1.0.0", file_size=10, visibility_status="approved",
    )
    conn.close()
    return entity_id


class TestAdminRescan:
    def test_rescan_clean_bundle_pending_llm(self, web_client, tmp_path):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, _ = _create_user(web_client, "u@x.com")
        eid = _stage_entity_with_bundle(tmp_path, u, "rescan-clean")
        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="rescan-clean", version="1.0.0",
            status="approved", entity_id=eid,
        )
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/rescan", cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        # No ANTHROPIC_API_KEY in test env → guardrails disabled → auto-approved.
        assert r.json()["status"] in {"pending_llm", "approved"}

        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(sid)
        assert "retry_count" not in sub  # v34: column dropped
        conn.close()

    def test_rescan_dirty_bundle_blocks_inline(self, web_client, tmp_path):
        """A bundle that introduces a static-security violation since the
        original review must rescan to blocked_inline."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from pathlib import Path
        u, _ = _create_user(web_client, "u@x.com")
        eid = _stage_entity_with_bundle(tmp_path, u, "rescan-dirty")
        # Inject a bash-eval script — re-rescan must catch it.
        bad = Path(tmp_path) / "store" / eid / "plugin" / "skills" / "rescan-dirty" / "run.sh"
        bad.write_text("#!/bin/sh\neval $1\n", encoding="utf-8")

        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="rescan-dirty", version="1.0.0",
            status="approved", entity_id=eid,
        )
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/rescan", cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "blocked_inline"

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent["visibility_status"] == "hidden"
        sub = StoreSubmissionsRepository(conn).get(sid)
        assert sub["status"] == "blocked_inline"
        # Static-security finding from the new bash-eval is captured.
        ic = sub["inline_checks"]
        assert ic["static_security"]["status"] == "fail"
        assert any(f["category"] == "code_exec" for f in ic["static_security"]["findings"])
        conn.close()

    def test_rescan_without_entity_409(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, _ = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="x", version="1.0.0",
            status="blocked_inline", entity_id=None,
        )
        conn.close()
        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/rescan", cookies=admin_cookies,
        )
        assert r.status_code == 409

    def test_rescan_missing_bundle_410(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, _ = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="x", version="1.0.0",
            status="approved", entity_id="missing-eid",
        )
        conn.close()
        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/rescan", cookies=admin_cookies,
        )
        assert r.status_code == 410

    def test_rescan_non_admin_forbidden(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, user_cookies = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="x", version="1.0.0", status="approved",
        )
        conn.close()
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/rescan", cookies=user_cookies,
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# v30: Download bundle, Sort by size, Quota
# ---------------------------------------------------------------------------


class TestAdminBundleDownload:
    def test_download_returns_zip(self, web_client):
        """Live blocked bundle is downloadable as a fresh ZIP."""
        _, user_cookies = _create_user(web_client, "u@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("dl"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        assert c.status_code == 422
        sub_id = c.json()["detail"]["submission_id"]

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/api/admin/store/submissions/{sub_id}/bundle.zip",
            cookies=admin_cookies,
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert "attachment" in r.headers["content-disposition"]
        # Body is a valid ZIP
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert any("SKILL.md" in n for n in zf.namelist())
            assert any("run.sh" in n for n in zf.namelist())

    def test_download_410_after_purge(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, _ = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="x", version="1.0.0",
            status="blocked_inline", entity_id=None,
        )
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/api/admin/store/submissions/{sub_id}/bundle.zip",
            cookies=admin_cookies,
        )
        assert r.status_code == 410

    def test_download_non_admin_forbidden(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, user_cookies = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="x", version="1.0.0",
            status="approved", entity_id="some-id",
        )
        conn.close()
        r = web_client.get(
            f"/api/admin/store/submissions/{sub_id}/bundle.zip",
            cookies=user_cookies,
        )
        assert r.status_code == 403


class TestAdminSortBySize:
    def test_sort_file_size_asc_desc(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, _ = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="big", version="1", status="approved",
            file_size=10000,
        )
        StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="med", version="1", status="approved",
            file_size=5000,
        )
        StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="tiny", version="1", status="approved",
            file_size=100,
        )
        conn.close()

        # Admin endpoint passes sort/order through to the repo whitelist
        # (#23). Confirm both directions via the API.
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/api/admin/store/submissions?sort=file_size&order=asc",
            cookies=admin_cookies,
        )
        assert r.status_code == 200
        names = [i["name"] for i in r.json()["items"]]
        assert names == ["tiny", "med", "big"], names

        r = web_client.get(
            "/api/admin/store/submissions?sort=file_size&order=desc",
            cookies=admin_cookies,
        )
        assert r.status_code == 200
        names = [i["name"] for i in r.json()["items"]]
        assert names == ["big", "med", "tiny"], names

    def test_invalid_sort_key_400(self, web_client):
        """#23 — sort whitelist rejects bogus keys at the API edge.
        Pre-fix, an unknown key fell through to a substring-replace
        chain that could surface 500s; now it's a clean 400."""
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/api/admin/store/submissions?sort=injected__column",
            cookies=admin_cookies,
        )
        assert r.status_code == 400, r.text
        assert "invalid_sort_key" in r.text


class TestQuota:
    def test_quota_blocks_after_threshold(self, web_client, monkeypatch):
        # Tiny quota for the test.
        monkeypatch.setenv("AGNES_QUOTA_DUMMY", "1")  # noop, just to use monkeypatch
        from app import instance_config as ic
        monkeypatch.setattr(ic, "get_guardrails_blocked_quota_per_day", lambda: 2)

        _, user_cookies = _create_user(web_client, "spammer@x.com")

        # First two bad uploads land as blocked_inline 422, third hits quota 429.
        for i in range(2):
            r = web_client.post(
                "/api/store/entities",
                files={"file": ("s.zip", _make_eval_skill_zip(f"bad{i}"), "application/zip")},
                data={"type": "skill"}, cookies=user_cookies,
            )
            assert r.status_code == 422, f"upload {i}: {r.status_code} {r.text}"

        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("bad-3"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        assert r.status_code == 429
        body = r.json()["detail"]
        assert body["code"] == "quota_exceeded"
        assert body["limit"] == 2

    def test_quota_disabled_with_zero(self, web_client, monkeypatch):
        from app import instance_config as ic
        monkeypatch.setattr(ic, "get_guardrails_blocked_quota_per_day", lambda: 0)

        _, user_cookies = _create_user(web_client, "trusted@x.com")
        for i in range(3):
            r = web_client.post(
                "/api/store/entities",
                files={"file": ("s.zip", _make_eval_skill_zip(f"q{i}"), "application/zip")},
                data={"type": "skill"}, cookies=user_cookies,
            )
            assert r.status_code == 422, f"upload {i}"

    def test_quota_counter_includes_blocked_llm_and_review_error(self, web_client):
        """#9 — pre-fix the counter only counted blocked_inline. A
        submitter triggering ten blocked_llm verdicts was unbounded.
        Post-fix: counter includes blocked_inline + blocked_llm +
        review_error so all three reject states share the cap."""
        from datetime import datetime, timezone, timedelta
        from src.repositories.store_submissions import StoreSubmissionsRepository

        # Seed three blocked submissions of different types directly via
        # the repo so we don't depend on triggering each verdict path
        # through the API (LLM mocking is involved).
        _, user_cookies = _create_user(web_client, "spammer-9@x.com")
        conn = get_system_db()
        repo = StoreSubmissionsRepository(conn)
        for i, status in enumerate(("blocked_inline", "blocked_llm", "review_error")):
            repo.create(
                submitter_id="spammer-9", submitter_email="spammer-9@x.com",
                type="skill", name=f"q9-{i}", version="1.0.0",
                status=status, entity_id=None,
                inline_checks={"manifest": {"status": "fail"}},
            )
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        count = repo.count_blocked_for_submitter_since("spammer-9", since)
        conn.close()
        assert count == 3, (
            f"counter must include all three reject states; got {count}"
        )


# ---------------------------------------------------------------------------
# v32+ quarantine semantics
# ---------------------------------------------------------------------------


class TestQuarantineGates:
    def test_owner_cannot_delete_quarantined(self, web_client):
        """Owner trying to DELETE their own blocked_inline entity must
        be refused — admin investigates first."""
        _, user_cookies = _create_user(web_client, "u@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("q1"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        eid = c.json()["detail"]["submission_id"]
        # The submission row carries entity_id; fetch it.
        from src.repositories.store_submissions import StoreSubmissionsRepository
        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(eid)
        entity_id = sub["entity_id"]
        conn.close()

        r = web_client.delete(
            f"/api/store/entities/{entity_id}", cookies=user_cookies,
        )
        assert r.status_code == 403
        body = r.json()["detail"]
        assert body["code"] == "quarantined_owner_cannot_delete"

    def test_admin_can_delete_quarantined(self, web_client):
        _, user_cookies = _create_user(web_client, "u@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("q2"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        sid = c.json()["detail"]["submission_id"]
        from src.repositories.store_submissions import StoreSubmissionsRepository
        conn = get_system_db()
        entity_id = StoreSubmissionsRepository(conn).get(sid)["entity_id"]
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.delete(
            f"/api/store/entities/{entity_id}", cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

    def test_non_owner_non_admin_cannot_view_quarantined(self, web_client):
        """Random user navigating to ANY per-entity asset endpoint gets
        404 — same as if the entity didn't exist (no leak via 403).
        Covers every ``_enforce_visibility`` caller in app/api/store.py
        + the marketplace flea detail."""
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("q3"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        )
        sid = c.json()["detail"]["submission_id"]
        from src.repositories.store_submissions import StoreSubmissionsRepository
        conn = get_system_db()
        entity_id = StoreSubmissionsRepository(conn).get(sid)["entity_id"]
        conn.close()

        _, intruder_cookies = _create_user(web_client, "snoop@x.com")
        # Detail
        r = web_client.get(
            f"/api/store/entities/{entity_id}", cookies=intruder_cookies,
        )
        assert r.status_code == 404, "detail must 404 for non-owner"
        # Files listing
        r = web_client.get(
            f"/api/store/entities/{entity_id}/files", cookies=intruder_cookies,
        )
        assert r.status_code == 404, "files must 404 for non-owner"
        # Photo (404 even when no photo uploaded — we want no leak via
        # status code differences anyway)
        r = web_client.get(
            f"/api/store/entities/{entity_id}/photo", cookies=intruder_cookies,
        )
        assert r.status_code == 404, "photo must 404 for non-owner"
        # Docs sub-path
        r = web_client.get(
            f"/api/store/entities/{entity_id}/docs/anything.md",
            cookies=intruder_cookies,
        )
        assert r.status_code == 404, "docs must 404 for non-owner"

    def test_quarantined_entity_excluded_from_store_entities_list(self, web_client):
        """Random non-owner non-admin hitting the public flea-listing
        (`/api/store/entities`) must NOT see another user's quarantined
        entry. Mirrors the marketplace-items coverage but on the
        store-namespaced listing."""
        _, owner_cookies = _create_user(web_client, "qowner@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("q-list"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        )
        sid = c.json()["detail"]["submission_id"]
        from src.repositories.store_submissions import StoreSubmissionsRepository
        conn = get_system_db()
        entity_id = StoreSubmissionsRepository(conn).get(sid)["entity_id"]
        conn.close()

        _, intruder_cookies = _create_user(web_client, "qsnoop@x.com")
        r = web_client.get("/api/store/entities", cookies=intruder_cookies)
        assert r.status_code == 200
        ids = {it["id"] for it in r.json().get("items", [])}
        assert entity_id not in ids, (
            "non-owner non-admin saw another user's quarantined entity "
            "in /api/store/entities listing"
        )

        # Owner sees own entry on the same listing (auto-include via
        # include_owner_id widening).
        r = web_client.get("/api/store/entities", cookies=owner_cookies)
        owner_ids = {it["id"] for it in r.json().get("items", [])}
        assert entity_id in owner_ids, (
            "owner should see own quarantined entity in their listing"
        )

    def test_owner_can_view_their_quarantined_entity(self, web_client):
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("q4"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        )
        sid = c.json()["detail"]["submission_id"]
        from src.repositories.store_submissions import StoreSubmissionsRepository
        conn = get_system_db()
        entity_id = StoreSubmissionsRepository(conn).get(sid)["entity_id"]
        conn.close()

        r = web_client.get(
            f"/api/store/entities/{entity_id}", cookies=owner_cookies,
        )
        assert r.status_code == 200

    def test_install_quarantined_refused_for_non_admin(self, web_client):
        """Even owner cannot add their own quarantined item to my-stack."""
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("q5"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        )
        sid = c.json()["detail"]["submission_id"]
        from src.repositories.store_submissions import StoreSubmissionsRepository
        conn = get_system_db()
        entity_id = StoreSubmissionsRepository(conn).get(sid)["entity_id"]
        conn.close()

        r = web_client.post(
            f"/api/store/entities/{entity_id}/install", cookies=owner_cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "entity_not_approved"


# ---------------------------------------------------------------------------
# v32+ marketplace consolidation: visibility gates + owner-visible cards
# ---------------------------------------------------------------------------


class TestMarketplaceFleaConsolidation:
    def test_marketplace_flea_detail_404_for_non_owner_quarantined(self, web_client):
        """Random non-owner non-admin pasting an entity_id into
        /marketplace/flea/{id} gets 404 — same policy as the now-deleted
        /store/{id}."""
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("c1"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        )
        sid = c.json()["detail"]["submission_id"]
        from src.repositories.store_submissions import StoreSubmissionsRepository
        conn = get_system_db()
        eid = StoreSubmissionsRepository(conn).get(sid)["entity_id"]
        conn.close()

        _, intruder_cookies = _create_user(web_client, "snoop@x.com")
        r = web_client.get(f"/marketplace/flea/{eid}", cookies=intruder_cookies)
        assert r.status_code == 404
        # API equivalent
        r = web_client.get(f"/api/marketplace/flea/{eid}/detail", cookies=intruder_cookies)
        assert r.status_code == 404

    def test_marketplace_flea_detail_owner_sees_quarantine_banner(self, web_client):
        """Owner landing on /marketplace/flea/{id} sees the quarantine
        banner with the failure summary AND the actual finding details
        — not just a generic "Quarantined" header."""
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("c2"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        )
        sid = c.json()["detail"]["submission_id"]
        from src.repositories.store_submissions import StoreSubmissionsRepository
        conn = get_system_db()
        eid = StoreSubmissionsRepository(conn).get(sid)["entity_id"]
        conn.close()

        r = web_client.get(f"/marketplace/flea/{eid}", cookies=owner_cookies)
        assert r.status_code == 200
        body = r.text
        # Banner partial rendered.
        assert "vis-banner" in body
        assert "Quarantined" in body
        # Concrete reason — the eval-shell rule was the offender; banner
        # must surface the finding details so the submitter knows WHY.
        assert "security:" in body, (
            "banner missing static_security findings list — user sees "
            "'Quarantined' label but no actionable reason"
        )
        assert "run.sh" in body, "banner missing path of offending file"

    def test_review_error_banner_shows_error_detail(self, web_client):
        """#review_error — banner must surface the underlying error
        message + any inline_checks the runner captured before bailing.
        Pre-fix the banner only said 'couldn't complete its check' with
        no actionable detail."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        owner_id, owner_cookies = _create_user(web_client, "rev-err@x.com")
        # Stage entity + submission directly so we can land in review_error.
        conn = get_system_db()
        StoreEntitiesRepository(conn).create(
            id="ent-rev-err", owner_user_id=owner_id, owner_username="rev-err",
            type="skill", name="rev-err", description="Test review_error banner",
            category=None, version="1.0.0", file_size=10,
            visibility_status="hidden",
        )
        StoreSubmissionsRepository(conn).create(
            submitter_id=owner_id, submitter_email="rev-err@x.com",
            type="skill", name="rev-err", version="1.0.0",
            status="review_error", entity_id="ent-rev-err",
            inline_checks={
                "manifest": {"status": "pass", "issues": []},
                "static_security": {"status": "pass", "findings": []},
                "quality": {"status": "pass", "issues": [],
                            "template_placeholders": 0},
            },
            llm_findings={
                "risk_level": None, "summary": None, "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": None,
                "error": "LLMTimeoutError: Anthropic connection error",
            },
        )
        conn.close()

        r = web_client.get(
            "/marketplace/flea/ent-rev-err", cookies=owner_cookies,
        )
        assert r.status_code == 200
        assert "vis-banner" in r.text
        assert "errored" in r.text or "Under review" in r.text
        # The actionable error detail must surface.
        assert "LLMTimeoutError" in r.text, (
            "review_error banner must surface llm_findings.error"
        )

    def test_legacy_store_detail_url_returns_404(self, web_client):
        """The /store/{id} route was deleted in v32+. Stale bookmarks 404."""
        _, owner_cookies = _create_user(web_client, "u@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("legacy"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        )
        eid = c.json()["id"]
        r = web_client.get(f"/store/{eid}", cookies=owner_cookies)
        assert r.status_code == 404

    def test_marketplace_listing_includes_owner_quarantined(self, web_client):
        """Submitter sees their own non-approved entries in the
        /api/marketplace/items?tab=flea grid; non-owner does not."""
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("c4"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        )
        sid = c.json()["detail"]["submission_id"]
        from src.repositories.store_submissions import StoreSubmissionsRepository
        conn = get_system_db()
        eid = StoreSubmissionsRepository(conn).get(sid)["entity_id"]
        conn.close()

        # Owner — their own quarantined card surfaces with is_viewer_owner=True.
        r = web_client.get("/api/marketplace/items?tab=flea", cookies=owner_cookies)
        assert r.status_code == 200
        items = r.json()["items"]
        own = [it for it in items if it["id"] == f"flea-{eid}"]
        assert own, f"owner should see own quarantined item; got {[it['id'] for it in items]}"
        assert own[0]["visibility_status"] != "approved"
        assert own[0]["is_viewer_owner"] is True

        # Non-owner — same listing must NOT surface the quarantined entry.
        _, snoop_cookies = _create_user(web_client, "snoop@x.com")
        r = web_client.get("/api/marketplace/items?tab=flea", cookies=snoop_cookies)
        assert r.status_code == 200
        items = r.json()["items"]
        snoop = [it for it in items if it["id"] == f"flea-{eid}"]
        assert not snoop, "non-owner must not see another user's quarantined item"


# ---------------------------------------------------------------------------
# v35 archive (soft delete) semantics
# ---------------------------------------------------------------------------


class TestArchiveSoftDelete:
    def _upload_clean(self, web_client, cookies, name="clean1"):
        """Helper: upload a clean skill that lands as approved (no API key
        in test env -> auto-approve via guardrails-disabled fallback)."""
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
            data={"type": "skill"}, cookies=cookies,
        )
        assert c.status_code == 201, c.text
        return c.json()["id"]

    def test_owner_can_archive_approved_entity(self, web_client):
        """DELETE without ?hard=true on owner's approved entity = soft archive.
        Bundle stays on disk; existing installs preserved."""
        from src.repositories.store_entities import StoreEntitiesRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="arch-1")

        # Pre-archive sanity.
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent["visibility_status"] == "approved"
        conn.close()

        r = web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)
        assert r.status_code == 200, r.text

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent is not None  # row preserved
        assert ent["visibility_status"] == "archived"
        assert ent["archived_at"] is not None
        assert ent["archived_by"] == "owner"
        # Bundle dir still on disk.
        from app.utils import get_store_dir
        assert (get_store_dir() / eid).exists()
        conn.close()

    def test_owner_hard_delete_refused(self, web_client):
        """Owner cannot pass ?hard=true — admin-only path."""
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="arch-2")
        r = web_client.delete(f"/api/store/entities/{eid}?hard=true", cookies=owner_cookies)
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "hard_delete_admin_only"

    def test_admin_can_hard_delete(self, web_client):
        from src.repositories.store_entities import StoreEntitiesRepository
        from app.utils import get_store_dir
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="arch-3")
        bundle_dir = get_store_dir() / eid
        assert bundle_dir.exists()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.delete(f"/api/store/entities/{eid}?hard=true", cookies=admin_cookies)
        assert r.status_code == 200, r.text

        conn = get_system_db()
        assert StoreEntitiesRepository(conn).get(eid) is None
        conn.close()
        assert not bundle_dir.exists()

    def test_archived_excluded_from_marketplace_listing(self, web_client):
        """Approved → archived: every browse listing hides it (even owner)."""
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="arch-4")
        web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)

        # Owner — own archived must NOT surface in the public-style listing.
        r = web_client.get("/api/marketplace/items?tab=flea", cookies=owner_cookies)
        assert r.status_code == 200
        ids = {it["id"] for it in r.json()["items"]}
        assert f"flea-{eid}" not in ids

    def test_install_refused_on_archived(self, web_client):
        """Archived entities can't be added to my stack."""
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="arch-5")
        web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)

        _, other_cookies = _create_user(web_client, "other@x.com")
        r = web_client.post(
            f"/api/store/entities/{eid}/install", cookies=other_cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "entity_not_approved"

    def test_archived_still_serves_existing_installs(self, web_client):
        """Pre-existing user_store_installs keep getting the entity in
        list_for_user even after archive (drives marketplace.zip serve)."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.user_store_installs import UserStoreInstallsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="arch-6")

        # Other user installs while approved.
        other_id, other_cookies = _create_user(web_client, "other@x.com")
        web_client.post(f"/api/store/entities/{eid}/install", cookies=other_cookies)

        # Owner archives.
        web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)

        # Other user's stack still has it.
        conn = get_system_db()
        installs = UserStoreInstallsRepository(conn).list_for_user(other_id)
        ids = {r["id"] for r in installs}
        assert eid in ids, "archived entity must still serve to existing installs"
        # And carries archived flag for the badge.
        row = next(r for r in installs if r["id"] == eid)
        assert row["visibility_status"] == "archived"
        conn.close()

    def test_owner_cannot_archive_quarantined(self, web_client):
        """Owner Delete on quarantined still refused (existing v32 policy)."""
        _, user_cookies = _create_user(web_client, "u@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("q-arch"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        sid = c.json()["detail"]["submission_id"]
        from src.repositories.store_submissions import StoreSubmissionsRepository
        conn = get_system_db()
        eid = StoreSubmissionsRepository(conn).get(sid)["entity_id"]
        conn.close()

        r = web_client.delete(f"/api/store/entities/{eid}", cookies=user_cookies)
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "quarantined_owner_cannot_delete"

    def test_admin_can_archive_quarantined(self, web_client):
        """Admin can archive a quarantined entity (separate from override
        + hard-delete paths — admin keeps full control)."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, user_cookies = _create_user(web_client, "u@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("q-arch2"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        sid = c.json()["detail"]["submission_id"]
        conn = get_system_db()
        eid = StoreSubmissionsRepository(conn).get(sid)["entity_id"]
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.delete(f"/api/store/entities/{eid}", cookies=admin_cookies)
        assert r.status_code == 200, r.text

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent["visibility_status"] == "archived"
        conn.close()

    def test_owners_endpoint_filters_quarantined_for_non_admin(self, web_client):
        """A user with only quarantined uploads must NOT appear in the
        public /api/store/owners dropdown."""
        _, user_cookies = _create_user(web_client, "spammer@x.com")
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("only-bad"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )

        # Different non-admin viewing owners.
        _, other_cookies = _create_user(web_client, "other@x.com")
        r = web_client.get("/api/store/owners", cookies=other_cookies)
        assert r.status_code == 200
        owner_ids = {o["user_id"] for o in r.json()}
        assert "spammer" not in owner_ids

    def test_categories_endpoint_filters_quarantined_for_non_owner(self, web_client):
        """`/api/marketplace/categories?tab=flea` aggregates per-category
        counts. The visibility predicate is duplicated inline in
        marketplace.py (drift risk against repo); this test locks the
        parity with marketplace items so a future change to the repo
        clause that misses the inline copy gets caught."""
        # Owner uploads ONE bad skill (lands at visibility=hidden).
        _, owner_cookies = _create_user(web_client, "qcat-owner@x.com")
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("qcat"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        )

        # Different non-admin user. Categories listing must NOT count
        # the quarantined entry in any bucket.
        _, snoop_cookies = _create_user(web_client, "qcat-snoop@x.com")
        r = web_client.get(
            "/api/marketplace/categories?tab=flea", cookies=snoop_cookies,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Response shape is `{"items": [{name, count, icon_key}, …]}`.
        # Non-owner non-admin must see 0 total since no approved entries
        # exist for this fresh user.
        total = sum(c.get("count", 0) for c in body.get("items", []))
        assert total == 0, (
            "non-owner saw quarantined entry counted in /categories: "
            f"{body}"
        )

        # Owner sees own entry counted (predicate widens to include
        # owner's non-archived non-approved entries).
        r = web_client.get(
            "/api/marketplace/categories?tab=flea", cookies=owner_cookies,
        )
        body = r.json()
        owner_total = sum(c.get("count", 0) for c in body.get("items", []))
        assert owner_total >= 1, (
            "owner should count own quarantined entry in /categories: "
            f"{body}"
        )


# ---------------------------------------------------------------------------
# v35 lifecycle marking on submissions (archived / deleted)
# ---------------------------------------------------------------------------


class TestSubmissionLifecycleMarking:
    def _upload_clean(self, web_client, cookies, name="lc"):
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
            data={"type": "skill"}, cookies=cookies,
        )
        assert c.status_code == 201, c.text
        return c.json()["id"]

    def test_archive_surfaces_via_entity_visibility_join(self, web_client):
        """Verdict on submission stays immutable; archived chip surfaces the row
        because store_entities.visibility_status flipped to 'archived'.
        Locks in the JOIN-based architecture replacing the prior denormalization."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="lc-arch")
        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).latest_for_entity(eid)
        sid = sub["id"]
        assert sub["status"] == "approved"
        conn.close()

        web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)

        # Verdict UNCHANGED — that's the whole point: submission.status is the
        # forensic record of what was decided at review time, not lifecycle.
        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(sid)
        assert sub["status"] == "approved"
        conn.close()

        # But the JOIN-based archived chip surfaces it because the entity's
        # visibility_status flipped.
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/api/admin/store/submissions?status=archived", cookies=admin_cookies,
        )
        assert r.status_code == 200
        names = {s["name"] for s in r.json()["items"]}
        assert "lc-arch" in names

    def test_archived_chip_surfaces_entity_archived_outside_delete_flow(self, web_client):
        """Regression for the user-reported bug: archived entity didn't show up
        in ?status=archived because the prior denormalized field never flipped.
        Pre-seed a submission with status='approved' and manually flip the
        linked entity to visibility_status='archived' (simulating any code path
        that bypasses the soft-delete API), then assert the archived chip
        surfaces it."""
        from src.repositories.store_entities import StoreEntitiesRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="lc-bypass-archive")

        # Bypass the API: flip visibility directly at the repo layer.
        conn = get_system_db()
        StoreEntitiesRepository(conn).set_visibility(eid, "archived")
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/api/admin/store/submissions?status=archived", cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        names = {s["name"] for s in r.json()["items"]}
        assert "lc-bypass-archive" in names

        # And the default queue excludes it.
        r = web_client.get("/api/admin/store/submissions", cookies=admin_cookies)
        names = {s["name"] for s in r.json()["items"]}
        assert "lc-bypass-archive" not in names

    def test_hard_delete_marks_submissions_deleted(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="lc-del")
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.delete(f"/api/store/entities/{eid}?hard=true", cookies=admin_cookies)
        assert r.status_code == 200, r.text

        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "deleted"
        # entity_id is preserved as a tombstone — the live entity row
        # is gone, but keeping the pointer lets the detail page
        # resolve the activity timeline by querying audit_log for
        # `store_entity:{entity_id}` even after the row is dropped.
        assert sub["entity_id"] == eid
        conn.close()

    def test_default_listing_excludes_archived_and_deleted(self, web_client):
        """Admin queue defaults to actionable rows. Archived + deleted
        only surface when the user clicks the dedicated chip."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid_arch = self._upload_clean(web_client, owner_cookies, name="lc-d-arch")
        eid_keep = self._upload_clean(web_client, owner_cookies, name="lc-d-keep")
        web_client.delete(f"/api/store/entities/{eid_arch}", cookies=owner_cookies)

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get("/api/admin/store/submissions", cookies=admin_cookies)
        assert r.status_code == 200
        names = {s["name"] for s in r.json()["items"]}
        assert "lc-d-keep" in names
        assert "lc-d-arch" not in names

        # Explicit chip surfaces it.
        r = web_client.get(
            "/api/admin/store/submissions?status=archived", cookies=admin_cookies,
        )
        names = {s["name"] for s in r.json()["items"]}
        assert names == {"lc-d-arch"}

    def test_deleted_chip_surfaces_hard_deleted(self, web_client):
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="lc-d-hard")
        _, admin_cookies = _create_admin(web_client)
        web_client.delete(f"/api/store/entities/{eid}?hard=true", cookies=admin_cookies)

        r = web_client.get(
            "/api/admin/store/submissions?status=deleted", cookies=admin_cookies,
        )
        names = {s["name"] for s in r.json()["items"]}
        assert "lc-d-hard" in names

    def test_deleted_submission_detail_renders_timeline(self, web_client):
        """Regression: hard-deleted submissions used to lose their activity
        timeline because mark_deleted_for_entity nulled entity_id, severing
        the audit_log linkage. Tombstone semantics: entity_id is preserved
        post-delete so `store_entity:{entity_id}` audits keep resolving."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="lc-d-timeline")

        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.delete(f"/api/store/entities/{eid}?hard=true", cookies=admin_cookies)
        assert r.status_code == 200, r.text

        # Detail page must render and include at least one audit row
        # (creation events scoped to store_entity:{eid} would otherwise
        # be invisible after the entity_id link was nulled).
        r = web_client.get(
            f"/admin/store/submissions/{sub_id}", cookies=admin_cookies,
        )
        assert r.status_code == 200
        # Sanity: the deleted submission's body references the original
        # entity_id (the tombstone), proving the linkage survives.
        assert eid in r.text


# ---------------------------------------------------------------------------
# Coverage gap fills (post-pre-PR audit)
# ---------------------------------------------------------------------------


class TestFleaDetailSubmissionStatusField:
    """The quarantine banner's auto-refresh JS polls
    `/api/marketplace/flea/{id}/detail` and reloads when
    `submission_status` flips off the pending verdicts. Visibility
    alone is insufficient because `blocked_llm` keeps the entity at
    `visibility_status='pending'`. This class locks in the contract
    that the field is populated for owner/admin only."""

    def _upload_clean(self, web_client, cookies, name="dst"):
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
            data={"type": "skill"}, cookies=cookies,
        )
        assert c.status_code == 201, c.text
        return c.json()["id"]

    def test_owner_sees_submission_status(self, web_client):
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="dst-own")
        r = web_client.get(
            f"/api/marketplace/flea/{eid}/detail", cookies=owner_cookies,
        )
        assert r.status_code == 200
        body = r.json()
        # Verdict landed by the time create returned 201 (clean upload skips
        # LLM since fixtures don't carry a real api key) — value is whatever
        # status the runner left; just assert the field is populated.
        assert body.get("submission_status") is not None

    def test_admin_sees_submission_status_for_any_entity(self, web_client):
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="dst-adm")
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/api/marketplace/flea/{eid}/detail", cookies=admin_cookies,
        )
        assert r.status_code == 200
        assert r.json().get("submission_status") is not None

    def test_other_user_does_not_see_submission_status(self, web_client):
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="dst-other")
        # Non-owner non-admin can hit the endpoint when entity is approved
        # (404 otherwise per the visibility gate). Field must stay null.
        _, viewer_cookies = _create_user(web_client, "viewer@x.com")
        r = web_client.get(
            f"/api/marketplace/flea/{eid}/detail", cookies=viewer_cookies,
        )
        if r.status_code == 200:
            assert r.json().get("submission_status") is None
        # If the entity isn't approved (404 for non-owner non-admin),
        # the leak isn't reachable either way — also acceptable.


class TestDetailPageEntityLifecycleRow:
    """The submission detail page renders Status (verdict) and Entity
    lifecycle side by side so admins see the verdict-vs-lifecycle
    distinction at a glance. Locks in the row's presence so a future
    template refactor can't silently drop it."""

    def _upload_clean(self, web_client, cookies, name="elr"):
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
            data={"type": "skill"}, cookies=cookies,
        )
        assert c.status_code == 201, c.text
        return c.json()["id"]

    def test_detail_renders_entity_lifecycle_row(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="elr-1")
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/admin/store/submissions/{sub_id}", cookies=admin_cookies,
        )
        assert r.status_code == 200
        # Both labels must coexist on the page.
        assert "Status (verdict)" in r.text
        assert "Entity lifecycle" in r.text


class TestAuditLogResourcePrefix:
    """Locks in the contract that submission-event audits land at
    resources the activity-timeline query knows how to find. Two
    paths emit them:

      * `app/api/store.py:_audit` helper hardcodes `store_entity:`
        prefix — submission events written this way live at
        `store_entity:{sub_id}`. Timeline query covers it.
      * `src/store_guardrails/runner.py` uses
        `store_submission:{sub_id}` — the post-fix convention.
        Timeline query covers it.

    Either format must surface in the rendered detail page so admins
    can audit the lifecycle of any submission."""

    def _upload_clean(self, web_client, cookies, name="aud"):
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
            data={"type": "skill"}, cookies=cookies,
        )
        assert c.status_code == 201, c.text
        return c.json()["id"]

    def test_helper_emitted_audits_surface_in_timeline(self, web_client):
        """The `_audit` helper writes resource=`store_entity:{sub_id}`
        for submission events. Timeline query must include that
        pattern so `store.submission.accepted` (or `.approved`) rows
        are visible on the detail page."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="aud-helper")
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        rows = conn.execute(
            """SELECT resource, action FROM audit_log
                WHERE resource = ?
                  AND action LIKE 'store.submission.%'""",
            [f"store_entity:{sub_id}"],
        ).fetchall()
        conn.close()
        assert rows, (
            "expected at least one store.submission.* audit row at "
            "resource=store_entity:{sub_id} — the helper format the "
            "timeline query relies on"
        )

        # And the rendered timeline must include the action.
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/admin/store/submissions/{sub_id}", cookies=admin_cookies,
        )
        assert r.status_code == 200
        # Either accepted (guardrails on) or approved (guardrails off);
        # both appear in rows returned by the timeline query when the
        # query covers the helper's `store_entity:` resource pattern.
        assert any(a in r.text for a in (
            "store.submission.accepted",
            "store.submission.approved",
        ))

    def test_runner_audit_uses_prefixed_resource(self, monkeypatch, web_client):
        """runner.py's audit calls must use `store_submission:{id}` —
        we drive that path by faking a config-loader failure inside
        run_llm_review so the runner emits a `review_error` audit
        synchronously."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails import runner as runner_mod

        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="aud-runner")
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        plugin_dir = Path(get_store_dir()) / eid / "plugin"
        conn.close()

        def boom() -> str:
            raise RuntimeError("missing api key for test")

        runner_mod.run_llm_review(
            submission_id=sub_id,
            plugin_dir=plugin_dir,
            conn_factory=get_system_db,
            api_key_loader=boom,
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        conn = get_system_db()
        rows = conn.execute(
            """SELECT resource, action FROM audit_log
                WHERE resource = ?
                  AND action = 'store.submission.review_error'""",
            [f"store_submission:{sub_id}"],
        ).fetchall()
        conn.close()
        assert rows, (
            "runner.py must emit prefixed store_submission:{id} so the "
            "timeline query resolves it — bare-id format is legacy only"
        )
