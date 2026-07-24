"""``bq_fqn`` must be load-bearing in the two remaining path-construction
sites (issue #343 follow-up).

``bq_fqn`` (v51, issue #343) added a per-row fully-qualified BigQuery path so
a registry row can point at a dataset (and a project) that differs from the
single configured ``data_source.bigquery.project``.
``connectors/bigquery/extractor.py`` already honors it when building master
views.

Two sites never got the same treatment and still hard-code the configured
project:

* ``app/api/v2_scan.py:_build_bq_sql`` builds
  ``\\`{project_id}.{bucket}.{source_table}\\``` for scan / estimate /
  ``agnes snapshot create``.
* ``app/api/query.py:_rewrite_bq_table_refs_to_native`` rewrites bare
  registered names to ``\\`{project}.{bucket}.{source_table}\\``` for
  ``--remote`` execution and dry-run.

Consequence: a row whose data lives in another project resolves to
``<configured-project>.<dataset>.<table>``, which does not exist. The table is
unqueryable by every user, and because the cross-project guard rejects
explicit full paths in user SQL, there is no workaround an analyst can apply.

Both sites keep the legacy ``bucket`` + ``source_table`` + configured-project
behaviour when ``bq_fqn`` is absent (pre-v51 registrations).
"""

import pytest

from app.api.query import _rewrite_bq_table_refs_to_native
from app.api.v2_scan import ScanRequest, _build_bq_sql


# ---------------------------------------------------------------------------
# v2_scan._build_bq_sql
# ---------------------------------------------------------------------------


class TestBuildBqSqlHonorsBqFqn:
    def test_bq_fqn_overrides_project_dataset_and_table(self):
        """A row carrying ``bq_fqn`` must scan the bq_fqn path, not the
        configured-project + bucket + source_table triplet."""
        row = {
            "bucket": "events_ds",
            "source_table": "events",
            "bq_fqn": "data-project.events_ds.events",
        }
        req = ScanRequest(table_id="events", limit=10)

        sql = _build_bq_sql(row, "configured-project", req)

        assert "`data-project.events_ds.events`" in sql
        assert "configured-project" not in sql

    def test_bq_fqn_dataset_may_differ_from_bucket_label(self):
        """``bq_fqn`` decouples the UX/RBAC ``bucket`` label from the
        physical dataset (the original issue #343 motivation)."""
        row = {
            "bucket": "Marketing",  # friendly label, not a dataset
            "source_table": "ignored_legacy_name",
            "bq_fqn": "other-project.real_dataset.real_table",
        }
        req = ScanRequest(table_id="t")

        sql = _build_bq_sql(row, "configured-project", req)

        assert "`other-project.real_dataset.real_table`" in sql
        assert "Marketing" not in sql
        assert "ignored_legacy_name" not in sql

    def test_no_bq_fqn_falls_back_to_legacy_triplet(self):
        """Pre-v51 rows (no ``bq_fqn``) keep the existing behaviour."""
        row = {
            "bucket": "finance",
            "source_table": "orders",
            "bq_fqn": None,
        }
        req = ScanRequest(table_id="orders")

        sql = _build_bq_sql(row, "configured-project", req)

        assert "`configured-project.finance.orders`" in sql

    def test_malformed_bq_fqn_is_rejected(self):
        """A malformed ``bq_fqn`` must raise rather than silently fall back
        to the legacy path and scan the wrong table."""
        row = {
            "bucket": "ds",
            "source_table": "tbl",
            "bq_fqn": "not.enough",  # two segments
        }
        req = ScanRequest(table_id="t")

        with pytest.raises(ValueError):
            _build_bq_sql(row, "configured-project", req)

    def test_bq_fqn_still_applies_select_where_and_limit(self):
        """Overriding the path must not disturb the rest of the builder."""
        row = {
            "bucket": "b",
            "source_table": "t",
            "bq_fqn": "project-two.d2.t2",
        }
        req = ScanRequest(table_id="t", select=["event_date"], limit=5)

        sql = _build_bq_sql(row, "project-one", req, safe_where="event_date IS NOT NULL")

        assert sql.startswith("SELECT `event_date` FROM `project-two.d2.t2`")
        assert "WHERE event_date IS NOT NULL" in sql
        assert sql.endswith("LIMIT 5")


# ---------------------------------------------------------------------------
# query._rewrite_bq_table_refs_to_native
# ---------------------------------------------------------------------------


class TestRewriterHonorsPerRowProject:
    def test_bare_name_uses_per_row_project_override(self):
        """A 4-tuple ``name_lookups`` entry carries an explicit project for
        that row; the bare name must rewrite to that project."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT COUNT(*) FROM events",
            [("events", "events_ds", "events", "data-project")],
            "configured-project",
        )

        assert "`data-project.events_ds.events`" in sql
        assert "configured-project" not in sql

    def test_legacy_three_tuple_still_uses_configured_project(self):
        """Backwards compat: existing callers pass 3-tuples and must keep
        resolving against the single configured project."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT * FROM ue",
            [("ue", "fin", "ue")],
            "some-prj",
        )

        assert "`some-prj.fin.ue`" in sql

    def test_none_project_override_falls_back_to_configured_project(self):
        """A 4-tuple whose override is ``None`` (row without ``bq_fqn``)
        behaves exactly like the 3-tuple form."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT * FROM ue",
            [("ue", "fin", "ue", None)],
            "some-prj",
        )

        assert "`some-prj.fin.ue`" in sql

    def test_mixed_overrides_resolve_independently(self):
        """A query joining a same-project row and a cross-project row must
        produce two different projects in one rewrite pass."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT * FROM orders JOIN events ON TRUE",
            [
                ("orders", "finance", "orders", None),
                ("events", "events_ds", "events", "data-project"),
            ],
            "configured-project",
        )

        assert "`configured-project.finance.orders`" in sql
        assert "`data-project.events_ds.events`" in sql


# ---------------------------------------------------------------------------
# The collection sites must actually populate the override from ``bq_fqn``.
# A helper that accepts the 4-tuple is inert until callers emit it.
# ---------------------------------------------------------------------------


class _FakeRepo:
    def __init__(self, rows):
        self._rows = rows

    def list_by_source(self, source_type):
        return [r for r in self._rows if r.get("source_type") == source_type]

    def list_all(self):
        return list(self._rows)

    def find_by_bq_path(self, dataset, table):
        for r in self._rows:
            if r.get("bucket") == dataset and r.get("source_table") == table:
                return r
        return None


_CROSS_PROJECT_ROW = {
    "id": "bq.events_ds.events",
    "name": "events",
    "source_type": "bigquery",
    "query_mode": "remote",
    "bucket": "events_ds",
    "source_table": "events",
    "bq_fqn": "data-project.events_ds.events",
}


class TestCollectionSitesEmitProjectOverride:
    def test_guardrail_inputs_carry_bq_fqn_project(self, monkeypatch):
        """``_bq_guardrail_inputs`` builds the ``name_lookups`` the dry-run
        rewriter consumes. A row with ``bq_fqn`` must contribute the
        bq_fqn project so the dry-run estimates the real table."""
        import app.api.query as query_mod

        monkeypatch.setattr(
            query_mod,
            "table_registry_repo",
            lambda: _FakeRepo([_CROSS_PROJECT_ROW]),
        )
        monkeypatch.setattr(query_mod, "is_user_admin", lambda *a, **k: True)

        sql = "SELECT COUNT(*) FROM events"
        _dry_run, name_lookups, blocked = query_mod._bq_guardrail_inputs(
            sql, sql.lower(), None, {"id": "u", "email": "u@example.com"}, None
        )

        assert blocked is None
        assert len(name_lookups) == 1
        assert name_lookups[0][:3] == ("events", "events_ds", "events")
        assert name_lookups[0][3] == "data-project"

    def test_remote_execution_plan_targets_bq_fqn_project(self, monkeypatch):
        """The ``--remote`` execution path must push a BQ-native inner SQL
        that names the bq_fqn project, not the configured data project."""
        import app.api.query as query_mod

        monkeypatch.setattr(
            query_mod,
            "table_registry_repo",
            lambda: _FakeRepo([_CROSS_PROJECT_ROW]),
        )

        class _Projects:
            data = "configured-project"
            billing = "billing-project"

        class _Bq:
            projects = _Projects()

        monkeypatch.setattr(query_mod, "get_bq_access", lambda: _Bq())

        rewritten, did_rewrite, billing, inner = query_mod._bq_remote_execution_plan(
            "SELECT COUNT(*) FROM events", None
        )

        assert did_rewrite is True
        assert billing == "billing-project"
        assert "`data-project.events_ds.events`" in inner
        assert "configured-project" not in inner
