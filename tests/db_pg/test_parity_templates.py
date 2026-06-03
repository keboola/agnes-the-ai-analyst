"""Backend-parity tests for the templates cluster (news / welcome / claude_md).

Each test seeds state through the backend-aware factory (so the row lands in
whichever backend is active) and exercises the admin HTTP endpoint via
``seeded_app_both`` — once on DuckDB, once on real Postgres.

Discriminator: duck-pass + pg-fail pinpoints a route that reads system state
through a raw DuckDB conn instead of the factory. duck-pass + pg-pass means the
route is correctly factory-routed (clean).
"""
from __future__ import annotations


def _auth(seeded_app_both, who="admin"):
    return {"Authorization": f"Bearer {seeded_app_both[f'{who}_token']}"}


# ---------------------------------------------------------------------------
# GET /api/admin/news/current — seeded via news_template_repo().save_draft +
# publish_draft. Returns the latest published version.
# ---------------------------------------------------------------------------

def test_news_current_reflects_published_version(seeded_app_both):
    from src.repositories import news_template_repo
    repo = news_template_repo()
    repo.save_draft(intro="Parity intro", content="Parity content", by="admin@test.com")
    repo.publish_draft(by="admin@test.com")

    r = seeded_app_both["client"].get(
        "/api/admin/news/current", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("published") is not False, (
        f"[{seeded_app_both['backend']}] /api/admin/news/current returned no "
        f"published version for a row seeded + published through the factory: {body}"
    )
    assert "Parity intro" in (body.get("intro") or ""), (
        f"[{seeded_app_both['backend']}] published intro missing: {body}"
    )


# ---------------------------------------------------------------------------
# GET /api/admin/news/draft — seeded via news_template_repo().save_draft.
# 404 when no draft exists; should surface the seeded draft on both backends.
# ---------------------------------------------------------------------------

def test_news_draft_reflects_seeded_draft(seeded_app_both):
    from src.repositories import news_template_repo
    news_template_repo().save_draft(
        intro="Draft intro", content="Draft content", by="admin@test.com"
    )

    r = seeded_app_both["client"].get(
        "/api/admin/news/draft", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] /api/admin/news/draft returned "
        f"{r.status_code} for a draft seeded through the factory: {r.text}"
    )
    assert "Draft intro" in (r.json().get("intro") or ""), (
        f"[{seeded_app_both['backend']}] seeded draft intro missing: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/admin/news/versions — seeded via news_template_repo().save_draft.
# Lists all versions (admin browse).
# ---------------------------------------------------------------------------

def test_news_versions_lists_seeded_version(seeded_app_both):
    from src.repositories import news_template_repo
    news_template_repo().save_draft(
        intro="Versioned intro", content="Versioned content", by="admin@test.com"
    )

    r = seeded_app_both["client"].get(
        "/api/admin/news/versions", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    versions = r.json().get("versions", [])
    intros = {v.get("intro") for v in versions}
    assert any("Versioned intro" in (i or "") for i in intros), (
        f"[{seeded_app_both['backend']}] seeded version missing from "
        f"/api/admin/news/versions: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/admin/welcome-template — seeded via welcome_template_repo().set().
# Returns the override content in the `content` field.
# ---------------------------------------------------------------------------

def test_welcome_template_reflects_override(seeded_app_both):
    from src.repositories import welcome_template_repo
    welcome_template_repo().set(
        "PARITY_WELCOME_OVERRIDE", updated_by="admin@test.com"
    )

    r = seeded_app_both["client"].get(
        "/api/admin/welcome-template", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("content") == "PARITY_WELCOME_OVERRIDE", (
        f"[{seeded_app_both['backend']}] /api/admin/welcome-template did not "
        f"return the override seeded through the factory: {body}"
    )


# ---------------------------------------------------------------------------
# GET /api/admin/workspace-prompt-template — seeded via
# claude_md_template_repo().set(). Returns the override content.
# ---------------------------------------------------------------------------

def test_workspace_prompt_template_reflects_override(seeded_app_both):
    from src.repositories import claude_md_template_repo
    claude_md_template_repo().set(
        "PARITY_CLAUDE_MD_OVERRIDE", updated_by="admin@test.com"
    )

    r = seeded_app_both["client"].get(
        "/api/admin/workspace-prompt-template", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("content") == "PARITY_CLAUDE_MD_OVERRIDE", (
        f"[{seeded_app_both['backend']}] /api/admin/workspace-prompt-template did "
        f"not return the override seeded through the factory: {body}"
    )
