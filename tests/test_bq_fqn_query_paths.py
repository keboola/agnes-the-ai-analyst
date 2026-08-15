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
# Issue #1322: a registered BQ table named after a SQL keyword (e.g. ``order``)
# must not corrupt the rewrite when the SQL also contains that keyword's
# clause form (ORDER BY / GROUP BY / PARTITION BY). A bare ``\bname\b`` match
# fires on both the real FROM/JOIN reference AND the keyword half of the
# clause; substituting both turns ``... ORDER BY x`` into
# ``... `native` `native` BY x``. The fix suppresses a match immediately
# followed by " by" — a position no real reference can occupy, since neither
# DuckDB nor BigQuery accepts a bare ``by`` as a table/alias identifier.
# ---------------------------------------------------------------------------


class TestRewriterKeywordNamedTableDoesNotCorruptSql:
    def test_order_by_survives_when_table_is_named_order(self):
        """Reproduces issue #1322: registering a table named ``order``
        must not turn ``FROM order ORDER BY x`` into the corrupted
        ``FROM `native` `native` BY x``."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT * FROM order ORDER BY event_date",
            [("order", "sales", "orders_tbl")],
            "proj",
        )

        assert sql == "SELECT * FROM `proj.sales.orders_tbl` ORDER BY event_date"
        # The issue's corruption signature must not appear.
        assert "`proj.sales.orders_tbl` `proj.sales.orders_tbl` BY" not in sql
        assert sql.count("`proj.sales.orders_tbl`") == 1

    def test_real_from_target_named_order_is_still_rewritten(self):
        """The keyword suppression must not blanket-skip a table that is
        genuinely referenced just because its name collides with a
        keyword — only the ORDER BY usage is left alone."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT * FROM order WHERE status = 'open'",
            [("order", "sales", "orders_tbl")],
            "proj",
        )

        assert sql == "SELECT * FROM `proj.sales.orders_tbl` WHERE status = 'open'"

    def test_group_by_survives_when_table_is_named_group(self):
        """Same defect class for GROUP BY."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT category, COUNT(*) FROM group GROUP BY category",
            [("group", "sales", "customer_groups")],
            "proj",
        )

        assert sql == ("SELECT category, COUNT(*) FROM `proj.sales.customer_groups` GROUP BY category")

    def test_partition_by_survives_when_table_is_named_partition(self):
        """Same defect class for PARTITION BY (window-function clause)."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT *, ROW_NUMBER() OVER (PARTITION BY region) FROM partition",
            [("partition", "sales", "table_partitions")],
            "proj",
        )

        assert sql == ("SELECT *, ROW_NUMBER() OVER (PARTITION BY region) FROM `proj.sales.table_partitions`")

    def test_case_insensitive_keyword_suppression(self):
        """The rewriter matches bare names case-insensitively; the keyword
        suppression must hold across casings too — only the real FROM
        target rewrites, the ORDER By clause (mixed case) is untouched."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT * FROM Order ORDER By event_date",
            [("order", "sales", "orders_tbl")],
            "proj",
        )

        assert sql == "SELECT * FROM `proj.sales.orders_tbl` ORDER By event_date"


# ---------------------------------------------------------------------------
# Issue #1322 follow-up (Devin review on PR #1331): the ``(?!\\s+by\\b)``
# suppression above only covers the two-word ``<KEYWORD> BY`` clauses, and
# only when nothing but plain whitespace separates the two words. Every other
# reserved keyword a table can be named after — ``all``, ``on``, ``as``,
# ``and``, ``select``, ``limit``, ``distinct``, … — still had its keyword
# occurrences substituted with a backticked BQ path.
#
# The rewriter now anchors the substitution of a RESERVED-KEYWORD-named table
# to table-reference positions (preceded by FROM / JOIN / a FROM-list comma,
# comments and newlines allowed in between). Non-keyword names keep the
# documented substitute-everywhere behaviour.
# ---------------------------------------------------------------------------


class TestRewriterKeywordNamedTableIsOnlyRewrittenInTableRefPositions:
    """One registered table, named after a reserved keyword; the SQL uses
    that keyword in its keyword role somewhere else. Only the FROM/JOIN
    occurrence may be substituted."""

    @pytest.mark.parametrize(
        "name,sql,expected",
        [
            # Devin's comment-between-the-words case: `\s+by` cannot see
            # across a block comment, so the old guard let ORDER through.
            (
                "order",
                "SELECT x FROM order ORDER /* tie-break */ BY x",
                "SELECT x FROM `proj.ds.tbl` ORDER /* tie-break */ BY x",
            ),
            # …nor across a line comment.
            (
                "order",
                "SELECT x FROM order ORDER\n-- tie-break\nBY x",
                "SELECT x FROM `proj.ds.tbl` ORDER\n-- tie-break\nBY x",
            ),
            # Every keyword the two-word guard never covered at all.
            (
                "all",
                "SELECT * FROM all UNION ALL SELECT * FROM all",
                "SELECT * FROM `proj.ds.tbl` UNION ALL SELECT * FROM `proj.ds.tbl`",
            ),
            (
                "select",
                "SELECT a FROM select",
                "SELECT a FROM `proj.ds.tbl`",
            ),
            (
                "on",
                "SELECT * FROM t JOIN on ON t.a = on.a",
                "SELECT * FROM t JOIN `proj.ds.tbl` ON t.a = on.a",
            ),
            (
                "limit",
                "SELECT * FROM limit LIMIT 10",
                "SELECT * FROM `proj.ds.tbl` LIMIT 10",
            ),
            (
                "distinct",
                "SELECT DISTINCT a FROM distinct",
                "SELECT DISTINCT a FROM `proj.ds.tbl`",
            ),
            (
                "as",
                "SELECT a AS b FROM as",
                "SELECT a AS b FROM `proj.ds.tbl`",
            ),
            (
                "and",
                "SELECT * FROM and WHERE a = 1 AND b = 2",
                "SELECT * FROM `proj.ds.tbl` WHERE a = 1 AND b = 2",
            ),
            (
                "group",
                "SELECT a, COUNT(*) FROM group GROUP BY ALL",
                "SELECT a, COUNT(*) FROM `proj.ds.tbl` GROUP BY ALL",
            ),
        ],
    )
    def test_keyword_role_occurrences_are_left_alone(self, name, sql, expected):
        assert _rewrite_bq_table_refs_to_native(sql, [(name, "ds", "tbl")], "proj") == expected

    def test_from_list_comma_position_still_rewrites(self):
        """An old-style comma cross-join is a table-reference position too —
        anchoring must not under-rewrite it (a surviving bare name would
        reach BQ as an unknown table)."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT * FROM other, order WHERE other.id = order.id",
            [("order", "ds", "tbl")],
            "proj",
        )

        assert sql == "SELECT * FROM other, `proj.ds.tbl` WHERE other.id = order.id"

    def test_comment_between_from_and_keyword_name_still_rewrites(self):
        """Comments are allowed between the FROM/JOIN token and the name."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT * FROM /* the fact table */ order",
            [("order", "ds", "tbl")],
            "proj",
        )

        assert sql == "SELECT * FROM /* the fact table */ `proj.ds.tbl`"

    def test_non_keyword_name_keeps_substitute_everywhere_behaviour(self):
        """Scope guard: the positional anchor applies ONLY to names that are
        reserved SQL keywords. A normal name keeps the long-standing
        documented behaviour (rewritten wherever the word appears), so this
        change cannot regress the working path."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT orders FROM orders WHERE orders > 1",
            [("orders", "ds", "tbl")],
            "proj",
        )

        assert sql == "SELECT `proj.ds.tbl` FROM `proj.ds.tbl` WHERE `proj.ds.tbl` > 1"

    def test_keyword_and_normal_names_share_one_pass(self):
        """Both classes go through the SAME single-pass alternation, so the
        freshly-inserted backticked text is never re-scanned — the
        project-ID-contains-name corruption the single-pass design fixed
        must not come back via the keyword branch (project id here contains
        the registered keyword name ``order``)."""
        sql = _rewrite_bq_table_refs_to_native(
            "SELECT o.id FROM order o JOIN events e ON o.id = e.id ORDER BY e.ts",
            [("order", "sales", "orders_tbl"), ("events", "ev", "events_tbl")],
            "my-order-project",
        )

        assert sql == (
            "SELECT o.id FROM `my-order-project.sales.orders_tbl` o "
            "JOIN `my-order-project.ev.events_tbl` e ON o.id = e.id ORDER BY e.ts"
        )


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


# ---------------------------------------------------------------------------
# Issue #1322: the two selection scans feeding the rewriter (the bare-name
# pass in ``_bq_guardrail_inputs`` and the mirrored pass + the cross-source
# skip in ``_bq_remote_execution_plan``) must apply the same keyword-collision
# suppression, so a registered name that happens to be a SQL keyword neither
# (a) gets pulled into a dry-run / rewrite it was never actually referenced
# in, nor (b) forces an unrelated query into the slower ATTACH-catalog
# fallback, just because the SQL contains that keyword's clause form.
# ---------------------------------------------------------------------------


_ORDER_BQ_ROW = {
    "id": "bq.sales.orders_tbl",
    "name": "order",
    "source_type": "bigquery",
    "query_mode": "remote",
    "bucket": "sales",
    "source_table": "orders_tbl",
    "bq_fqn": None,
}

_EVENTS_BQ_ROW = {
    "id": "bq.events_ds.events",
    "name": "events",
    "source_type": "bigquery",
    "query_mode": "remote",
    "bucket": "events_ds",
    "source_table": "events",
    "bq_fqn": None,
}

_LOCAL_ORDER_ROW = {
    "id": "kbc.sales.order",
    "name": "order",
    "source_type": "keboola",
    "query_mode": "local",
    "bucket": "sales",
    "source_table": "order",
}


class TestGuardrailInputsKeywordCollision:
    """``_bq_guardrail_inputs`` bare-name pass (the ``dry_run``/``name_lookups``
    selection scan feeding the rewriter)."""

    def test_ignores_order_by_when_table_not_actually_referenced(self, monkeypatch):
        """A registered BQ table named ``order`` must not be pulled into
        the dry-run/name_lookups set just because the SQL contains an
        innocent ``ORDER BY`` — that would force an unnecessary (and
        possibly cap-rejecting) dry-run scan of a table the query never
        touches."""
        import app.api.query as query_mod

        monkeypatch.setattr(query_mod, "table_registry_repo", lambda: _FakeRepo([_ORDER_BQ_ROW]))
        monkeypatch.setattr(query_mod, "is_user_admin", lambda *a, **k: True)

        sql = "SELECT * FROM some_other_view ORDER BY id"
        dry_run, name_lookups, blocked = query_mod._bq_guardrail_inputs(
            sql, sql.lower(), None, {"id": "u", "email": "u@example.com"}, None
        )

        assert blocked is None
        assert dry_run == []
        assert name_lookups == []

    def test_still_detects_table_named_order_as_a_real_reference(self, monkeypatch):
        """The suppression must not blanket-hide a table genuinely named
        ``order`` when the SQL actually selects FROM it."""
        import app.api.query as query_mod

        monkeypatch.setattr(query_mod, "table_registry_repo", lambda: _FakeRepo([_ORDER_BQ_ROW]))
        monkeypatch.setattr(query_mod, "is_user_admin", lambda *a, **k: True)

        sql = "SELECT * FROM order ORDER BY id"
        dry_run, name_lookups, blocked = query_mod._bq_guardrail_inputs(
            sql, sql.lower(), None, {"id": "u", "email": "u@example.com"}, None
        )

        assert blocked is None
        assert len(name_lookups) == 1
        assert name_lookups[0][:3] == ("order", "sales", "orders_tbl")
        assert len(dry_run) == 1


class TestRemoteExecutionPlanCrossSourceKeywordCollision:
    """``_bq_remote_execution_plan``'s ``rewrite_skip_cross_source`` check."""

    def test_local_table_named_order_does_not_force_attach_fallback(self, monkeypatch):
        """A local-mode table registered as ``order`` must not make every
        BQ query containing an innocent ``ORDER BY`` fall back to the
        slower ATTACH-catalog path."""
        import app.api.query as query_mod

        monkeypatch.setattr(
            query_mod,
            "table_registry_repo",
            lambda: _FakeRepo([_EVENTS_BQ_ROW, _LOCAL_ORDER_ROW]),
        )

        class _Projects:
            data = "data-prj"
            billing = "billing-prj"

        class _Bq:
            projects = _Projects()

        monkeypatch.setattr(query_mod, "get_bq_access", lambda: _Bq())

        rewritten, did_rewrite, billing, inner = query_mod._bq_remote_execution_plan(
            "SELECT * FROM events ORDER BY event_date", None
        )

        assert did_rewrite is True
        assert inner is not None
        assert "`data-prj.events_ds.events`" in inner
        assert "ORDER BY event_date" in inner
