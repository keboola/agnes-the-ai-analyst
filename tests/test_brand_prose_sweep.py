"""Brandable product name — a rebranded instance (`instance.brand` /
`AGNES_INSTANCE_BRAND`) must show its own name in PROSE across the web UI,
with no hardcoded "Agnes" leaking through on a representative page set.

Renders chat, home (not-onboarded), login, and how-it-works with a custom
brand and asserts:
  (a) the custom brand appears in the rendered prose;
  (b) the literal "Agnes" does NOT appear in that prose.

"Prose" excludes `<script>`, `<style>`, `<code>`, and `<pre>` content — CLI
examples, JS identifiers (`window.AgnesTime`), asset paths
(`agnes-orb.png`), and the setup-script clipboard payload (a separate,
intentionally CLI-flavored surface, always rendered inside <pre>/<code> or a
<script> array — see `_claude_setup_instructions.jinja`) are not product-name
prose and are exempt by construction, not by name-matching.

A companion test pins the opposite invariant: a page documenting CLI usage
must still show the lowercase `agnes` command untouched — the sweep must not
have clobbered a technical identifier while chasing prose.
"""

from __future__ import annotations

import re

import pytest

CUSTOM_BRAND = "Zenith Analytics"

_CODE_LIKE_RE = re.compile(r"<(script|style|code|pre)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _prose(html: str) -> str:
    """Strip script/style/code/pre content AND HTML comments, leaving only
    rendered prose. HTML comments (e.g. `_app_scripts.html`'s `<!-- window.
    AgnesTime ... -->` doc header) are never visible to a page reader —
    same exemption as a Jinja `{# ... #}` comment, just a different
    delimiter that survives into the response body."""
    return _HTML_COMMENT_RE.sub("", _CODE_LIKE_RE.sub("", html))


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def branded(seeded_app, monkeypatch):
    """`seeded_app` + a custom brand + an explicit chat grant for Admin.

    The grant matters beyond RBAC: the rail's "Set up {brand}" onboarding
    widget (`_app_rail.html`) only renders when the viewer holds an
    EXPLICIT chat grant (`can_chat`), not merely admin god-mode — see
    `_compute_can_chat`'s docstring. Granting it here is what actually
    exercises that widget's brand-templated title/aria-label on `/chat`,
    instead of silently skipping it.
    """
    monkeypatch.setenv("AGNES_INSTANCE_BRAND", CUSTOM_BRAND)
    monkeypatch.delenv("AGNES_INSTANCE_BRAND_SHORT", raising=False)

    from types import SimpleNamespace

    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository

    conn = get_system_db()
    try:
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
        ResourceGrantsRepository(conn).create(admin_gid, "chat", "chat", assigned_by="test")
    finally:
        conn.close()

    # `seeded_app`'s TestClient is never used as a context manager, so the
    # app's lifespan (which normally sets app.state.chat_config) never runs —
    # same workaround as tests/test_chat_page_no_grant.py.
    seeded_app["client"].app.state.chat_config = SimpleNamespace(enabled=True)

    return seeded_app


def _representative_pages(branded) -> dict:
    """One rendered response per representative page.

    ``/login`` needs ``?error=deactivated``: its brand-templated copy lives
    in a conditional error banner (`{% if _err and _err_messages.get(_err)
    %}`) — the plain unauthenticated page renders neither the brand nor the
    literal "Agnes" anywhere in prose (the hero H1 deliberately shows
    ``instance_name``, the deployment label, not the product brand — see
    ``get_instance_name`` vs. ``get_instance_brand``'s docstrings), so
    hitting it without an error would test nothing.
    """
    client = branded["client"]
    return {
        "chat": client.get("/chat", headers=_auth(branded["admin_token"])),
        "home_not_onboarded": client.get("/home", headers=_auth(branded["analyst_token"])),
        "login": client.get("/login?error=deactivated"),
        "how_it_works": client.get("/how-it-works", headers=_auth(branded["analyst_token"])),
    }


@pytest.mark.parametrize(
    "page_name",
    ["chat", "home_not_onboarded", "login", "how_it_works"],
)
def test_custom_brand_appears_in_prose(branded, page_name):
    resp = _representative_pages(branded)[page_name]
    assert resp.status_code == 200, resp.text
    assert CUSTOM_BRAND in _prose(resp.text)


@pytest.mark.parametrize(
    "page_name",
    ["chat", "home_not_onboarded", "login", "how_it_works"],
)
def test_no_literal_agnes_in_prose(branded, page_name):
    resp = _representative_pages(branded)[page_name]
    assert resp.status_code == 200, resp.text
    prose = _prose(resp.text)
    assert "Agnes" not in prose, (
        f"literal 'Agnes' leaked into rendered prose on /{page_name} with AGNES_INSTANCE_BRAND={CUSTOM_BRAND!r} set"
    )


def test_cli_command_examples_survive_the_brand_sweep(branded):
    """The lowercase `agnes` binary name in a CLI example is a technical
    identifier, not the product name — the prose sweep must not have
    touched it. `/how-it-works` documents `agnes catalog` inside a
    `<pre>` block."""
    resp = branded["client"].get("/how-it-works", headers=_auth(branded["analyst_token"]))
    assert resp.status_code == 200
    assert "agnes catalog" in resp.text
