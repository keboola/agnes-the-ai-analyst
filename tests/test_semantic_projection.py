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
