"""Repository tests for v56 ``data_packages`` extended content fields.

The Foundry Data team spec adds owner attribution + curated tags + a long-
form markdown body + use/skip arrays + package-level example questions.
JSON list fields round-trip via ``json.dumps`` on write +
``json.loads`` on read; NULL → empty list.

Back-compat: legacy ``create()`` signature (without any v56 field) must
still work, and ``get()`` of a legacy row must return empty defaults
rather than KeyError.
"""

from __future__ import annotations

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.data_packages import DataPackagesRepository


@pytest.fixture
def repo():
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return DataPackagesRepository(conn)


class TestOwnerFields:
    def test_create_with_owner_name_and_team(self, repo):
        pid = repo.create(
            name="Sales", slug="sales", description=None,
            icon=None, color=None, created_by="admin",
            owner_name="Jane Doe", owner_team="Sales Ops",
        )
        pkg = repo.get(pid)
        assert pkg["owner_name"] == "Jane Doe"
        assert pkg["owner_team"] == "Sales Ops"

    def test_create_without_owner_leaves_nulls(self, repo):
        pid = repo.create(
            name="Sales", slug="sales", description=None,
            icon=None, color=None, created_by="admin",
        )
        pkg = repo.get(pid)
        assert pkg.get("owner_name") is None
        assert pkg.get("owner_team") is None

    def test_update_partial_owner_keeps_other_fields(self, repo):
        pid = repo.create(
            name="Sales", slug="sales", description="x",
            icon=None, color=None, created_by="admin",
            owner_name="Jane", owner_team="Ops",
        )
        repo.update(pid, owner_name="Janet")
        pkg = repo.get(pid)
        assert pkg["owner_name"] == "Janet"
        assert pkg["owner_team"] == "Ops"  # not touched
        assert pkg["description"] == "x"


class TestTags:
    def test_create_with_tags_roundtrips(self, repo):
        pid = repo.create(
            name="Sales", slug="sales", description=None,
            icon=None, color=None, created_by="admin",
            tags=["Finance", "Revenue", "Margin"],
        )
        pkg = repo.get(pid)
        assert pkg["tags"] == ["Finance", "Revenue", "Margin"]

    def test_unset_tags_returns_empty_list(self, repo):
        pid = repo.create(
            name="Sales", slug="sales", description=None,
            icon=None, color=None, created_by="admin",
        )
        pkg = repo.get(pid)
        assert pkg["tags"] == []

    def test_update_replaces_tags_atomically(self, repo):
        pid = repo.create(
            name="Sales", slug="sales", description=None,
            icon=None, color=None, created_by="admin",
            tags=["A", "B"],
        )
        repo.update(pid, tags=["C"])
        assert repo.get(pid)["tags"] == ["C"]

    def test_update_clears_tags_with_empty_list(self, repo):
        pid = repo.create(
            name="Sales", slug="sales", description=None,
            icon=None, color=None, created_by="admin",
            tags=["A", "B"],
        )
        repo.update(pid, tags=[])
        assert repo.get(pid)["tags"] == []


class TestLongDescription:
    def test_create_with_long_description(self, repo):
        body = "Multi-line markdown\n\n- bullet one\n- bullet two"
        pid = repo.create(
            name="Sales", slug="sales", description="short",
            icon=None, color=None, created_by="admin",
            long_description=body,
        )
        pkg = repo.get(pid)
        assert pkg["long_description"] == body

    def test_update_long_description(self, repo):
        pid = repo.create(
            name="Sales", slug="sales", description="short",
            icon=None, color=None, created_by="admin",
            long_description="v1",
        )
        repo.update(pid, long_description="v2")
        assert repo.get(pid)["long_description"] == "v2"


class TestUseSkipAndExampleQuestions:
    def test_create_with_when_to_use(self, repo):
        bullets = ["You need X", "You're computing Y"]
        pid = repo.create(
            name="Sales", slug="sales", description=None,
            icon=None, color=None, created_by="admin",
            when_to_use=bullets,
        )
        assert repo.get(pid)["when_to_use"] == bullets

    def test_create_with_when_not_to_use(self, repo):
        bullets = ["You only need session counts"]
        pid = repo.create(
            name="Sales", slug="sales", description=None,
            icon=None, color=None, created_by="admin",
            when_not_to_use=bullets,
        )
        assert repo.get(pid)["when_not_to_use"] == bullets

    def test_create_with_example_questions(self, repo):
        qs = [
            "What was revenue last week?",
            "Top 10 customers by spend.",
        ]
        pid = repo.create(
            name="Sales", slug="sales", description=None,
            icon=None, color=None, created_by="admin",
            example_questions=qs,
        )
        assert repo.get(pid)["example_questions"] == qs

    def test_all_unset_json_fields_default_to_empty_list(self, repo):
        pid = repo.create(
            name="Sales", slug="sales", description=None,
            icon=None, color=None, created_by="admin",
        )
        pkg = repo.get(pid)
        assert pkg["tags"] == []
        assert pkg["when_to_use"] == []
        assert pkg["when_not_to_use"] == []
        assert pkg["example_questions"] == []


class TestListShape:
    def test_list_includes_all_new_fields(self, repo):
        repo.create(
            name="Sales", slug="sales", description=None,
            icon=None, color=None, created_by="admin",
            owner_name="Jane", owner_team="Ops",
            tags=["Finance"], long_description="body",
            when_to_use=["a"], when_not_to_use=["b"],
            example_questions=["q"],
        )
        rows = repo.list()
        assert len(rows) == 1
        pkg = rows[0]
        assert pkg["owner_name"] == "Jane"
        assert pkg["owner_team"] == "Ops"
        assert pkg["tags"] == ["Finance"]
        assert pkg["long_description"] == "body"
        assert pkg["when_to_use"] == ["a"]
        assert pkg["when_not_to_use"] == ["b"]
        assert pkg["example_questions"] == ["q"]
