"""Repository tests for v56 ``table_registry`` extended doc fields.

Adds structured per-table documentation columns alongside the v52
``sample_questions`` / ``things_to_know`` / ``pairs_well_with`` blob
columns:

  * ``grain`` — one-line "1 row per session × event_date"
  * ``platforms`` — JSON list of platform names
  * ``partition_col`` — single column name used for partitioning
  * ``history`` — short string ("Full", "Rolling 15 months", "Nov 2025+")
  * ``gotchas`` — JSON list of ``{key: bool, body: str}`` dicts; first
    entry with ``key=True`` is the "Key gotcha" rendered distinctly.

All additive + NULLABLE. Default reads return None for scalar columns
and ``[]`` for JSON lists.
"""

from __future__ import annotations

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.table_registry import TableRegistryRepository


@pytest.fixture
def repo():
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return TableRegistryRepository(conn)


def _seed(repo, tid="tbl_t"):
    repo.conn.execute(
        "INSERT INTO table_registry(id, name, source_type, query_mode) "
        "VALUES (?, ?, 'keboola', 'local')",
        [tid, "test_table"],
    )
    return tid


class TestExtendedDocs:
    def test_patch_grain(self, repo):
        tid = _seed(repo)
        repo.update_docs(tid, grain="1 row per order event")
        row = repo.get(tid)
        assert row["grain"] == "1 row per order event"

    def test_patch_platforms_roundtrips(self, repo):
        tid = _seed(repo)
        repo.update_docs(tid, platforms=["MBNXT", "Legacy"])
        row = repo.get(tid)
        assert row["platforms"] == ["MBNXT", "Legacy"]

    def test_patch_partition_col(self, repo):
        tid = _seed(repo)
        repo.update_docs(tid, partition_col="event_date")
        assert repo.get(tid)["partition_col"] == "event_date"

    def test_patch_history(self, repo):
        tid = _seed(repo)
        repo.update_docs(tid, history="Rolling 15 months")
        assert repo.get(tid)["history"] == "Rolling 15 months"

    def test_patch_gotchas_array_of_dicts(self, repo):
        tid = _seed(repo)
        gotchas = [
            {"key": True, "body": "Joins via LOWER(bcookie); forgetting LOWER drops rows."},
            {"key": False, "body": "Filter user_brand_affiliation = 'acme' for P&L."},
            {"key": False, "body": "Exclude is_excluded_from_pnl = 1 for revenue."},
        ]
        repo.update_docs(tid, gotchas=gotchas)
        assert repo.get(tid)["gotchas"] == gotchas

    def test_unset_json_fields_default_to_empty(self, repo):
        tid = _seed(repo)
        row = repo.get(tid)
        assert row["platforms"] == []
        assert row["gotchas"] == []
        assert row["grain"] is None
        assert row["partition_col"] is None
        assert row["history"] is None


class TestAtomicityAndPreservation:
    def test_patch_all_v56_fields_atomically(self, repo):
        tid = _seed(repo)
        repo.update_docs(
            tid,
            grain="1 row per session",
            platforms=["MBNXT"],
            partition_col="event_date",
            history="Full",
            gotchas=[{"key": True, "body": "Always filter mbnxt"}],
        )
        row = repo.get(tid)
        assert row["grain"] == "1 row per session"
        assert row["platforms"] == ["MBNXT"]
        assert row["partition_col"] == "event_date"
        assert row["history"] == "Full"
        assert row["gotchas"][0]["body"] == "Always filter mbnxt"

    def test_patch_preserves_v52_docs_fields(self, repo):
        """Setting v56 fields must not nuke pre-existing v52 docs
        (sample_questions / things_to_know / pairs_well_with)."""
        tid = _seed(repo)
        repo.update_docs(
            tid,
            sample_questions=["What is revenue?"],
            things_to_know="Some legacy free text",
            pairs_well_with=["tbl_other"],
        )
        repo.update_docs(tid, grain="1 row per session")
        row = repo.get(tid)
        assert row["sample_questions"] == ["What is revenue?"]
        assert row["things_to_know"] == "Some legacy free text"
        assert row["pairs_well_with"] == ["tbl_other"]
        assert row["grain"] == "1 row per session"
