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

    def test_empty_state_names_connections_that_lack_a_master_token(self, seeded_app):
        """A connected Keboola project with no master token must be named.

        "No Keboola projects have a master token configured yet" was the whole
        empty state, and to an admin looking at a working, table-syncing
        connection it reads as "your project isn't connected" — the master
        token is a separate vault slot the wizard never fills, so this is the
        state EVERY wizard-connected instance starts in.
        """
        from src.repositories import source_connections_repo

        source_connections_repo().create(
            id="conn-storage-only",
            name="Acme Warehouse",
            source_type="keboola",
            config={"stack_url": "https://connection.keboola.com"},
            is_default=True,
            created_by="test",
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/semantic-layer", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text

        assert "Acme Warehouse" in body
        assert "/admin/data-sources" in body
        # The old wording claimed nothing is connected; it must not come back
        # while a connection exists.
        assert "No Keboola projects are connected yet" not in body

    def test_tokenless_connections_stay_visible_once_another_project_has_a_token(self, seeded_app, vault_key):
        """The mixed case: one project set up, another not.

        Listing tokenless connections only in the empty state hid them the
        moment ANY project got a master token — such a connection is not a
        Sources row and is not orphaned either (unless it happens to own
        previously-imported rows), so it appeared nowhere on the page. Same
        silent state this page set out to remove, one level up. Devin Review
        on #1242.
        """
        from src.repositories import source_connections_repo

        _make_master_connection(
            "conn-ready",
            name="Configured Project",
            stack_url="https://connection.keboola.com",
            token="master-tok",
            is_default=True,
        )
        source_connections_repo().create(
            id="conn-tokenless",
            name="Forgotten Project",
            source_type="keboola",
            config={"stack_url": "https://connection.keboola.com"},
            is_default=False,
            created_by="test",
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        body = c.get("/admin/semantic-layer", headers=_auth(token)).text

        # The Sources table renders (a project IS configured) ...
        assert "Configured Project" in body
        assert "One row per Keboola project with a master token" in body
        # ... and the one that will never sync is still named.
        assert "Forgotten Project" in body

    def test_a_tokened_connection_without_a_stack_url_says_so(self, seeded_app, vault_key):
        """"No master token" is only one of three reasons a connection is
        skipped. Telling an admin to add a token they already added — while
        the real cause is a missing stack URL — sends them to fix the wrong
        thing. Devin Review on #1242."""
        from app.api.admin_source_connections import master_secret_key
        from src.repositories import connection_secrets_repo, source_connections_repo

        source_connections_repo().create(
            id="conn-nostack",
            name="Half Built Project",
            source_type="keboola",
            config={},
            is_default=True,
            created_by="test",
        )
        connection_secrets_repo().upsert(master_secret_key("conn-nostack"), "master-tok")

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        body = c.get("/admin/semantic-layer", headers=_auth(token)).text

        assert "Half Built Project" in body
        assert "no stack URL" in body
        # Must NOT claim the token is missing — it isn't.
        assert "Half Built Project</strong> — no master (owner) token" not in body

    def test_orphan_row_naming_a_live_connection_shows_its_name(self, seeded_app):
        """An orphaned ref that still matches a connection is not a mystery
        UUID — it is "this project lost its master token", and the page must
        say so instead of printing a bare id nobody can act on."""
        from src.repositories import metric_repo, source_connections_repo

        source_connections_repo().create(
            id="conn-lost-master",
            name="Acme Warehouse",
            source_type="keboola",
            config={"stack_url": "https://connection.keboola.com"},
            is_default=True,
            created_by="test",
        )
        metric_repo().create(
            id="revenue/stranded",
            name="stranded",
            display_name="Stranded",
            category="revenue",
            sql="SELECT 1",
            source="keboola_semantic_layer",
            source_ref="conn-lost-master",
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        body = c.get("/admin/semantic-layer", headers=_auth(token)).text

        assert "orphan" in body.lower()
        assert "Acme Warehouse" in body
        assert "master token missing" in body

    def test_skipped_unresolved_metrics_are_surfaced_with_their_tables(self, seeded_app, vault_key):
        """The skip counter must reach a human.

        Verified on a live instance: a sync reported 9 glossary terms and 0
        metrics, and the reason — 50 metrics dropped because 12 datasets point
        at tables nobody registered — existed only as a number in the API
        response. Neither the page nor the log named a single table.
        """
        from app.api import keboola_semantic_layer_refresh as endpoint_module

        _make_master_connection(
            "conn-a",
            name="Production Project",
            stack_url="https://connection.keboola.com",
            token="master-tok",
            is_default=True,
        )
        endpoint_module._refresh_state["last_result"] = {
            "status": "ok",
            "sources": [
                {
                    "connection_id": "conn-a",
                    "status": "ok",
                    "created_or_updated": 0,
                    "glossary_created_or_updated": 9,
                    "skipped_unresolved_table": 50,
                    "unresolved_tables": ["in.c-demo.customers", "in.c-demo.orders_demo"],
                }
            ],
        }

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        body = c.get("/admin/semantic-layer", headers=_auth(token)).text

        assert "50" in body
        assert "in.c-demo.customers" in body
        assert "in.c-demo.orders_demo" in body
        assert "Browse &amp; register tables" in body or "Browse & register tables" in body

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

    def test_semantic_layer_page_null_rows_surface_when_default_has_no_master_token(self, seeded_app):
        """NULL-``source_ref`` legacy rows normally fold into the default
        connection's row (see test_semantic_layer_page_renders_sources). But
        when the default Keboola connection has NO master token, it's never
        enumerated by ``_enumerate_master_sources()`` and never appears in
        ``sources`` — so the fold never happens. Before the fix, the truthy
        ``source_ref`` filter for "orphaned" rows also excluded NULL refs,
        so these rows appeared nowhere on the page. They must now surface as
        a distinct "legacy / unattributed" row in the orphaned section."""
        from src.repositories import metric_repo, source_connections_repo

        # A default Keboola connection that has NO master token at all — it
        # is a valid source_connections row (so _default_keboola_connection()
        # resolves it) but is absent from _enumerate_master_sources().
        source_connections_repo().create(
            id="conn-no-master",
            name="No Master Token Project",
            source_type="keboola",
            config={"stack_url": "https://connection.keboola.com"},
            is_default=True,
            created_by="test",
        )

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

        assert "orphan" in body.lower()
        assert "legacy / unattributed" in body
        # Not folded into any per-connection row — none is rendered, since the
        # only connection has no master token. The Sources *table* is what must
        # stay absent; the connection's NAME now appears deliberately, in the
        # empty state, so an admin can see which project is missing a token
        # instead of reading "no projects are configured" next to a project
        # they just connected.
        assert "One row per Keboola project with a master token" not in body
        assert "No Master Token Project" in body


class TestTheUnresolvedTableListSaysWhenItIsASubset:
    """Devin Review on this PR: the list is capped at 20 per project.

    The page presents it as the set to go and register, so an admin on a
    project with many unregistered tables could register everything shown,
    sync again, and still lose metrics with nothing naming the rest.
    """

    def test_a_truncated_list_says_how_many_there_are(self, seeded_app, vault_key):
        from app.api import keboola_semantic_layer_refresh as endpoint_module

        _make_master_connection(
            "conn-a",
            name="Production Project",
            stack_url="https://connection.keboola.com",
            token="master-tok",
            is_default=True,
        )
        listed = [f"in.c-demo.t{i}" for i in range(20)]
        endpoint_module._refresh_state["last_result"] = {
            "status": "ok",
            "sources": [
                {
                    "connection_id": "conn-a",
                    "status": "ok",
                    "created_or_updated": 0,
                    "skipped_unresolved_table": 120,
                    "unresolved_tables": listed,
                    "unresolved_tables_total": 57,
                }
            ],
        }

        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text

        assert "Showing 20 of 57" in body, "the page presents a capped list as the complete set"
        assert "will not be enough" in body

    def test_a_complete_list_carries_no_subset_note(self, seeded_app, vault_key):
        """The note must not appear when the list IS everything."""
        from app.api import keboola_semantic_layer_refresh as endpoint_module

        _make_master_connection(
            "conn-b",
            name="Small Project",
            stack_url="https://connection.keboola.com",
            token="master-tok",
            is_default=True,
        )
        endpoint_module._refresh_state["last_result"] = {
            "status": "ok",
            "sources": [
                {
                    "connection_id": "conn-b",
                    "status": "ok",
                    "created_or_updated": 0,
                    "skipped_unresolved_table": 3,
                    "unresolved_tables": ["in.c-demo.a", "in.c-demo.b"],
                    "unresolved_tables_total": 2,
                }
            ],
        }

        body = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"])).text

        assert "in.c-demo.a" in body
        assert "Showing" not in body or "of 2" not in body

    def test_the_sync_result_reports_the_true_total(self):
        """The payload must carry the count even though the list is cut."""
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[1] / "connectors" / "keboola" / "semantic_layer.py"
        ).read_text(encoding="utf-8")
        assert '"unresolved_tables_total": len(unresolved_tables),' in src
        cut = src.index('"unresolved_tables": unresolved_tables[:_MAX_REPORTED_UNRESOLVED_TABLES]')
        tot = src.index('"unresolved_tables_total"')
        assert tot > cut, "the total must sit alongside the truncated list"

    def test_the_page_renders_the_subset_note(self):
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[1]
            / "app"
            / "web"
            / "templates"
            / "admin_semantic_layer.html"
        ).read_text(encoding="utf-8")
        assert "unresolved_tables_total > unresolved_tables|length" in src
        assert ".sl-note {" in src, "the note class must be styled, not bare"
