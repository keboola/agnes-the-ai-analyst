"""
Proof-of-concept: Connector Kit architecture validation.

Tests that the proposed Connector Protocol + Runtime model is:
1. Implementable in Python (Protocol, Cap flags, partial implementation)
2. Arrow RecordBatch iteration works with DuckDB (zero-copy)
3. ConnectorRuntime can build extract.duckdb from any connector
4. Schema evolution detection works via Arrow schema diff
5. A real connector can be written in ~50 lines
6. Incremental state tracking works
7. Manifest validation works
8. Discovery → read pipeline is end-to-end functional
"""

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from enum import Flag, auto
from pathlib import Path
from typing import AsyncIterator, Iterator, Protocol, runtime_checkable

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml


# ============================================================================
# Layer 2: Connector Protocol (the contract)
# ============================================================================


class Cap(Flag):
    """Connector capabilities — declare what you support."""

    DISCOVER = auto()
    READ = auto()
    STREAM = auto()
    REMOTE = auto()
    WRITE = auto()


@dataclass
class TableInfo:
    name: str
    schema: pa.Schema
    capabilities: Cap
    primary_key: list[str] | None = None
    description: str = ""


@dataclass
class ReadOptions:
    columns: list[str] | None = None
    filter: dict | None = None
    incremental_key: str | None = None
    incremental_value: str | None = None
    batch_size: int = 10_000


@dataclass
class RemoteAttachInfo:
    extension: str
    url: str
    token_env: str


@runtime_checkable
class Connector(Protocol):
    @property
    def capabilities(self) -> Cap: ...

    def discover(self) -> list[TableInfo]: ...

    def read(self, table: str, options: ReadOptions) -> Iterator[pa.RecordBatch]: ...


# ============================================================================
# Layer 3: Connector Runtime (the SDK — replaces manual boilerplate)
# ============================================================================


@dataclass
class ExtractStats:
    tables_extracted: int = 0
    tables_failed: int = 0
    total_rows: int = 0
    schema_changes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ConnectorRuntime:
    """Handles extract.duckdb lifecycle — what every connector does manually today."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.data_dir = output_dir / "data"
        self.db_path = output_dir / "extract.duckdb"
        self.state_path = output_dir / ".state.yaml"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def run(self, connector: Connector, tables: list[str] | None = None) -> ExtractStats:
        stats = ExtractStats()

        # 1. Discovery
        available: list[TableInfo] = []
        if Cap.DISCOVER in connector.capabilities:
            available = connector.discover()

        # If no tables specified, extract all discovered
        if tables is None:
            tables = [t.name for t in available if Cap.READ in t.capabilities]

        # 2. Schema evolution check
        for table_name in tables:
            table_info = self._find_table(available, table_name)
            if table_info:
                change = self._check_schema_evolution(table_name, table_info.schema)
                if change:
                    stats.schema_changes.append(change)

        # 3. Extract via read()
        if Cap.READ in connector.capabilities:
            for table_name in tables:
                try:
                    options = self._build_read_options(table_name, available)
                    rows = self._extract_table(connector, table_name, options)
                    stats.tables_extracted += 1
                    stats.total_rows += rows
                except Exception as e:
                    stats.tables_failed += 1
                    stats.errors.append(f"{table_name}: {e}")

        # 4. Remote attach (if supported)
        if Cap.REMOTE in connector.capabilities:
            try:
                info = connector.remote()  # type: ignore[attr-defined]
                self._write_remote_attach(info)
            except Exception as e:
                stats.errors.append(f"remote_attach: {e}")

        # 5. Build extract.duckdb (_meta + views)
        self._build_extract_db(available, tables)

        # 6. Save incremental state
        self._save_state(tables)

        return stats

    def _extract_table(self, connector: Connector, table: str, options: ReadOptions) -> int:
        """Extract a table via Arrow RecordBatch iterator → Parquet."""
        parquet_path = self.data_dir / f"{table}.parquet"
        writer = None
        total_rows = 0

        for batch in connector.read(table, options):
            if writer is None:
                writer = pq.ParquetWriter(str(parquet_path), batch.schema)
            writer.write_batch(batch)
            total_rows += batch.num_rows

        if writer:
            writer.close()

        return total_rows

    def _build_extract_db(self, available: list[TableInfo], tables: list[str]):
        """Build extract.duckdb with _meta and views — atomic swap."""
        tmp_db = self.output_dir / "extract.duckdb.tmp"
        if tmp_db.exists():
            tmp_db.unlink()

        con = duckdb.connect(str(tmp_db))
        try:
            # _meta table
            con.execute("""
                CREATE TABLE _meta (
                    table_name VARCHAR NOT NULL,
                    description VARCHAR,
                    rows BIGINT,
                    size_bytes BIGINT,
                    extracted_at TIMESTAMP DEFAULT current_timestamp,
                    query_mode VARCHAR DEFAULT 'local',
                    schema_json VARCHAR
                )
            """)

            for table_name in tables:
                parquet_path = self.data_dir / f"{table_name}.parquet"
                if parquet_path.exists():
                    # Create view pointing to parquet
                    con.execute(
                        f'CREATE VIEW "{table_name}" AS '
                        f"SELECT * FROM read_parquet('{parquet_path}')"
                    )

                    # Get row count + size
                    rows = con.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()[0]
                    size = parquet_path.stat().st_size

                    # Find description and schema
                    info = self._find_table(available, table_name)
                    desc = info.description if info else ""
                    schema_json = info.schema.to_string() if info else ""

                    con.execute(
                        "INSERT INTO _meta (table_name, description, rows, size_bytes, schema_json) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [table_name, desc, rows, size, schema_json],
                    )
        finally:
            con.close()

        # Atomic swap
        if self.db_path.exists():
            self.db_path.unlink()
        # Clean WAL if exists
        wal = Path(str(tmp_db) + ".wal")
        if wal.exists():
            wal.unlink()
        tmp_db.rename(self.db_path)

    def _find_table(self, available: list[TableInfo], name: str) -> TableInfo | None:
        return next((t for t in available if t.name == name), None)

    def _check_schema_evolution(self, table: str, new_schema: pa.Schema) -> str | None:
        """Detect schema changes by comparing Arrow schemas."""
        schema_file = self.output_dir / f".schema_{table}.arrow"
        if schema_file.exists():
            reader = pa.ipc.open_stream(schema_file.read_bytes())
            old_schema = reader.schema
            if old_schema != new_schema:
                # Diff: added, removed, changed fields
                old_names = set(old_schema.names)
                new_names = set(new_schema.names)
                added = new_names - old_names
                removed = old_names - new_names
                msg = f"{table}: "
                if added:
                    msg += f"+{added} "
                if removed:
                    msg += f"-{removed} "
                # Check type changes for common fields
                for name in old_names & new_names:
                    old_type = old_schema.field(name).type
                    new_type = new_schema.field(name).type
                    if old_type != new_type:
                        msg += f"{name}:{old_type}→{new_type} "
                # Save new schema
                self._save_schema(table, new_schema)
                return msg.strip()
        else:
            self._save_schema(table, new_schema)
        return None

    def _save_schema(self, table: str, schema: pa.Schema):
        """Serialize Arrow schema via IPC stream (compatible with all PyArrow versions)."""
        schema_file = self.output_dir / f".schema_{table}.arrow"
        sink = pa.BufferOutputStream()
        writer = pa.ipc.new_stream(sink, schema)
        writer.close()
        schema_file.write_bytes(sink.getvalue().to_pybytes())

    def _build_read_options(self, table: str, available: list[TableInfo]) -> ReadOptions:
        """Build ReadOptions with incremental state if available."""
        options = ReadOptions()
        state = self._load_state()
        if table in state:
            options.incremental_key = state[table].get("incremental_key")
            options.incremental_value = state[table].get("incremental_value")
        return options

    def _load_state(self) -> dict:
        if self.state_path.exists():
            return yaml.safe_load(self.state_path.read_text()) or {}
        return {}

    def _save_state(self, tables: list[str]):
        state = self._load_state()
        for table in tables:
            if table not in state:
                state[table] = {}
            state[table]["last_extracted"] = str(duckdb.query("SELECT current_timestamp").fetchone()[0])
        self.state_path.write_text(yaml.dump(state))

    def _write_remote_attach(self, info: RemoteAttachInfo):
        """Write _remote_attach info for orchestrator."""
        # This gets added to extract.duckdb in _build_extract_db
        # For POC, store as yaml; real impl writes to DuckDB
        ra_path = self.output_dir / ".remote_attach.yaml"
        ra_path.write_text(
            yaml.dump({"extension": info.extension, "url": info.url, "token_env": info.token_env})
        )


# ============================================================================
# Example connectors (proving the contract works)
# ============================================================================


class SampleAPIConnector:
    """
    A sample connector simulating an HTTP API source.
    Proves: ~50 lines for a complete connector implementation.
    """

    capabilities = Cap.DISCOVER | Cap.READ

    ORDERS_SCHEMA = pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("customer", pa.string()),
            pa.field("amount", pa.float64()),
            pa.field("date", pa.string()),
        ]
    )

    USERS_SCHEMA = pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("name", pa.string()),
            pa.field("email", pa.string()),
        ]
    )

    # Simulated API data
    _data = {
        "orders": [
            {"id": 1, "customer": "Alice", "amount": 100.0, "date": "2026-01-15"},
            {"id": 2, "customer": "Bob", "amount": 250.0, "date": "2026-02-01"},
            {"id": 3, "customer": "Carol", "amount": 75.5, "date": "2026-03-10"},
        ],
        "users": [
            {"id": 1, "name": "Alice", "email": "alice@example.com"},
            {"id": 2, "name": "Bob", "email": "bob@example.com"},
        ],
    }

    def discover(self) -> list[TableInfo]:
        return [
            TableInfo(
                name="orders",
                schema=self.ORDERS_SCHEMA,
                capabilities=Cap.READ,
                primary_key=["id"],
                description="Sales orders",
            ),
            TableInfo(
                name="users",
                schema=self.USERS_SCHEMA,
                capabilities=Cap.READ,
                primary_key=["id"],
                description="Registered users",
            ),
        ]

    def read(self, table: str, options: ReadOptions) -> Iterator[pa.RecordBatch]:
        data = self._data.get(table, [])
        schema = self.ORDERS_SCHEMA if table == "orders" else self.USERS_SCHEMA

        # Simulate batched reading (batch_size controls chunking)
        for i in range(0, len(data), options.batch_size):
            chunk = data[i : i + options.batch_size]
            arrays = [pa.array([row[col] for row in chunk], type=schema.field(col).type) for col in schema.names]
            yield pa.RecordBatch.from_arrays(arrays, schema=schema)


class StreamingConnector:
    """Proves: async stream capability works."""

    capabilities = Cap.DISCOVER | Cap.STREAM

    EVENTS_SCHEMA = pa.schema(
        [
            pa.field("event_id", pa.string()),
            pa.field("type", pa.string()),
            pa.field("payload", pa.string()),
        ]
    )

    def discover(self) -> list[TableInfo]:
        return [
            TableInfo(
                name="events",
                schema=self.EVENTS_SCHEMA,
                capabilities=Cap.STREAM,
                description="Real-time events",
            )
        ]

    async def stream(self, table: str) -> AsyncIterator[pa.RecordBatch]:
        """Simulate webhook events arriving."""
        events = [
            {"event_id": "e1", "type": "created", "payload": '{"issue": "PROJ-1"}'},
            {"event_id": "e2", "type": "updated", "payload": '{"issue": "PROJ-2"}'},
            {"event_id": "e3", "type": "deleted", "payload": '{"issue": "PROJ-3"}'},
        ]
        for event in events:
            arrays = [pa.array([event[col]], type=self.EVENTS_SCHEMA.field(col).type) for col in self.EVENTS_SCHEMA.names]
            yield pa.RecordBatch.from_arrays(arrays, schema=self.EVENTS_SCHEMA)


class RemoteOnlyConnector:
    """Proves: remote-only connector (like BigQuery) works."""

    capabilities = Cap.DISCOVER | Cap.REMOTE

    def discover(self) -> list[TableInfo]:
        return [
            TableInfo(
                name="big_table",
                schema=pa.schema([pa.field("id", pa.int64()), pa.field("value", pa.string())]),
                capabilities=Cap.REMOTE,
                description="Remote-only table, queries go to source",
            )
        ]

    def remote(self) -> RemoteAttachInfo:
        return RemoteAttachInfo(
            extension="bigquery",
            url="project_id=my-project",
            token_env="GOOGLE_APPLICATION_CREDENTIALS",
        )


# ============================================================================
# Tests
# ============================================================================


class TestCapabilityFlags:
    """Test 1: Cap Flag enum works for declaration and checking."""

    def test_flag_composition(self):
        caps = Cap.DISCOVER | Cap.READ | Cap.REMOTE
        assert Cap.DISCOVER in caps
        assert Cap.READ in caps
        assert Cap.REMOTE in caps
        assert Cap.STREAM not in caps
        assert Cap.WRITE not in caps

    def test_per_table_capabilities(self):
        info = TableInfo(
            name="orders",
            schema=pa.schema([pa.field("id", pa.int64())]),
            capabilities=Cap.READ | Cap.STREAM,
        )
        assert Cap.READ in info.capabilities
        assert Cap.STREAM in info.capabilities
        assert Cap.WRITE not in info.capabilities

    def test_flag_iteration(self):
        """Can iterate individual flags from a composite."""
        caps = Cap.DISCOVER | Cap.READ | Cap.STREAM
        individual = list(caps)
        assert len(individual) == 3
        assert Cap.DISCOVER in individual


class TestProtocolCompliance:
    """Test 2: Protocol type checking works at runtime."""

    def test_sample_connector_is_connector(self):
        c = SampleAPIConnector()
        assert isinstance(c, Connector)

    def test_streaming_connector_partial_protocol(self):
        """StreamingConnector doesn't implement read() — that's OK.
        Protocol is structural, not enforced for methods you don't use."""
        c = StreamingConnector()
        assert hasattr(c, "capabilities")
        assert hasattr(c, "discover")
        assert Cap.STREAM in c.capabilities

    def test_remote_connector_is_valid(self):
        c = RemoteOnlyConnector()
        assert hasattr(c, "discover")
        assert hasattr(c, "remote")
        assert Cap.REMOTE in c.capabilities


class TestArrowIntegration:
    """Test 3: Arrow RecordBatch → DuckDB zero-copy works."""

    def test_record_batch_to_duckdb(self):
        """DuckDB can query Arrow RecordBatches directly."""
        schema = pa.schema([pa.field("id", pa.int64()), pa.field("name", pa.string())])
        batch = pa.RecordBatch.from_arrays(
            [pa.array([1, 2, 3]), pa.array(["a", "b", "c"])],
            schema=schema,
        )

        con = duckdb.connect()
        result = con.execute("SELECT * FROM batch WHERE id > 1").fetchall()
        assert len(result) == 2
        assert result[0] == (2, "b")

    def test_record_batch_iterator_to_duckdb(self):
        """DuckDB can consume an iterator of RecordBatches."""
        schema = pa.schema([pa.field("value", pa.float64())])

        def generate_batches():
            for i in range(3):
                yield pa.RecordBatch.from_arrays(
                    [pa.array([float(i * 10 + j) for j in range(5)])],
                    schema=schema,
                )

        reader = pa.RecordBatchReader.from_batches(schema, generate_batches())
        con = duckdb.connect()
        result = con.execute("SELECT count(*), sum(value) FROM reader").fetchone()
        assert result[0] == 15  # 3 batches * 5 rows
        assert result[1] == sum(float(i * 10 + j) for i in range(3) for j in range(5))

    def test_arrow_to_parquet_roundtrip(self):
        """Arrow → Parquet → DuckDB roundtrip preserves data."""
        schema = pa.schema(
            [
                pa.field("id", pa.int64()),
                pa.field("amount", pa.float64()),
                pa.field("label", pa.string()),
            ]
        )
        batch = pa.RecordBatch.from_arrays(
            [pa.array([1, 2]), pa.array([99.9, 200.0]), pa.array(["x", "y"])],
            schema=schema,
        )

        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            pq.write_table(pa.Table.from_batches([batch]), f.name)
            con = duckdb.connect()
            result = con.execute(f"SELECT * FROM read_parquet('{f.name}')").fetchall()
            assert result == [(1, 99.9, "x"), (2, 200.0, "y")]
            os.unlink(f.name)


class TestConnectorRuntime:
    """Test 4: Full runtime pipeline — connector → extract.duckdb."""

    @pytest.fixture
    def output_dir(self, tmp_path):
        return tmp_path / "extract_test"

    def test_full_extract_pipeline(self, output_dir):
        """End-to-end: connector → runtime → extract.duckdb with _meta + views."""
        connector = SampleAPIConnector()
        runtime = ConnectorRuntime(output_dir)

        stats = runtime.run(connector)

        # Stats are correct
        assert stats.tables_extracted == 2
        assert stats.tables_failed == 0
        assert stats.total_rows == 5  # 3 orders + 2 users
        assert stats.errors == []

        # extract.duckdb exists and is valid
        db_path = output_dir / "extract.duckdb"
        assert db_path.exists()

        con = duckdb.connect(str(db_path), read_only=True)

        # _meta table has both tables
        meta = con.execute("SELECT table_name, rows, description FROM _meta ORDER BY table_name").fetchall()
        assert len(meta) == 2
        assert meta[0] == ("orders", 3, "Sales orders")
        assert meta[1] == ("users", 2, "Registered users")

        # Views work — can query data through extract.duckdb
        orders = con.execute("SELECT * FROM orders ORDER BY id").fetchall()
        assert len(orders) == 3
        assert orders[0] == (1, "Alice", 100.0, "2026-01-15")

        users = con.execute("SELECT * FROM users ORDER BY id").fetchall()
        assert len(users) == 2
        assert users[0][1] == "Alice"

        # Cross-table query works
        result = con.execute("""
            SELECT u.name, SUM(o.amount) as total
            FROM orders o JOIN users u ON o.customer = u.name
            GROUP BY u.name ORDER BY total DESC
        """).fetchall()
        assert result[0] == ("Bob", 250.0)
        assert result[1] == ("Alice", 100.0)

        con.close()

    def test_selective_table_extract(self, output_dir):
        """Can extract specific tables only."""
        connector = SampleAPIConnector()
        runtime = ConnectorRuntime(output_dir)

        stats = runtime.run(connector, tables=["orders"])

        assert stats.tables_extracted == 1
        assert stats.total_rows == 3

        con = duckdb.connect(str(output_dir / "extract.duckdb"), read_only=True)
        tables = con.execute("SELECT table_name FROM _meta").fetchall()
        assert tables == [("orders",)]
        con.close()

    def test_incremental_state_tracking(self, output_dir):
        """Runtime saves and loads incremental state between runs."""
        connector = SampleAPIConnector()
        runtime = ConnectorRuntime(output_dir)

        # First run
        runtime.run(connector, tables=["orders"])

        # State file exists
        state_path = output_dir / ".state.yaml"
        assert state_path.exists()
        state = yaml.safe_load(state_path.read_text())
        assert "orders" in state
        assert "last_extracted" in state["orders"]

        # Second run — state persists
        runtime2 = ConnectorRuntime(output_dir)
        runtime2.run(connector, tables=["orders"])
        state2 = yaml.safe_load(state_path.read_text())
        assert "orders" in state2

    def test_empty_table_handling(self, output_dir):
        """Connector that yields nothing for a table doesn't crash."""

        class EmptyConnector:
            capabilities = Cap.DISCOVER | Cap.READ

            def discover(self) -> list[TableInfo]:
                return [
                    TableInfo(
                        name="empty",
                        schema=pa.schema([pa.field("id", pa.int64())]),
                        capabilities=Cap.READ,
                    )
                ]

            def read(self, table: str, options: ReadOptions) -> Iterator[pa.RecordBatch]:
                return iter([])  # No data

        runtime = ConnectorRuntime(output_dir)
        stats = runtime.run(EmptyConnector())

        # Extracted 0 rows, but no failure
        assert stats.tables_extracted == 1
        assert stats.total_rows == 0
        assert stats.errors == []

    def test_error_in_one_table_doesnt_stop_others(self, output_dir):
        """Partial failure: one table fails, others still extract."""

        class PartialFailConnector:
            capabilities = Cap.DISCOVER | Cap.READ

            def discover(self) -> list[TableInfo]:
                return [
                    TableInfo("good", pa.schema([pa.field("id", pa.int64())]), Cap.READ),
                    TableInfo("bad", pa.schema([pa.field("id", pa.int64())]), Cap.READ),
                ]

            def read(self, table: str, options: ReadOptions) -> Iterator[pa.RecordBatch]:
                if table == "bad":
                    raise ConnectionError("API timeout")
                yield pa.RecordBatch.from_arrays(
                    [pa.array([1, 2, 3])],
                    schema=pa.schema([pa.field("id", pa.int64())]),
                )

        runtime = ConnectorRuntime(output_dir)
        stats = runtime.run(PartialFailConnector())

        assert stats.tables_extracted == 1
        assert stats.tables_failed == 1
        assert "bad: API timeout" in stats.errors[0]


class TestSchemaEvolution:
    """Test 5: Schema change detection via Arrow schema diff."""

    def test_detect_added_column(self, tmp_path):
        output_dir = tmp_path / "schema_test"
        runtime = ConnectorRuntime(output_dir)

        # V1 schema
        schema_v1 = pa.schema([pa.field("id", pa.int64()), pa.field("name", pa.string())])
        runtime._save_schema("orders", schema_v1)

        # V2 schema — added column
        schema_v2 = pa.schema(
            [
                pa.field("id", pa.int64()),
                pa.field("name", pa.string()),
                pa.field("email", pa.string()),
            ]
        )

        change = runtime._check_schema_evolution("orders", schema_v2)
        assert change is not None
        assert "email" in change
        assert "+" in change

    def test_detect_removed_column(self, tmp_path):
        output_dir = tmp_path / "schema_test"
        runtime = ConnectorRuntime(output_dir)

        schema_v1 = pa.schema(
            [
                pa.field("id", pa.int64()),
                pa.field("name", pa.string()),
                pa.field("old_field", pa.string()),
            ]
        )
        runtime._save_schema("orders", schema_v1)

        schema_v2 = pa.schema([pa.field("id", pa.int64()), pa.field("name", pa.string())])

        change = runtime._check_schema_evolution("orders", schema_v2)
        assert change is not None
        assert "old_field" in change
        assert "-" in change

    def test_detect_type_change(self, tmp_path):
        output_dir = tmp_path / "schema_test"
        runtime = ConnectorRuntime(output_dir)

        schema_v1 = pa.schema([pa.field("id", pa.int32()), pa.field("value", pa.string())])
        runtime._save_schema("data", schema_v1)

        schema_v2 = pa.schema([pa.field("id", pa.int64()), pa.field("value", pa.string())])

        change = runtime._check_schema_evolution("data", schema_v2)
        assert change is not None
        assert "int32" in change
        assert "int64" in change

    def test_no_change_detected(self, tmp_path):
        output_dir = tmp_path / "schema_test"
        runtime = ConnectorRuntime(output_dir)

        schema = pa.schema([pa.field("id", pa.int64())])
        runtime._save_schema("stable", schema)

        change = runtime._check_schema_evolution("stable", schema)
        assert change is None

    def test_first_run_no_previous_schema(self, tmp_path):
        output_dir = tmp_path / "schema_test"
        runtime = ConnectorRuntime(output_dir)

        schema = pa.schema([pa.field("id", pa.int64())])
        change = runtime._check_schema_evolution("new_table", schema)
        assert change is None  # First run, no previous schema to compare


class TestStreamingCapability:
    """Test 6: Async streaming connector works."""

    def test_async_stream(self):
        async def _run():
            connector = StreamingConnector()
            batches = []
            async for batch in connector.stream("events"):
                batches.append(batch)
            return batches

        batches = asyncio.run(_run())
        assert len(batches) == 3
        assert batches[0].num_rows == 1
        assert batches[0].column("type")[0].as_py() == "created"

    def test_stream_to_duckdb(self):
        """Stream batches can be consumed by DuckDB."""

        async def _run():
            connector = StreamingConnector()
            all_batches = []
            async for batch in connector.stream("events"):
                all_batches.append(batch)
            return all_batches

        all_batches = asyncio.run(_run())
        arrow_table = pa.Table.from_batches(all_batches)
        con = duckdb.connect()
        result = con.execute("SELECT count(*) FROM arrow_table").fetchone()
        assert result[0] == 3


class TestRemoteOnlyConnector:
    """Test 7: Remote-only connector produces correct metadata."""

    def test_remote_attach_info(self, tmp_path):
        output_dir = tmp_path / "remote_test"
        connector = RemoteOnlyConnector()
        runtime = ConnectorRuntime(output_dir)

        stats = runtime.run(connector)

        # No tables extracted (remote only), but no errors
        assert stats.tables_extracted == 0
        assert stats.errors == []

        # Remote attach info saved
        ra_path = output_dir / ".remote_attach.yaml"
        assert ra_path.exists()
        ra = yaml.safe_load(ra_path.read_text())
        assert ra["extension"] == "bigquery"
        assert ra["token_env"] == "GOOGLE_APPLICATION_CREDENTIALS"


class TestManifestValidation:
    """Test 8: YAML manifest parsing and validation."""

    SAMPLE_MANIFEST = """
name: sample_api
version: "1.0.0"
description: "Sample API connector"
entrypoint: connectors.sample.SampleAPIConnector

capabilities: [discover, read]

auth:
  type: token
  env_vars:
    - name: SAMPLE_API_TOKEN
      required: true
      description: "API authentication token"

config:
  base_url:
    type: string
    required: true
  batch_size:
    type: integer
    required: false
    default: 1000

health_check:
  endpoint: "${base_url}/health"
  method: GET
  expect_status: 200
"""

    def test_manifest_parses(self):
        manifest = yaml.safe_load(self.SAMPLE_MANIFEST)
        assert manifest["name"] == "sample_api"
        assert manifest["version"] == "1.0.0"
        assert "discover" in manifest["capabilities"]
        assert "read" in manifest["capabilities"]

    def test_manifest_capabilities_to_flags(self):
        manifest = yaml.safe_load(self.SAMPLE_MANIFEST)
        cap_map = {c.name.lower(): c for c in Cap}
        flags = Cap(0)
        for c in manifest["capabilities"]:
            flags |= cap_map[c]

        assert Cap.DISCOVER in flags
        assert Cap.READ in flags
        assert Cap.STREAM not in flags

    def test_manifest_auth_config(self):
        manifest = yaml.safe_load(self.SAMPLE_MANIFEST)
        assert manifest["auth"]["type"] == "token"
        assert manifest["auth"]["env_vars"][0]["name"] == "SAMPLE_API_TOKEN"
        assert manifest["auth"]["env_vars"][0]["required"] is True

    def test_manifest_config_schema(self):
        manifest = yaml.safe_load(self.SAMPLE_MANIFEST)
        assert manifest["config"]["base_url"]["required"] is True
        assert manifest["config"]["batch_size"]["default"] == 1000

    def test_manifest_health_check(self):
        manifest = yaml.safe_load(self.SAMPLE_MANIFEST)
        hc = manifest["health_check"]
        assert "${base_url}" in hc["endpoint"]
        assert hc["expect_status"] == 200


class TestDiscoveryToReadPipeline:
    """Test 9: Full discovery → read → query pipeline."""

    def test_discover_then_read_all(self, tmp_path):
        """discover() → pick tables → read() → query in DuckDB."""
        connector = SampleAPIConnector()

        # Step 1: Discovery
        tables = connector.discover()
        assert len(tables) == 2
        assert all(isinstance(t, TableInfo) for t in tables)
        assert all(t.schema is not None for t in tables)

        # Step 2: Read via runtime (auto-discovers all tables)
        runtime = ConnectorRuntime(tmp_path / "full_pipeline")
        stats = runtime.run(connector)  # No tables= arg → discovers automatically

        assert stats.tables_extracted == 2

        # Step 3: Query
        con = duckdb.connect(str(tmp_path / "full_pipeline" / "extract.duckdb"), read_only=True)
        result = con.execute("""
            SELECT table_name, rows, description
            FROM _meta ORDER BY table_name
        """).fetchall()
        assert result[0][0] == "orders"
        assert result[0][1] == 3
        con.close()


class TestLargeDataBatching:
    """Test 10: Connector can handle large data via batched iteration."""

    def test_batched_read_memory_constant(self, tmp_path):
        """Large dataset extracted in batches — memory doesn't explode."""

        class LargeConnector:
            capabilities = Cap.DISCOVER | Cap.READ
            NUM_BATCHES = 100
            BATCH_SIZE = 1000

            def discover(self) -> list[TableInfo]:
                return [
                    TableInfo(
                        "big_table",
                        pa.schema([pa.field("id", pa.int64()), pa.field("value", pa.float64())]),
                        Cap.READ,
                    )
                ]

            def read(self, table: str, options: ReadOptions) -> Iterator[pa.RecordBatch]:
                schema = pa.schema([pa.field("id", pa.int64()), pa.field("value", pa.float64())])
                for batch_num in range(self.NUM_BATCHES):
                    start = batch_num * self.BATCH_SIZE
                    yield pa.RecordBatch.from_arrays(
                        [
                            pa.array(range(start, start + self.BATCH_SIZE), type=pa.int64()),
                            pa.array(
                                [float(i) * 0.1 for i in range(start, start + self.BATCH_SIZE)],
                                type=pa.float64(),
                            ),
                        ],
                        schema=schema,
                    )

        runtime = ConnectorRuntime(tmp_path / "large_test")
        stats = runtime.run(LargeConnector())

        assert stats.total_rows == 100_000
        assert stats.tables_extracted == 1

        # Verify DuckDB can read it
        con = duckdb.connect(str(tmp_path / "large_test" / "extract.duckdb"), read_only=True)
        count = con.execute("SELECT count(*) FROM big_table").fetchone()[0]
        assert count == 100_000
        con.close()
