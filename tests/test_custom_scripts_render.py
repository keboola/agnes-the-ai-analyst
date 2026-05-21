"""``instance.custom_scripts`` template-render coverage.

Validates that each placement slot in ``base.html`` actually fires:
``head_start`` lands before the first ``<link>`` in ``<head>``,
``head_end`` lands before ``</head>``, and ``body_end`` lands before
``</body>``. Together with ``test_instance_config.py::TestCustomScripts``
(the normalization layer), this covers the yaml-to-rendered-page path
end-to-end.

Hits ``/login`` since it extends ``base.html`` and needs no auth.
"""

from __future__ import annotations

import tempfile

import pytest


@pytest.fixture
def render_client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        from fastapi.testclient import TestClient
        from app.main import app
        yield TestClient(app, follow_redirects=False)


def _patch_scripts(monkeypatch, scripts):
    """Replace ``app.web.router.get_custom_scripts`` with a stub returning
    ``scripts``. router.py binds the import at module load, so patching
    here is what _render_ctx actually sees at call time."""
    import app.web.router as router_mod
    monkeypatch.setattr(router_mod, "get_custom_scripts", lambda: scripts)


def test_no_custom_scripts_renders_no_snippets(render_client, monkeypatch):
    _patch_scripts(monkeypatch, [])
    resp = render_client.get("/login")
    assert resp.status_code == 200
    body = resp.text
    # Sentinel strings used in the other tests — must be absent here.
    assert "AGNES_CUSTOM_SCRIPT_HEAD_START" not in body
    assert "AGNES_CUSTOM_SCRIPT_HEAD_END" not in body
    assert "AGNES_CUSTOM_SCRIPT_BODY_END" not in body


def test_head_end_snippet_lands_before_head_close(render_client, monkeypatch):
    _patch_scripts(monkeypatch, [{
        "name": "marker-io",
        "enabled": True,
        "placement": "head_end",
        "html": "<script>window.AGNES_CUSTOM_SCRIPT_HEAD_END=1;</script>",
    }])
    body = render_client.get("/login").text
    sentinel = "AGNES_CUSTOM_SCRIPT_HEAD_END"
    assert sentinel in body
    snippet_idx = body.index(sentinel)
    head_close_idx = body.index("</head>")
    assert snippet_idx < head_close_idx, "head_end must render before </head>"


def test_head_start_snippet_lands_after_charset_before_first_link(render_client, monkeypatch):
    _patch_scripts(monkeypatch, [{
        "name": "gtm-init",
        "enabled": True,
        "placement": "head_start",
        "html": "<script>window.AGNES_CUSTOM_SCRIPT_HEAD_START=1;</script>",
    }])
    body = render_client.get("/login").text
    sentinel = "AGNES_CUSTOM_SCRIPT_HEAD_START"
    assert sentinel in body
    snippet_idx = body.index(sentinel)
    charset_idx = body.index('<meta charset="UTF-8">')
    viewport_idx = body.index('<meta name="viewport"')
    first_link_idx = body.index("<link")
    head_close_idx = body.index("</head>")
    # HTML5 spec: <meta charset> must appear within the first 1024 bytes.
    # head_start MUST land after both required <meta> tags so a long
    # operator snippet can't push the charset declaration past that window
    # (which would trigger locale-default encoding fallback + historical
    # UTF-7 charset-confusion XSS).
    assert charset_idx < snippet_idx, "head_start must render AFTER <meta charset>"
    assert viewport_idx < snippet_idx, "head_start must render AFTER <meta viewport>"
    # Still before CSS/JS so vendor hooks (e.g. GTM dataLayer init) install
    # before any other script can read them.
    assert snippet_idx < first_link_idx, "head_start must render before first <link>"
    assert snippet_idx < head_close_idx


def test_body_end_snippet_lands_before_body_close(render_client, monkeypatch):
    _patch_scripts(monkeypatch, [{
        "name": "bottom-tag",
        "enabled": True,
        "placement": "body_end",
        "html": "<script>window.AGNES_CUSTOM_SCRIPT_BODY_END=1;</script>",
    }])
    body = render_client.get("/login").text
    sentinel = "AGNES_CUSTOM_SCRIPT_BODY_END"
    assert sentinel in body
    snippet_idx = body.index(sentinel)
    body_close_idx = body.index("</body>")
    head_close_idx = body.index("</head>")
    assert snippet_idx > head_close_idx, "body_end must render after </head>"
    assert snippet_idx < body_close_idx


def test_all_three_placements_render_in_correct_order(render_client, monkeypatch):
    _patch_scripts(monkeypatch, [
        {"name": "a", "enabled": True, "placement": "head_start",
         "html": "<script>window.AGNES_CUSTOM_SCRIPT_HEAD_START=1;</script>"},
        {"name": "b", "enabled": True, "placement": "head_end",
         "html": "<script>window.AGNES_CUSTOM_SCRIPT_HEAD_END=1;</script>"},
        {"name": "c", "enabled": True, "placement": "body_end",
         "html": "<script>window.AGNES_CUSTOM_SCRIPT_BODY_END=1;</script>"},
    ])
    body = render_client.get("/login").text
    head_start_idx = body.index("AGNES_CUSTOM_SCRIPT_HEAD_START")
    head_end_idx = body.index("AGNES_CUSTOM_SCRIPT_HEAD_END")
    body_end_idx = body.index("AGNES_CUSTOM_SCRIPT_BODY_END")
    assert head_start_idx < head_end_idx < body_end_idx
