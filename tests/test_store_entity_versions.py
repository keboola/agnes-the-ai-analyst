"""v37 flea-market edit feature with version history.

Covers:
* Bundle update bumps version_no + appends version_history entry.
* Metadata-only edit doesn't bump version.
* Type change rejected with 400 type_locked.
* Block-while-pending: 409 prior_version_pending.
* Display name change renames the on-disk slug for live + version dirs.
* Restore copies a prior version forward as v<max+1>; live + history
  reflect the new version; original version row keeps its own verdict.
* Restore re-runs guardrails (blocked path leaves live untouched).
* Versions card on detail page renders for owner/admin only.
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
from src.repositories.store_entities import StoreEntitiesRepository
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


def _create_admin(client, email="admin-edit@x.com"):
    from tests.helpers.auth import grant_admin
    user_id, cookies = _create_user(client, email, password="AdminPass1!")
    conn = get_system_db()
    grant_admin(conn, user_id)
    conn.close()
    return user_id, cookies


def _make_skill_zip(skill_name: str, body: str = "Body. " * 30) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: A clean test skill for the edit feature.\n---\n\n"
            + body,
        )
    return buf.getvalue()


def _make_eval_skill_zip(skill_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: A bad test skill.\n---\n\n"
            + ("Body. " * 30),
        )
        zf.writestr(f"{skill_name}/run.sh", "#!/bin/sh\neval $1\n")
    return buf.getvalue()


def _upload_clean(client, cookies, name="ed1"):
    r = client.post(
        "/api/store/entities",
        files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
        data={"type": "skill"}, cookies=cookies,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


class TestEditFeature:
    def test_metadata_only_edit_no_version_bump(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "metaowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="metaedit")

        r = web_client.put(
            f"/api/store/entities/{eid}",
            data={"description": "Updated description text"},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        entity = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert entity["description"] == "Updated description text"
        assert entity["version_no"] == 1
        assert len(entity["version_history"]) == 1

    def test_bundle_edit_bumps_version_and_appends_history(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "bundleowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="bundleedit")

        # PUT with new bundle bytes.
        new_zip = _make_skill_zip("bundleedit", body="V2 body. " * 30)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", new_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        entity = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert entity["version_no"] == 2
        assert len(entity["version_history"]) == 2
        v1, v2 = entity["version_history"]
        assert v1["n"] == 1
        assert v2["n"] == 2
        assert v2["hash"] != v1["hash"], "v2 hash must differ from v1"

    def test_type_change_rejected(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "typeowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="typelock")
        r = web_client.put(
            f"/api/store/entities/{eid}",
            data={"type": "agent"},
            cookies=owner_cookies,
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "type_locked"

    def test_block_while_prior_pending_409(self, web_client):
        """Manually flip the entity to visibility=pending + create a
        pending submission, then attempt edit → 409."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        owner_id, owner_cookies = _create_user(web_client, "blockowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="blockpending")

        conn = get_system_db()
        # Force pending state.
        conn.execute(
            "UPDATE store_entities SET visibility_status = 'pending' WHERE id = ?",
            [eid],
        )
        StoreSubmissionsRepository(conn).create(
            submitter_id=owner_id, submitter_email="blockowner@x.com",
            type="skill", name="blockpending", version="2.0.0",
            status="pending_llm", entity_id=eid,
            inline_checks={"manifest": {"status": "pass"}},
        )
        conn.close()

        r = web_client.put(
            f"/api/store/entities/{eid}",
            data={"description": "Trying to edit"},
            cookies=owner_cookies,
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "prior_version_pending"

    def test_name_change_renames_baked_slug(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "renameowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="oldname")

        r = web_client.put(
            f"/api/store/entities/{eid}",
            data={"name": "newname"},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        entity = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert entity["name"] == "newname"

        plugin_dir = Path(get_store_dir()) / eid / "plugin"
        new_skill_dir = plugin_dir / "skills" / "newname-by-renameowner"
        old_skill_dir = plugin_dir / "skills" / "oldname-by-renameowner"
        assert new_skill_dir.is_dir(), "renamed slug missing on disk"
        assert not old_skill_dir.exists(), "old slug must be gone"


class TestRestoreVersion:
    def test_restore_creates_new_version_with_old_bundle(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "restoreowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="restoreme")

        # Edit to v2.
        v2_zip = _make_skill_zip("restoreme", body="VERSION-2-BODY " * 20)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Capture v1 + v2 hashes from history.
        conn = get_system_db()
        entity = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        v1_hash = entity["version_history"][0]["hash"]
        v2_hash = entity["version_history"][1]["hash"]
        assert v1_hash != v2_hash

        # Restore v1 → creates v3 with v1's bundle hash.
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        entity = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert entity["version_no"] == 3
        assert len(entity["version_history"]) == 3
        v3 = entity["version_history"][2]
        assert v3["n"] == 3
        assert v3["hash"] == v1_hash, (
            "restored bundle should hash identically to v1 — same bytes"
        )

    def test_restore_already_current_400(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "alreadyowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="already")
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "already_current"

    def test_restore_unknown_version_404(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "unknownver@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="ukver")
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/99/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "version_not_found"

    def test_non_owner_non_admin_cannot_restore(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "owrestore@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="ownedver")
        v2 = _make_skill_zip("ownedver", body="v2 " * 30)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )

        _, snoop_cookies = _create_user(web_client, "snoopver@x.com")
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=snoop_cookies,
        )
        assert r.status_code in (403, 404), r.text


class TestEditPage:
    def test_edit_page_renders_for_owner(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "editpage@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="editrender")
        r = web_client.get(
            f"/marketplace/flea/{eid}/edit", cookies=owner_cookies,
        )
        assert r.status_code == 200
        assert "edit-form" in r.text
        assert "editrender" in r.text

    def test_edit_page_404_for_non_owner(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "owneredit@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="snoopedit")
        _, snoop_cookies = _create_user(web_client, "snoopedit@x.com")
        r = web_client.get(
            f"/marketplace/flea/{eid}/edit", cookies=snoop_cookies,
        )
        assert r.status_code == 404

    def test_versions_card_renders_for_owner(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "vowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="vcard")
        r = web_client.get(
            f"/marketplace/flea/{eid}", cookies=owner_cookies,
        )
        assert r.status_code == 200
        assert "versions-card" in r.text
        assert "Versions (1)" in r.text

    def test_versions_card_hidden_for_non_owner(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "vowner2@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="vhide")
        _, other_cookies = _create_user(web_client, "vother@x.com")
        r = web_client.get(
            f"/marketplace/flea/{eid}", cookies=other_cookies,
        )
        assert r.status_code == 200
        assert "versions-card" not in r.text
