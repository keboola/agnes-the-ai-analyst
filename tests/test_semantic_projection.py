"""Projecting an Ossie document into the flat tables queries actually read
(metric_definitions, glossary_terms, column_metadata), scoped and pruned per
(source, source_ref) so two sources can never delete each other's rows.

Reuses the ``e2e_env`` DATA_DIR-isolation fixture from ``tests/conftest.py``
under the ``system_db`` name the plan assumed — ``tests/conftest.py`` has no
fixture literally named ``system_db``; ``e2e_env`` gives each test its own
DATA_DIR (and therefore its own system.duckdb, auto-migrated on first
``get_system_db()`` call), which is exactly the isolation these tests need.
"""

import json

import pytest

from src.semantic.projection import project_document


@pytest.fixture
def system_db(e2e_env):
    return e2e_env


DOC = {
    "semantic_model": [
        {
            "name": "retail",
            "datasets": [
                {
                    "name": "orders",
                    "source": "db.public.orders",
                    "fields": [
                        {
                            "name": "order_date",
                            "datatype": "Date",
                            "description": "when the order was placed",
                            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "order_date"}]},
                        },
                    ],
                }
            ],
            "metrics": [
                {
                    "name": "revenue",
                    "datatype": "Decimal",
                    "description": "total revenue",
                    "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}]},
                },
                {
                    "name": "wh_only",
                    "expression": {"dialects": [{"dialect": "SNOWFLAKE", "expression": "TRY_CAST(x AS NUMBER)"}]},
                },
            ],
        }
    ]
}


def test_projects_metrics_and_columns(system_db):
    report = project_document(DOC, source="git", source_ref="repo-a")
    assert report.metrics_written == 1
    assert report.columns_written == 1


def test_unusable_metric_is_reported_not_written(system_db):
    report = project_document(DOC, source="git", source_ref="repo-a")
    skipped = [s for s in report.skipped if s["name"] == "wh_only"]
    assert len(skipped) == 1
    assert "SNOWFLAKE" in skipped[0]["reason"]


def _stub_dataset(name="orders"):
    # The real schema sets `minItems: 1` on `datasets` and requires
    # ["name", "datasets"] on a model, so `"datasets": []` is NOT a legal
    # document even though project_document never validates. Keep fixtures
    # schema-legal or they become a trap the moment anything validates them.
    return {"name": name, "source": f"db.public.{name}", "fields": []}


def test_reprojection_prunes_only_this_origin(system_db):
    project_document(DOC, source="git", source_ref="repo-a")
    other = {
        "semantic_model": [
            {
                "name": "fin",
                "datasets": [_stub_dataset("costs")],
                "metrics": [
                    {"name": "cost", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(c)"}]}}
                ],
            }
        ]
    }
    project_document(other, source="git", source_ref="repo-b")

    shrunk = {"semantic_model": [{"name": "retail", "datasets": [_stub_dataset()], "metrics": []}]}
    project_document(shrunk, source="git", source_ref="repo-a")

    from src.repositories import metric_repo

    # NOTE: the plan's Step-1 test body calls `metric_repo().list_all()`, but
    # MetricRepository has no `list_all` — only `list(category=None)`, which
    # already returns every metric when called with no argument. Adapted here
    # (see the report for this task).
    remaining = {m["name"] for m in metric_repo().list()}
    assert "revenue" not in remaining, "repo-a's dropped metric should be pruned"
    assert "cost" in remaining, "prune must not cross a source_ref boundary"


# --- Additional coverage beyond the plan's Step-1 body -----------------
#
# The plan's own test bodies never exercise the glossary-via-custom_extensions
# rule or the column-level prune, but both are explicit projection rules for
# this task. Covered here so the behavior has a regression test at all.


def test_glossary_is_projected_from_custom_extensions(system_db):
    doc = {
        "semantic_model": [
            {
                "name": "retail",
                "datasets": [_stub_dataset()],
                "custom_extensions": [
                    {
                        "vendor_name": "AGNES",
                        "data": json.dumps(
                            {
                                "glossary": [
                                    {"term": "ARR", "definition": "Annual recurring revenue."},
                                ]
                            }
                        ),
                    }
                ],
            }
        ]
    }
    report = project_document(doc, source="git", source_ref="repo-a")
    assert report.glossary_written == 1

    from src.repositories import glossary_repo

    terms = {g["term"] for g in glossary_repo().list(limit=1000)}
    assert "ARR" in terms


def test_document_without_glossary_extension_writes_none(system_db):
    report = project_document(DOC, source="git", source_ref="repo-a")
    assert report.glossary_written == 0


def test_column_prune_removes_a_dropped_field(system_db):
    project_document(DOC, source="git", source_ref="repo-a")

    shrunk = {"semantic_model": [{"name": "retail", "datasets": [_stub_dataset()], "metrics": []}]}
    project_document(shrunk, source="git", source_ref="repo-a")

    from src.repositories import column_metadata_repo

    remaining = column_metadata_repo().list_for_table("db.public.orders")
    assert remaining == []


def test_glossary_custom_extension_from_another_vendor_is_ignored(system_db):
    doc = {
        "semantic_model": [
            {
                "name": "retail",
                "datasets": [_stub_dataset()],
                "custom_extensions": [
                    {"vendor_name": "SNOWFLAKE", "data": json.dumps({"glossary": [{"term": "X", "definition": "y"}]})}
                ],
            }
        ]
    }
    report = project_document(doc, source="git", source_ref="repo-a")
    assert report.glossary_written == 0


# ---------------------------------------------------------------------------
# The AGNES custom_extensions block. Ossie's Metric has
# `additionalProperties: false` and no dataset link at all, so a metric cannot
# carry its table binding, its grain or its constraints in the core schema.
# The Keboola adapter already files all three under the AGNES vendor name
# (connectors/keboola/semantic_ossie.py) — this is the projector learning to
# read what the adapter has been writing, which is the whole flat-table
# cutover in one step.
# ---------------------------------------------------------------------------

_AGNES = "AGNES"


def _ext(payload: dict) -> dict:
    return {"vendor_name": _AGNES, "data": json.dumps(payload)}


def _register_keboola_table(bucket: str, source_table: str, name: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=name,
            name=name,
            source_type="keboola",
            bucket=bucket,
            source_table=source_table,
            query_mode="local",
        )
    finally:
        conn.close()


def _doc(*, metric_ext=None, dataset_ext=None, model_ext=None, table_id="in.c-shop.orders"):
    metric: dict = {
        "name": "revenue",
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}]},
    }
    if metric_ext is not None:
        metric["custom_extensions"] = [_ext(metric_ext)]

    dataset: dict = {"name": "orders", "source": table_id}
    if dataset_ext is not None:
        dataset["custom_extensions"] = [_ext(dataset_ext)]

    model: dict = {"name": "retail", "datasets": [dataset], "metrics": [metric]}
    if model_ext is not None:
        model["custom_extensions"] = [_ext(model_ext)]
    return {"semantic_model": [model]}


def _only_metric(source="keboola_metastore", source_ref="conn-1"):
    from src.repositories import metric_repo

    rows = [m for m in metric_repo().list() if (m.get("source") or "") == source]
    assert len(rows) == 1, rows
    row = dict(rows[0])
    # `validation` comes back as a JSON STRING on DuckDB (the repo's
    # `_row_to_dict` only zips columns) and as a dict on Postgres (JSONB).
    # Readers already cope with both — `cli/commands/catalog.py` parses a
    # string, `src/data_semantics_scaffold.py::_maybe_json` too — so these
    # tests normalize rather than pin one backend's representation.
    if isinstance(row.get("validation"), str):
        row["validation"] = json.loads(row["validation"])
    return row


class TestTableBinding:
    def test_a_bound_metric_gets_runnable_sql_and_its_table(self, system_db):
        """The legacy Keboola composer produced `SELECT <frag> FROM "view" AS t`.
        Projecting a bare fragment instead would be a regression at cutover:
        `agnes catalog --metrics --show` would hand the agent SQL it cannot run.
        """
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        project_document(
            _doc(metric_ext={"dataset": "in.c-shop.orders"}),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        row = _only_metric()
        assert row["table_name"] == "shop_orders"
        assert row["sql"].startswith("SELECT ")
        assert "SUM(amount)" in row["sql"]
        assert "FROM" in row["sql"]

    def test_an_unbound_metric_keeps_its_fragment(self, system_db):
        """A document with no AGNES binding — a plain upstream Ossie file from a
        git source — projects the fragment as-is. Asserted explicitly so nobody
        later "fixes" it into a guess about which table it belongs to.
        """
        project_document(_doc(), source="keboola_metastore", source_ref="conn-1")

        row = _only_metric()
        assert row["sql"] == "SUM(amount)"
        assert row["table_name"] is None

    def test_a_binding_to_an_unregistered_table_is_skipped(self, system_db):
        """A metric that DECLARES a table binding it cannot honor is dropped,
        matching the legacy Keboola composer (which skips `unresolved_table`).
        The steady state — a semantic layer describing more tables than the
        instance registers — is exactly when this fires, and keeping the metric
        as a bare fragment would make the flat-table cutover start surfacing
        unrunnable metrics on tables nobody registered. Contrast
        `test_an_unbound_metric_keeps_its_fragment`: a metric that declares NO
        binding keeps its fragment, because it never claimed a table."""
        from src.repositories import metric_repo

        project_document(
            _doc(metric_ext={"dataset": "in.c-nowhere.ghosts"}),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        assert [m for m in metric_repo().list() if m.get("source") == "keboola_metastore"] == []


class TestConstraints:
    def test_model_constraints_reach_the_metric_they_name(self, system_db):
        project_document(
            _doc(
                model_ext={
                    "constraints": [
                        {
                            "name": "non_negative",
                            "constraint_type": "range",
                            "rule": "value >= 0",
                            "metrics": ["revenue"],
                            "severity": "error",
                        }
                    ]
                }
            ),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        validation = _only_metric()["validation"]
        assert validation is not None
        assert [r["name"] for r in validation["rules"]] == ["non_negative"]
        assert validation["rules"][0]["rule"] == "value >= 0"
        assert validation["rules"][0]["severity"] == "error"

    def test_a_constraint_naming_another_metric_is_not_attached(self, system_db):
        project_document(
            _doc(
                model_ext={
                    "constraints": [
                        {"name": "other_rule", "rule": "value < 10", "metrics": ["margin"], "severity": "warning"}
                    ]
                }
            ),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        assert _only_metric()["validation"] is None


class TestDatasetGrain:
    def test_dataset_grain_is_reported_as_a_note_not_as_the_metrics_grain(self, system_db):
        """A dataset's grain is a true fact about the DATASET. Writing it into
        `metric_definitions.grain` restates it as a fact about the metric, which
        is the misattribution wave 0 removed. As a note it keeps both the fact
        and its scope."""
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        project_document(
            _doc(metric_ext={"dataset": "in.c-shop.orders"}, dataset_ext={"grain": "monthly"}),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        row = _only_metric()
        assert row["grain"] is None
        assert any("monthly" in n for n in (row["notes"] or []))

    def test_no_dataset_grain_means_no_note(self, system_db):
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        project_document(
            _doc(metric_ext={"dataset": "in.c-shop.orders"}),
            source="keboola_metastore",
            source_ref="conn-1",
        )

        row = _only_metric()
        assert row["grain"] is None
        assert not [n for n in (row["notes"] or []) if "grain" in n]


class TestColumnBinding:
    """A Keboola dataset's `source` is the raw Storage tableId
    (`in.c-shop.orders`), but every column_metadata reader keys on the Agnes
    `table_registry` view name (`shop_orders`) — same binder the metric leg
    already applies."""

    def test_keboola_field_descriptions_land_under_the_view_name(self, system_db):
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        doc = {
            "semantic_model": [
                {
                    "name": "retail",
                    "datasets": [
                        {
                            "name": "orders",
                            "source": "in.c-shop.orders",
                            "fields": [
                                {"name": "amount", "datatype": "Decimal", "description": "Order amount, in cents."}
                            ],
                        }
                    ],
                }
            ]
        }
        report = project_document(doc, source="keboola_metastore", source_ref="conn-1")
        assert report.columns_written == 1

        from src.repositories import column_metadata_repo

        repo = column_metadata_repo()
        under_view_name = repo.list_for_table("shop_orders")
        assert [c["column_name"] for c in under_view_name] == ["amount"]
        assert under_view_name[0]["description"] == "Order amount, in cents."
        # Nothing lands under the raw, unresolved tableId.
        assert repo.list_for_table("in.c-shop.orders") == []

    def test_prune_stays_scoped_to_the_resolved_view_name(self, system_db):
        _register_keboola_table("in.c-shop", "orders", "shop_orders")

        def _doc_with_fields(field_names):
            return {
                "semantic_model": [
                    {
                        "name": "retail",
                        "datasets": [
                            {
                                "name": "orders",
                                "source": "in.c-shop.orders",
                                "fields": [{"name": n} for n in field_names],
                            }
                        ],
                    }
                ]
            }

        project_document(_doc_with_fields(["amount", "region"]), source="keboola_metastore", source_ref="conn-1")
        project_document(_doc_with_fields(["amount"]), source="keboola_metastore", source_ref="conn-1")

        from src.repositories import column_metadata_repo

        remaining = {c["column_name"] for c in column_metadata_repo().list_for_table("shop_orders")}
        assert remaining == {"amount"}, "the dropped field must be pruned under the resolved view name"

    def test_unregistered_table_falls_back_to_the_raw_id(self, system_db):
        """No keboola tables registered at all -> the binder is unavailable
        and column_metadata keeps today's behavior (raw dataset source)."""
        doc = {
            "semantic_model": [
                {
                    "name": "retail",
                    "datasets": [
                        {"name": "orders", "source": "in.c-nowhere.orders", "fields": [{"name": "amount"}]},
                    ],
                }
            ]
        }
        project_document(doc, source="keboola_metastore", source_ref="conn-1")

        from src.repositories import column_metadata_repo

        assert [c["column_name"] for c in column_metadata_repo().list_for_table("in.c-nowhere.orders")] == ["amount"]


class TestDuplicateModelName:
    """`_scoped_id` keys on the model NAME; two same-named models in one
    document must not silently overwrite each other's rows."""

    def test_second_model_with_a_duplicate_name_is_reported_not_merged(self, system_db):
        doc = {
            "semantic_model": [
                {
                    "name": "core",
                    "datasets": [_stub_dataset("first")],
                    "metrics": [
                        {
                            "name": "metric_a",
                            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(a)"}]},
                        }
                    ],
                },
                {
                    "name": "core",
                    "datasets": [_stub_dataset("second")],
                    "metrics": [
                        {
                            "name": "metric_b",
                            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(b)"}]},
                        }
                    ],
                },
            ]
        }
        report = project_document(doc, source="git", source_ref="repo-a")

        assert report.metrics_written == 1
        assert {"kind": "model", "name": "core", "reason": "duplicate_model_name"} in report.skipped

        from src.repositories import metric_repo

        names = {m["name"] for m in metric_repo().list()}
        assert "metric_a" in names
        assert "metric_b" not in names
