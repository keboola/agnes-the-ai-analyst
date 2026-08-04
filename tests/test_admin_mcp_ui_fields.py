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
    assert 'id="new-env"' in html  # env KEY=VALUE textarea
    assert 'id="new-scope"' in html  # scope selector
    assert "legacy" in html.lower()  # auth_secret_env relabelled as legacy/advanced
    # the misleading claim is gone
    assert "value itself is not stored in the db" not in html.lower()


def test_detail_form_has_env_scope_and_vault_secret_controls():
    html = _read("admin_mcp_source_detail.html")
    assert 'id="edit-env"' in html
    assert 'id="edit-scope"' in html
    assert 'id="set-vault-secret"' in html  # secret value input
    assert "/secret" in html  # PUT/DELETE vault secret endpoint used by JS
    assert "legacy" in html.lower()


def test_materialize_toast_reports_the_run_outcome():
    """A 200 from /materialize does not mean the tool produced rows: an emptied
    table and a per-tool failure both come back inside the body. The toast used
    to say "Materialize triggered" for all three."""
    html = _read("admin_mcp_source_detail.html")
    assert "empty_upstream" in html  # branches on the extractor's error/table code
    assert "reset to 0 rows" in html  # upstream went empty
    assert "rows materialized" in html  # normal run reports the row count


def test_list_shows_secret_status():
    html = _read("admin_mcp_sources.html")
    assert "has_vault_secret" in html  # list JS reads the flag to render a badge


def test_detail_has_inline_my_connection_panel():
    """per_user sources: the admin can connect + test their OWN credential
    right on the detail page instead of hopping to /me/connections first."""
    html = _read("admin_mcp_source_detail.html")
    assert 'id="my-connection-card"' in html
    assert 'id="myconn-token"' in html
    assert "myconn-save-btn" in html  # ds.button ids appear as macro args in raw template
    assert "myconn-test-btn" in html
    assert "myconn-clear-btn" in html
    assert "/my-secret" in html  # per-user secret API used by JS


def test_admin_connection_card_handles_expired_stored_connection():
    """The admin "Your connection" JS must have a third branch: stored but
    unusable (expired, no refresh path) still offers Disconnect (Devin
    Review on #1130)."""
    html = _read("admin_mcp_source_detail.html")
    assert "Connection expired — reconnect or disconnect" in html


def test_auth_method_selects_offer_oauth():
    """Both the create and edit forms must offer auth_method='oauth' — a
    select without the option silently coerces an oauth source to '' on
    save, flipping auth away from oauth and (by design) purging everyone's
    tokens (Devin Review on #1130)."""
    for name in ("admin_mcp_sources.html", "admin_mcp_source_detail.html"):
        assert 'value="oauth"' in _read(name), name
