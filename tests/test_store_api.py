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


# Strong defaults so the per-component content guardrail (≥ 30 chars,
# ≥ 4 distinct words, body ≥ 200 chars for skills/agents) passes for
# every helper bundle. Individual tests that want to exercise failure
# modes pass shorter overrides explicitly.
_OK_DESC = "Use when validating the store upload pipeline across every guardrail tier"
_OK_BODY = (
    "Body explaining when to invoke the component, what inputs it needs, "
    "and the behavior contract. Long enough to clear the 200-char body floor. "
    "Repeated content for length."
) * 2


def _make_skill_zip(
    skill_name: str = "code-review",
    desc: str = _OK_DESC,
    body: str = _OK_BODY,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: {desc}\n---\n\n{body}\n",
        )
    return buf.getvalue()


def _make_plugin_zip(
    name: str = "my-plugin",
    desc: str = _OK_DESC,
    body: str = _OK_BODY,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            ".claude-plugin/plugin.json",
            json.dumps({"name": name, "description": desc, "version": "0.1"}),
        )
        zf.writestr(
            "skills/dummy/SKILL.md",
            f"---\nname: dummy\ndescription: {desc}\n---\n\n{body}\n",
        )
    return buf.getvalue()


def _make_agent_zip(
    name: str = "my-agent",
    desc: str = _OK_DESC,
    body: str = _OK_BODY,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{name}.md",
            f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n",
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
            data={"type": "skill", "description": _OK_DESC}, cookies=a_cookies,
        )
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("a2"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=a_cookies,
        )
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("b1"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=b_cookies,
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
            data={"type": "skill", "description": _OK_DESC}, cookies=a_cookies,
        )
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("b-only"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=b_cookies,
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
            data={"type": "skill", "description": _OK_DESC},
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
            data={"type": "skill", "description": _OK_DESC},
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
            data={"type": "plugin", "description": _OK_DESC},
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
            data={"type": "skill", "description": _OK_DESC},
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
            data={"type": "skill", "description": _OK_DESC},
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
            data={"type": "skill", "description": _OK_DESC},
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
            data={"type": "skill", "description": _OK_DESC},
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
            data={"type": "skill", "description": _OK_DESC},
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
            data={"type": "plugin", "description": _OK_DESC},
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
            data={"type": "agent", "description": _OK_DESC},
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
                data={"type": "skill", "description": _OK_DESC},
                cookies=cookies,
            )
            assert r.status_code == 201, r.text
        r2 = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", zip_bytes, "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
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
            data={"type": "plugin", "description": _OK_DESC},
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
            data={"type": "agent", "description": _OK_DESC},
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
            data={"type": "skill", "description": _OK_DESC},
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
            data={"type": "agent", "description": _OK_DESC},
            cookies=cookies,
        )
        assert r.status_code == 422
        # Plugin ZIP also contains a skills/dummy/SKILL.md which trips the
        # skill-mismatch guard first; either error code is acceptable proof
        # that the validator caught the mismatch.
        assert r.json()["detail"] in {"zip_looks_like_plugin", "zip_looks_like_skill"}


class TestStoreV49Metadata:
    """v49 phase-1 — title, tagline, synthetic_name fields end-to-end.

    Preview returns humanized title; POST accepts user-supplied title/tagline
    and falls back to the humanizer; PUT round-trips the partial update; the
    response always carries the v49 columns.
    """

    def test_preview_returns_humanized_title(self, web_client):
        _, cookies = _create_user(web_client, "preview@x.com")
        zip_bytes = _make_skill_zip("mcp-builder")
        r = web_client.post(
            "/api/store/entities/preview",
            files={"file": ("s.zip", zip_bytes, "application/zip")},
            data={"type": "skill"},
            cookies=cookies,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "mcp-builder"
        assert body["title"] == "MCP Builder", body

    def test_post_with_explicit_title_and_tagline(self, web_client):
        _, cookies = _create_user(web_client, "v49post@x.com")
        zip_bytes = _make_skill_zip("code-review")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", zip_bytes, "application/zip")},
            data={
                "type": "skill",
                "description": _OK_DESC,
                "title": "PR Reviewer (custom)",
                "tagline": "Spots missing tests and weak assertions.",
            },
            cookies=cookies,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["title"] == "PR Reviewer (custom)"
        assert body["tagline"] == "Spots missing tests and weak assertions."
        assert body["synthetic_name"] == "code-review-by-v49post"

    def test_post_falls_back_to_humanized_title_when_omitted(self, web_client):
        _, cookies = _create_user(web_client, "fallback@x.com")
        zip_bytes = _make_skill_zip("oauth-server")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", zip_bytes, "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=cookies,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        # Server-side humanize fallback uses the same acronym dict as JS.
        assert body["title"] == "OAuth Server"
        assert body["tagline"] is None
        assert body["synthetic_name"] == "oauth-server-by-fallback"

    def test_post_rejects_oversize_title(self, web_client):
        _, cookies = _create_user(web_client, "oversize@x.com")
        zip_bytes = _make_skill_zip("long-title")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", zip_bytes, "application/zip")},
            data={"type": "skill", "description": _OK_DESC, "title": "x" * 101},
            cookies=cookies,
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "title_too_long"

    def test_post_rejects_oversize_tagline(self, web_client):
        _, cookies = _create_user(web_client, "oversizetag@x.com")
        zip_bytes = _make_skill_zip("long-tag")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", zip_bytes, "application/zip")},
            data={
                "type": "skill", "description": _OK_DESC,
                "tagline": "x" * 201,
            },
            cookies=cookies,
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "tagline_too_long"

    def test_put_updates_title_and_tagline_and_recomputes_synthetic_on_rename(
        self, web_client,
    ):
        _, cookies = _create_user(web_client, "v49put@x.com")
        zip_bytes = _make_skill_zip("starter-name")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", zip_bytes, "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]

        # Pure metadata edit: title + tagline.
        e = web_client.put(
            f"/api/store/entities/{eid}",
            data={
                "title": "New Title",
                "tagline": "A pithy tagline",
            },
            cookies=cookies,
        )
        assert e.status_code == 200, e.text
        body = e.json()
        assert body["title"] == "New Title"
        assert body["tagline"] == "A pithy tagline"
        # name unchanged → synthetic unchanged.
        assert body["synthetic_name"] == "starter-name-by-v49put"

        # Rename: synthetic_name must follow.
        e2 = web_client.put(
            f"/api/store/entities/{eid}",
            data={"name": "renamed-thing"},
            cookies=cookies,
        )
        assert e2.status_code == 200, e2.text
        assert e2.json()["synthetic_name"] == "renamed-thing-by-v49put"

    def test_invocation_name_reads_from_synthetic_column(self, web_client):
        """v49 phase-3: ``invocation_name`` in StoreEntityResponse sources
        from the stored ``synthetic_name`` column, not a fresh recompute.
        Manually override the column with a non-canonical value and verify
        the API returns it verbatim — proves read paths consume the column
        rather than recomputing ``<name>-by-<owner_username>`` on the fly."""
        from src.db import get_system_db
        _, cookies = _create_user(web_client, "synthread@x.com")
        up = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("orig-name"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=cookies,
        )
        eid = up.json()["id"]
        # Manual divergence — pretend an admin fix-up landed a non-canonical
        # synthetic. A pure recompute path would not see this; a column-read
        # path will.
        conn = get_system_db()
        try:
            conn.execute(
                "UPDATE store_entities SET synthetic_name = ? WHERE id = ?",
                ["manual-override-xyz", eid],
            )
        finally:
            conn.close()
        r = web_client.get(f"/api/store/entities/{eid}", cookies=cookies)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["synthetic_name"] == "manual-override-xyz"
        assert body["invocation_name"] == "manual-override-xyz"


class TestStoreSecurityFixes:
    """Regression tests for the three security blockers and one correctness
    bug found in PR #180 review (F1, F2, F4, F5)."""

    def test_video_url_javascript_scheme_rejected_on_create(self, web_client):
        """F1 — `javascript:` URI must not be stored. Otherwise a malicious
        uploader can pop XSS in any viewer's session via the
        store_detail "Watch video" link."""
        _, cookies = _create_user(web_client, "f1a@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("vid1"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC, "video_url": "javascript:alert(1)"},
            cookies=cookies,
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == "invalid_video_url"

    def test_video_url_data_scheme_rejected(self, web_client):
        _, cookies = _create_user(web_client, "f1b@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("vid2"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC, "video_url": "data:text/html,<script>alert(1)</script>"},
            cookies=cookies,
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "invalid_video_url"

    def test_video_url_https_accepted(self, web_client):
        _, cookies = _create_user(web_client, "f1c@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("vid3"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC, "video_url": "https://www.youtube.com/watch?v=abc"},
            cookies=cookies,
        )
        assert r.status_code == 201, r.text
        assert r.json()["video_url"] == "https://www.youtube.com/watch?v=abc"

    def test_video_url_javascript_scheme_rejected_on_update(self, web_client):
        _, cookies = _create_user(web_client, "f1d@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("vid4"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=cookies,
        )
        eid = c.json()["id"]
        u = web_client.put(
            f"/api/store/entities/{eid}",
            data={"video_url": "javascript:alert(1)"},
            cookies=cookies,
        )
        assert u.status_code == 400
        assert u.json()["detail"] == "invalid_video_url"

    def test_zip_bomb_uncompressed_size_rejected(self, tmp_path):
        """F2 — _safe_zip_extract must refuse when the sum of declared
        file_size across infolist() exceeds MAX_ZIP_UNCOMPRESSED, BEFORE
        extractall touches disk.

        We test the helper directly because Python's ``ZipFile.writestr``
        rewrites ``ZipInfo.file_size`` to the real payload length, making
        an end-to-end ZIP-with-fake-size impossible without manual header
        surgery. The bomb defense is in ``_safe_zip_extract``, so target
        it directly with a stub ZipFile whose ``infolist()`` returns
        entries with inflated declared sizes.
        """
        from fastapi import HTTPException

        from app.api import store as store_module

        class _FakeZipFile:
            def __init__(self, infolist):
                self._infolist = infolist
                self.extracted = False

            def infolist(self):
                return self._infolist

            def extractall(self, dest):
                # Must not be reached — the guard is supposed to raise
                # before extractall. Mark and let the caller assert.
                self.extracted = True

        zi = zipfile.ZipInfo("code-review/SKILL.md")
        zi.file_size = store_module.MAX_ZIP_UNCOMPRESSED + 1
        zf = _FakeZipFile([zi])

        try:
            store_module._safe_zip_extract(zf, tmp_path)
        except HTTPException as exc:
            assert exc.status_code == 413
            assert "zip_too_large_uncompressed" in str(exc.detail)
        else:
            raise AssertionError("expected HTTPException 413, got none")
        assert zf.extracted is False, "guard fired AFTER extractall — bug in fix"

    def test_admin_can_update_non_owned_entity(self, web_client):
        """F4 — UPDATE must permit owner OR admin (parity with DELETE)."""
        from argon2 import PasswordHasher
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from tests.helpers.auth import grant_admin

        owner_id, owner_cookies = _create_user(web_client, "owner-f4@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("f4-skill"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=owner_cookies,
        )
        eid = c.json()["id"]

        ph = PasswordHasher()
        conn = get_system_db()
        UserRepository(conn).create(
            id="adm-f4", email="adm-f4@x.com", name="adm",
            password_hash=ph.hash("AdminPass1!"),
        )
        grant_admin(conn, "adm-f4")
        admin_token = web_client.post(
            "/auth/token", json={"email": "adm-f4@x.com", "password": "AdminPass1!"}
        ).json()["access_token"]
        admin_cookies = {"access_token": admin_token}

        u = web_client.put(
            f"/api/store/entities/{eid}",
            data={"description": "moderated by admin"},
            cookies=admin_cookies,
        )
        assert u.status_code == 200, u.text
        assert u.json()["description"] == "moderated by admin"

    def test_non_owner_non_admin_cannot_update(self, web_client):
        """F4 negative — random user still gets 403 on UPDATE."""
        _, owner_cookies = _create_user(web_client, "owner-f4b@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("f4b-skill"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=owner_cookies,
        )
        eid = c.json()["id"]
        _, intruder_cookies = _create_user(web_client, "intruder-f4@x.com")
        u = web_client.put(
            f"/api/store/entities/{eid}",
            data={"description": "hijack"},
            cookies=intruder_cookies,
        )
        assert u.status_code == 403
        assert u.json()["detail"] == "not_owner"

    def test_admin_sees_action_buttons_on_marketplace_flea_detail(self, web_client):
        """F4 (v32+ port): admin must see owner-actions panel on the
        unified /marketplace/flea/{id} detail page even when not the
        owner. Original test targeted the now-deleted /store/{id};
        the policy itself is unchanged."""
        from argon2 import PasswordHasher
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from tests.helpers.auth import grant_admin

        _, owner_cookies = _create_user(web_client, "owner-ui@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("ui-skill"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=owner_cookies,
        )
        eid = c.json()["id"]

        ph = PasswordHasher()
        conn = get_system_db()
        UserRepository(conn).create(
            id="adm-ui", email="adm-ui@x.com", name="adm",
            password_hash=ph.hash("AdminPass1!"),
        )
        grant_admin(conn, "adm-ui")
        admin_token = web_client.post(
            "/auth/token", json={"email": "adm-ui@x.com", "password": "AdminPass1!"}
        ).json()["access_token"]
        admin_cookies = {"access_token": admin_token}

        page = web_client.get(f"/marketplace/flea/{eid}", cookies=admin_cookies)
        assert page.status_code == 200, page.text
        # Admin-non-owner sees the owner-actions panel.
        assert "owner-actions" in page.text
        # v35: admin sees Archive (soft) + Hard delete buttons.
        assert 'id="owner-archive-btn"' in page.text
        assert 'id="owner-hard-delete-btn"' in page.text

    def test_cross_owner_suffix_collision_rejected(self, web_client):
        """F5 — two emails can sanitize to the same username
        (alice.smith / alice_smith → alice-smith). Both uploading a skill
        called `code-review` would yield the same `code-review-by-alice-smith`
        and silently collide in the served bundle + manifest. The upload
        endpoint must refuse the second one."""
        _, a_cookies = _create_user(web_client, "alice.smith@x.com")
        r1 = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("collide"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=a_cookies,
        )
        assert r1.status_code == 201, r1.text
        assert r1.json()["invocation_name"] == "collide-by-alice-smith"

        _, b_cookies = _create_user(web_client, "alice_smith@x.com")
        r2 = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("collide"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=b_cookies,
        )
        assert r2.status_code == 409, r2.text
        assert r2.json()["detail"] == "conflict_global_suffix"

    def test_scratch_dir_cleaned_up_after_failed_extraction(self, web_client, monkeypatch, tmp_path):
        """Devin: ZIP-validation failure inside _safe_zip_extract was leaving
        the ``agnes_store_*`` scratch dir on disk because scratch creation
        and cleanup lived in different try/finally scopes. After the fix
        both share one outer try/finally; assert the dir really is gone.

        Issue #252: redirect ``tempfile.mkdtemp()`` to a per-test ``tmp_path``
        via ``monkeypatch.setattr(tempfile, "tempdir", ...)`` so the
        ``agnes_store_*`` glob is scoped to this test's exclusive directory.
        Pre-#252 the glob ran against the shared system tmp and would flake
        when a sibling pytest-xdist worker's store test happened to be
        mid-creation inside the [before, after] window.
        """
        import tempfile as _tempfile
        from pathlib import Path as _Path

        # FastAPI app runs in-process under TestClient → patching the
        # tempfile module here also redirects the server-side mkdtemp call.
        monkeypatch.setattr(_tempfile, "tempdir", str(tmp_path))

        # A ZIP whose only member traverses out of the destination —
        # _safe_zip_extract raises 422 zip_unsafe_path before it touches
        # extractall. That's the simplest trigger that exits via
        # HTTPException without doing anything to scratch.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../escape.txt", "boom")
        bad_zip = buf.getvalue()

        tmp_root = tmp_path
        before = {p.name for p in tmp_root.glob("agnes_store_*")}

        _, cookies = _create_user(web_client, "leak@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("bad.zip", bad_zip, "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=cookies,
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == "zip_unsafe_path"

        after = {p.name for p in tmp_root.glob("agnes_store_*")}
        leaked = after - before
        assert not leaked, f"scratch dir leaked: {leaked}"

    def test_distinct_suffixes_pass(self, web_client):
        """F5 — uploads that yield distinct suffixed names must pass. (Avoid
        regressing into rejecting all distinct uploads.)"""
        _, a_cookies = _create_user(web_client, "alice@x.com")
        _, b_cookies = _create_user(web_client, "bob@x.com")
        for cookies, skill_name in [(a_cookies, "alpha"), (b_cookies, "beta")]:
            r = web_client.post(
                "/api/store/entities",
                files={"file": ("s.zip", _make_skill_zip(skill_name), "application/zip")},
                data={"type": "skill", "description": _OK_DESC}, cookies=cookies,
            )
            assert r.status_code == 201, r.text


class TestStoreBundle:
    """GET /api/store/bundle.zip + POST /api/store/import-bundle."""

    def _upload_skill(self, web_client, cookies, name="bundled-skill"):
        return web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=cookies,
        )

    def test_bundle_zip_contains_manifest_and_entity_tree(self, web_client):
        _, cookies = _create_user(web_client, "owner-bundle@x.com")
        r1 = self._upload_skill(web_client, cookies, name="bundle-a")
        r2 = self._upload_skill(web_client, cookies, name="bundle-b")
        eid_a, eid_b = r1.json()["id"], r2.json()["id"]

        bundle = web_client.get("/api/store/bundle.zip", cookies=cookies)
        assert bundle.status_code == 200
        assert bundle.headers["content-type"] == "application/zip"
        assert bundle.headers["x-bundle-entry-count"] == "2"

        with zipfile.ZipFile(io.BytesIO(bundle.content)) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert f"entities/{eid_a}/plugin/skills/bundle-a-by-owner-bundle/SKILL.md" in names
            assert f"entities/{eid_b}/plugin/skills/bundle-b-by-owner-bundle/SKILL.md" in names

            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["format"] == 1
            assert manifest["entry_count"] == 2
            entries_by_id = {e["entity_id"]: e for e in manifest["entries"]}
            assert entries_by_id[eid_a]["owner_email"] == "owner-bundle@x.com"
            assert entries_by_id[eid_a]["name"] == "bundle-a"

    def test_bundle_zip_owner_me_resolves_to_caller(self, web_client):
        """`?owner=me` magic value resolves server-side to the caller's
        user_id, so `agnes store mine` can pull a self-bundle without
        having to look up its own id first."""
        _, alice_cookies = _create_user(web_client, "mine-a@x.com")
        _, bob_cookies = _create_user(web_client, "mine-b@x.com")
        self._upload_skill(web_client, alice_cookies, name="alice-1")
        self._upload_skill(web_client, alice_cookies, name="alice-2")
        self._upload_skill(web_client, bob_cookies, name="bob-1")

        # Alice asks for owner=me → only her two entities.
        r = web_client.get("/api/store/bundle.zip?owner=me", cookies=alice_cookies)
        assert r.status_code == 200
        assert r.headers["x-bundle-entry-count"] == "2"

        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
        assert any("alice-1-by-mine-a" in n for n in names)
        assert any("alice-2-by-mine-a" in n for n in names)
        assert not any("bob-1-by-mine-b" in n for n in names)

    def test_bundle_zip_filters(self, web_client):
        _, cookies = _create_user(web_client, "filter@x.com")
        self._upload_skill(web_client, cookies, name="keep-this")
        web_client.post(
            "/api/store/entities",
            files={"file": ("p.zip", _make_plugin_zip("filter-out"), "application/zip")},
            data={"type": "plugin", "description": _OK_DESC}, cookies=cookies,
        )

        only_skill = web_client.get(
            "/api/store/bundle.zip?type=skill", cookies=cookies,
        )
        assert only_skill.headers["x-bundle-entry-count"] == "1"

    def test_import_bundle_round_trip_preserves_entity(self, web_client, tmp_path):
        from argon2 import PasswordHasher
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from tests.helpers.auth import grant_admin

        # Source instance: create entity, pull bundle.
        _, owner_cookies = _create_user(web_client, "src-owner@x.com")
        r = self._upload_skill(web_client, owner_cookies, name="rt-skill")
        eid = r.json()["id"]
        bundle_bytes = web_client.get(
            "/api/store/bundle.zip", cookies=owner_cookies,
        ).content

        # Wipe Store DB rows + on-disk dir to simulate empty target.
        conn = get_system_db()
        conn.execute("DELETE FROM store_entities WHERE id = ?", [eid])
        import shutil as _shutil
        _shutil.rmtree(tmp_path / "store" / eid, ignore_errors=True)

        # Promote a different user to admin and import.
        ph = PasswordHasher()
        UserRepository(conn).create(
            id="adm-bundle", email="adm-bundle@x.com", name="adm",
            password_hash=ph.hash("AdminPass1!"),
        )
        grant_admin(conn, "adm-bundle")
        admin_token = web_client.post(
            "/auth/token", json={"email": "adm-bundle@x.com", "password": "AdminPass1!"}
        ).json()["access_token"]
        admin_cookies = {"access_token": admin_token}

        imp = web_client.post(
            "/api/store/import-bundle",
            files={"file": ("b.zip", bundle_bytes, "application/zip")},
            data={"mode": "merge"},
            cookies=admin_cookies,
        )
        assert imp.status_code == 200, imp.text
        body = imp.json()
        assert body["imported"] == 1
        assert body["replaced"] == 0
        # Owner email matched existing user (src-owner@x.com), no stub needed.
        assert body["stub_users_created"] == 0

        # Entity should be present again.
        r2 = web_client.get(f"/api/store/entities/{eid}", cookies=admin_cookies)
        assert r2.status_code == 200
        assert r2.json()["name"] == "rt-skill"
        assert (tmp_path / "store" / eid / "plugin" / "skills" / "rt-skill-by-src-owner" / "SKILL.md").is_file()

    def test_import_bundle_creates_stub_for_unknown_owner(self, web_client, tmp_path):
        """When the bundle's owner_email is not in users table, server
        creates a disabled stub so the entity row has a valid owner_user_id.
        """
        from argon2 import PasswordHasher
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from tests.helpers.auth import grant_admin

        _, owner_cookies = _create_user(web_client, "vanishing@x.com")
        r = self._upload_skill(web_client, owner_cookies, name="orphan-skill")
        eid = r.json()["id"]
        bundle_bytes = web_client.get(
            "/api/store/bundle.zip", cookies=owner_cookies,
        ).content

        # Delete the owner + the entity (simulate fresh target instance).
        conn = get_system_db()
        conn.execute("DELETE FROM store_entities WHERE id = ?", [eid])
        # We can't easily delete users via repo (no method), so just rename
        # so email lookup misses. Brute SQL.
        conn.execute("UPDATE users SET email = 'gone@x.com' WHERE email = 'vanishing@x.com'")
        import shutil as _shutil
        _shutil.rmtree(tmp_path / "store" / eid, ignore_errors=True)

        ph = PasswordHasher()
        UserRepository(conn).create(
            id="adm-stub", email="adm-stub@x.com", name="adm",
            password_hash=ph.hash("AdminPass1!"),
        )
        grant_admin(conn, "adm-stub")
        admin_token = web_client.post(
            "/auth/token", json={"email": "adm-stub@x.com", "password": "AdminPass1!"}
        ).json()["access_token"]
        admin_cookies = {"access_token": admin_token}

        imp = web_client.post(
            "/api/store/import-bundle",
            files={"file": ("b.zip", bundle_bytes, "application/zip")},
            data={"mode": "merge"},
            cookies=admin_cookies,
        )
        assert imp.status_code == 200, imp.text
        body = imp.json()
        assert body["imported"] == 1
        assert body["stub_users_created"] == 1

        stub = conn.execute(
            "SELECT id, active FROM users WHERE email = 'vanishing@x.com'"
        ).fetchone()
        assert stub is not None
        assert stub[0].startswith("imported-")
        assert stub[1] is False  # disabled

    def test_import_bundle_skip_mode_keeps_existing(self, web_client):
        from argon2 import PasswordHasher
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from tests.helpers.auth import grant_admin

        _, owner_cookies = _create_user(web_client, "skip@x.com")
        r = self._upload_skill(web_client, owner_cookies, name="skip-existing")
        eid = r.json()["id"]
        bundle_bytes = web_client.get(
            "/api/store/bundle.zip", cookies=owner_cookies,
        ).content

        conn = get_system_db()
        ph = PasswordHasher()
        UserRepository(conn).create(
            id="adm-skip", email="adm-skip@x.com", name="adm",
            password_hash=ph.hash("AdminPass1!"),
        )
        grant_admin(conn, "adm-skip")
        admin_token = web_client.post(
            "/auth/token", json={"email": "adm-skip@x.com", "password": "AdminPass1!"}
        ).json()["access_token"]
        admin_cookies = {"access_token": admin_token}

        # Import without wiping → entity already present → mode=skip
        # should report 1 skipped, 0 imported, 0 replaced.
        imp = web_client.post(
            "/api/store/import-bundle",
            files={"file": ("b.zip", bundle_bytes, "application/zip")},
            data={"mode": "skip"},
            cookies=admin_cookies,
        )
        assert imp.status_code == 200
        assert imp.json() == {
            "imported": 0, "replaced": 0, "skipped": 1,
            "stub_users_created": 0, "errors": [],
        }

    def test_import_bundle_admin_only(self, web_client):
        _, cookies = _create_user(web_client, "non-admin@x.com")
        # Build the smallest valid bundle: just manifest.json + no entries.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps({
                "format": 1, "generated_at": "2026-01-01T00:00:00Z",
                "entry_count": 0, "entries": [],
            }))
        r = web_client.post(
            "/api/store/import-bundle",
            files={"file": ("b.zip", buf.getvalue(), "application/zip")},
            data={"mode": "merge"}, cookies=cookies,
        )
        # require_admin denies non-admin with 403.
        assert r.status_code == 403, r.text

    def test_bundle_zip_filters_quarantined_for_non_owner(
        self, web_client, monkeypatch,
    ):
        """Codex adversarial review [CRITICAL]: GET /bundle.zip used
        ``repo.list(...)`` without a visibility filter. An
        authenticated non-admin could download pending / blocked v1
        bytes by hitting the bundle endpoint. Fixed by mirroring the
        browse-listing gate: non-admin sees only ``approved`` (plus
        their own non-approved entries)."""
        from src.repositories.store_entities import StoreEntitiesRepository

        # Owner uploads a clean skill (lands approved with guardrails off).
        owner_id, owner_cookies = _create_user(web_client, "bundle-owner@x.com")
        r = self._upload_skill(web_client, owner_cookies, name="bundle-public")
        eid_public = r.json()["id"]

        from src.db import get_system_db
        # Owner also has a SECOND skill that we manually flip to
        # visibility=pending (simulating in-review).
        r = self._upload_skill(web_client, owner_cookies, name="bundle-pending")
        eid_pending = r.json()["id"]
        conn = get_system_db()
        StoreEntitiesRepository(conn).set_visibility(eid_pending, "pending")
        conn.close()

        # Snoop is a different non-admin user.
        _, snoop_cookies = _create_user(web_client, "bundle-snoop@x.com")
        r = web_client.get("/api/store/bundle.zip", cookies=snoop_cookies)
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
        # Snoop sees the approved entity ...
        assert any(f"entities/{eid_public}/" in n for n in names), (
            "approved entity must be present in bundle"
        )
        # ... but NEVER the pending one.
        assert not any(f"entities/{eid_pending}/" in n for n in names), (
            "non-admin must NOT see pending entities via bundle.zip"
        )
        # Manifest entry count reflects the filter.
        manifest = json.loads(
            zipfile.ZipFile(io.BytesIO(r.content)).read("manifest.json"),
        )
        manifest_ids = {e["entity_id"] for e in manifest["entries"]}
        assert eid_public in manifest_ids
        assert eid_pending not in manifest_ids

    def test_bundle_zip_owner_sees_own_pending(self, web_client):
        """Owner-of-pending sees their own non-approved entries in
        their bundle export (matches the browse-listing affordance
        via include_owner_id)."""
        from src.repositories.store_entities import StoreEntitiesRepository

        from src.db import get_system_db
        owner_id, owner_cookies = _create_user(web_client, "bundle-mine@x.com")
        r = self._upload_skill(web_client, owner_cookies, name="mine-pending")
        eid = r.json()["id"]
        conn = get_system_db()
        StoreEntitiesRepository(conn).set_visibility(eid, "pending")
        conn.close()

        r = web_client.get("/api/store/bundle.zip", cookies=owner_cookies)
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
        assert any(f"entities/{eid}/" in n for n in names), (
            "owner must see their OWN pending entity in their bundle"
        )

    def test_bundle_zip_admin_sees_all(self, web_client):
        """Admin sees pending entries from other users too."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from tests.helpers.auth import grant_admin

        from src.db import get_system_db
        owner_id, owner_cookies = _create_user(web_client, "bundle-other-owner@x.com")
        r = self._upload_skill(web_client, owner_cookies, name="other-pending")
        eid = r.json()["id"]
        conn = get_system_db()
        StoreEntitiesRepository(conn).set_visibility(eid, "pending")
        conn.close()

        _, admin_cookies = _create_user(web_client, "bundle-admin@x.com")
        conn = get_system_db()
        grant_admin(conn, "bundle-admin")
        conn.close()

        r = web_client.get("/api/store/bundle.zip", cookies=admin_cookies)
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
        assert any(f"entities/{eid}/" in n for n in names), (
            "admin must see pending entities from any owner"
        )


class TestInstallCycle:
    def test_install_uninstall_and_count(self, web_client):
        # Owner uploads, two other users install, install_count = 2.
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("share"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
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

    def test_owner_delete_archives_but_preserves_existing_installs(self, web_client):
        """v35 soft-delete semantics. Owner DELETE = soft archive. Bundle
        + install rows preserved so already-installed users keep getting
        the plugin through marketplace.zip / .git. The installer still
        sees the entity in their My AI Stack with an 'Archived' badge."""
        _, owner_cookies = _create_user(web_client, "o2@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("cascade"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        eid = r.json()["id"]
        _, u_cookies = _create_user(web_client, "victim@x.com")
        web_client.post(f"/api/store/entities/{eid}/install", cookies=u_cookies)

        # Owner soft-archives (default DELETE semantics in v35).
        d = web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)
        assert d.status_code == 200

        # Detail still reachable for owner — visibility flipped, not deleted.
        det = web_client.get(f"/api/store/entities/{eid}", cookies=owner_cookies).json()
        assert det["visibility_status"] == "archived"

        # Installer's My AI Stack STILL contains the entity (existing
        # install survives archive — that's the whole point).
        ms = web_client.get("/api/my-stack", cookies=u_cookies).json()
        assert any(e["entity_id"] == eid for e in ms["store"]), (
            "archived entity must remain in existing installer's stack"
        )
        archived_entry = next(e for e in ms["store"] if e["entity_id"] == eid)
        assert archived_entry["visibility_status"] == "archived"

    def test_admin_hard_delete_cascades_installs(self, web_client):
        """v35 hard delete (admin only): bundle dropped + install rows
        cascade. Existing users lose the plugin on next sync."""
        _, owner_cookies = _create_user(web_client, "owner-hd@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("cascade-hd"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        eid = r.json()["id"]
        _, u_cookies = _create_user(web_client, "victim-hd@x.com")
        web_client.post(f"/api/store/entities/{eid}/install", cookies=u_cookies)

        # Admin hard-deletes via ?hard=true.
        from argon2 import PasswordHasher
        from src.db import get_system_db as _gdb
        from src.repositories.users import UserRepository
        from tests.helpers.auth import grant_admin
        ph = PasswordHasher()
        conn = _gdb()
        UserRepository(conn).create(id="adm-hd", email="adm-hd@x.com", name="adm",
                                    password_hash=ph.hash("AdminPass1!"))
        grant_admin(conn, "adm-hd")
        conn.close()
        admin_token = web_client.post(
            "/auth/token", json={"email": "adm-hd@x.com", "password": "AdminPass1!"}
        ).json()["access_token"]
        d = web_client.delete(
            f"/api/store/entities/{eid}?hard=true",
            cookies={"access_token": admin_token},
        )
        assert d.status_code == 200, d.text

        # GET 404 + install row gone.
        assert web_client.get(
            f"/api/store/entities/{eid}", cookies=owner_cookies,
        ).status_code == 404
        ms = web_client.get("/api/my-stack", cookies=u_cookies).json()
        assert all(e["entity_id"] != eid for e in ms["store"])

    def test_non_owner_cannot_delete(self, web_client):
        _, owner_cookies = _create_user(web_client, "o3@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("guarded"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
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
            data={"type": "skill", "description": _OK_DESC}, cookies=owner_cookies,
        ).json()["id"]
        skill_b = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("beta"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=owner_cookies,
        ).json()["id"]
        agent_c = web_client.post(
            "/api/store/entities",
            files={"file": ("a.zip", _make_agent_zip("gamma"), "application/zip")},
            data={"type": "agent", "description": _OK_DESC}, cookies=owner_cookies,
        ).json()["id"]
        plugin_d = web_client.post(
            "/api/store/entities",
            files={"file": ("p.zip", _make_plugin_zip("delta"), "application/zip")},
            data={"type": "plugin", "description": _OK_DESC}, cookies=owner_cookies,
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
            data={"type": "skill", "description": _OK_DESC}, cookies=owner_cookies,
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
            data={"type": "skill", "description": _OK_DESC}, cookies=owner_cookies,
        ).json()["id"]
        b = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("dropme"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=owner_cookies,
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
    def test_store_upload_page_renders(self, web_client):
        _, cookies = _create_user(web_client, "page2@x.com")
        r = web_client.get("/store/new", cookies=cookies)
        assert r.status_code == 200
        assert "Upload" in r.text

    def test_marketplace_flea_detail_page_renders(self, web_client):
        """v32+: /store/{id} was deleted; /marketplace/flea/{id} is the
        canonical detail surface.

        v49 phase-2: SSR pre-render uses ``entity.title`` (humanized)
        rather than the kebab-case entity ``name`` for the page heading.
        Both the friendly + technical forms should be present in the
        page (title in the hero / breadcrumbs, slug in JS data / detail
        URL parameter passed to fetch).
        """
        _, cookies = _create_user(web_client, "page4@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("page-skill"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=cookies,
        )
        eid = r.json()["id"]
        det = web_client.get(f"/marketplace/flea/{eid}", cookies=cookies)
        assert det.status_code == 200
        # Humanized title sits in the hero h1 + browser title.
        assert "Page Skill" in det.text
        # Entity id (slug-equivalent for routing) survives in detail URL.
        assert eid in det.text
        # Confirm the legacy URL is gone (404, not 200).
        legacy = web_client.get(f"/store/{eid}", cookies=cookies)
        assert legacy.status_code == 404


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

    def test_subscribe_toggle_updates_my_stack(self, web_client):
        """Model B: granted plugins start unsubscribed (`enabled=False`).
        Toggling enabled=True writes a subscription; enabled=False removes it."""
        from src.db import get_system_db
        user_id, cookies = _create_user(web_client, "stack@x.com")
        conn = get_system_db()
        self._seed_admin_grant(conn, user_id=user_id, marketplace="mkt-x", plugin="alpha")
        conn.close()

        ms = web_client.get("/api/my-stack", cookies=cookies).json()
        assert len(ms["curated"]) == 1
        assert ms["curated"][0]["enabled"] is False  # default unsubscribed

        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": True},
            cookies=cookies,
        )
        assert r.status_code == 200

        ms2 = web_client.get("/api/my-stack", cookies=cookies).json()
        assert ms2["curated"][0]["enabled"] is True

        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": False},
            cookies=cookies,
        )
        assert r.status_code == 200
        ms3 = web_client.get("/api/my-stack", cookies=cookies).json()
        assert ms3["curated"][0]["enabled"] is False

    def test_grant_delete_drops_subscriptions(self, web_client):
        """The admin grant-delete hook must clean up everyone's subscriptions
        so a re-grant restarts at the default (unsubscribed)."""
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
            json={"enabled": True}, cookies=user_cookies,
        )
        assert r.status_code == 200

        # Admin deletes the grant.
        d = web_client.delete(f"/api/admin/grants/{grant_id}", cookies=admin_cookies)
        assert d.status_code == 204, d.text

        # Subscription should be gone too.
        from src.repositories.user_curated_subscriptions import (
            UserCuratedSubscriptionsRepository,
        )
        conn = get_system_db()
        try:
            assert UserCuratedSubscriptionsRepository(conn).is_subscribed(
                user_id, "mkt-y", "beta",
            ) is False
        finally:
            conn.close()
