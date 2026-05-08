"""End-to-end smoke tests for v32 endpoints + Flea allowlist behavior.

* GET /marketplace/format-guide — auth-gated (any logged-in user, no admin),
  renders the markdown source.
* /api/marketplace/curated/.../asset/{path} — path-traversal guard, RBAC.
* Flea upload allowlist on /api/store/entities (photo + doc).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest


# --- /marketplace/format-guide -------------------------------------------


def test_format_guide_requires_login(seeded_app):
    """Anonymous user gets redirected (302) to /login — no public access."""
    client = seeded_app["client"]
    r = client.get("/marketplace/format-guide", follow_redirects=False)
    # The guide endpoint is wrapped by the same auth dependency the rest of
    # /marketplace/* uses; the exact response is a 302/303 to /login or a
    # 401 depending on the auth provider. Either way it's not a 200.
    assert r.status_code in (302, 303, 307, 401)


def test_format_guide_renders_for_logged_in_user(seeded_app):
    """Any logged-in user (admin in this seeded fixture) sees the rendered page.

    The rendered HTML must:
    * carry the curator-focused title (post-walkthrough rewrite),
    * include the explicit "Curated Marketplace channel only" disclaimer
      that distinguishes this guide from the Flea wizard,
    * render the fenced JSON example as a <pre><code> block.
    """
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.get("/marketplace/format-guide", headers=headers)
    assert r.status_code == 200
    body = r.text
    # Title points at curators (rewrite from the original "format guide").
    assert "Curated Marketplace" in body
    # Disclaimer making the curated/flea split explicit.
    assert "Flea Market" in body
    # JSON examples render as code blocks.
    assert "<pre>" in body or "<code" in body


# --- /api/store/entities — Flea allowlist enforcement --------------------


@pytest.fixture
def _flea_zip_for_skill(tmp_path):
    """Return the bytes of a minimal valid skill .zip for /entities upload.

    Mirrors the layout `app/api/store.py::_validate_and_extract_metadata`
    expects: a single SKILL.md at the top of the archive with frontmatter
    declaring `name` + `description`.
    """
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "SKILL.md",
            "---\nname: testskill\ndescription: A test skill\n---\nbody\n",
        )
    return buf.getvalue()


def test_flea_doc_upload_rejects_docx(seeded_app, _flea_zip_for_skill):
    """v32: Flea doc upload allowlist — .docx is rejected with 415."""
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.post(
        "/api/store/entities",
        headers=headers,
        files=[
            ("file", ("skill.zip", _flea_zip_for_skill, "application/zip")),
            ("docs", ("notes.docx", b"PKfake-docx-content", "application/vnd.openxmlformats")),
        ],
        data={"type": "skill", "version": "1.0"},
    )
    assert r.status_code == 415
    assert "unsupported_doc_type" in r.text or "doc_extension" in r.text


def test_flea_doc_upload_accepts_pdf(seeded_app, _flea_zip_for_skill):
    """A real PDF (with %PDF magic bytes) makes it through both extension +
    body validation."""
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    pdf_body = b"%PDF-1.4\n% comment\n"
    r = client.post(
        "/api/store/entities",
        headers=headers,
        files=[
            ("file", ("skill.zip", _flea_zip_for_skill, "application/zip")),
            ("docs", ("setup.pdf", pdf_body, "application/pdf")),
        ],
        data={"type": "skill", "version": "1.0"},
    )
    assert r.status_code == 201, r.text


def test_flea_doc_upload_rejects_pdf_with_bad_magic_bytes(seeded_app, _flea_zip_for_skill):
    """Defense in depth: a file named .pdf but with non-PDF body bytes is
    rejected by the magic-bytes check."""
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.post(
        "/api/store/entities",
        headers=headers,
        files=[
            ("file", ("skill.zip", _flea_zip_for_skill, "application/zip")),
            ("docs", ("evil.pdf", b"not a pdf at all", "application/pdf")),
        ],
        data={"type": "skill", "version": "1.0"},
    )
    assert r.status_code == 415
    assert "magic_bytes" in r.text or "unsupported" in r.text


def test_flea_photo_upload_rejects_svg(seeded_app, _flea_zip_for_skill):
    """SVG photos are rejected — extension check (svg not in allowlist) fires
    first and returns 415 with photo_unsupported_format."""
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.post(
        "/api/store/entities",
        headers=headers,
        files=[
            ("file", ("skill.zip", _flea_zip_for_skill, "application/zip")),
            ("photo", ("logo.svg", b"<svg></svg>", "image/svg+xml")),
        ],
        data={"type": "skill", "version": "1.0"},
    )
    assert r.status_code == 415
    assert "photo_unsupported_format" in r.text


# --- /api/marketplace/curated/.../asset path-traversal guard -------------


def test_curated_asset_endpoint_blocks_path_traversal(seeded_app, monkeypatch, tmp_path):
    """Hitting the asset endpoint with `../` segments must return 404 (not the
    file outside the marketplace dir). The route's ``Path.resolve(strict=True)
    + is_relative_to`` guard is the load-bearing check."""
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    # Stand up a tiny on-disk marketplace at the configured marketplaces dir.
    data_dir = Path(seeded_app["env"]["data_dir"])
    repo_root = data_dir / "marketplaces" / "test-mp"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "ok.txt").write_text("safe", encoding="utf-8")

    # Register the marketplace (with curator) + grant Admin access to its
    # synthetic plugin so the resource_grants check passes.
    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="test-mp", name="Test", url="https://example.com/x.git",
            curator_name="C", curator_email="c@example.com",
        )
        # Insert a plugin row so the RBAC check has a target. Resource id is
        # `<slug>/<plugin>`.
        conn.execute(
            "INSERT OR REPLACE INTO marketplace_plugins "
            "(marketplace_id, name) VALUES ('test-mp', 'demo')"
        )
        admin_group = conn.execute(
            "SELECT id FROM user_groups WHERE name = 'Admin'"
        ).fetchone()
        if admin_group:
            try:
                ResourceGrantsRepository(conn).create(
                    group_id=admin_group[0],
                    resource_type="marketplace_plugin",
                    resource_id="test-mp/demo",
                    assigned_by="test",
                )
            except Exception:
                pass  # already granted from a prior test run
    finally:
        conn.close()

    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    # Sanity: a normal in-tree fetch returns 200.
    r = client.get(
        "/api/marketplace/curated/test-mp/demo/asset/ok.txt",
        headers=headers,
    )
    assert r.status_code == 200
    assert r.text == "safe"

    # Traversal attempt: `..` segments. FastAPI's path param accepts the
    # value; our ``_path_under`` resolves it then checks containment, so the
    # result is 404 (not 200, not 500).
    r = client.get(
        "/api/marketplace/curated/test-mp/demo/asset/../escape.txt",
        headers=headers,
    )
    assert r.status_code == 404


# --- Content-Disposition force-download on /doc and /mirrored/docs --------


def _seed_marketplace_with_doc(seeded_app, *, slug="dl-mp", plugin="demo"):
    """Helper: register a marketplace, drop a sample doc into its working
    tree, and grant Admin RBAC. Returns the data_dir for further fixturing."""
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    data_dir = Path(seeded_app["env"]["data_dir"])
    repo_root = data_dir / "marketplaces" / slug
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "docs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (repo_root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id=slug, name=slug, url=f"https://example.com/{slug}.git",
            curator_name="C", curator_email="c@example.com",
        )
        conn.execute(
            "INSERT OR REPLACE INTO marketplace_plugins "
            "(marketplace_id, name) VALUES (?, ?)", [slug, plugin],
        )
        admin_group = conn.execute(
            "SELECT id FROM user_groups WHERE name = 'Admin'"
        ).fetchone()
        if admin_group:
            try:
                ResourceGrantsRepository(conn).create(
                    group_id=admin_group[0],
                    resource_type="marketplace_plugin",
                    resource_id=f"{slug}/{plugin}",
                    assigned_by="test",
                )
            except Exception:
                pass
    finally:
        conn.close()
    return data_dir


def test_curated_doc_endpoint_sets_attachment_disposition(seeded_app):
    """/doc serves as `Content-Disposition: attachment` so clicks download
    the file rather than open it inline. /asset stays inline (covers belong
    in <img>, not as downloads)."""
    _seed_marketplace_with_doc(seeded_app)
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    # Doc → attachment + filename hint
    r = client.get(
        "/api/marketplace/curated/dl-mp/demo/doc/docs/guide.md",
        headers=headers,
    )
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "").lower()
    assert "guide.md" in r.headers.get("content-disposition", "")

    # Asset (cover image) → no attachment header (renders inline)
    r = client.get(
        "/api/marketplace/curated/dl-mp/demo/asset/logo.png",
        headers=headers,
    )
    assert r.status_code == 200
    assert "attachment" not in r.headers.get("content-disposition", "").lower()


def test_curated_doc_endpoint_rejects_non_allowlist_extension(seeded_app):
    """The /doc endpoint refuses to serve a file whose extension isn't in
    the allowlist (PDF / Markdown / plain text). Defense-in-depth even when
    the parser lets a curator's exotic path slip through."""
    data_dir = _seed_marketplace_with_doc(seeded_app)
    # Plant a .docx file in the seeded marketplace
    (data_dir / "marketplaces" / "dl-mp" / "docs" / "evil.docx").write_text(
        "PKfake", encoding="utf-8",
    )
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.get(
        "/api/marketplace/curated/dl-mp/demo/doc/docs/evil.docx",
        headers=headers,
    )
    assert r.status_code == 415
    assert "unsupported_doc_extension" in r.text
