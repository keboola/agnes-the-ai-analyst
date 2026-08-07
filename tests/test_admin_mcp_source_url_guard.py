"""The admin API applies the source-url policy — and applies it in the right order.

``src/net/mcp_source_url.py`` owns WHERE the line falls (pinned by
``test_mcp_source_url_policy.py``). This module pins that the endpoints
actually consult it, and the two things about the wiring that are easy to get
wrong and silent when wrong:

* the check must run on the MERGED row, so a patch that only flips ``transport``
  — making an already-stored url live for the first time — is judged too;
* it must run BEFORE the credential purge, or a request that is about to 400
  destroys the vault secret and every analyst's per-user secret on its way out.

``stdio`` is exempt by design: there the secret goes into the subprocess
environment and ``url`` is never dialed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from src.db import get_system_db
from src.repositories.mcp_sources import MCPSourceRepository


def _auth(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['admin_token']}"}


def _post(seeded_app, **over):
    body = {"name": "src_ok", "transport": "http", "url": "https://mcp.vendor.example/mcp"}
    body.update(over)
    return seeded_app["client"].post("/api/admin/mcp-sources", headers=_auth(seeded_app), json=body)


def _seed(source_id, **over):
    row = {
        "id": source_id,
        "name": source_id,
        "transport": "http",
        "url": "https://mcp.vendor.example/mcp",
    }
    row.update(over)
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(**row)
    conn.close()


# ── create ──────────────────────────────────────────────────────────────────


def test_create_refuses_the_metadata_endpoint(seeded_app):
    """The url from #1154. A literal IP, so this holds with no DNS at all."""
    r = _post(seeded_app, name="src_meta", url="http://169.254.169.254/mcp")
    assert r.status_code == 400, r.text
    assert "blocked_range" in r.json()["detail"]


def test_create_refuses_cleartext_to_a_public_address(seeded_app):
    r = _post(seeded_app, name="src_clear", url="http://93.184.216.34/mcp")
    assert r.status_code == 400
    assert "cleartext_http_to_public_address" in r.json()["detail"]


def test_create_allows_an_internal_source(seeded_app):
    """The case the strict reading would have broken: an organization's own
    MCP server on the intranet."""
    r = _post(seeded_app, name="src_internal", url="http://10.10.0.7:8080/mcp")
    assert r.status_code == 201, r.text


def test_create_refuses_a_non_http_scheme(seeded_app):
    r = _post(seeded_app, name="src_file", url="file:///etc/passwd")
    assert r.status_code == 400


def test_stdio_source_is_exempt(seeded_app):
    """`url` on a stdio row is inert documentation — the secret rides the
    subprocess environment and nothing ever dials it."""
    r = _post(
        seeded_app,
        name="src_stdio",
        transport="stdio",
        command="/usr/bin/mcp-thing",
        url="http://169.254.169.254/whatever",
    )
    assert r.status_code == 201, r.text


# ── update ──────────────────────────────────────────────────────────────────


def test_update_refuses_repointing_at_the_metadata_endpoint(seeded_app):
    _seed("src_up1")
    r = seeded_app["client"].put(
        "/api/admin/mcp-sources/src_up1",
        headers=_auth(seeded_app),
        json={"url": "http://169.254.169.254/mcp"},
    )
    assert r.status_code == 400
    assert "blocked_range" in r.json()["detail"]


def test_update_judges_the_merged_row_not_just_the_patch(seeded_app):
    """Flipping stdio→http makes a stored url live for the first time. The
    patch mentions only `transport`, so a check reading the payload alone
    would wave it through — and the credential would start being sent as an
    Authorization header to the metadata endpoint."""
    _seed("src_up2", transport="stdio", command="/bin/thing", url="http://169.254.169.254/mcp")
    r = seeded_app["client"].put(
        "/api/admin/mcp-sources/src_up2",
        headers=_auth(seeded_app),
        json={"transport": "http"},
    )
    assert r.status_code == 400, r.text
    assert "blocked_range" in r.json()["detail"]


def test_a_refused_url_does_not_purge_credentials(seeded_app, monkeypatch):
    """Repointing a url purges every stored credential for the source. That
    purge must not fire for a request that then 400s — the admin would lose
    the vault secret and every analyst's per-user secret to a typo."""
    from cryptography.fernet import Fernet

    from src.repositories import per_user_secrets_repo, shared_secrets_repo

    # Storing a secret at all needs the vault key; without it the repos refuse
    # and this test would pass for the wrong reason (nothing to purge).
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())

    _seed("src_up3")
    shared_secrets_repo().upsert("src_up3", "shared-token-value")
    per_user_secrets_repo().upsert("src_up3", "u1", "per-user-token-value")
    assert shared_secrets_repo().has("src_up3")

    r = seeded_app["client"].put(
        "/api/admin/mcp-sources/src_up3",
        headers=_auth(seeded_app),
        json={"url": "http://169.254.169.254/mcp"},
    )
    assert r.status_code == 400

    assert shared_secrets_repo().has("src_up3"), "vault secret destroyed by a request that was refused"
    assert per_user_secrets_repo().list_for_source("src_up3") == ["u1"], (
        "per-user secret destroyed by a request that was refused"
    )
    conn = get_system_db()
    still = MCPSourceRepository(conn).get("src_up3")
    conn.close()
    assert still["url"] == "https://mcp.vendor.example/mcp", "row was repointed despite the 400"


def test_unrelated_edit_survives_an_unresolvable_host(seeded_app):
    """The reason an unresolvable host is a warning rather than a refusal: the
    check runs over the merged row, so refusing here would block a rename
    whenever DNS hiccuped."""
    _seed("src_up4", url="https://not-up-yet.example/mcp")
    r = seeded_app["client"].put(
        "/api/admin/mcp-sources/src_up4",
        headers=_auth(seeded_app),
        json={"name": "src_up4_renamed"},
    )
    assert r.status_code == 200, r.text
