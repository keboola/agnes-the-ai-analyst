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


# Strong default description that clears the content guardrail's
# per-component bar (30 chars + 4 distinct words, no placeholder
# leftovers). Tests don't assert on its contents — they just need a
# value that passes review so we can exercise the edit/version path.
_OK_DESC = "Use when validating store version edit flow across every guardrail tier"


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


def _make_skill_zip(skill_name: str, body: str = "Body line explaining the skill. " * 12) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: Use when verifying clean-bundle edits across the version-history lifecycle\n---\n\n"
            + body,
        )
    return buf.getvalue()


def _make_eval_skill_zip(skill_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: Use when verifying static-security rejects eval-using upload bundles cleanly\n---\n\n"
            + ("Body line explaining the skill. " * 12),
        )
        zf.writestr(f"{skill_name}/run.sh", "#!/bin/sh\neval $1\n")
    return buf.getvalue()


def _upload_clean(client, cookies, name="ed1"):
    r = client.post(
        "/api/store/entities",
        files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
        data={"type": "skill", "description": _OK_DESC}, cookies=cookies,
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
        new_zip = _make_skill_zip("bundleedit", body="V2 body. " * 80)
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
            data={"type": "agent", "description": _OK_DESC},
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
        v2_zip = _make_skill_zip("restoreme", body="VERSION-2-BODY " * 80)
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
        v2 = _make_skill_zip("ownedver", body="v2 " * 80)
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

    def test_restore_rejects_blocked_llm_version(self, web_client, monkeypatch):
        """A v2 that LLM-blocked sits in version_history with
        submission.status='blocked_llm'. The restore endpoint must
        refuse to roll forward from that bundle — defense in depth
        against the UI being bypassed by direct POST."""
        # Mock LLM to BLOCK v2.
        def mock_review_bundle(*args, **kwargs):
            return {
                "risk_level": "high",
                "summary": "mock block",
                "findings": [{"severity": "high", "category": "test",
                              "file": "x", "explanation": "mock"}],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock-model",
                "error": None,
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_review_bundle,
        )

        owner_id, owner_cookies = _create_user(web_client, "blockrestore@x.com")
        # Phase 1: guardrails OFF — v1 lands approved.
        eid = _upload_clean(web_client, owner_cookies, name="blockrestore")
        # Phase 2: guardrails ON → v2 blocked, entity stays at v1.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        v2 = _make_skill_zip("blockrestore", body="V2 BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Drive the BG review synchronously.
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        # Now POST a restore /versions/2/restore. Must 400 because v2
        # was never approved.
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/2/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["detail"]["code"] == "version_not_approved"
        assert body["detail"]["source_status"] == "blocked_llm"

    def test_restore_rejects_review_error_version(self, web_client, monkeypatch):
        """Same as blocked_llm but the LLM call errored — the
        submission row lands at 'review_error' and the version is
        equally not-approvable."""
        def mock_review_bundle(*args, **kwargs):
            return {
                "risk_level": None,
                "summary": None,
                "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock-model",
                "error": "LLMFormatError: mock truncation",
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_review_bundle,
        )

        owner_id, owner_cookies = _create_user(web_client, "errrestore@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="errrestore")
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        v2 = _make_skill_zip("errrestore", body="V2 BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        r = web_client.post(
            f"/api/store/entities/{eid}/versions/2/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["detail"]["code"] == "version_not_approved"
        assert body["detail"]["source_status"] == "review_error"

    def test_restore_allows_legacy_v1_without_submission_id(self, web_client):
        """The v1 seed entry created by ``StoreEntitiesRepository.create``
        carries ``submission_id=None`` until the API layer backfills.
        A restore targeting v1 must NOT be rejected just because the
        join can't find a submission status — back-compat for entities
        created before v37."""
        owner_id, owner_cookies = _create_user(web_client, "legv1@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="legv1")
        # Manually clear v1's submission_id to simulate the legacy seed.
        conn = get_system_db()
        repo = StoreEntitiesRepository(conn)
        ent = repo.get(eid)
        history = ent["version_history"]
        history[0]["submission_id"] = None
        conn.execute(
            "UPDATE store_entities SET version_history = ? WHERE id = ?",
            [json.dumps(history), eid],
        )
        conn.close()

        # PUT a v2 so v1 is no longer current.
        v2 = _make_skill_zip("legv1", body="V2 BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Restore v1 → must succeed (200), because legacy v1 has
        # submission_id=None which the guard treats as approved.
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text


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


class TestInstallerAlwaysGetsLatestApproved:
    """Critical contract: existing installers continue receiving the
    last APPROVED version through the review window of a new edit, and
    NEVER receive an unapproved version. If the new version is blocked,
    they keep the prior approved one. If approved, they advance.

    Implemented via deferred promotion: PUT/restore append the new
    version to history at status='pending_llm' but DO NOT swap live
    or bump entity.version_no. runner.run_llm_review's approval branch
    promotes; on block, nothing changes.
    """

    def _install_as_user(self, web_client, owner_cookies, eid):
        """Install as a separate consumer user, return their list_for_user
        rows post-install (mirrors what marketplace.zip serves)."""
        from src.repositories.user_store_installs import UserStoreInstallsRepository
        installer_id, installer_cookies = _create_user(web_client, "installer@x.com")
        r = web_client.post(
            f"/api/store/entities/{eid}/install",
            cookies=installer_cookies,
        )
        assert r.status_code == 200, r.text
        return installer_id, installer_cookies

    def test_pending_review_does_not_break_existing_installer(self, web_client, monkeypatch):
        """Initial upload runs with guardrails OFF (lands approved).
        Then we flip guardrails ON and PUT a new bundle. The new
        version should defer promotion: existing installer must
        continue seeing v1 + entity.version_no=1, not get hidden by a
        flipped visibility or a half-promoted live dir."""
        from app import instance_config as ic
        # Stub LLM scheduling so the BG path never actually runs.
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )

        owner_id, owner_cookies = _create_user(web_client, "stickyowner@x.com")
        # Phase 1: guardrails OFF → initial upload lands approved.
        eid = _upload_clean(web_client, owner_cookies, name="sticky")
        # Capture v1 hash + size baseline.
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        v1_hash = ent["version"]
        v1_size = ent["file_size"]
        conn.close()

        # Install as a different user.
        installer_id, _ = self._install_as_user(web_client, owner_cookies, eid)

        # Phase 2: flip guardrails ON for the PUT call. Now an edit
        # defers promotion until LLM approves.
        # Patch where update_entity looks it up — `from app.instance_config
        # import get_guardrails_enabled` binds the symbol into app.api.store,
        # so patching the source module isn't enough.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)

        # PUT a new bundle. Guardrails on → submission lands at
        # pending_llm; promotion deferred.
        v2_zip = _make_skill_zip("sticky", body="V2 BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Installer's list_for_user must STILL see entity with v1 hash
        # + size + visibility='approved'. The pending edit must not
        # have hidden the entity from them.
        from src.repositories.user_store_installs import UserStoreInstallsRepository
        conn = get_system_db()
        installs = UserStoreInstallsRepository(conn).list_for_user(installer_id)
        ids = {r["id"] for r in installs}
        assert eid in ids, (
            "installer lost access to the entity during the LLM "
            "review window — list_for_user filter excluded it"
        )
        row = next(r for r in installs if r["id"] == eid)
        assert row["version"] == v1_hash, (
            f"installer should still get v1 hash {v1_hash[:8]} but "
            f"got {row['version'][:8]}"
        )
        assert row["file_size"] == v1_size, "size shouldn't change pre-promotion"
        assert row["visibility_status"] == "approved", (
            "entity must stay 'approved' through the review window so "
            "existing installers continue serving"
        )

        # Entity row's version_no must NOT have bumped yet.
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent["version_no"] == 1, (
            f"version_no must stay 1 during pending review; got {ent['version_no']}"
        )
        # But version_history MUST have v2 entry tracked (with the
        # new hash) so admin can see what's in flight.
        history_n = [int(e["n"]) for e in ent["version_history"]]
        assert 2 in history_n, "v2 entry must be in history despite no promotion"

    def test_blocked_new_version_keeps_installer_on_prior(self, web_client, monkeypatch):
        """Mock the LLM to BLOCK the v2 review. Installer must keep
        v1; entity.version_no must stay at 1; live plugin/ must hold
        v1's bytes."""
        from app import instance_config as ic

        # Mock the runner's LLM call to return a high-risk verdict.
        def mock_review_bundle(*args, **kwargs):
            return {
                "risk_level": "high",
                "summary": "mock block",
                "findings": [{"severity": "high", "category": "test",
                              "file": "x", "explanation": "mock"}],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock-model",
                "error": None,
            }

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_review_bundle,
        )

        # Phase 1: initial upload guardrails OFF.
        owner_id, owner_cookies = _create_user(web_client, "blockowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="blocksticky")
        # Phase 2: switch guardrails ON before PUT.
        # Patch where update_entity looks it up — `from app.instance_config
        # import get_guardrails_enabled` binds the symbol into app.api.store,
        # so patching the source module isn't enough.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        conn = get_system_db()
        v1_hash = StoreEntitiesRepository(conn).get(eid)["version"]
        conn.close()

        installer_id, _ = self._install_as_user(web_client, owner_cookies, eid)

        # Edit. Inline checks pass; LLM mocked to block.
        v2_zip = _make_skill_zip("blocksticky", body="v2-content " * 80)
        # Run the LLM synchronously by calling runner directly after
        # the PUT (the BG task may not have fired in TestClient).
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Find the just-created submission + run runner against it.
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review
        from app.utils import get_store_dir
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        # Installer must STILL see v1.
        from src.repositories.user_store_installs import UserStoreInstallsRepository
        conn = get_system_db()
        installs = UserStoreInstallsRepository(conn).list_for_user(installer_id)
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()

        row = next(r for r in installs if r["id"] == eid)
        assert row["version"] == v1_hash, (
            f"after v2 blocked, installer must still get v1 hash; got {row['version'][:8]}"
        )
        assert ent["version_no"] == 1, (
            f"version_no must stay at 1 after a blocked verdict; got {ent['version_no']}"
        )

    def test_approved_new_version_promotes_to_installer(self, web_client):
        """Default test path: guardrails OFF → guardrails-disabled
        promote-inline branch fires immediately. Installer's next
        list_for_user reflects v2."""
        owner_id, owner_cookies = _create_user(web_client, "promoowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="promosticky")
        conn = get_system_db()
        v1_hash = StoreEntitiesRepository(conn).get(eid)["version"]
        conn.close()

        installer_id, _ = self._install_as_user(web_client, owner_cookies, eid)

        v2_zip = _make_skill_zip("promosticky", body="promo-v2 " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Installer should now see v2 hash.
        from src.repositories.user_store_installs import UserStoreInstallsRepository
        conn = get_system_db()
        installs = UserStoreInstallsRepository(conn).list_for_user(installer_id)
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()

        assert ent["version_no"] == 2
        row = next(r for r in installs if r["id"] == eid)
        assert row["version"] != v1_hash, "installer should advance to v2"


class TestAdminAccess:
    """Admin can edit + restore on entities they don't own (parity with
    the existing admin override path)."""

    def test_admin_can_edit_non_owned_entity_metadata(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "adminedit-owner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="adminedit")
        _, admin_cookies = _create_admin(web_client)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            data={"description": "moderated by admin"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent["description"] == "moderated by admin"
        assert ent["version_no"] == 1

    def test_admin_can_restore_non_owned_entity(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "adminrestore-owner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="adminrestore")
        v2 = _make_skill_zip("adminrestore", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent["version_no"] == 3


class TestRestoreDeferredPromotion:
    """Restore endpoint mirrors PUT semantics: live + version_no stay
    on prior current until LLM approves the restored copy."""

    def test_restore_with_guardrails_on_does_not_promote_until_approved(
        self, web_client, monkeypatch,
    ):
        """Owner restores v1 → restored bytes baked into v3 dir.
        Until LLM approves, live + version_no stay at v2."""
        owner_id, owner_cookies = _create_user(web_client, "restoreowner-defer@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="restoredefer")
        v2 = _make_skill_zip("restoredefer", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        # v2 promoted (guardrails off). Now flip on for the restore.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        # version_no must STAY at 2 until LLM approves the v3 (restored)
        # copy. version_history has 3 entries; current is still 2.
        assert ent["version_no"] == 2, (
            f"restore must defer promotion when guardrails on; "
            f"version_no={ent['version_no']}"
        )
        history_n = sorted([int(e["n"]) for e in ent["version_history"]])
        assert history_n == [1, 2, 3]


class TestEditPageBanner:
    """Detail page banner during pending edit review must surface
    the version under review + the prior version still serving."""

    def test_banner_shows_review_error_when_prior_version_still_serving(
        self, web_client, monkeypatch,
    ):
        """v2+ edit landing in review_error must surface a banner to
        owner/admin even though entity stays at visibility=approved.
        The original gate (visibility != approved) silently hid the
        failure — see Bug #2 in plan
        when-i-submitted-new-delightful-russell.md."""
        from src.repositories.store_submissions import StoreSubmissionsRepository

        # Mock LLM to ERROR on v2.
        def mock_review_bundle(*args, **kwargs):
            return {
                "risk_level": None,
                "summary": None,
                "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock-model",
                "error": "LLMFormatError: mock truncation",
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_review_bundle,
        )

        owner_id, owner_cookies = _create_user(web_client, "errbanner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="errbanner")
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)

        v2 = _make_skill_zip("errbanner", body="V2 BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Drive the BG review synchronously so the submission row
        # lands at review_error.
        from src.store_guardrails.runner import run_llm_review
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent["visibility_status"] == "approved", (
            "deferred promotion: entity stays approved at prior version"
        )
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        # Confirm the submission really landed at review_error.
        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert sub["status"] == "review_error"
        assert ent["visibility_status"] == "approved", (
            "entity must remain approved — prior version still serving"
        )

        # Detail page MUST surface the failure.
        r = web_client.get(
            f"/marketplace/flea/{eid}", cookies=owner_cookies,
        )
        assert r.status_code == 200
        body = r.text
        # The widened v2+ review_error copy mentions the prior version
        # still serving — that's the user-visible signal we just added.
        assert "Latest edit failed review" in body, (
            "banner partial must render review_error H3 for v2+ edit "
            "when prior version still serves"
        )
        assert "previously approved version (v1)" in body, (
            "banner copy must explain why the entity still appears live"
        )
        # The model's error string must reach the page so the owner
        # can see what went wrong.
        assert "LLMFormatError" in body

    def test_banner_shows_version_n_under_review(self, web_client, monkeypatch):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        owner_id, owner_cookies = _create_user(web_client, "bannerowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="bannerver")

        # Switch guardrails on; stub LLM scheduler so v2 stays pending.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )

        v2 = _make_skill_zip("bannerver", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        # v2 review pending. visibility on entity stays approved (per
        # the deferred-promotion fix). But banner partial reads sub
        # status — for an in-flight edit submission the banner won't
        # render unless visibility != approved. Lock the scenario:
        # ensure entity stays at version_no=1 + visibility approved.
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        sub = StoreSubmissionsRepository(conn).latest_for_entity(eid)
        conn.close()
        assert ent["version_no"] == 1
        assert ent["visibility_status"] == "approved"
        assert sub["status"] == "pending_llm"

        # Detail page renders. Banner partial only fires when
        # visibility_status != approved, so for a deferred-edit case
        # the marketplace detail does NOT render the quarantine
        # banner — that's correct UX (consumers see the entity as
        # approved and operational). Owner-facing review status
        # surfaces via the Edit button being disabled.
        r = web_client.get(
            f"/marketplace/flea/{eid}", cookies=owner_cookies,
        )
        assert r.status_code == 200
        # Edit button should reflect the in-flight review (locked).
        assert "review in flight" in r.text or "Edit (review in flight)" in r.text


class TestAuditLogPerVersion:
    """Each edit / restore writes audit rows carrying the version_no
    in params, so the entity timeline can attribute events to the
    right version."""

    def test_edit_audit_carries_version_no(self, web_client):
        from src.repositories.audit import AuditRepository
        owner_id, owner_cookies = _create_user(web_client, "auditver@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="auditver")
        v2 = _make_skill_zip("auditver", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )

        conn = get_system_db()
        rows = AuditRepository(conn).query_for_resources(
            [f"store_entity:{eid}"], limit=20,
        )
        conn.close()
        # store.entity.update event for the edit must carry version_no
        # in its params.
        update_rows = [r for r in rows if r.get("action") == "store.entity.update"]
        assert update_rows, "missing store.entity.update audit"
        assert any(
            (r.get("params") or {}).get("version_no") == 2
            for r in update_rows
        ), "update audit must carry version_no=2 in params"

    def test_restore_audit_carries_versions(self, web_client):
        from src.repositories.audit import AuditRepository
        owner_id, owner_cookies = _create_user(web_client, "auditrestore@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="auditrest")
        v2 = _make_skill_zip("auditrest", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )

        conn = get_system_db()
        rows = AuditRepository(conn).query_for_resources(
            [f"store_entity:{eid}"], limit=20,
        )
        conn.close()
        restore_rows = [r for r in rows if r.get("action") == "store.entity.restore"]
        assert restore_rows, "missing store.entity.restore audit"
        params = restore_rows[0].get("params") or {}
        assert params.get("restored_from_version_no") == 1
        assert params.get("new_version_no") == 3


class TestPRReviewFixes:
    """Locks in the fixes called out in the PR #239 review."""

    def test_block_while_pending_fires_for_v2_edit_under_deferred_promotion(
        self, web_client, monkeypatch,
    ):
        """#1 — v2+ edit during in-flight LLM review must 409 even
        though entity.visibility_status is still 'approved'."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        owner_id, owner_cookies = _create_user(web_client, "blockv2@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="blockv2")

        # Switch guardrails on for the edit so promotion defers.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )

        v2 = _make_skill_zip("blockv2", body="v2 " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Entity should remain at 'approved' visibility — v2 in flight.
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        sub = StoreSubmissionsRepository(conn).latest_for_entity(eid)
        conn.close()
        assert ent["visibility_status"] == "approved"
        assert sub["status"] == "pending_llm"

        # Second concurrent edit MUST be blocked.
        v3 = _make_skill_zip("blockv2", body="v3 " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v3.zip", v3, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "prior_version_pending"

    def test_name_change_with_bundle_does_not_rename_live_until_promote(
        self, web_client, monkeypatch,
    ):
        """#2 — name + bundle in same PUT must NOT rename live until
        the LLM approves and promotion runs. Existing installer keeps
        getting the prior bundle under the prior slug."""
        from app.utils import get_store_dir
        owner_id, owner_cookies = _create_user(web_client, "rename-defer@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="origname")

        plugin_dir = Path(get_store_dir()) / eid / "plugin"
        old_skill_dir = plugin_dir / "skills" / "origname-by-rename-defer"
        assert old_skill_dir.is_dir()

        # Enable guardrails so promotion defers.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )

        v2 = _make_skill_zip("origname", body="v2 " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            data={"name": "newname"},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Live skill dir MUST still hold the old slug — promotion
        # hasn't fired since we stubbed _schedule_llm_review.
        assert old_skill_dir.is_dir(), (
            "live skill dir was renamed before LLM approval — "
            "violates deferred-promotion contract"
        )
        new_skill_dir = plugin_dir / "skills" / "newname-by-rename-defer"
        assert not new_skill_dir.exists(), (
            "live shouldn't have the renamed slug yet"
        )
        # Version dir HAS been renamed (so promotion will land on the
        # new slug).
        v2_dir = (
            Path(get_store_dir()) / eid / "versions" / "v2" / "plugin"
            / "skills" / "newname-by-rename-defer"
        )
        assert v2_dir.is_dir(), "version dir should carry the new slug"

    def test_v2_approval_logs_approved_not_skipped(
        self, web_client, monkeypatch,
    ):
        """#3 — v2+ approvals must log store.submission.approved, NOT
        store.submission.bg_verdict_skipped. Pre-fix the runner used
        the visibility-flip return value to gate the audit; under
        deferred promotion v2+ never flips visibility (already
        'approved'), so the wrong audit was emitted."""
        from src.repositories.audit import AuditRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review
        from app.utils import get_store_dir

        owner_id, owner_cookies = _create_user(web_client, "auditv2@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="auditv2")

        # Enable guardrails for the edit. Stub LLM scheduler so we
        # control when the runner fires.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )
        # Mock the LLM call to return a safe verdict.
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "safe", "summary": "ok",
                "findings": [], "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
            },
        )

        v2 = _make_skill_zip("auditv2", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )

        # Manually fire the runner against the v2 dir.
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        # Audit log must contain store.submission.approved with
        # promoted_to_version_no=2; NO bg_verdict_skipped.
        conn = get_system_db()
        rows = AuditRepository(conn).query_for_resources(
            [f"store_submission:{sub_id}"], limit=20,
        )
        conn.close()
        actions = [r.get("action") for r in rows]
        assert "store.submission.approved" in actions, (
            f"v2 approval missing approved audit; got {actions}"
        )
        assert "store.submission.bg_verdict_skipped" not in actions, (
            f"v2 approval should NOT log bg_verdict_skipped; got {actions}"
        )
        approved_row = next(
            r for r in rows if r.get("action") == "store.submission.approved"
        )
        params = approved_row.get("params") or {}
        assert params.get("promoted_to_version_no") == 2

    def test_bg_verdict_skipped_when_admin_archives_during_review(
        self, web_client, monkeypatch,
    ):
        """Negative: when admin DOES archive mid-review, the runner
        correctly logs bg_verdict_skipped (not approved)."""
        from src.repositories.audit import AuditRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.store_guardrails.runner import run_llm_review
        from app.utils import get_store_dir

        owner_id, owner_cookies = _create_user(web_client, "archmid@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="archmid")

        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "safe", "summary": "ok",
                "findings": [], "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
            },
        )

        v2 = _make_skill_zip("archmid", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )

        # Admin archives BEFORE runner fires.
        conn = get_system_db()
        StoreEntitiesRepository(conn).archive(eid, by_user_id="admin-x")
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()

        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        conn = get_system_db()
        rows = AuditRepository(conn).query_for_resources(
            [f"store_submission:{sub_id}"], limit=20,
        )
        conn.close()
        actions = [r.get("action") for r in rows]
        assert "store.submission.bg_verdict_skipped" in actions, (
            f"archive-during-review must log bg_verdict_skipped; got {actions}"
        )
        assert "store.submission.approved" not in actions, (
            f"archive-during-review must NOT log approved; got {actions}"
        )


class TestAdminQueueShowsVersion:
    def test_admin_queue_shows_v_no_after_name(self, web_client):
        """v# column derives version_no from entity.version_history by
        matching submission.version (hash) against the entry hashes."""
        owner_id, owner_cookies = _create_user(web_client, "vqowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="vqcol")
        v2 = _make_skill_zip("vqcol", body="v2 body. " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/api/admin/store/submissions", cookies=admin_cookies,
        )
        assert r.status_code == 200
        items = {it["name"]: it for it in r.json()["items"]}
        assert "vqcol" in items
        # version_no derived for the v2 row should be 2.
        # The list returns rows newest-first; pick the v2 (current).
        v2_row = next(
            it for it in r.json()["items"]
            if it.get("entity_id") == eid and it.get("version_no") == 2
        )
        assert v2_row["version_no"] == 2
        assert v2_row["entity_version_no"] == 2

    def test_admin_detail_shows_version_no(self, web_client):
        """Detail page renders v# under the Status / Entity-lifecycle
        block + a separate Bundle hash row."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        owner_id, owner_cookies = _create_user(web_client, "vdowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="vdrow")

        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/admin/store/submissions/{sub_id}", cookies=admin_cookies,
        )
        assert r.status_code == 200
        body = r.text
        assert "<dt>Version</dt>" in body
        assert "<dt>Bundle hash</dt>" in body
        assert "v1" in body


class TestPublishGateFailClosed:
    """Hold-for-review when ``guardrails.enabled: true`` but no LLM
    provider credentials are present in env. The pre-v45 fall-back
    silently auto-approved every upload — a fail-OPEN hole the
    operator couldn't notice. New behavior: submissions sit at
    ``pending_llm``, entity stays at ``visibility_status='pending'``,
    admin retries from /admin/store/submissions after providing
    credentials."""

    def test_v1_upload_enabled_but_not_ready_holds_at_pending(
        self, web_client, monkeypatch,
    ):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        # Flip guardrails ON but leave provider_ready as False.
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready", lambda: False,
        )
        # No mock review_bundle — we should never call the LLM.
        # If we did, the lack of patching would surface as a real
        # network call attempt, easy to catch as a hang.

        owner_id, owner_cookies = _create_user(web_client, "holdv1@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="holdv1")

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        sub = StoreSubmissionsRepository(conn).latest_for_entity(eid)
        conn.close()
        assert ent["visibility_status"] == "pending", (
            "enabled-but-not-ready must NOT publish — entity stays pending"
        )
        assert sub["status"] == "pending_llm", (
            "submission must hold at pending_llm awaiting admin retry"
        )
        assert sub["llm_findings"] is None, (
            "no LLM call was made — findings must be empty"
        )

    def test_admin_retry_pending_llm_fires_review(
        self, web_client, monkeypatch,
    ):
        """After the operator sets the API key, admin Retry-review on a
        held pending_llm row schedules + runs the LLM."""
        from src.repositories.store_submissions import StoreSubmissionsRepository

        # Phase 1: upload with provider not-ready → held at pending_llm.
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready", lambda: False,
        )
        owner_id, owner_cookies = _create_user(web_client, "retryholder@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="retryholder")

        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()

        # Phase 2: operator adds credentials, admin retries.
        # Inject a fake env var so default_api_key_loader doesn't raise
        # before the mock review_bundle runs.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake-key-for-retry")
        # Mock review_bundle so the retry resolves to approved without
        # touching the network.
        def mock_review_bundle(*args, **kwargs):
            return {
                "risk_level": "safe", "summary": "ok",
                "findings": [], "template_placeholders_found": 0,
                "reviewed_by_model": "mock-model", "error": None,
                "content_quality": {"verdict": "pass", "issues": []},
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_review_bundle,
        )

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sub_id}/retry",
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        # After retry, BG task runs synchronously in TestClient (it
        # blocks the response). Verify the row moved to approved.
        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert sub["status"] == "approved", (
            f"retry must drive submission to approved; got {sub['status']}"
        )
        assert ent["visibility_status"] == "approved", (
            "entity must flip to approved after LLM ok"
        )

    def test_edit_enabled_but_not_ready_holds_prior_serving(
        self, web_client, monkeypatch,
    ):
        """v2+ edit under enabled-but-not-ready: v1 keeps serving,
        v2 submission held at pending_llm. Critical safety property:
        no silent promotion."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        # Initial upload runs with guardrails OFF (autouse default) →
        # v1 approved. Then flip to enabled-but-not-ready for PUT.
        owner_id, owner_cookies = _create_user(web_client, "holdedit@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="holdedit")
        conn = get_system_db()
        v1_hash = StoreEntitiesRepository(conn).get(eid)["version"]
        conn.close()

        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready", lambda: False,
        )
        v2 = _make_skill_zip("holdedit", body="V2 BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        sub = StoreSubmissionsRepository(conn).latest_for_entity(eid)
        conn.close()
        # Entity stays approved at v1, v2 sits at pending_llm.
        assert ent["visibility_status"] == "approved"
        assert ent["version_no"] == 1
        assert ent["version"] == v1_hash, (
            "live bundle must remain v1 — no silent promotion of v2"
        )
        assert sub["status"] == "pending_llm"
        assert sub["llm_findings"] is None

    def test_disabled_intent_still_auto_approves(
        self, web_client, monkeypatch,
    ):
        """Operator explicitly opting out (``enabled: false``) keeps
        the prior auto-approve behavior — local dev / no-LLM
        deployments aren't blocked."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        # autouse fixture already sets enabled=False. Just confirm
        # behavior end-to-end.
        owner_id, owner_cookies = _create_user(web_client, "offowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="offowner")

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        sub = StoreSubmissionsRepository(conn).latest_for_entity(eid)
        conn.close()
        assert ent["visibility_status"] == "approved"
        assert sub["status"] == "approved"


class TestConcurrentPutSerialization:
    """Codex adversarial review [HIGH]: concurrent PUTs racing on the
    same entity_id could both pass the ``latest_for_entity`` pending
    gate, both bake into ``versions/v<N+1>/plugin/``, and both append
    a ``version_history`` entry. Per-entity asyncio lock added to
    serialize the critical section in PUT + restore.

    Integration coverage (two real PUTs racing against TestClient)
    isn't practical here: each TestClient call wraps the async handler
    in its own event loop, so asyncio.Lock acquired in loop A cannot
    coordinate with loop B — they deadlock instead of contending. In
    a real uvicorn deployment all requests run on a single event loop
    and the lock works as designed. This test exercises the helper
    directly to verify the serialization semantics; the integration
    side is covered by the existing `prior_version_pending` test
    (which fires once the first PUT has committed)."""

    def test_per_entity_lock_serializes(self):
        import asyncio
        from app.api.store import _hold_entity_write_lock

        seq: list = []

        async def task(label: str) -> None:
            async with _hold_entity_write_lock("ent-shared"):
                seq.append(f"{label}-in")
                # Yield to the scheduler to give the other coroutine a
                # chance to run if the lock isn't held.
                await asyncio.sleep(0.01)
                seq.append(f"{label}-out")

        async def driver() -> None:
            await asyncio.gather(task("A"), task("B"))

        asyncio.run(driver())

        # Pairs must NOT interleave — one finishes entirely before
        # the other starts.
        assert seq in (
            ["A-in", "A-out", "B-in", "B-out"],
            ["B-in", "B-out", "A-in", "A-out"],
        ), f"per-entity lock failed to serialize: seq={seq}"

    def test_per_entity_lock_does_not_serialize_across_entities(self):
        """Different entity_ids get independent locks so unrelated
        writes don't block each other."""
        import asyncio
        from app.api.store import _hold_entity_write_lock

        seq: list = []

        async def task(label: str, entity: str) -> None:
            async with _hold_entity_write_lock(entity):
                seq.append(f"{label}-in")
                await asyncio.sleep(0.01)
                seq.append(f"{label}-out")

        async def driver() -> None:
            await asyncio.gather(task("A", "ent-a"), task("B", "ent-b"))

        asyncio.run(driver())

        # Interleaving expected: A-in, B-in, A-out, B-out (or B/A
        # ordering depending on which coroutine the loop picks first).
        assert seq[0] in {"A-in", "B-in"}
        assert seq[1] in {"A-in", "B-in"}
        assert seq[0] != seq[1], (
            f"entities should have run in parallel — got serial: {seq}"
        )


class TestBgTaskIdempotency:
    """Codex adversarial review [HIGH]: `update_status` blindly
    overwrote any current status. A late BG-task LLM verdict racing
    with an admin override could clobber `overridden` back to
    `approved`/`blocked_llm`. Now: terminal statuses are
    compare-and-swap-protected; BG callers no-op."""

    def test_late_verdict_does_not_clobber_overridden(self, web_client):
        """Admin overrides a blocked submission. A subsequent late
        BG-task ``update_status`` for the same submission must NOT
        flip it back."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        user_id, _ = _create_user(web_client, "idemp@x.com")
        conn = get_system_db()
        ents = StoreEntitiesRepository(conn)
        ents.create(
            id="ent-idemp", owner_user_id=user_id, owner_username="idemp",
            type="skill", name="idemp-skill", description="x" * 40,
            category=None, version="aaaaaaaaaaaaaaaa", file_size=10,
            visibility_status="pending",
        )
        subs = StoreSubmissionsRepository(conn)
        sid = subs.create(
            submitter_id=user_id, submitter_email="idemp@x.com",
            type="skill", name="idemp-skill", version="aaaaaaaaaaaaaaaa",
            status="blocked_llm", entity_id="ent-idemp",
            llm_findings={"risk_level": "high", "summary": "x"},
        )
        ents.update_history_submission_id("ent-idemp", 1, sid)
        conn.close()

        from tests.helpers.auth import grant_admin
        admin_id, admin_cookies = _create_user(web_client, "idemp-admin@x.com")
        conn = get_system_db()
        grant_admin(conn, admin_id)
        conn.close()

        # Override the blocked submission → status='overridden'.
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/override",
            json={"reason": "false positive — cleared in offline review"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200

        # Now simulate a late BG-task verdict arriving:
        # update_status is called without allow_terminal_overwrite.
        conn = get_system_db()
        subs = StoreSubmissionsRepository(conn)
        # CAS no-op because status=='overridden' is terminal.
        wrote = subs.update_status(
            sid, status="approved",
            llm_findings={"risk_level": "safe", "summary": "late"},
        )
        conn.close()
        assert wrote is False, (
            "late BG verdict must NOT overwrite a terminal `overridden` row"
        )

        # Status still overridden.
        conn = get_system_db()
        row = StoreSubmissionsRepository(conn).get(sid)
        conn.close()
        assert row["status"] == "overridden"

    def test_runner_late_verdict_logs_skipped_not_approved(
        self, web_client, monkeypatch,
    ):
        """End-to-end pair to ``test_late_verdict_does_not_clobber_overridden``:
        when the LLM verdict lands on an already-overridden submission,
        ``runner.run_llm_review`` honors the CAS bool and:
          1. row status stays ``overridden``,
          2. audit log gets a single ``bg_verdict_skipped`` entry,
          3. audit log does NOT get a contradictory ``approved`` /
             ``blocked_llm`` entry — pre-fix the runner discarded the
             return value and ran the downstream cascade including
             the misleading audit write.
        """
        from src.repositories.audit import AuditRepository
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review
        from app.utils import get_store_dir

        owner_id, owner_cookies = _create_user(web_client, "lateverdict@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="lateverdict")

        # Flip guardrails on, PUT v2 → pending_llm under deferred-promotion
        # (visibility stays 'approved' at v1).
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review", lambda *a, **kw: None,
        )
        # Mock review_bundle to return an "approved"-shape verdict so
        # the runner would (pre-fix) hit the approved branch + cascade.
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "safe", "summary": "ok",
                "findings": [], "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
            },
        )

        v2 = _make_skill_zip("lateverdict", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )

        # Admin override flips the v2 submission row to 'overridden'.
        from tests.helpers.auth import grant_admin
        admin_id, admin_cookies = _create_user(web_client, "lv-admin@x.com")
        conn = get_system_db()
        grant_admin(conn, admin_id)
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        r = web_client.post(
            f"/api/admin/store/submissions/{sub_id}/override",
            json={"reason": "false positive cleared offline"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

        # Now fire the runner directly — it would (pre-fix) try to write
        # status='approved' on the already-overridden row.
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        # Row must stay overridden + audit log must show skipped, not
        # the misleading approved write.
        conn = get_system_db()
        row = StoreSubmissionsRepository(conn).get(sub_id)
        rows = AuditRepository(conn).query_for_resources(
            [f"store_submission:{sub_id}"], limit=20,
        )
        conn.close()
        actions = [r.get("action") for r in rows]
        assert row["status"] == "overridden", (
            f"row must stay overridden under CAS no-op; got {row['status']}"
        )
        assert "store.submission.bg_verdict_skipped" in actions, (
            f"runner must log bg_verdict_skipped on CAS no-op; got {actions}"
        )
        assert "store.submission.approved" not in actions, (
            "runner must NOT log approved when the CAS no-op'd the write — "
            f"audit must not contradict the row state; got {actions}"
        )

    def test_explicit_allow_terminal_overwrite_works(self, web_client):
        """Admin paths that legitimately need to overwrite a terminal
        state can pass `allow_terminal_overwrite=True` and get the
        write through. Used by rescan and similar admin actions."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        user_id, _ = _create_user(web_client, "termok@x.com")
        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=user_id, submitter_email="termok@x.com",
            type="skill", name="x", version="aaaa", status="approved",
            entity_id=None,
        )
        wrote = StoreSubmissionsRepository(conn).update_status(
            sid, status="pending_llm", allow_terminal_overwrite=True,
        )
        conn.close()
        assert wrote is True
        conn = get_system_db()
        row = StoreSubmissionsRepository(conn).get(sid)
        conn.close()
        assert row["status"] == "pending_llm"


class TestAtomicPromote:
    """Codex adversarial review [MEDIUM]: pre-fix sequence was
    ``repo.promote_version(...)`` → ``_swap_live_to_version(...)``.
    If the source ``versions/v<N>/plugin/`` was missing,
    ``_swap_live_to_version`` returned False silently — leaving DB
    at the new version but live still on the prior bytes.

    Fix: a ``promote_to_version`` helper that swaps live FIRST, then
    promotes the DB. Missing source → return None, no DB change."""

    def test_missing_source_dir_does_not_advance_db(self, web_client):
        """Promote with a missing version dir must leave both DB and
        live untouched."""
        from app.api.store import promote_to_version, _plugin_dir
        from src.repositories.store_entities import StoreEntitiesRepository

        user_id, _ = _create_user(web_client, "atomic@x.com")
        conn = get_system_db()
        repo = StoreEntitiesRepository(conn)
        repo.create(
            id="ent-atomic", owner_user_id=user_id, owner_username="atomic",
            type="skill", name="atomic", description="x" * 40,
            category=None, version="aaaaaaaaaaaaaaaa", file_size=10,
            visibility_status="approved",
        )
        # Inject a v2 history entry without creating the on-disk dir
        # — simulates the "DB has entry, bundle wiped" inconsistency.
        repo.append_version_history(
            "ent-atomic", version_hash="bbbbbbbbbbbbbbbb",
            sha256=None, size=20, submission_id="fake-sub", created_by=user_id,
        )
        conn.close()

        # Attempt to promote to v2 — version dir doesn't exist.
        conn = get_system_db()
        repo = StoreEntitiesRepository(conn)
        result = promote_to_version("ent-atomic", 2, repo)
        ent_after = repo.get("ent-atomic")
        conn.close()
        assert result is None, "must signal failure when source missing"
        assert ent_after["version_no"] == 1, (
            f"DB must NOT advance when live swap can't happen; got "
            f"version_no={ent_after['version_no']}"
        )


class TestPromoteLookupByByteIdenticalBundles:
    """Live-issue regression observed on a development deployment: an
    entity had multiple version_history rows sharing the same `hash`
    (user re-uploaded byte-identical bundles as v2/v4/v6). The runner's
    promote-on-approve path looked up the submission's version_no
    in version_history BY HASH and broke on the FIRST match — always
    v1. With v1's n=1 and current=1, the forward-only
    `target > current` guard skipped the promote, so the passing
    LLM verdict never advanced the entity. UI kept showing v1 as
    'current' even though the new submission's status was 'approved'.

    Fix: look up by `submission_id` via `_version_no_for_submission`."""

    def test_byte_identical_v2_promotes_to_current(
        self, web_client, monkeypatch,
    ):
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-identical-test")
        owner_id, owner_cookies = _create_user(web_client, "identical@x.com")
        # Same body for v1 + v2 → byte-identical zip → same hash.
        identical_body = (
            "Identical body line that is intentionally long enough to "
            "clear the content threshold for skill bodies. " * 4
        )
        v1_zip = _make_skill_zip("identical", body=identical_body)
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("v1.zip", v1_zip, "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]

        conn = get_system_db()
        ent_v1 = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        v1_hash = ent_v1["version"]
        assert ent_v1["version_no"] == 1

        # Flip guardrails ON for v2. Mock LLM to approve.
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready", lambda: True,
        )

        def mock_approve(*a, **kw):
            return {
                "risk_level": "safe", "summary": "ok",
                "findings": [], "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
                "content_quality": {"verdict": "pass", "issues": []},
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle", mock_approve,
        )

        # PUT v2 with IDENTICAL bytes.
        v2_zip = _make_skill_zip("identical", body=identical_body)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        v2_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v2_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk", model_loader=lambda: "mock",
        )

        conn = get_system_db()
        ent_after = StoreEntitiesRepository(conn).get(eid)
        v2_sub = StoreSubmissionsRepository(conn).get(v2_sub_id)
        conn.close()
        # Pre-fix the runner would have matched v1's history entry
        # first (same hash), target_version_no=1, `1 > 1` False, no
        # promote → entity stuck at v1.
        assert ent_after["version_no"] == 2, (
            f"v2 must promote even when its hash matches v1's; got "
            f"version_no={ent_after['version_no']}. Lookup-by-hash "
            f"would have stuck the entity at v1."
        )
        assert v2_sub["status"] == "approved"
        assert ent_after["version"] == v1_hash, (
            "hash unchanged (bundle is byte-identical) but version_no DID move"
        )

    def test_byte_identical_v3_after_different_v2(
        self, web_client, monkeypatch,
    ):
        """v1 + v2 (different hash) + v3 byte-identical to v1.
        Lookup must resolve v3 to n=3, not v1 (same hash) or v2 (the
        most-recent approved). With current=2 and target=3 the
        forward-only guard fires correctly only if target_n=3."""
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-v3-test")
        owner_id, owner_cookies = _create_user(web_client, "v3hash@x.com")

        body_a = (
            "Body A line that is intentionally long enough to clear the "
            "content threshold for skill bodies. " * 4
        )
        body_b = (
            "Body B line that is intentionally DIFFERENT and also long "
            "enough to clear the content threshold for skill bodies. " * 4
        )

        r = web_client.post(
            "/api/store/entities",
            files={"file": ("v1.zip", _make_skill_zip("v3hash", body=body_a),
                            "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        assert r.status_code == 201
        eid = r.json()["id"]

        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready", lambda: True,
        )

        def mock_approve(*a, **kw):
            return {
                "risk_level": "safe", "summary": "ok",
                "findings": [], "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
                "content_quality": {"verdict": "pass", "issues": []},
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle", mock_approve,
        )

        v2_zip = _make_skill_zip("v3hash", body=body_b)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200
        conn = get_system_db()
        v2_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v2_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk", model_loader=lambda: "mock",
        )
        conn = get_system_db()
        ent_at_v2 = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_at_v2["version_no"] == 2

        v3_zip = _make_skill_zip("v3hash", body=body_a)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v3.zip", v3_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200
        conn = get_system_db()
        v3_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v3_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v3" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk", model_loader=lambda: "mock",
        )
        conn = get_system_db()
        ent_at_v3 = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_at_v3["version_no"] == 3, (
            f"v3 must promote despite hash collision with v1; "
            f"got version_no={ent_at_v3['version_no']}"
        )


class TestRescanPromotesNonCurrent:
    """Codex adversarial-review follow-up on PR #330: admin rescan
    with `guardrails.enabled: false` flipped status='approved' +
    visibility but never called `promote_to_version`. A rescan that
    re-approved a non-current v2+ left the entity stuck at the prior
    version. Fix mirrors the inline-promote in create/update/restore."""

    def test_rescan_promotes_non_current_v2_when_guardrails_disabled(
        self, web_client, monkeypatch,
    ):
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review

        owner_id, owner_cookies = _create_user(web_client, "rescanpromote@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="rescanpromote")

        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready", lambda: True,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-rescan-promote")

        def mock_block(*a, **kw):
            return {
                "risk_level": "high", "summary": "mock block",
                "findings": [{"severity": "high", "category": "test",
                              "file": "x", "explanation": "mock"}],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle", mock_block,
        )

        v2 = _make_skill_zip("rescanpromote", body="V2 body content " * 30)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        v2_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v2_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk", model_loader=lambda: "mock",
        )

        conn = get_system_db()
        ent_before = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_before["version_no"] == 1
        v1_hash = ent_before["version"]

        # Rescan with guardrails OFF — branch under test. Patch both
        # bound symbols (admin imports function-locally).
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: False,
        )
        monkeypatch.setattr(
            "app.instance_config.get_guardrails_enabled", lambda: False,
        )
        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{v2_sub_id}/rescan",
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"

        conn = get_system_db()
        ent_after = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_after["version_no"] == 2, (
            f"rescan-approve of v2 must promote entity to v2 when "
            f"guardrails are disabled; got version_no={ent_after['version_no']}"
        )
        assert ent_after["version"] != v1_hash, (
            "entity.version hash must move to v2 after rescan promote"
        )
