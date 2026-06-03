"""Template-content assertions for the MCP-source admin UI (Phase 2).

Cheap, deterministic checks that the create/edit forms surface the new
``env`` + ``scope`` fields, relabel the legacy ``auth_secret_env`` path,
drop the misleading help text, and that the detail page carries the
write-only vault-secret control + a list secret-status badge.
"""
from pathlib import Path

TPL = Path("app/web/templates")


def _read(name):
    return (TPL / name).read_text()


def test_create_form_has_env_and_scope_and_legacy_label():
    html = _read("admin_mcp_sources.html")
    assert 'id="new-env"' in html              # env KEY=VALUE textarea
    assert 'id="new-scope"' in html            # scope selector
    assert "legacy" in html.lower()            # auth_secret_env relabelled as legacy/advanced
    # the misleading claim is gone
    assert "value itself is not stored in the db" not in html.lower()


def test_detail_form_has_env_scope_and_vault_secret_controls():
    html = _read("admin_mcp_source_detail.html")
    assert 'id="edit-env"' in html
    assert 'id="edit-scope"' in html
    assert 'id="set-vault-secret"' in html     # secret value input
    assert "/secret" in html                   # PUT/DELETE vault secret endpoint used by JS
    assert "legacy" in html.lower()


def test_list_shows_secret_status():
    html = _read("admin_mcp_sources.html")
    assert "has_vault_secret" in html          # list JS reads the flag to render a badge
