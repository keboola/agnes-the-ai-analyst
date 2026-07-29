"""Unit tests for the linked-app projection reconciler + Keboola adapter.

Hermetic: drives a DuckDB `DataAppsRepository` on a temp system db (no live
Keboola MCP), injected into `project(repo=...)`.
"""

from __future__ import annotations

import duckdb
import pytest

from src.data_apps import keboola_adapter as adapter
from src.data_apps.linked_projection import project


@pytest.fixture
def repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.data_apps import DataAppsRepository

    conn = duckdb.connect(str(tmp_path / "sys.duckdb"))
    _ensure_schema(conn)
    yield DataAppsRepository(conn)
    conn.close()


def _rec(app_id, name="N", desc="", url="https://example.com/x"):
    return adapter.LinkedAppRecord(external_app_id=app_id, name=name, description=desc, external_url=url)


def test_map_row_tolerates_column_variants():
    rec = adapter.map_row({"id": "123", "name": "Sales", "url": "https://example.com/s", "description": "d"})
    assert rec.external_app_id == "123"
    assert rec.name == "Sales"
    assert rec.external_url == "https://example.com/s"
    assert rec.description == "d"


def test_slug_is_deterministic_and_unique_across_connections():
    a = adapter.slug_for("conn1", "42")
    assert a == adapter.slug_for("conn1", "42")  # deterministic
    assert a != adapter.slug_for("conn2", "42")  # same app id, different connection


def test_project_create_then_reconcile_hides_missing(repo):
    r1 = project("conn1", [_rec("a1", "A1"), _rec("a2", "A2")], repo=repo)
    assert (r1.created, r1.updated, r1.hidden) == (2, 0, 0)
    assert {x["slug"] for x in repo.list_linked()} == {
        adapter.slug_for("conn1", "a1"),
        adapter.slug_for("conn1", "a2"),
    }

    # second round: a2 gone → hidden; a1 updated; a different connection untouched
    project("conn2", [_rec("b1", "B1")], repo=repo)
    r2 = project("conn1", [_rec("a1", "A1 renamed")], repo=repo)
    assert (r2.created, r2.updated, r2.hidden) == (0, 1, 1)
    active = {x["slug"] for x in repo.list_linked()}
    assert adapter.slug_for("conn1", "a2") not in active  # hidden
    assert adapter.slug_for("conn2", "b1") in active  # other connection kept


def test_project_preserves_description_override(repo):
    project("c", [_rec("a", "A", desc="synced")], repo=repo)
    slug = adapter.slug_for("c", "a")
    repo.set_description_override(slug, "admin desc")

    project("c", [_rec("a", "A", desc="synced-2")], repo=repo)  # re-sync
    row = repo.get_by_slug(slug)
    assert row["description"] == "synced-2"
    assert repo.effective_description(row) == "admin desc"


def test_project_reappearance_relinks_same_row(repo):
    project("c", [_rec("a", "A")], repo=repo)
    slug = adapter.slug_for("c", "a")
    original_id = repo.get_by_slug(slug)["id"]
    project("c", [], repo=repo)  # disappears → hidden
    assert repo.list_linked() == []

    r = project("c", [_rec("a", "A back")], repo=repo)  # returns
    assert (r.created, r.updated) == (0, 1)  # reactivated, not a new row
    row = repo.get_by_slug(slug)
    assert row["id"] == original_id
    assert row["state"] == "linked"
