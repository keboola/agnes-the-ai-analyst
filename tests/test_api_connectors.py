"""Integration tests for the connectors API.

Covers:
  * GET /api/connectors/manifest auth gate (401 when no token)
  * 200 + bundled connectors when no IWT configured
  * source flag flips between iwt / bundled
  * GET /api/connectors/params returns shape with globals + per-connector
    blocks parsed from instance.yaml overlay
  * Auth-required (no anonymous access)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_admin(monkeypatch, tmp_path: Path):
    """Boot the FastAPI app against a temp DATA_DIR + bootstrap an admin
    user, return (client, token).
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Disable LLM guardrails so the test boot doesn't warn about API keys.
    monkeypatch.setenv("AGNES_DISABLE_GUARDRAILS", "1")

    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/auth/bootstrap",
        json={
            "email": "admin@example.com",
            "name": "Admin",
            "password": "TestPass123!",
        },
    )
    if resp.status_code == 403:
        # Users already exist on a re-run — skip; admin tests do this on fresh DBs only.
        pytest.skip("admin already bootstrapped")
    assert resp.status_code == 200, resp.text
    return client, resp.json()["access_token"]


def test_manifest_requires_auth(client_with_admin):
    client, _token = client_with_admin
    resp = client.get("/api/connectors/manifest")
    # No Authorization header → 401 (FastAPI auth dependency rejects)
    assert resp.status_code in (401, 403)


def test_manifest_returns_bundled_when_no_iwt(client_with_admin):
    """Fresh install (no Initial Workspace Template configured) → manifest
    sources from the bundled seed inside the wheel. The bundle ships the
    three canonical connectors (asana, atlassian, gws).
    """
    client, token = client_with_admin
    resp = client.get(
        "/api/connectors/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == 1
    assert body["source"] == "bundled"
    slugs = sorted(c["slug"] for c in body["connectors"])
    assert slugs == [
        "connector-asana",
        "connector-atlassian",
        "connector-gws",
    ]
    # Sanity-check fields make it through unmolested
    asana = next(c for c in body["connectors"] if c["slug"] == "connector-asana")
    assert asana["display_name"] == "Asana"
    assert asana["estimated_minutes"] > 0
    assert asana["vendor_url"].startswith("https://")


def test_params_empty_when_overlay_absent(client_with_admin):
    """No `connectors:` section in instance.yaml → endpoint returns empty
    params + empty globals. `agnes init` treats this as "use defaults".
    """
    client, token = client_with_admin
    resp = client.get(
        "/api/connectors/params",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == 1
    assert body["params"] == {}
    assert body["globals"] == {}


def test_params_filters_overlay_to_manifest_allowlist(client_with_admin, monkeypatch, caplog):
    """Code review on PR #462: the per-tenant `connectors:` overlay was
    emitted verbatim, so an operator typo (`connector-atlasian:` instead
    of `connector-atlassian:`) would land in the analyst's `.env` as a
    junk slug, polluting it AND silently dropping the real connector's
    params. The manifest is the source of truth for "which slugs
    exist"; everything else is dropped + logged at WARNING.

    `globals:` bypasses the allowlist (it's not slug-scoped) — verify it
    still passes through unchanged.
    """
    client, token = client_with_admin

    # Synthesize the overlay shape `_load_current_instance_yaml` returns.
    # Three keys exercise the three branches: one valid manifest slug
    # (asana — should survive), one typo of a real slug (atlasian —
    # should drop), one completely unrelated key (random-junk — should
    # also drop). globals is non-slug-scoped — should always pass.
    overlay = {
        "connectors": {
            "globals": {"AGNES_INSTANCE_BRAND": "Acme"},
            "connector-asana": {"AGNES_ASANA_PAT_ENV": "AGNES_ASANA_PAT"},
            "connector-atlasian": {"ATLASSIAN_BASE_URL": "https://typo.example"},
            "random-junk": {"X": "Y"},
        },
    }
    monkeypatch.setattr(
        "app.api.admin._load_current_instance_yaml",
        lambda: overlay,
    )

    import logging
    with caplog.at_level(logging.WARNING, logger="app.api.connectors"):
        resp = client.get(
            "/api/connectors/params",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    # Only the manifest-known slug survives. Operator typo + unrelated
    # key are silently dropped (silent FROM the analyst's perspective;
    # noisy in the server log — asserted below).
    assert body["params"] == {
        "connector-asana": {"AGNES_ASANA_PAT_ENV": "AGNES_ASANA_PAT"},
    }
    # globals bypass the allowlist.
    assert body["globals"] == {"AGNES_INSTANCE_BRAND": "Acme"}
    # Server-side warning names BOTH ignored slugs (sorted for stable
    # diagnostic output) so the operator can spot a typo in logs.
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("connector-atlasian" in m and "random-junk" in m for m in warnings), (
        f"expected single warning naming both ignored slugs; got {warnings}"
    )


def test_bundled_seed_files_present():
    """The wheel-resident bundled seed must include the install-prompt
    template + the three connector SKILL.md files. This guards against
    a release that forgot to update src/_bundled_seed/ via
    scripts/sync_bundled_seed.sh.
    """
    from src.initial_workspace import bundled_seed_path

    bundle = bundled_seed_path()
    assert (bundle / "install-prompt" / "template.md.tmpl").is_file()
    for slug in ("connector-asana", "connector-atlassian", "connector-gws"):
        assert (
            bundle / "workspace" / ".claude" / "skills" / slug / "SKILL.md"
        ).is_file(), f"missing bundled {slug}"
    assert (bundle / ".source_ref").is_file()
