"""Integration tests for the /api/store endpoints.

Covers the upload + bake pipeline, install / uninstall, delete cascade, and
the cross-cutting hook that drops opt-outs when admin removes a grant.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db
    close_system_db()
    from app.main import create_app
    app = create_app()
    yield TestClient(app)
    close_system_db()


def _create_user(client, email, password="UserPass1!"):
    """Create user via SQL + password hash, return JWT token via /auth/token."""
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository
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


def _make_skill_zip(skill_name: str = "code-review", desc: str = "Reviews code.") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: {desc}\n---\n\nDo the thing.\n",
        )
    return buf.getvalue()


def _make_plugin_zip(name: str = "my-plugin") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            ".claude-plugin/plugin.json",
            json.dumps({"name": name, "description": "test", "version": "0.1"}),
        )
        zf.writestr("skills/dummy/SKILL.md", "---\nname: dummy\n---\nbody")
    return buf.getvalue()


def _make_agent_zip(name: str = "my-agent") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{name}.md",
            f"---\nname: {name}\ndescription: A test agent.\n---\n\nBe helpful.\n",
        )
    return buf.getvalue()


class TestStoreOwners:
    def test_owners_endpoint_lists_uploaders(self, web_client):
        a_id, a_cookies = _create_user(web_client, "alice@x.com")
        b_id, b_cookies = _create_user(web_client, "bob@x.com")
        # Alice uploads two; Bob uploads one.
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("a1"), "application/zip")},
            data={"type": "skill"}, cookies=a_cookies,
        )
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("a2"), "application/zip")},
            data={"type": "skill"}, cookies=a_cookies,
        )
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("b1"), "application/zip")},
            data={"type": "skill"}, cookies=b_cookies,
        )
        # A third user with no uploads must NOT appear.
        _, _ = _create_user(web_client, "carol@x.com")

        r = web_client.get("/api/store/owners", cookies=a_cookies)
        assert r.status_code == 200
        owners = r.json()
        ids_to_count = {o["user_id"]: o["entity_count"] for o in owners}
        assert ids_to_count == {a_id: 2, b_id: 1}

    def test_owners_endpoint_filters_listing(self, web_client):
        a_id, a_cookies = _create_user(web_client, "owner-a@x.com")
        b_id, b_cookies = _create_user(web_client, "owner-b@x.com")
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("a-only"), "application/zip")},
            data={"type": "skill"}, cookies=a_cookies,
        )
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("b-only"), "application/zip")},
            data={"type": "skill"}, cookies=b_cookies,
        )
        # Filter by Alice's id → only Alice's entity comes back.
        r = web_client.get(f"/api/store/entities?owner={a_id}", cookies=a_cookies)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["name"] == "a-only"


class TestStorePreview:
    def test_preview_skill_returns_frontmatter(self, web_client):
        _, cookies = _create_user(web_client, "preview1@x.com")
        zip_bytes = _make_skill_zip("from-preview", desc="Pulled from frontmatter.")
        r = web_client.post(
            "/api/store/entities/preview",
            files={"file": ("s.zip", zip_bytes, "application/zip")},
            data={"type": "skill"},
            cookies=cookies,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "skill"
        assert body["name"] == "from-preview"
        assert body["description"] == "Pulled from frontmatter."

    def test_preview_does_not_persist(self, web_client):
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.db import get_system_db
        _, cookies = _create_user(web_client, "preview2@x.com")
        web_client.post(
            "/api/store/entities/preview",
            files={"file": ("s.zip", _make_skill_zip("ghost"), "application/zip")},
            data={"type": "skill"},
            cookies=cookies,
        )
        conn = get_system_db()
        try:
            items, total = StoreEntitiesRepository(conn).list(skip=0, limit=10)
            assert total == 0
        finally:
            conn.close()

    def test_preview_wrong_type_422(self, web_client):
        _, cookies = _create_user(web_client, "preview3@x.com")
        r = web_client.post(
            "/api/store/entities/preview",
            files={"file": ("s.zip", _make_skill_zip("oops"), "application/zip")},
            data={"type": "plugin"},
            cookies=cookies,
        )
        assert r.status_code == 422


class TestStoreDocsUpload:
    def test_create_with_docs(self, web_client, tmp_path):
        _, cookies = _create_user(web_client, "docs@x.com")
        r = web_client.post(
            "/api/store/entities",
            files=[
                ("file", ("s.zip", _make_skill_zip("with-docs"), "application/zip")),
                ("docs", ("readme.md", b"# Readme", "text/markdown")),
                ("docs", ("howto.txt", b"do this then that", "text/plain")),
            ],
            data={"type": "skill"},
            cookies=cookies,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert sorted(body["doc_paths"]) == ["assets/docs/howto.txt", "assets/docs/readme.md"]
        assert (tmp_path / "store" / body["id"] / "assets" / "docs" / "readme.md").is_file()
        assert (tmp_path / "store" / body["id"] / "assets" / "docs" / "howto.txt").is_file()

    def test_doc_filename_collision_renames(self, web_client, tmp_path):
        _, cookies = _create_user(web_client, "dup-doc@x.com")
        r = web_client.post(
            "/api/store/entities",
            files=[
                ("file", ("s.zip", _make_skill_zip("dupdoc"), "application/zip")),
                ("docs", ("notes.md", b"first", "text/markdown")),
                ("docs", ("notes.md", b"second", "text/markdown")),
            ],
            data={"type": "skill"},
            cookies=cookies,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert "assets/docs/notes.md" in body["doc_paths"]
        assert "assets/docs/notes-2.md" in body["doc_paths"]

    def test_doc_download_route(self, web_client):
        _, cookies = _create_user(web_client, "dl-doc@x.com")
        r = web_client.post(
            "/api/store/entities",
            files=[
                ("file", ("s.zip", _make_skill_zip("dl"), "application/zip")),
                ("docs", ("readme.md", b"# Hello", "text/markdown")),
            ],
            data={"type": "skill"},
            cookies=cookies,
        )
        eid = r.json()["id"]
        d = web_client.get(f"/api/store/entities/{eid}/docs/readme.md", cookies=cookies)
        assert d.status_code == 200
        assert b"Hello" in d.content

    def test_doc_path_traversal_blocked(self, web_client):
        _, cookies = _create_user(web_client, "trav@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("trav"), "application/zip")},
            data={"type": "skill"},
            cookies=cookies,
        )
        eid = r.json()["id"]
        # Direct .. is blocked at the FastAPI route level (won't even match
        # the path-segment param), so try a percent-encoded form — server
        # should still resolve to a path outside the docs dir and 400/404.
        d = web_client.get(
            f"/api/store/entities/{eid}/docs/..%2F..%2Fplugin",
            cookies=cookies,
        )
        assert d.status_code in (400, 404)


class TestStoreUpload:
    def test_upload_skill_creates_baked_tree(self, web_client, tmp_path):
        _, cookies = _create_user(web_client, "alice@x.com")
        zip_bytes = _make_skill_zip("code-review")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", zip_bytes, "application/zip")},
            data={"type": "skill"},
            cookies=cookies,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["type"] == "skill"
        assert body["name"] == "code-review"
        assert body["owner_username"] == "alice"
        assert body["invocation_name"] == "code-review-by-alice"
        assert body["version"]
        # On-disk: the suffixed skill folder exists with rewritten frontmatter.
        plugin_dir = tmp_path / "store" / body["id"] / "plugin"
        skill_md = plugin_dir / "skills" / "code-review-by-alice" / "SKILL.md"
        assert skill_md.is_file()
        assert "name: code-review-by-alice" in skill_md.read_text()
        assert (plugin_dir / ".claude-plugin" / "plugin.json").is_file()

    def test_upload_plugin_rewrites_name(self, web_client, tmp_path):
        _, cookies = _create_user(web_client, "bob@x.com")
        zip_bytes = _make_plugin_zip("my-plugin")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("p.zip", zip_bytes, "application/zip")},
            data={"type": "plugin"},
            cookies=cookies,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        plugin_dir = tmp_path / "store" / body["id"] / "plugin"
        pj = json.loads((plugin_dir / ".claude-plugin" / "plugin.json").read_text())
        assert pj["name"] == "my-plugin-by-bob"
        # Inner skill stays untouched (per spec — plugin all-or-nothing).
        assert (plugin_dir / "skills" / "dummy" / "SKILL.md").is_file()

    def test_upload_agent(self, web_client, tmp_path):
        _, cookies = _create_user(web_client, "carol@x.com")
        zip_bytes = _make_agent_zip("planner")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("a.zip", zip_bytes, "application/zip")},
            data={"type": "agent"},
            cookies=cookies,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        plugin_dir = tmp_path / "store" / body["id"] / "plugin"
        agent_md = plugin_dir / "agents" / "planner-by-carol.md"
        assert agent_md.is_file()
        assert "name: planner-by-carol" in agent_md.read_text()

    def test_upload_same_name_twice_409(self, web_client):
        _, cookies = _create_user(web_client, "dan@x.com")
        zip_bytes = _make_skill_zip("dup")
        for _ in range(1):
            r = web_client.post(
                "/api/store/entities",
                files={"file": ("s.zip", zip_bytes, "application/zip")},
                data={"type": "skill"},
                cookies=cookies,
            )
            assert r.status_code == 201, r.text
        r2 = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", zip_bytes, "application/zip")},
            data={"type": "skill"},
            cookies=cookies,
        )
        assert r2.status_code == 409
        assert r2.json()["detail"] == "conflict_owner_name"

    def test_upload_wrong_type_for_zip(self, web_client):
        _, cookies = _create_user(web_client, "eve@x.com")
        # ZIP shaped like a skill, but declared as plugin → 422.
        zip_bytes = _make_skill_zip("foo")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", zip_bytes, "application/zip")},
            data={"type": "plugin"},
            cookies=cookies,
        )
        assert r.status_code == 422
        assert "claude_plugin_json" in r.json()["detail"]

    def test_skill_zip_rejected_as_agent(self, web_client):
        """SKILL.md has the same name+description frontmatter shape as an
        agent file, so the type-agent validator must explicitly reject any
        ZIP that contains a SKILL.md — otherwise a skill upload would
        silently masquerade as an agent (issue surfaced during user-test)."""
        _, cookies = _create_user(web_client, "skill-as-agent@x.com")
        zip_bytes = _make_skill_zip("trick")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", zip_bytes, "application/zip")},
            data={"type": "agent"},
            cookies=cookies,
        )
        assert r.status_code == 422
        assert r.json()["detail"] == "zip_looks_like_skill"

    def test_plugin_zip_rejected_as_skill(self, web_client):
        _, cookies = _create_user(web_client, "plugin-as-skill@x.com")
        zip_bytes = _make_plugin_zip("p1")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("p.zip", zip_bytes, "application/zip")},
            data={"type": "skill"},
            cookies=cookies,
        )
        assert r.status_code == 422
        assert r.json()["detail"] == "zip_looks_like_plugin"

    def test_plugin_zip_rejected_as_agent(self, web_client):
        _, cookies = _create_user(web_client, "plugin-as-agent@x.com")
        zip_bytes = _make_plugin_zip("p2")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("p.zip", zip_bytes, "application/zip")},
            data={"type": "agent"},
            cookies=cookies,
        )
        assert r.status_code == 422
        # Plugin ZIP also contains a skills/dummy/SKILL.md which trips the
        # skill-mismatch guard first; either error code is acceptable proof
        # that the validator caught the mismatch.
        assert r.json()["detail"] in {"zip_looks_like_plugin", "zip_looks_like_skill"}


class TestInstallCycle:
    def test_install_uninstall_and_count(self, web_client):
        # Owner uploads, two other users install, install_count = 2.
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("share"), "application/zip")},
            data={"type": "skill"},
            cookies=owner_cookies,
        )
        eid = r.json()["id"]

        _, a_cookies = _create_user(web_client, "alpha@x.com")
        _, b_cookies = _create_user(web_client, "beta@x.com")
        assert web_client.post(f"/api/store/entities/{eid}/install", cookies=a_cookies).status_code == 200
        # Idempotent — second call doesn't double-bump.
        assert web_client.post(f"/api/store/entities/{eid}/install", cookies=a_cookies).status_code == 200
        assert web_client.post(f"/api/store/entities/{eid}/install", cookies=b_cookies).status_code == 200

        det = web_client.get(f"/api/store/entities/{eid}", cookies=owner_cookies).json()
        assert det["install_count"] == 2

        # Uninstall.
        web_client.delete(f"/api/store/entities/{eid}/install", cookies=a_cookies)
        det = web_client.get(f"/api/store/entities/{eid}", cookies=owner_cookies).json()
        assert det["install_count"] == 1

    def test_delete_entity_cascades_installs(self, web_client):
        _, owner_cookies = _create_user(web_client, "o2@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("cascade"), "application/zip")},
            data={"type": "skill"},
            cookies=owner_cookies,
        )
        eid = r.json()["id"]
        _, u_cookies = _create_user(web_client, "victim@x.com")
        web_client.post(f"/api/store/entities/{eid}/install", cookies=u_cookies)

        # Owner deletes.
        d = web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)
        assert d.status_code == 200

        # GET 404.
        assert web_client.get(f"/api/store/entities/{eid}", cookies=owner_cookies).status_code == 404
        # The install row is gone — installer's /api/my-stack store list shrunk.
        ms = web_client.get("/api/my-stack", cookies=u_cookies).json()
        assert all(e["entity_id"] != eid for e in ms["store"])

    def test_non_owner_cannot_delete(self, web_client):
        _, owner_cookies = _create_user(web_client, "o3@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("guarded"), "application/zip")},
            data={"type": "skill"},
            cookies=owner_cookies,
        )
        eid = r.json()["id"]
        _, intruder_cookies = _create_user(web_client, "intruder@x.com")
        d = web_client.delete(f"/api/store/entities/{eid}", cookies=intruder_cookies)
        assert d.status_code == 403


class TestMarketplaceBundle:
    """End-to-end: the served /marketplace.zip merges installed Store skills
    and agents into a single ``store-bundle`` plugin, while ``type='plugin'``
    Store entities stay standalone."""

    def _zip_entries(self, content: bytes) -> set[str]:
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            return set(zf.namelist())

    def _read_zip_file(self, content: bytes, name: str) -> bytes:
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            return zf.read(name)

    def test_skill_and_agent_merge_into_one_bundle(self, web_client):
        import json as _json
        owner_id, owner_cookies = _create_user(web_client, "owner@bundle.x")
        # Two skills + one agent + one plugin
        skill_a = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("alpha"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        ).json()["id"]
        skill_b = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("beta"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        ).json()["id"]
        agent_c = web_client.post(
            "/api/store/entities",
            files={"file": ("a.zip", _make_agent_zip("gamma"), "application/zip")},
            data={"type": "agent"}, cookies=owner_cookies,
        ).json()["id"]
        plugin_d = web_client.post(
            "/api/store/entities",
            files={"file": ("p.zip", _make_plugin_zip("delta"), "application/zip")},
            data={"type": "plugin"}, cookies=owner_cookies,
        ).json()["id"]

        _, installer_cookies = _create_user(web_client, "installer@bundle.x")
        for eid in (skill_a, skill_b, agent_c, plugin_d):
            assert web_client.post(
                f"/api/store/entities/{eid}/install", cookies=installer_cookies,
            ).status_code == 200

        r = web_client.get("/marketplace.zip", cookies=installer_cookies)
        assert r.status_code == 200, r.text
        names = self._zip_entries(r.content)

        # Bundle exists with synth plugin.json + every skill + agent file.
        assert "plugins/store-bundle/.claude-plugin/plugin.json" in names
        assert "plugins/store-bundle/skills/alpha-by-owner/SKILL.md" in names
        assert "plugins/store-bundle/skills/beta-by-owner/SKILL.md" in names
        assert "plugins/store-bundle/agents/gamma-by-owner.md" in names

        # The plugin-typed entity is a separate dir; skills inside its tree
        # carry their original (non-suffixed) names per spec.
        assert f"plugins/store-{plugin_d}/.claude-plugin/plugin.json" in names

        # Manifest has exactly two plugin entries: the bundle + the standalone.
        manifest = _json.loads(self._read_zip_file(
            r.content, ".claude-plugin/marketplace.json",
        ))
        names_in_catalog = sorted(p["name"] for p in manifest["plugins"])
        assert names_in_catalog == ["agnes-store-bundle", "delta-by-owner"]

        # Bundle's own plugin.json carries the synth fields.
        bundle_pj = _json.loads(self._read_zip_file(
            r.content, "plugins/store-bundle/.claude-plugin/plugin.json",
        ))
        assert bundle_pj["name"] == "agnes-store-bundle"
        assert bundle_pj["version"]  # non-empty hash

    def test_only_skills_yields_only_bundle(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "ob@x.x")
        eid = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("solo"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        ).json()["id"]
        _, installer_cookies = _create_user(web_client, "ib@x.x")
        web_client.post(f"/api/store/entities/{eid}/install", cookies=installer_cookies)

        r = web_client.get("/marketplace.zip", cookies=installer_cookies)
        assert r.status_code == 200
        names = self._zip_entries(r.content)
        assert "plugins/store-bundle/skills/solo-by-ob/SKILL.md" in names
        # No standalone entry for the skill — bundle is the only Store-derived
        # plugin dir present.
        assert not any(n.startswith(f"plugins/store-{eid}/") for n in names)

    def test_uninstalling_skill_drops_from_bundle(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "oc@x.x")
        a = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("keepme"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        ).json()["id"]
        b = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("dropme"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        ).json()["id"]
        _, installer_cookies = _create_user(web_client, "ic@x.x")
        web_client.post(f"/api/store/entities/{a}/install", cookies=installer_cookies)
        web_client.post(f"/api/store/entities/{b}/install", cookies=installer_cookies)

        # Both skills present.
        r1 = web_client.get("/marketplace.zip", cookies=installer_cookies)
        names1 = self._zip_entries(r1.content)
        assert "plugins/store-bundle/skills/keepme-by-oc/SKILL.md" in names1
        assert "plugins/store-bundle/skills/dropme-by-oc/SKILL.md" in names1

        # Uninstall one — bundle still exists, but only the kept skill remains.
        web_client.delete(f"/api/store/entities/{b}/install", cookies=installer_cookies)
        r2 = web_client.get("/marketplace.zip", cookies=installer_cookies)
        names2 = self._zip_entries(r2.content)
        assert "plugins/store-bundle/skills/keepme-by-oc/SKILL.md" in names2
        assert "plugins/store-bundle/skills/dropme-by-oc/SKILL.md" not in names2


class TestWebPages:
    def test_store_listing_page_renders(self, web_client):
        _, cookies = _create_user(web_client, "page1@x.com")
        r = web_client.get("/store", cookies=cookies)
        assert r.status_code == 200
        assert "Store" in r.text

    def test_store_upload_page_renders(self, web_client):
        _, cookies = _create_user(web_client, "page2@x.com")
        r = web_client.get("/store/new", cookies=cookies)
        assert r.status_code == 200
        assert "Upload" in r.text

    def test_my_ai_stack_page_renders(self, web_client):
        _, cookies = _create_user(web_client, "page3@x.com")
        r = web_client.get("/my-ai-stack", cookies=cookies)
        assert r.status_code == 200
        assert "My AI Stack" in r.text

    def test_store_detail_page_renders(self, web_client):
        _, cookies = _create_user(web_client, "page4@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("page-skill"), "application/zip")},
            data={"type": "skill"},
            cookies=cookies,
        )
        eid = r.json()["id"]
        det = web_client.get(f"/store/{eid}", cookies=cookies)
        assert det.status_code == 200
        assert "page-skill" in det.text


class TestMyStackOptout:
    def _seed_admin_grant(self, conn, *, user_id, marketplace, plugin, group_name="Test"):
        """Helper: register marketplace + plugin, put user in a group with grant."""
        from datetime import datetime, timezone
        conn.execute(
            "INSERT INTO marketplace_registry (id, name, url, registered_at) "
            "VALUES (?, ?, ?, ?)",
            [marketplace, marketplace.upper(),
             f"https://example/{marketplace}.git", datetime.now(timezone.utc)],
        )
        conn.execute(
            "INSERT INTO marketplace_plugins (marketplace_id, name, version, raw, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [marketplace, plugin, "1.0",
             json.dumps({"name": plugin, "version": "1.0"}),
             datetime.now(timezone.utc)],
        )
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.resource_grants import ResourceGrantsRepository
        gid = UserGroupsRepository(conn).create(name=group_name)["id"]
        UserGroupMembersRepository(conn).add_member(user_id, gid, source="admin")
        grant_id = ResourceGrantsRepository(conn).create(
            group_id=gid, resource_type="marketplace_plugin",
            resource_id=f"{marketplace}/{plugin}",
        )
        return gid, grant_id

    def test_optout_removes_from_my_stack(self, web_client):
        from src.db import get_system_db
        user_id, cookies = _create_user(web_client, "stack@x.com")
        conn = get_system_db()
        self._seed_admin_grant(conn, user_id=user_id, marketplace="mkt-x", plugin="alpha")
        conn.close()

        ms = web_client.get("/api/my-stack", cookies=cookies).json()
        assert len(ms["curated"]) == 1
        assert ms["curated"][0]["enabled"] is True

        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": False},
            cookies=cookies,
        )
        assert r.status_code == 200

        ms2 = web_client.get("/api/my-stack", cookies=cookies).json()
        assert ms2["curated"][0]["enabled"] is False

    def test_grant_delete_drops_optouts(self, web_client):
        """The admin grant-delete hook must clean up everyone's opt-outs so a
        re-grant restarts at the default (enabled)."""
        from src.db import get_system_db
        from tests.helpers.auth import grant_admin
        from src.repositories.users import UserRepository
        from argon2 import PasswordHasher

        # Bootstrap an admin user.
        ph = PasswordHasher()
        conn = get_system_db()
        UserRepository(conn).create(
            id="adm", email="adm@x.com", name="adm", password_hash=ph.hash("AdminPass1!"),
        )
        grant_admin(conn, "adm")
        conn.close()
        admin_token = web_client.post(
            "/auth/token", json={"email": "adm@x.com", "password": "AdminPass1!"}
        ).json()["access_token"]
        admin_cookies = {"access_token": admin_token}

        # Regular user with a grant + an opt-out.
        user_id, user_cookies = _create_user(web_client, "stack2@x.com")
        conn = get_system_db()
        gid, grant_id = self._seed_admin_grant(
            conn, user_id=user_id, marketplace="mkt-y", plugin="beta",
            group_name="Other",
        )
        conn.close()

        r = web_client.put(
            "/api/my-stack/curated/mkt-y/beta",
            json={"enabled": False}, cookies=user_cookies,
        )
        assert r.status_code == 200

        # Admin deletes the grant.
        d = web_client.delete(f"/api/admin/grants/{grant_id}", cookies=admin_cookies)
        assert d.status_code == 204, d.text

        # Opt-out should be gone.
        from src.repositories.user_plugin_optouts import UserPluginOptoutsRepository
        conn = get_system_db()
        try:
            assert UserPluginOptoutsRepository(conn).is_opted_out(
                user_id, "mkt-y", "beta",
            ) is False
        finally:
            conn.close()
