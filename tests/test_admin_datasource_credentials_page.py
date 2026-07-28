"""Tests for /admin/datasource-credentials after the admin secrets/data-sources
UX consolidation.

Keboola project connect/browse/register/default/rotate now lives entirely on
/admin/data-sources (see tests/test_admin_data_sources_page.py). This page
keeps Google Workspace + BigQuery instance-secret management and a callout
pointing at /admin/data-sources for Keboola.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestDatasourceCredentialsPageAuth:
    def test_admin_can_load_page(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.cookies.set("access_token", token)
        try:
            resp = c.get("/admin/datasource-credentials", headers={"Accept": "text/html"})
        finally:
            c.cookies.clear()
            _reset_ephemeral_key_for_tests()
        assert resp.status_code == 200, resp.text
        body = resp.text

        # Retitled to kill the name collision with /admin/data-sources.
        assert "Instance secrets" in body
        assert "<title>Instance secrets" in body

        # GWS + BigQuery cards untouched.
        assert 'id="gws-card"' in body
        assert 'id="bq-card"' in body
        assert "Google Workspace" in body
        assert "BigQuery" in body

        # Keboola project management fully removed — ported to /admin/data-sources.
        assert "kbc-add-btn" not in body
        assert "kbc-projects-list" not in body
        assert "kbc-add-modal" not in body
        assert "Add Keboola project" not in body
        assert "setKbcDefault" not in body
        assert "toggleRotate" not in body
        assert "saveRotatedToken" not in body
        assert "_renderConnectionCard" not in body
        assert "loadKbcConnections" not in body

        # Callout pointing to the new home for Keboola projects.
        assert "Keboola projects moved" in body
        assert '/admin/data-sources">Data sources</a>' in body

    def test_non_admin_cannot_load_page(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/admin/datasource-credentials", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauthenticated_redirects(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin/datasource-credentials", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
