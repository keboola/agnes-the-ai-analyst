"""Tests for the /admin/data-sources "Add Keboola project" wizard page (#755).

Covers:
- Auth gate (admin loads, non-admin 403, unauthenticated redirect).
- Page-shell markers the JS hangs off.
- Vault-key-not-configured blocking banner + disabled affordance.
- Vault-key-configured: no banner, affordance enabled.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    """`_refresh_state` is a module-level dict shared with the refresh
    endpoint tests — reset it around every test in this file too."""
    from app.api import keboola_semantic_layer_refresh as endpoint_module

    reset = {
        "run_id": None,
        "started_at": None,
        "last_completed_at": None,
        "last_status": None,
        "last_result": None,
    }
    endpoint_module._refresh_state.update(reset)
    yield
    endpoint_module._refresh_state.update(reset)


class TestDataSourcesPageAuth:
    def test_admin_can_load_page(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.cookies.set("access_token", token)
        try:
            resp = c.get("/admin/data-sources", headers={"Accept": "text/html"})
        finally:
            c.cookies.clear()
            _reset_ephemeral_key_for_tests()
        assert resp.status_code == 200, resp.text
        body = resp.text

        # Hero + nav-distinguishing copy (#755 acceptance: data vs MCP sources
        # legible from the page itself).
        assert "Data sources" in body
        assert "/admin/mcp-sources" in body

        # Page-shell markers the JS targets.
        assert 'id="ds-add-btn"' in body
        assert 'id="ds-conn-list"' in body
        assert 'id="ds-wizard-overlay"' in body
        assert 'id="ds-new-stack"' in body
        assert 'id="ds-new-token"' in body

        # Endpoint constants — guards against URL drift between UI and API.
        assert "/api/admin/source-connections" in body
        assert "/api/admin/register-table" in body

        # Per-card "Set as default" + "Rotate token" controls, ported from
        # the now-retired Keboola section of /admin/datasource-credentials.
        assert "setDefaultConn" in body
        assert "Set as default" in body
        assert "toggleRotate" in body
        assert "Rotate token" in body
        assert 'class="ds-rotate-row"' in body

        # Reciprocal link to the vault-secrets page.
        assert "/admin/datasource-credentials" in body

    def test_non_admin_cannot_load_page(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauthenticated_redirects(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin/data-sources", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)


class TestDataSourcesPageVaultBanner:
    def test_banner_shown_when_vault_key_unset(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
        _reset_ephemeral_key_for_tests()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.cookies.set("access_token", token)
        try:
            resp = c.get("/admin/data-sources")
        finally:
            c.cookies.clear()
            _reset_ephemeral_key_for_tests()
        assert resp.status_code == 200
        body = resp.text
        assert "Vault key not configured" in body
        assert "AGNES_VAULT_KEY" in body
        # The "add" flow is disabled without a vault key.
        assert 'id="ds-add-btn" disabled' in body

    def test_no_banner_when_vault_key_set(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.cookies.set("access_token", token)
        try:
            resp = c.get("/admin/data-sources")
        finally:
            c.cookies.clear()
            _reset_ephemeral_key_for_tests()
        assert resp.status_code == 200
        body = resp.text
        assert "Vault key not configured" not in body
        assert 'id="ds-add-btn" disabled' not in body


class TestDataSourcesPageSemanticLayerSummary:
    """Small summary card: 'Semantic layer: N metrics, M glossary terms
    synced' — surfaces the Keboola semantic-layer sync (#920) result and
    links to /catalog/semantics. Counts are global (metric_definitions /
    glossary_terms carry no per-connection column), scoped to
    source='keboola_semantic_layer' rows only — manual/yaml_import/
    openmetadata rows don't count toward "synced from Keboola".

    #953: the card must always show SOME state (never-synced / ok / error),
    not just after a successful sync with nonzero counts — an admin who
    hasn't synced yet, or whose last attempt failed, needs to see that too.
    """

    def test_never_synced_state_shown_when_nothing_synced_yet(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Semantic layer" in body
        assert "Sync now" in body
        assert 'id="ds-semantic-sync-btn"' in body
        assert "never" in body.lower()

    def test_error_state_shown_after_failed_sync(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import _record_completion

        _record_completion("error", "needs a master token")

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Semantic layer" in body
        assert "failed" in body.lower()
        assert "needs a master token" in body
        # Still offers a retry.
        assert 'id="ds-semantic-sync-btn"' in body

    def test_ok_state_shown_after_successful_sync_even_with_zero_counts(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import _record_completion

        _record_completion("ok", {"status": "ok", "created_or_updated": 0, "pruned": 0})

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Semantic layer" in body
        assert "0 metrics" in body

    def test_card_shows_counts_and_links_after_sync(self, seeded_app):
        from src.repositories import glossary_repo, metric_repo

        metric_repo().create(
            id="revenue/mrr",
            name="mrr",
            display_name="MRR",
            category="revenue",
            sql="SELECT 1",
            source="keboola_semantic_layer",
        )
        metric_repo().create(
            id="revenue/arr",
            name="arr",
            display_name="ARR",
            category="revenue",
            sql="SELECT 1",
            source="keboola_semantic_layer",
        )
        # A manual metric must NOT count toward the "synced from Keboola" total.
        metric_repo().create(
            id="manual/x",
            name="x",
            display_name="X",
            category="manual",
            sql="SELECT 1",
            source="manual",
        )
        glossary_repo().create(id="kb/m/mrr", term="MRR", definition="…", source="keboola_semantic_layer")

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Semantic layer" in body
        assert "2 metrics" in body
        assert "1 glossary term" in body
        assert "/catalog/semantics" in body
