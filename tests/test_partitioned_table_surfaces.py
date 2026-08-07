"""A partition-synced table stores its data as a DIRECTORY of per-period
parquets (``data/<table_id>/<partition>.parquet``) instead of a single
``data/<table_id>.parquet``.

PR #1189 taught the PREVIEW surface that layout (``resolve_local_parquet_glob``);
the other read surfaces still used the single-file lookup and therefore
reported a healthy, fully-synced partitioned table as having no data — a 404
from ``/api/v2/schema``, a 404 from ``/api/v2/scan``, and a missing size hint
in ``/api/v2/catalog`` (Devin Review on #1189).

The pending-first-sync case must keep behaving as before: a partition
directory that holds no parquet yet is genuinely "no data".
"""

from __future__ import annotations

import datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def _write_partitions(data_dir, table_id: str) -> dict[str, int]:
    """Write two per-period parquets under ``data/<table_id>/``. Returns
    ``{filename: size_bytes}``."""
    part_dir = data_dir / table_id
    part_dir.mkdir(parents=True, exist_ok=True)
    nov = pa.table(
        {
            "v": [1, 2],
            "d": [datetime.date(2025, 11, 1), datetime.date(2025, 11, 15)],
        }
    )
    dec = pa.table({"v": [3], "d": [datetime.date(2025, 12, 1)]})
    pq.write_table(nov, part_dir / "2025_11.parquet")
    pq.write_table(dec, part_dir / "2025_12.parquet")
    return {p.name: p.stat().st_size for p in part_dir.glob("*.parquet")}


def _write_hive_partitions(data_dir, table_id: str) -> None:
    """Write the NESTED hive layout (``month=YYYY-MM/data.parquet``) the Jira
    connector produces.

    The December part deliberately carries a column November does not: hive part
    schemas drift month to month, so a recursive glob only reads without
    ``union_by_name`` by accident. This is what makes that flag load-bearing
    rather than decorative.
    """
    part_dir = data_dir / table_id
    nov = pa.table({"v": [1, 2], "d": [datetime.date(2025, 11, 1), datetime.date(2025, 11, 15)]})
    dec = pa.table({"v": [3], "d": [datetime.date(2025, 12, 1)], "resolution": ["Done"]})
    for month, tbl in (("2025-11", nov), ("2025-12", dec)):
        month_dir = part_dir / f"month={month}"
        month_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(tbl, month_dir / "data.parquet")


class TestPartitionedSizeBytes:
    """``app.utils.local_parquet_size_bytes`` — one number for either layout."""

    def test_sums_every_partition_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        sizes = _write_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.utils import local_parquet_size_bytes

        assert local_parquet_size_bytes("kbc_sales", "keboola") == sum(sizes.values())
        assert len(sizes) == 2, "precondition: the sum must span more than one file"

    def test_single_file_layout_reports_that_files_size(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        data = tmp_path / "extracts" / "keboola" / "data"
        data.mkdir(parents=True)
        pq.write_table(pa.table({"a": [1]}), data / "kbc_orders.parquet")

        from app.utils import local_parquet_size_bytes

        expected = (data / "kbc_orders.parquet").stat().st_size
        assert local_parquet_size_bytes("kbc_orders", "keboola") == expected

    def test_nested_hive_parts_are_counted_too(self, tmp_path, monkeypatch):
        """The Jira layout nests one level (``month=YYYY-MM/data.parquet``);
        its bytes are on disk exactly like a flat partition's."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        hive = tmp_path / "extracts" / "jira" / "data" / "issues" / "month=2025-11"
        hive.mkdir(parents=True)
        pq.write_table(pa.table({"a": [1]}), hive / "data.parquet")

        from app.utils import local_parquet_size_bytes

        assert local_parquet_size_bytes("issues", "jira") == (hive / "data.parquet").stat().st_size

    def test_empty_partition_directory_is_no_data_yet(self, tmp_path, monkeypatch):
        """A directory with no parquet in it means the sync has not produced a
        partition yet — that IS the pending-sync case, so there is no size to
        report (0 would read as "synced, empty")."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "extracts" / "keboola" / "data" / "kbc_empty").mkdir(parents=True)

        from app.utils import local_parquet_size_bytes

        assert local_parquet_size_bytes("kbc_empty", "keboola") is None

    def test_no_layout_at_all_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "extracts" / "keboola" / "data").mkdir(parents=True)

        from app.utils import local_parquet_size_bytes

        assert local_parquet_size_bytes("kbc_missing", "keboola") is None


class TestCatalogSizeHint:
    """``/api/v2/catalog``'s ``rough_size_hint`` for a partitioned row."""

    def test_hint_is_bucketed_from_the_summed_partition_bytes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        sizes = _write_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.api import v2_catalog

        seen: list[int] = []
        monkeypatch.setattr(v2_catalog, "_bucket_size", lambda n: seen.append(n) or "small")

        hint = v2_catalog._materialized_parquet_size_bucket("kbc_sales", "keboola", "local")

        assert hint == "small"
        assert seen == [sum(sizes.values())], "the whole table's bytes, not one partition's"

    def test_empty_partition_directory_reports_no_hint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "extracts" / "keboola" / "data" / "kbc_empty").mkdir(parents=True)

        from app.api import v2_catalog

        assert v2_catalog._materialized_parquet_size_bucket("kbc_empty", "keboola", "local") is None


class TestProfileRefreshSurface:
    """``POST /api/catalog/profile/{table}/refresh``. The profiler itself has
    always understood a directory of parts (``src/profiler.py`` globs ``**``,
    and the scheduled run passes it a directory) — only this manual endpoint's
    lookup was single-file, so an admin could not re-profile a partitioned
    table the nightly run profiles fine."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import importlib

        import src.db as db_module

        importlib.reload(db_module)
        yield db_module

    def _refresh(self, db, monkeypatch, table_name: str):
        from app.api import catalog

        monkeypatch.setattr(catalog, "can_access_table", lambda *a, **kw: True)
        conn = db.get_system_db()
        try:
            return catalog.refresh_profile(table_name, user={"id": "u1"}, conn=conn)
        finally:
            conn.close()

    def test_profiles_a_partitioned_table(self, db, tmp_path, monkeypatch):
        _write_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        out = self._refresh(db, monkeypatch, "kbc_sales")

        assert out["status"] == "ok"
        assert out["columns"] == 2

    def test_empty_partition_directory_still_404s(self, db, tmp_path, monkeypatch):
        from fastapi import HTTPException

        (tmp_path / "extracts" / "keboola" / "data" / "kbc_empty").mkdir(parents=True)

        with pytest.raises(HTTPException) as exc_info:
            self._refresh(db, monkeypatch, "kbc_empty")
        assert exc_info.value.status_code == 404


class TestSchemaSurface:
    """``/api/v2/schema`` reads columns from the parquet."""

    def _row(self, table_id: str) -> dict:
        return {
            "id": table_id,
            "source_type": "keboola",
            "query_mode": "local",
            "bucket": "in.c-main",
            "source_table": table_id,
        }

    def test_columns_resolve_from_a_partition_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _write_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.api.v2_schema import build_schema_uncached

        payload = build_schema_uncached(conn=None, table_id="kbc_sales", bq=object(), row=self._row("kbc_sales"))

        assert {c["name"] for c in payload["columns"]} == {"v", "d"}
        assert payload["sql_flavor"] == "duckdb"

    def test_columns_resolve_from_a_nested_hive_directory(self, tmp_path, monkeypatch):
        """The catalog already sums hive parts for its size hint, so schema has
        to resolve the same layout — otherwise the catalog advertises a size for
        a table this surface 404s on (Devin Review on #1198)."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _write_hive_partitions(tmp_path / "extracts" / "jira" / "data", "issues")

        from app.api.v2_schema import build_schema_uncached

        row = self._row("issues") | {"source_type": "jira"}
        payload = build_schema_uncached(conn=None, table_id="issues", bq=object(), row=row)

        names = {c["name"] for c in payload["columns"]}
        # `resolution` proves union_by_name (only the December part has it);
        # `month` proves hive_partitioning turned the directory key into a column,
        # the same shape the Jira extract's own view exposes.
        assert {"v", "d", "resolution", "month"} <= names

    def test_empty_partition_directory_still_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "extracts" / "keboola" / "data" / "kbc_empty").mkdir(parents=True)

        from app.api.v2_schema import NotFound, build_schema_uncached

        with pytest.raises(NotFound):
            build_schema_uncached(conn=None, table_id="kbc_empty", bq=object(), row=self._row("kbc_empty"))


class TestScanSurface:
    """``/api/v2/scan`` executes locally against the parquet."""

    SCHEMA = {"v": "INT64", "d": "DATE"}

    @pytest.fixture
    def reload_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import importlib

        import src.db as db_module

        importlib.reload(db_module)
        yield db_module

    def _seed(self, conn, table_id: str):
        from src.db import SYSTEM_ADMIN_GROUP
        from src.repositories.table_registry import TableRegistryRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.users import UserRepository

        if UserRepository(conn).get_by_id("admin1") is None:
            UserRepository(conn).create(id="admin1", email="admin1@test.com", name="Admin")
        gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()
        if gid:
            UserGroupMembersRepository(conn).add_member("admin1", gid[0], source="system_seed")
        TableRegistryRepository(conn).register(
            id=table_id,
            name=table_id,
            source_type="keboola",
            bucket="in.c-main",
            source_table=table_id,
            query_mode="local",
        )

    def _run(self, reload_db, monkeypatch, req):
        from app.api import v2_scan

        monkeypatch.setattr(v2_scan, "_resolve_schema", lambda *a, **kw: dict(self.SCHEMA))
        conn = reload_db.get_system_db()
        try:
            self._seed(conn, req["table_id"])
            from connectors.bigquery.access import BqAccess, BqProjects

            return v2_scan.run_scan(
                conn,
                {"id": "admin1", "email": "a@x.com"},
                req,
                bq=BqAccess(BqProjects(billing="b", data="d")),
                quota=v2_scan._build_quota_tracker(),
            )
        finally:
            conn.close()

    def test_scan_reads_every_partition(self, reload_db, tmp_path, monkeypatch):
        _write_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.api.v2_arrow import parse_ipc_bytes

        ipc = self._run(reload_db, monkeypatch, {"table_id": "kbc_sales", "select": ["v"]})

        assert sorted(parse_ipc_bytes(ipc).column("v").to_pylist()) == [1, 2, 3]

    def test_scan_filters_across_partitions(self, reload_db, tmp_path, monkeypatch):
        _write_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.api.v2_arrow import parse_ipc_bytes

        ipc = self._run(
            reload_db,
            monkeypatch,
            {"table_id": "kbc_sales", "select": ["v"], "where": "v > 1", "order_by": ["v"]},
        )

        assert parse_ipc_bytes(ipc).column("v").to_pylist() == [2, 3]

    def test_scan_reads_every_hive_partition(self, reload_db, tmp_path, monkeypatch):
        """Same divergence as schema: the nested layout has to be scannable, or
        the catalog's size hint points at a table scan refuses to read."""
        _write_hive_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.api.v2_arrow import parse_ipc_bytes

        ipc = self._run(reload_db, monkeypatch, {"table_id": "kbc_sales", "select": ["v"]})

        assert sorted(parse_ipc_bytes(ipc).column("v").to_pylist()) == [1, 2, 3]

    def test_empty_partition_directory_still_raises_not_found(self, reload_db, tmp_path, monkeypatch):
        (tmp_path / "extracts" / "keboola" / "data" / "kbc_empty").mkdir(parents=True)

        with pytest.raises(FileNotFoundError):
            self._run(reload_db, monkeypatch, {"table_id": "kbc_empty", "select": ["v"]})
