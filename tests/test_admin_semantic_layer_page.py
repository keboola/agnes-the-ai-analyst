"""Tests for the /admin/semantic-layer sources page.

Per-connection view of the Keboola semantic-layer sync (#853/#920/#953):
one row per enumerated master-token source with its metric/glossary
counts (NULL-``source_ref`` rows fold into the default connection's row)
plus an "orphaned" section for rows whose ``source_ref`` no longer
matches any enumerated source (connection deleted/rotated away).

Clones the auth/render pattern from tests/test_catalog_semantics_page.py
and tests/test_admin_data_sources_page.py.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests


def _auth(token: str) -> dict:
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


@pytest.fixture
def vault_key(monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


def _make_master_connection(conn_id: str, *, name: str, stack_url: str, token: str, is_default: bool = False):
    from app.api.admin_source_connections import master_secret_key
    from src.repositories import connection_secrets_repo, source_connections_repo

    source_connections_repo().create(
        id=conn_id,
        name=name,
        source_type="keboola",
        config={"stack_url": stack_url},
        is_default=is_default,
        created_by="test",
    )
    connection_secrets_repo().upsert(master_secret_key(conn_id), token)
    return conn_id


class TestSemanticLayerPageAuth:
    def test_semantic_layer_page_requires_admin(self, seeded_app):
        c = seeded_app["client"]

        anon_resp = c.get("/admin/semantic-layer", follow_redirects=False)
        assert anon_resp.status_code in (302, 303, 307)

        token = seeded_app["analyst_token"]
        non_admin_resp = c.get("/admin/semantic-layer", headers=_auth(token))
        assert non_admin_resp.status_code == 403

    def test_admin_can_load_page(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/semantic-layer", headers=_auth(token))
        assert resp.status_code == 200
        assert "Semantic layer" in resp.text


class TestSemanticLayerPageSources:
    def test_semantic_layer_page_renders_sources(self, seeded_app, vault_key):
        from src.repositories import glossary_repo, metric_repo

        _make_master_connection(
            "conn-a",
            name="Production Project",
            stack_url="https://connection.keboola.com",
            token="master-tok",
            is_default=True,
        )

        metric_repo().create(
            id="revenue/mrr",
            name="mrr",
            display_name="MRR",
            category="revenue",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref="conn-a",
        )
        metric_repo().create(
            id="revenue/arr",
            name="arr",
            display_name="ARR",
            category="revenue",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref="conn-a",
        )
        glossary_repo().create(
            id="kb/m/mrr",
            term="MRR",
            definition="…",
            source="keboola_semantic_layer",
            source_ref="conn-a",
        )
        # A NULL-source_ref legacy row belongs to the default connection and
        # folds into its count.
        metric_repo().create(
            id="revenue/legacy",
            name="legacy",
            display_name="Legacy",
            category="revenue",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref=None,
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/semantic-layer", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text

        assert "Production Project" in body
        # 2 conn-a metrics + 1 NULL-ref legacy metric folded into the default row.
        assert '<td class="num">3</td>' in body
        assert '<td class="num">1</td>' in body

    def test_semantic_layer_page_renders_skipped_source_neutrally(self, seeded_app, vault_key):
        """A source whose last-sync entry has status='skipped' (the
        duplicate-project dedupe short-circuit in
        connectors/keboola/semantic_layer.py — carries no 'error' key) must
        render with neutral copy/styling, not as a failure. Regression for
        a review finding: the generic `{% elif s.last %}` fallback treated
        any non-'ok' status as an error and rendered '✗ failed'."""
        from app.api.keboola_semantic_layer_refresh import _record_completion

        _make_master_connection(
            "conn-a",
            name="Production Project",
            stack_url="https://connection.keboola.com",
            token="master-tok",
            is_default=True,
        )
        _record_completion(
            "ok",
            {
                "status": "ok",
                "sources": [
                    {
                        "connection_id": "conn-a",
                        "name": "Production Project",
                        "status": "skipped",
                        "skipped_duplicate_project": 1,
                    }
                ],
            },
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/semantic-layer", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text

        assert "skipped" in body.lower()
        assert 'class="sl-last-skipped"' in body
        # Must NOT be rendered as a failure — the page's CSS/JS always
        # contain the "sl-last-error"/"failed" strings (class definition,
        # toolbar error toast), so scope this to the actual rendered
        # per-source markup: no danger-styled span, no bare "✗".
        assert 'class="sl-last-error"' not in body
        assert "✗" not in body

    def test_semantic_layer_page_empty_state(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/semantic-layer", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text

        assert "/admin/data-sources" in body
        assert "no" in body.lower()

    def test_semantic_layer_page_orphaned_rows(self, seeded_app, vault_key):
        from src.repositories import metric_repo

        _make_master_connection(
            "conn-a",
            name="Production Project",
            stack_url="https://connection.keboola.com",
            token="master-tok",
            is_default=True,
        )
        # This row's source_ref points at a connection that no longer exists
        # (deleted/rotated away) — it must surface as "orphaned", not silently
        # vanish or get attributed to conn-a.
        metric_repo().create(
            id="revenue/ghost",
            name="ghost",
            display_name="Ghost",
            category="revenue",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref="conn-deleted",
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/semantic-layer", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text

        assert "orphan" in body.lower()
        assert "conn-deleted" in body
