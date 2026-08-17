"""connectors/databricks/extractor.materialize_query — fake client, real parquet/duckdb."""

from __future__ import annotations

from typing import List, Optional

import duckdb
import pytest

from connectors.bigquery.extractor import MaterializeBudgetError
from connectors.databricks.extractor import full_table_sql, materialize_query, split_bucket

pa = pytest.importorskip("pyarrow")


class FakeArrowResult:
    def __init__(
        self,
        batches: List,
        *,
        truncated: bool = False,
        schema_columns: Optional[list] = None,
        total_byte_count: int = 0,
    ):
        self._batches = batches
        self.truncated = truncated
        self.schema_columns = schema_columns or []
        self.total_byte_count = total_byte_count
        self.total_row_count = sum(b.num_rows for b in batches)

    def iter_batches(self):
        yield from self._batches


class FakeClient:
    def __init__(self, result: FakeArrowResult):
        self._result = result
        self.statements: List[dict] = []

    def execute_to_arrow_batches(self, statement, *, byte_limit=None, timeout_s=None, catalog=None, schema=None):
        self.statements.append({"statement": statement, "byte_limit": byte_limit, "timeout_s": timeout_s})
        return self._result


def _batch(values):
    return pa.record_batch({"n": pa.array(values)})


class TestHelpers:
    def test_split_bucket_plain_uses_default_catalog(self):
        assert split_bucket("sales", "main") == ("main", "sales")

    def test_split_bucket_dotted_overrides_catalog(self):
        assert split_bucket("other.sales", "main") == ("other", "sales")

    def test_full_table_sql_quotes_segments(self):
        assert full_table_sql("main", "sales", "orders") == "SELECT * FROM `main`.`sales`.`orders`"

    @pytest.mark.parametrize("bad", ["", "with space", "tick`inject", "dot.ted", "semi;colon"])
    def test_full_table_sql_rejects_unsafe_segments(self, bad):
        with pytest.raises(ValueError):
            full_table_sql("main", bad, "orders")


class TestMaterializeQuery:
    def test_happy_path_writes_parquet_and_registers_extract(self, tmp_path):
        client = FakeClient(FakeArrowResult([_batch([1, 2]), _batch([3])]))
        stats = materialize_query(
            "orders_daily",
            client=client,
            output_dir=str(tmp_path),
            source_query="SELECT n FROM `main`.`sales`.`orders`",
            max_bytes=10 * 2**30,
            statement_timeout_s=60,
        )
        assert stats["rows"] == 3
        assert stats["query_mode"] == "materialized"
        assert stats["size_bytes"] > 0
        assert len(stats["hash"]) == 32

        parquet = tmp_path / "data" / "orders_daily.parquet"
        assert parquet.exists()
        assert not (tmp_path / "data" / "orders_daily.parquet.tmp").exists()

        # extract.duckdb is created on first materialize (no extractor
        # subprocess exists for Databricks) with the contract _meta shape.
        extract_db = tmp_path / "extract.duckdb"
        assert extract_db.exists()
        with duckdb.connect(str(extract_db), read_only=True) as conn:
            meta = conn.execute("SELECT table_name, rows, size_bytes, query_mode FROM _meta").fetchall()
            assert meta == [("orders_daily", 3, stats["size_bytes"], "materialized")]
            assert conn.execute('SELECT COUNT(*) FROM "orders_daily"').fetchone()[0] == 3

        # byte_limit + timeout threaded into the client call
        assert client.statements[0]["byte_limit"] == 10 * 2**30
        assert client.statements[0]["timeout_s"] == 60

    def test_rematerialize_replaces_meta_row(self, tmp_path):
        client = FakeClient(FakeArrowResult([_batch([1])]))
        materialize_query("t1", client=client, output_dir=str(tmp_path), source_query="SELECT 1")
        client2 = FakeClient(FakeArrowResult([_batch([1, 2])]))
        materialize_query("t1", client=client2, output_dir=str(tmp_path), source_query="SELECT 1")
        with duckdb.connect(str(tmp_path / "extract.duckdb"), read_only=True) as conn:
            meta = conn.execute("SELECT table_name, rows FROM _meta").fetchall()
        assert meta == [("t1", 2)]

    def test_truncated_result_raises_budget_error_and_writes_nothing(self, tmp_path):
        client = FakeClient(FakeArrowResult([], truncated=True, total_byte_count=2048))
        with pytest.raises(MaterializeBudgetError) as exc_info:
            materialize_query(
                "capped",
                client=client,
                output_dir=str(tmp_path),
                source_query="SELECT * FROM big",
                max_bytes=1024,
            )
        assert exc_info.value.table_id == "capped"
        assert exc_info.value.limit == 1024
        assert not (tmp_path / "data" / "capped.parquet").exists()
        assert not (tmp_path / "extract.duckdb").exists()

    def test_derives_full_table_sql_from_bucket(self, tmp_path):
        client = FakeClient(FakeArrowResult([_batch([1])]))
        materialize_query(
            "orders",
            client=client,
            output_dir=str(tmp_path),
            source_query=None,
            catalog="main",
            bucket="sales",
            source_table="orders",
        )
        assert client.statements[0]["statement"] == "SELECT * FROM `main`.`sales`.`orders`"

    def test_no_sql_and_no_bucket_raises(self, tmp_path):
        client = FakeClient(FakeArrowResult([_batch([1])]))
        with pytest.raises(ValueError, match="no source_query"):
            materialize_query("orders", client=client, output_dir=str(tmp_path))

    def test_unsafe_table_id_rejected(self, tmp_path):
        client = FakeClient(FakeArrowResult([_batch([1])]))
        with pytest.raises(ValueError, match="unsafe table_id"):
            materialize_query("../escape", client=client, output_dir=str(tmp_path), source_query="SELECT 1")

    def test_empty_result_writes_typed_empty_parquet(self, tmp_path):
        client = FakeClient(
            FakeArrowResult(
                [],
                schema_columns=[
                    {"name": "id", "type_name": "LONG", "position": 0},
                    {"name": "label", "type_name": "STRING", "position": 1},
                    {"name": "seen_at", "type_name": "TIMESTAMP", "position": 2},
                ],
            )
        )
        stats = materialize_query("empty_t", client=client, output_dir=str(tmp_path), source_query="SELECT 1 WHERE 1=0")
        assert stats["rows"] == 0
        with duckdb.connect() as conn:
            cols = conn.execute(
                f"SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet('{tmp_path / 'data' / 'empty_t.parquet'}'))"
            ).fetchall()
        assert [c[0] for c in cols] == ["id", "label", "seen_at"]
