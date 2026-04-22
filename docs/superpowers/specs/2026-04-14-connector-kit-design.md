# Connector Kit — Design Spec

**Date:** 2026-04-14
**Status:** Draft
**Scope:** Standardized connector SDK replacing ad-hoc extractor implementations
**Issue:** [#5 — RFC: Connector SDK](https://github.com/keboola/agnes-the-ai-analyst/issues/5)
**POC:** `tests/test_connector_kit_poc.py` (29/29 passing)

---

## 1. Problem Statement

The platform currently has three connectors (Keboola, BigQuery, Jira), each written ad-hoc with different interfaces:

| Connector | Entry point | Capabilities | Lines |
|-----------|-------------|-------------|-------|
| Keboola | `run(output_dir, table_configs, url, token)` | batch + remote | ~300 |
| BigQuery | `init_extract(output_dir, project_id, table_configs)` | remote only | ~150 |
| Jira | `init_extract(output_dir)` + `update_meta(output_dir, table)` | batch + webhook | ~200 |

All three produce `extract.duckdb` with `_meta` tables, but each re-implements:
- DuckDB file creation and atomic swap with WAL cleanup
- `_meta` table management (slightly different schemas across connectors)
- `_remote_attach` table (duplicated SQL)
- Error handling and progress reporting
- Parquet writing logic

Adding a new connector requires studying existing implementations and copying ~100 lines of boilerplate. There is no formal interface, no discovery mechanism, no schema evolution tracking, and no contract tests.

### Design goals

1. **New connector in ~50-80 lines** — author writes only API-specific code
2. **Formal contract** — Python Protocol with explicit capabilities
3. **Discovery built-in** — `discover()` returns available tables + Arrow schemas
4. **Schema evolution** — automatic detection of added/removed/changed columns
5. **Backward compatible** — existing connectors keep working, migrate incrementally
6. **Tested** — contract tests that any connector can run against itself

### Non-goals

- Replacing DuckDB as the query engine
- Building a full ETL framework (we are not dlt/Airbyte)
- Supporting non-Python connectors (future consideration, not this spec)
- SQL translation layer (we are not CData — DuckDB IS our SQL engine)

---

## 2. Architecture

### Layer model

```
┌──────────────────────────────────────────────────────────────┐
│  Layer 3: ConnectorRuntime                                   │
│  extract.duckdb lifecycle, schema tracking, state mgmt,      │
│  retry, progress reporting, contract tests, CLI scaffold     │
├──────────────────────────────────────────────────────────────┤
│  Layer 2: Connector Protocol                                 │
│  discover() → read() → stream() → remote()                  │
│  Python Protocol — implement only what you support           │
├──────────────────────────────────────────────────────────────┤
│  Layer 1: API client (external, not our concern)             │
│  HTTP calls, auth, pagination — raw data from source         │
│  May be hand-written or generated via driver_builder         │
└──────────────────────────────────────────────────────────────┘
```

### Data flow

```
Connector.discover()
    │
    ▼
ConnectorRuntime.run()
    ├─ Cap.READ  → Connector.read(table, options) → Iterator[pa.RecordBatch]
    │                                                  │
    │                                          ParquetBatchWriter
    │                                                  │
    │                                          data/{table}.parquet
    │
    ├─ Cap.STREAM → Connector.stream(table) → AsyncIterator[pa.RecordBatch]
    │                                                  │
    │                                          PartitionedParquetWriter
    │                                                  │
    │                                          data/{table}/YYYY-MM.parquet
    │
    ├─ Cap.REMOTE → Connector.remote() → RemoteAttachInfo
    │                                          │
    │                                   _remote_attach table
    │
    └─ finalize → extract.duckdb (_meta + views, atomic swap)
                         │
              SyncOrchestrator.rebuild()  (unchanged)
                         │
                  analytics.duckdb
```

### Relationship to existing code

| Current | After Connector Kit | Change |
|---------|-------------------|--------|
| `connectors/keboola/extractor.py:run()` | `KeboolaConnector` class + `ConnectorRuntime` | Refactor |
| `connectors/bigquery/extractor.py:init_extract()` | `BigQueryConnector` class + `ConnectorRuntime` | Refactor |
| `connectors/jira/extract_init.py` + `webhook.py` | `JiraConnector` class + `ConnectorRuntime` | Refactor |
| `src/orchestrator.py` | Unchanged — still reads extract.duckdb | No change |
| `app/api/sync.py` subprocess pattern | Updated to use `ConnectorRuntime.run()` | Minor change |

---

## 3. Connector Protocol

### 3.1 Capability flags

```python
# File: src/connector_kit/protocol.py

from enum import Flag, auto

class Cap(Flag):
    """Capabilities a connector can declare.

    Uses Flag enum for composability: Cap.READ | Cap.DISCOVER
    Check membership: Cap.READ in connector.capabilities
    Iterate: list(connector.capabilities) → individual flags
    """
    DISCOVER = auto()   # Can list tables + schemas from source
    READ     = auto()   # Can download data in batches (full or incremental)
    STREAM   = auto()   # Can receive continuous changes (webhooks, CDC)
    REMOTE   = auto()   # Can configure DuckDB extension pass-through
    WRITE    = auto()   # Can push data back to source
```

**Design decision: `Flag` over `set[str]`.**
Flag enum is type-safe, composable (`|`, `in`), iterable, and serializable to/from YAML via name mapping. Validated in POC test `TestCapabilityFlags`.

### 3.2 Data types

```python
# File: src/connector_kit/protocol.py

from dataclasses import dataclass, field
import pyarrow as pa

@dataclass
class TableInfo:
    """Describes a table available in the source."""
    name: str                              # View name in analytics.duckdb
    schema: pa.Schema                      # Arrow schema with types + nullability
    capabilities: Cap                      # Per-table capabilities (subset of connector caps)
    primary_key: list[str] | None = None   # For merge/upsert strategies
    description: str = ""                  # Human-readable, stored in _meta

@dataclass
class ReadOptions:
    """Options passed to read() — runtime builds these from state + config."""
    columns: list[str] | None = None       # Projection pushdown (None = all)
    filter: dict | None = None             # Filter pushdown: {"date": {">=": "2026-01-01"}}
    incremental_key: str | None = None     # Column name for incremental extraction
    incremental_value: str | None = None   # Last known value (from previous run state)
    batch_size: int = 10_000               # Rows per RecordBatch yield

@dataclass
class RemoteAttachInfo:
    """Configuration for DuckDB extension pass-through."""
    extension: str      # DuckDB extension name: 'keboola', 'bigquery'
    url: str            # Connection string for ATTACH
    token_env: str      # Environment variable name holding auth token (NOT the token)
    alias: str = ""     # DuckDB alias; defaults to extension name

@dataclass
class ExtractStats:
    """Returned by ConnectorRuntime.run() — replaces ad-hoc result dicts."""
    tables_extracted: int = 0
    tables_failed: int = 0
    total_rows: int = 0
    schema_changes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
```

**Why Arrow schema?**
- DuckDB consumes Arrow zero-copy (`SELECT * FROM batch`)
- Schema evolution is diffable: added/removed fields, type changes
- Cross-language (Rust, C++ connectors can produce Arrow)
- Parquet IS Arrow on disk — no conversion needed
- Validated in POC: `TestArrowIntegration` (3 tests)

### 3.3 Protocol definition

```python
# File: src/connector_kit/protocol.py

from typing import Protocol, Iterator, AsyncIterator, runtime_checkable

@runtime_checkable
class Connector(Protocol):
    """
    Structural typing contract for connectors.

    Implement only the methods matching your declared capabilities.
    The runtime checks capabilities before calling methods, so unimplemented
    methods are never invoked.

    Why Protocol over ABC:
    - Structural subtyping (duck typing) — no inheritance required
    - isinstance() check works at runtime via @runtime_checkable
    - Partial implementation is natural — no NotImplementedError stubs
    - Plays well with dataclasses and existing code
    """

    @property
    def capabilities(self) -> Cap:
        """Declare what this connector supports. Required by all connectors."""
        ...

    def discover(self) -> list[TableInfo]:
        """List available tables in the source with their schemas.

        Called by runtime before extraction to:
        - Auto-populate table list if none specified
        - Detect schema evolution (compare with previous run)
        - Provide discovery in CLI: `da connector discover <name>`

        Required when: Cap.DISCOVER in capabilities
        """
        ...

    def read(self, table: str, options: ReadOptions) -> Iterator[pa.RecordBatch]:
        """Extract data from a table as Arrow RecordBatch stream.

        MUST yield RecordBatch objects — not dicts, not DataFrames.
        Each batch should contain `options.batch_size` rows (approximately).
        The runtime writes batches to Parquet incrementally (constant memory).

        For incremental extraction:
        - Check options.incremental_key and options.incremental_value
        - Only yield rows where incremental_key > incremental_value
        - Runtime tracks state between runs automatically

        Required when: Cap.READ in capabilities
        """
        ...

    def stream(self, table: str) -> AsyncIterator[pa.RecordBatch]:
        """Receive continuous changes as Arrow RecordBatch stream.

        Each yield = one event or micro-batch of events.
        Runtime handles:
        - Writing to partitioned parquets (YYYY-MM.parquet)
        - File locking for concurrent webhook writes
        - _meta updates after each write

        Required when: Cap.STREAM in capabilities
        """
        ...

    def remote(self) -> RemoteAttachInfo:
        """Provide DuckDB extension pass-through configuration.

        The runtime writes this to _remote_attach table in extract.duckdb.
        The orchestrator reads it and re-ATTACHes the extension at query time.

        IMPORTANT: Never include actual tokens — only env var names.

        Required when: Cap.REMOTE in capabilities
        """
        ...
```

**Validated in POC:** `TestProtocolCompliance` confirms `isinstance(connector, Connector)` works, and partial implementations (e.g., stream-only connector without `read()`) are accepted.

---

## 4. Connector Manifest

### 4.1 Format

Each connector has a `connector.yaml` in its directory:

```yaml
# File: connectors/{name}/connector.yaml

name: keboola                    # Unique identifier, matches directory name
version: "1.0.0"                 # Semver
description: "Keboola Storage connector — batch extraction and remote query"
entrypoint: connectors.keboola.connector.KeboolaConnector  # Python import path

capabilities: [discover, read, remote]  # Maps to Cap flags

auth:
  type: token                    # token | oauth | basic | service_account | none
  env_vars:
    - name: KEBOOLA_STORAGE_TOKEN
      required: true
      description: "Keboola Storage API token"

config:                          # Connector-specific config (JSON Schema subset)
  url:
    type: string
    format: uri
    required: true
    description: "Keboola stack URL (e.g., https://connection.keboola.com)"
  bucket:
    type: string
    required: false
    description: "Default bucket for table extraction"

health_check:                    # Optional: runtime calls before extraction
  endpoint: "${url}/v2/storage"
  method: GET
  headers:
    X-StorageApi-Token: "${KEBOOLA_STORAGE_TOKEN}"
  expect_status: 200
  timeout_seconds: 10
```

### 4.2 Manifest loading

```python
# File: src/connector_kit/manifest.py

@dataclass
class ConnectorManifest:
    name: str
    version: str
    description: str
    entrypoint: str
    capabilities: Cap
    auth: dict
    config: dict
    health_check: dict | None = None

    @classmethod
    def load(cls, path: Path) -> "ConnectorManifest":
        """Load and validate connector.yaml."""
        data = yaml.safe_load(path.read_text())
        # Map capability strings to Cap flags
        cap_map = {c.name.lower(): c for c in Cap}
        caps = Cap(0)
        for c in data["capabilities"]:
            if c not in cap_map:
                raise ValueError(f"Unknown capability: {c}. Valid: {list(cap_map)}")
            caps |= cap_map[c]
        return cls(
            name=data["name"],
            version=data["version"],
            description=data["description"],
            entrypoint=data["entrypoint"],
            capabilities=caps,
            auth=data.get("auth", {}),
            config=data.get("config", {}),
            health_check=data.get("health_check"),
        )

    def instantiate(self, config: dict) -> Connector:
        """Import and instantiate the connector class."""
        module_path, class_name = self.entrypoint.rsplit(".", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls(config)
```

**Validated in POC:** `TestManifestValidation` (5 tests) confirms YAML parsing, capability mapping, auth config, and health check extraction.

---

## 5. Connector Runtime

### 5.1 Responsibilities

The runtime replaces all boilerplate currently duplicated across connectors:

| Responsibility | Currently | Runtime handles |
|----------------|-----------|----------------|
| Create output_dir + data/ | Each connector | `__init__()` |
| Create extract.duckdb | Each connector | `_build_extract_db()` |
| Create _meta table | Each connector (slightly different schemas) | `_build_extract_db()` |
| Create _remote_attach | Keboola + BigQuery | `_write_remote_attach()` |
| Write parquets from data | Each connector | `_extract_table()` |
| Atomic swap + WAL cleanup | Each connector | `_atomic_swap()` |
| Error handling per table | Each connector | `run()` try/except loop |
| Schema tracking | Nobody | `_check_schema_evolution()` |
| Incremental state | Nobody (Jira has manual partitioning) | `_save_state()` / `_load_state()` |
| Progress reporting | Nobody | `_report_progress()` callback |

### 5.2 Implementation

```python
# File: src/connector_kit/runtime.py

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

class ConnectorRuntime:
    """Manages the extract.duckdb lifecycle for any Connector implementation."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.data_dir = output_dir / "data"
        self.db_path = output_dir / "extract.duckdb"
        self.state_path = output_dir / ".state.yaml"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_identifier(name: str) -> bool:
        """Validate DuckDB identifier. Same regex as src/orchestrator.py."""
        return bool(_SAFE_IDENTIFIER.match(name))

    def run(
        self,
        connector: Connector,
        tables: list[str] | None = None,
        on_progress: Callable[[str, int], None] | None = None,
    ) -> ExtractStats:
        """Execute the full extraction pipeline.

        Args:
            connector: Any object satisfying the Connector protocol.
            tables: Specific tables to extract. None = auto-discover all.
            on_progress: Optional callback(table_name, rows_so_far).

        Returns:
            ExtractStats with counts, errors, and schema changes.
        """
        stats = ExtractStats()

        # --- Phase 1: Discovery ---
        available: list[TableInfo] = []
        if Cap.DISCOVER in connector.capabilities:
            available = connector.discover()

        if tables is None:
            tables = [t.name for t in available if Cap.READ in t.capabilities]

        # Validate all table names (SQL injection prevention)
        for name in tables:
            if not self._validate_identifier(name):
                raise ValueError(f"Invalid table name: {name!r} (must match {_SAFE_IDENTIFIER.pattern})")

        # --- Phase 2: Schema evolution check ---
        for table_name in tables:
            table_info = self._find_table(available, table_name)
            if table_info:
                change = self._check_schema_evolution(table_name, table_info.schema)
                if change:
                    stats.schema_changes.append(change)

        # --- Phase 3: Batch extraction ---
        if Cap.READ in connector.capabilities:
            for table_name in tables:
                try:
                    options = self._build_read_options(table_name)
                    rows = self._extract_table(connector, table_name, options, on_progress)
                    stats.tables_extracted += 1
                    stats.total_rows += rows
                except Exception as e:
                    stats.tables_failed += 1
                    stats.errors.append(f"{table_name}: {e}")
                    logger.exception("Failed to extract table %s", table_name)

        # --- Phase 4: Remote attach ---
        if Cap.REMOTE in connector.capabilities:
            try:
                info = connector.remote()
                self._write_remote_attach(info)
            except Exception as e:
                stats.errors.append(f"remote_attach: {e}")

        # --- Phase 5: Build extract.duckdb ---
        self._build_extract_db(available, tables)

        # --- Phase 6: Save state ---
        self._save_state(tables)

        return stats
```

### 5.3 Extract table (Arrow → Parquet)

```python
    def _extract_table(
        self,
        connector: Connector,
        table: str,
        options: ReadOptions,
        on_progress: Callable | None,
    ) -> int:
        """Extract via Arrow RecordBatch iterator → single Parquet file.

        Memory usage is constant regardless of table size — each batch
        is written and then discarded. Validated with 100K rows in POC.
        """
        parquet_path = self.data_dir / f"{table}.parquet"
        writer: pq.ParquetWriter | None = None
        total_rows = 0

        try:
            for batch in connector.read(table, options):
                if writer is None:
                    writer = pq.ParquetWriter(
                        str(parquet_path),
                        batch.schema,
                        compression="zstd",
                    )
                writer.write_batch(batch)
                total_rows += batch.num_rows
                if on_progress:
                    on_progress(table, total_rows)
        finally:
            if writer:
                writer.close()

        return total_rows
```

**Key details:**
- `compression="zstd"` — best compression/speed tradeoff for analytical data
- Writer is lazy-initialized from first batch schema (handles empty tables)
- `finally` ensures parquet file is properly closed even on errors
- Validated in POC: `TestLargeDataBatching` (100 batches x 1000 rows)

### 5.4 Build extract.duckdb

```python
    def _build_extract_db(self, available: list[TableInfo], tables: list[str]):
        """Build extract.duckdb with _meta + views. Atomic swap.

        Produces the same contract as current connectors — orchestrator
        sees no difference. _meta schema matches existing convention with
        one addition: schema_json for evolution tracking.
        """
        tmp_db = self.output_dir / "extract.duckdb.tmp"
        if tmp_db.exists():
            tmp_db.unlink()

        con = duckdb.connect(str(tmp_db))
        try:
            # _meta table — matches existing schema + schema_json column
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

            # _remote_attach table (if .remote_attach.yaml exists)
            ra_path = self.output_dir / ".remote_attach.yaml"
            if ra_path.exists():
                ra = yaml.safe_load(ra_path.read_text())
                con.execute("""
                    CREATE TABLE _remote_attach (
                        alias VARCHAR,
                        extension VARCHAR,
                        url VARCHAR,
                        token_env VARCHAR
                    )
                """)
                con.execute(
                    "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
                    [
                        ra.get("alias") or ra["extension"],
                        ra["extension"],
                        ra["url"],
                        ra["token_env"],
                    ],
                )

            # Views and _meta entries for each extracted table
            for table_name in tables:
                parquet_path = self.data_dir / f"{table_name}.parquet"
                if parquet_path.exists():
                    con.execute(
                        f'CREATE VIEW "{table_name}" AS '
                        f"SELECT * FROM read_parquet('{parquet_path}')"
                    )
                    rows = con.execute(
                        f'SELECT count(*) FROM "{table_name}"'
                    ).fetchone()[0]
                    size = parquet_path.stat().st_size
                elif Cap.REMOTE in (self._find_table(available, table_name) or TableInfo(
                    name="", schema=pa.schema([]), capabilities=Cap(0)
                )).capabilities:
                    # Remote-only table — no parquet, just _meta entry
                    rows = 0
                    size = 0
                else:
                    continue

                info = self._find_table(available, table_name)
                desc = info.description if info else ""
                schema_str = info.schema.to_string() if info else ""

                con.execute(
                    "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, ?, ?)",
                    [table_name, desc, rows, size, "local", schema_str],
                )

            con.execute("CHECKPOINT")
        finally:
            con.close()

        # Atomic swap (same pattern as existing connectors)
        self._atomic_swap(tmp_db, self.db_path)
```

### 5.5 Atomic swap

```python
    @staticmethod
    def _atomic_swap(tmp_path: Path, target_path: Path):
        """Atomic DB swap with WAL cleanup.

        Same pattern used by all existing connectors — ensures readers
        on the old file continue uninterrupted (Unix inode semantics).
        """
        # Remove old WAL
        old_wal = Path(str(target_path) + ".wal")
        if old_wal.exists():
            old_wal.unlink()

        # Remove old DB
        if target_path.exists():
            target_path.unlink()

        # Clean temp WAL before move
        tmp_wal = Path(str(tmp_path) + ".wal")
        if tmp_wal.exists():
            tmp_wal.unlink()

        # Atomic move
        tmp_path.rename(target_path)
```

### 5.6 Schema evolution detection

```python
    def _check_schema_evolution(self, table: str, new_schema: pa.Schema) -> str | None:
        """Compare Arrow schemas between runs. Returns human-readable diff or None.

        Serializes schemas via Arrow IPC stream format (compatible with all
        PyArrow versions including 23.x). Validated in POC: TestSchemaEvolution.
        """
        schema_file = self.output_dir / f".schema_{table}.arrow"

        if schema_file.exists():
            reader = pa.ipc.open_stream(schema_file.read_bytes())
            old_schema = reader.schema

            if old_schema != new_schema:
                old_names = set(old_schema.names)
                new_names = set(new_schema.names)
                added = new_names - old_names
                removed = old_names - new_names

                parts = [f"{table}:"]
                if added:
                    parts.append(f"added {added}")
                if removed:
                    parts.append(f"removed {removed}")
                for name in old_names & new_names:
                    old_t = old_schema.field(name).type
                    new_t = new_schema.field(name).type
                    if old_t != new_t:
                        parts.append(f"{name}: {old_t} → {new_t}")

                self._save_schema(table, new_schema)
                return " ".join(parts)

        # First run or no change
        self._save_schema(table, new_schema)
        return None

    def _save_schema(self, table: str, schema: pa.Schema):
        schema_file = self.output_dir / f".schema_{table}.arrow"
        sink = pa.BufferOutputStream()
        writer = pa.ipc.new_stream(sink, schema)
        writer.close()
        schema_file.write_bytes(sink.getvalue().to_pybytes())
```

### 5.7 Incremental state management

```python
    def _build_read_options(self, table: str) -> ReadOptions:
        """Build ReadOptions with incremental state from previous run."""
        state = self._load_state()
        options = ReadOptions()
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
            state[table]["last_extracted"] = datetime.utcnow().isoformat()
        self.state_path.write_text(yaml.dump(state, default_flow_style=False))
```

### 5.8 Streaming support

```python
    async def run_stream(
        self,
        connector: Connector,
        table: str,
        event_data: dict,
    ) -> int:
        """Process a single stream event (e.g., webhook payload).

        Called by webhook handlers. Writes to partitioned parquets
        (YYYY-MM.parquet) matching existing Jira pattern.

        Returns number of rows written.
        """
        if Cap.STREAM not in connector.capabilities:
            raise ValueError(f"Connector does not support streaming")

        table_dir = self.data_dir / table
        table_dir.mkdir(parents=True, exist_ok=True)

        rows_written = 0
        async for batch in connector.stream(table):
            partition = datetime.utcnow().strftime("%Y-%m")
            parquet_path = table_dir / f"{partition}.parquet"

            if parquet_path.exists():
                # Append to existing partition
                existing = pq.read_table(str(parquet_path))
                combined = pa.concat_tables([existing, pa.Table.from_batches([batch])])
                pq.write_table(combined, str(parquet_path), compression="zstd")
            else:
                pq.write_table(
                    pa.Table.from_batches([batch]),
                    str(parquet_path),
                    compression="zstd",
                )

            rows_written += batch.num_rows

        # Update _meta for this table (same as Jira's update_meta pattern)
        self._update_meta_for_stream_table(table)
        return rows_written
```

---

## 6. Example Connector Implementations

### 6.1 Keboola (batch + remote)

Current `connectors/keboola/extractor.py:run()` is ~300 lines. After refactor:

```python
# File: connectors/keboola/connector.py

class KeboolaConnector:
    """Keboola Storage connector — batch extraction and remote query."""

    capabilities = Cap.DISCOVER | Cap.READ | Cap.REMOTE

    def __init__(self, config: dict):
        self.url = config["url"]
        self.token = os.environ["KEBOOLA_STORAGE_TOKEN"]
        self._default_bucket = config.get("bucket", "")
        self._table_buckets: dict[str, str] = {}  # Populated by discover()
        # Layer 1: API client (existing connectors/keboola/client.py)
        self.client = KeboolaClient(self.url, self.token)
        # DuckDB extension availability (checked once)
        self._has_extension = self._check_extension()

    def discover(self) -> list[TableInfo]:
        """List tables in configured Keboola buckets."""
        tables = []
        for bucket in self.client.list_buckets():
            for table_meta in self.client.list_bucket_tables(bucket["id"]):
                schema = self._columns_to_arrow_schema(table_meta.get("columns", []))
                tables.append(TableInfo(
                    name=table_meta["name"],
                    schema=schema,
                    capabilities=Cap.READ | Cap.REMOTE,
                    primary_key=table_meta.get("primaryKey"),
                    description=table_meta.get("description", ""),
                ))
        return tables

    def read(self, table: str, options: ReadOptions) -> Iterator[pa.RecordBatch]:
        """Extract table data — via DuckDB extension or legacy CSV export."""
        if self._has_extension:
            yield from self._read_via_extension(table, options)
        else:
            yield from self._read_via_csv(table, options)

    def remote(self) -> RemoteAttachInfo:
        return RemoteAttachInfo(
            extension="keboola",
            url=self.url,
            token_env="KEBOOLA_STORAGE_TOKEN",
            alias="kbc",
        )

    def _read_via_extension(self, table, options):
        """Use DuckDB Keboola extension for direct parquet export.

        Note: bucket is passed per-table via ReadOptions or looked up from
        table_registry config. The runtime resolves this before calling read().
        """
        con = duckdb.connect()
        con.execute("INSTALL keboola FROM community; LOAD keboola")
        token_escaped = self.token.replace("'", "''")
        con.execute(f"ATTACH '{self.url}' AS kbc (TYPE keboola, TOKEN '{token_escaped}')")

        # Bucket comes from table_registry config, resolved by runtime
        bucket = self._table_buckets.get(table, self._default_bucket)
        query = f'SELECT * FROM kbc."{bucket}"."{table}"'
        result = con.execute(query)

        while True:
            batch = result.fetch_record_batch(options.batch_size)
            if batch.num_rows == 0:
                break
            yield batch

        con.close()

    def _read_via_csv(self, table, options):
        """Fallback: legacy KeboolaClient CSV export → Arrow."""
        for chunk_df in self.client.export_table_chunked(table, chunk_size=options.batch_size):
            yield pa.RecordBatch.from_pandas(chunk_df)

    # ... helper methods (~20 lines)
```

**Result: ~80 lines** (API-specific code only). Runtime handles extract.duckdb, _meta, atomic swap, schema tracking, state.

### 6.2 BigQuery (remote only)

```python
# File: connectors/bigquery/connector.py

class BigQueryConnector:
    """BigQuery connector — remote-only via DuckDB extension."""

    capabilities = Cap.DISCOVER | Cap.REMOTE

    def __init__(self, config: dict):
        self.project_id = config["project_id"]

    def discover(self) -> list[TableInfo]:
        """List tables in BigQuery datasets via DuckDB extension."""
        con = duckdb.connect()
        con.execute("INSTALL bigquery FROM community; LOAD bigquery")
        con.execute(f"ATTACH 'project={self.project_id}' AS bq (TYPE bigquery, READ_ONLY)")
        # Query information_schema for table list
        tables = con.execute("""
            SELECT table_schema, table_name
            FROM bq.information_schema.tables
            WHERE table_type = 'BASE TABLE'
        """).fetchall()
        con.close()
        return [
            TableInfo(
                name=f"{schema}_{name}",
                schema=pa.schema([]),  # Schema inferred at query time
                capabilities=Cap.REMOTE,
                description=f"BigQuery: {schema}.{name}",
            )
            for schema, name in tables
        ]

    def remote(self) -> RemoteAttachInfo:
        return RemoteAttachInfo(
            extension="bigquery",
            url=f"project={self.project_id}",
            token_env="",  # Auth via GOOGLE_APPLICATION_CREDENTIALS
            alias="bq",
        )
```

**Result: ~40 lines.**

### 6.3 Jira (batch + stream)

```python
# File: connectors/jira/connector.py

class JiraConnector:
    """Jira connector — REST API batch + webhook streaming."""

    capabilities = Cap.DISCOVER | Cap.READ | Cap.STREAM

    TABLES = {
        "issues": ISSUES_SCHEMA,
        "comments": COMMENTS_SCHEMA,
        "changelog": CHANGELOG_SCHEMA,
        "attachments": ATTACHMENTS_SCHEMA,
        "issuelinks": ISSUELINKS_SCHEMA,
        "remote_links": REMOTE_LINKS_SCHEMA,
    }

    def __init__(self, config: dict):
        self.base_url = config["url"]
        self.token = config.secret("JIRA_API_TOKEN")
        self.email = config.get("email", "")
        self._webhook_queue: asyncio.Queue = asyncio.Queue()

    def discover(self) -> list[TableInfo]:
        return [
            TableInfo(
                name=name,
                schema=schema,
                capabilities=Cap.READ | Cap.STREAM,
                description=f"Jira {name}",
            )
            for name, schema in self.TABLES.items()
        ]

    def read(self, table: str, options: ReadOptions) -> Iterator[pa.RecordBatch]:
        """Backfill — iterate Jira REST API search results."""
        jql = f"updated >= '{options.incremental_value}'" if options.incremental_value else ""
        for page in self._search_paginated(table, jql, options.batch_size):
            transformed = transform_jira_page(table, page)  # existing transform.py
            yield pa.RecordBatch.from_pylist(transformed, schema=self.TABLES[table])

    async def stream(self, table: str) -> AsyncIterator[pa.RecordBatch]:
        """Process webhook events from queue."""
        while not self._webhook_queue.empty():
            event = await self._webhook_queue.get()
            transformed = transform_jira_event(table, event)
            if transformed:
                yield pa.RecordBatch.from_pylist(
                    [transformed],
                    schema=self.TABLES[table],
                )

    def push_event(self, event: dict):
        """Called by webhook handler to enqueue events."""
        self._webhook_queue.put_nowait(event)
```

**Result: ~60 lines** (excluding existing transform.py which stays unchanged).

---

## 7. CLI Integration

### 7.1 New CLI commands

```
da connector list                        # List installed connectors + capabilities
da connector discover <name>             # Run discover(), show available tables
da connector test <name>                 # Run contract tests against connector
da connector new <name> [--caps ...]     # Scaffold new connector from template
```

### 7.2 Scaffold template

`da connector new hubspot --caps discover,read,write` generates:

```
connectors/hubspot/
├── connector.yaml          # Manifest (pre-filled with name, caps)
├── connector.py            # Connector class skeleton
├── __init__.py
└── tests/
    └── test_connector.py   # Contract tests (from runtime)
```

Generated `connector.py`:

```python
"""HubSpot connector — generated scaffold."""

import pyarrow as pa
from src.connector_kit.protocol import Cap, Connector, ReadOptions, TableInfo

class HubspotConnector:
    capabilities = Cap.DISCOVER | Cap.READ | Cap.WRITE

    def __init__(self, config: dict):
        # TODO: Initialize API client
        pass

    def discover(self) -> list[TableInfo]:
        # TODO: Query HubSpot API for available objects
        return []

    def read(self, table: str, options: ReadOptions) -> Iterator[pa.RecordBatch]:
        # TODO: Implement data extraction
        yield from []
```

### 7.3 Contract tests

The runtime provides reusable test functions that any connector can run:

```python
# File: src/connector_kit/contract_tests.py

def test_discover_returns_valid_tables(connector: Connector):
    """Every discovered table must have a name, schema, and valid capabilities."""
    if Cap.DISCOVER not in connector.capabilities:
        pytest.skip("Connector does not support DISCOVER")
    tables = connector.discover()
    assert len(tables) > 0, "discover() must return at least one table"
    for t in tables:
        assert t.name, "Table name must not be empty"
        assert isinstance(t.schema, pa.Schema), f"Table {t.name} schema must be Arrow Schema"
        assert t.capabilities, f"Table {t.name} must declare capabilities"

def test_read_yields_valid_batches(connector: Connector):
    """read() must yield valid Arrow RecordBatches matching declared schema."""
    if Cap.READ not in connector.capabilities:
        pytest.skip("Connector does not support READ")
    tables = connector.discover() if Cap.DISCOVER in connector.capabilities else []
    readable = [t for t in tables if Cap.READ in t.capabilities]
    if not readable:
        pytest.skip("No readable tables discovered")
    table = readable[0]
    options = ReadOptions(batch_size=10)
    batches = list(itertools.islice(connector.read(table.name, options), 3))
    for batch in batches:
        assert isinstance(batch, pa.RecordBatch)
        assert batch.num_rows > 0 or batch.num_rows == 0  # Empty is OK
        assert batch.schema == table.schema, (
            f"Batch schema mismatch for {table.name}: "
            f"expected {table.schema}, got {batch.schema}"
        )

def test_full_extract_pipeline(connector: Connector, tmp_path: Path):
    """End-to-end: connector → runtime → extract.duckdb."""
    runtime = ConnectorRuntime(tmp_path / "test_extract")
    stats = runtime.run(connector)
    assert stats.tables_failed == 0, f"Extraction errors: {stats.errors}"
    db_path = tmp_path / "test_extract" / "extract.duckdb"
    assert db_path.exists()
    con = duckdb.connect(str(db_path), read_only=True)
    meta = con.execute("SELECT table_name FROM _meta").fetchall()
    assert len(meta) > 0, "extract.duckdb must have at least one table in _meta"
    con.close()

def test_remote_attach_info(connector: Connector):
    """remote() must return valid extension info without embedded secrets."""
    if Cap.REMOTE not in connector.capabilities:
        pytest.skip("Connector does not support REMOTE")
    info = connector.remote()
    assert info.extension, "Extension name must not be empty"
    assert info.url, "URL must not be empty"
    # SECURITY: token_env must be an env var name, not an actual token
    if info.token_env:
        assert not info.token_env.startswith("sk-"), "token_env must be env var name, not token"
        assert not info.token_env.startswith("xox"), "token_env must be env var name, not token"
        assert len(info.token_env) < 100, "token_env looks like a token, not an env var name"
```

Usage in a connector's test file:

```python
# File: connectors/hubspot/tests/test_connector.py

from src.connector_kit.contract_tests import *

@pytest.fixture
def connector():
    return HubspotConnector({"url": "https://api.hubspot.com", ...})

# All contract tests run automatically via the wildcard import
```

---

## 8. Integration with sync.py

### 8.1 Updated sync flow

The subprocess pattern stays (DuckDB lock isolation), but the subprocess now uses ConnectorRuntime:

```python
# In app/api/sync.py — updated _run_sync()

# Before (current):
cmd = [sys.executable, "-c", """
import json, sys
configs = json.load(sys.stdin)
from connectors.keboola.extractor import run
result = run(output_dir, configs, url, token)
print(json.dumps(result))
"""]

# After (with Connector Kit):
cmd = [sys.executable, "-c", """
import json, sys
from pathlib import Path
from src.connector_kit.manifest import ConnectorManifest
from src.connector_kit.runtime import ConnectorRuntime

payload = json.load(sys.stdin)
manifest = ConnectorManifest.load(Path(payload["manifest_path"]))
connector = manifest.instantiate(payload["config"])
runtime = ConnectorRuntime(Path(payload["output_dir"]))
stats = runtime.run(connector, tables=payload.get("tables"))
print(json.dumps(stats.__dict__))
"""]
```

### 8.2 Orchestrator compatibility

**No changes to `src/orchestrator.py`.** The runtime produces the same `extract.duckdb` contract:
- `_meta` table with `table_name, description, rows, size_bytes, extracted_at, query_mode` (+ optional `schema_json`)
- `_remote_attach` table with `alias, extension, url, token_env`
- Views pointing to `read_parquet(...)` for local tables

The orchestrator's `_attach_and_create_views()` and `_attach_remote_extensions()` continue to work unchanged. The orchestrator SELECTs only 4 specific columns from `_meta` (`table_name, rows, size_bytes, query_mode`), so the added `schema_json` column is invisible to it.

**Note:** `src/db.py:get_analytics_db_readonly()` also reads `_remote_attach` via `_reattach_remote_extensions()` — this is a second consumer of the same 4-column contract, and also requires no changes.

### 8.3 Sync.py additional concerns

The current `_run_sync()` in `app/api/sync.py` does more than just run extractors:

1. **Custom connectors** — scans `connectors/custom/*/extractor.py` and runs each in a subprocess. Must be preserved: during transition, scan for both legacy `extractor.py` and new `connector.yaml`.
2. **Auto-profiling** — runs `ProfilerService.profile_table()` after sync for first 10 tables per source. Must be preserved in the refactored sync flow.
3. **Auto-discovery** — when no tables are registered and KEBOOLA_STORAGE_TOKEN is set, attempts automatic table discovery. With Connector Kit this becomes cleaner: `connector.discover()` provides this natively.

---

## 9. File Layout

### New files

```
src/connector_kit/
├── __init__.py              # Public API exports
├── protocol.py              # Cap, TableInfo, ReadOptions, RemoteAttachInfo, Connector
├── runtime.py               # ConnectorRuntime
├── manifest.py              # ConnectorManifest (YAML loader)
├── contract_tests.py        # Reusable test functions
└── scaffold.py              # CLI scaffold generator (da connector new)
```

### Modified files

```
connectors/keboola/
├── connector.yaml           # NEW: manifest
├── connector.py             # NEW: KeboolaConnector class
├── extractor.py             # KEPT: deprecated, delegates to connector.py
├── client.py                # UNCHANGED: legacy API client
└── ...

connectors/bigquery/
├── connector.yaml           # NEW
├── connector.py             # NEW: BigQueryConnector class
├── extractor.py             # KEPT: deprecated, delegates to connector.py
└── ...

connectors/jira/
├── connector.yaml           # NEW
├── connector.py             # NEW: JiraConnector class
├── extract_init.py          # KEPT: deprecated, delegates to connector.py
├── transform.py             # UNCHANGED (stable infrastructure per CLAUDE.md)
├── file_lock.py             # UNCHANGED (stable infrastructure per CLAUDE.md)
└── ...

app/api/sync.py              # MODIFIED: use ConnectorRuntime in subprocess
cli/                         # MODIFIED: add `da connector` subcommands
tests/test_connector_kit_poc.py  # EXISTS: POC validation (29 tests)
```

### Unchanged files (per CLAUDE.md: stable infrastructure)

- `connectors/jira/file_lock.py`
- `connectors/jira/transform.py`
- `services/ws_gateway/`
- `src/orchestrator.py`

---

## 10. Migration Plan

### Phase 1: Core SDK (this spec)

1. Create `src/connector_kit/` package with Protocol, Runtime, Manifest
2. Move POC code from `tests/test_connector_kit_poc.py` to production
3. Add contract tests
4. Add `da connector list` and `da connector test` CLI commands
5. Update `tests/helpers/contract.py` to accept optional `schema_json` column in `_meta` (currently enforces exact 6-column match, new SDK produces 7 columns)
6. Add POC test for `_remote_attach` table in extract.duckdb (current POC only validates YAML, not DuckDB table)

**Deliverable:** SDK exists, no connectors migrated yet. Old code untouched.

### Phase 2: Keboola migration

1. Create `connectors/keboola/connector.yaml` + `connector.py`
2. `KeboolaConnector` wraps existing `client.py` + DuckDB extension logic
3. Old `extractor.py:run()` delegates to `ConnectorRuntime + KeboolaConnector`
4. Verify: `da connector test keboola` passes contract tests
5. Verify: `pytest tests/test_keboola_extractor.py` still passes (backward compat)

**Deliverable:** Keboola works via new SDK. Old API still works.

### Phase 3: BigQuery + Jira migration

1. Same pattern as Phase 2 for BigQuery (simplest — remote only)
2. Jira is most complex — stream capability, existing transform.py
3. Jira requires modifying `connectors/jira/webhook.py` to bridge existing synchronous webhook handler to the queue-based `stream()` interface. Note: `webhook.py` is NOT marked as stable infrastructure (only `transform.py` and `file_lock.py` are protected)
4. Verify all existing tests pass

**Deliverable:** All three connectors use SDK. Old APIs deprecated.

### Phase 4: CLI scaffold + developer experience

1. `da connector new <name>` scaffold command
2. `da connector discover <name>` for interactive discovery
3. Documentation for third-party connector authors
4. Remove deprecated `extractor.py` entry points

**Deliverable:** External developers can create connectors.

### Phase 5: driver_builder integration (optional/future)

1. `da connector generate-client <name> <api_docs_url>`
2. Uses driver_builder to generate API client (Layer 1)
3. Generates connector scaffold wrapping the client
4. Developer fills in Arrow schema mapping

**Deliverable:** New connector from API docs in minutes.

---

## 11. Validation

### POC results (already passing)

Test file: `tests/test_connector_kit_poc.py` — **29/29 tests, 0.69s**

| Test class | Tests | What it validates |
|------------|-------|-------------------|
| `TestCapabilityFlags` | 3 | Flag composition, per-table caps, iteration |
| `TestProtocolCompliance` | 3 | `isinstance()` check, partial implementation, structural typing |
| `TestArrowIntegration` | 3 | RecordBatch → DuckDB zero-copy, iterator consumption, Parquet roundtrip |
| `TestConnectorRuntime` | 5 | Full pipeline, selective extract, incremental state, empty tables, partial failure |
| `TestSchemaEvolution` | 5 | Added/removed columns, type changes, no-change, first-run |
| `TestStreamingCapability` | 2 | AsyncIterator, stream → DuckDB |
| `TestRemoteOnlyConnector` | 1 | Remote-only metadata without data |
| `TestManifestValidation` | 5 | YAML parsing, capability mapping, auth, config schema, health check |
| `TestDiscoveryToReadPipeline` | 1 | End-to-end: discover → read → query |
| `TestLargeDataBatching` | 1 | 100K rows in constant memory |

### Acceptance criteria for production

- [ ] All 29 POC tests pass after moving code to `src/connector_kit/`
- [ ] Existing test suite (633 tests) passes with no regressions
- [ ] `da connector test keboola` passes all contract tests
- [ ] `da connector test bigquery` passes all contract tests
- [ ] `da connector test jira` passes all contract tests
- [ ] Orchestrator produces identical analytics.duckdb from SDK-wrapped connectors
- [ ] Sync API (`POST /api/sync/trigger`) works unchanged
- [ ] Schema evolution detected on real Keboola table schema change

---

## 12. Open Questions

1. **Incremental merge strategy.** Current spec supports incremental via `incremental_key` / `incremental_value`, but doesn't specify how to merge new data with existing parquets (append vs. replace vs. upsert). Phase 1 uses full replace (current behavior); upsert support is a Phase 3+ concern.

2. **Partitioned parquet vs. single file.** Jira uses `YYYY-MM.parquet` partitions, others use single `{table}.parquet`. The runtime should support both — configurable per-table or per-connector. Current spec defaults to single file for `read()`, partitioned for `stream()`.

3. **Concurrent webhook writes.** Jira's `file_lock.py` handles concurrent webhook-to-parquet writes. The runtime should integrate this, but `file_lock.py` is marked as stable infrastructure in CLAUDE.md. Resolution: runtime delegates to existing `file_lock.py`, no changes needed.

4. **Health check execution.** Manifest declares health check, but who executes it? Options: (a) runtime before extraction, (b) CLI on demand, (c) scheduler periodically. Phase 1: CLI only (`da connector test <name>` runs health check). Automatic health check before extraction in Phase 2.

5. **Custom connector auto-discovery.** Current `sync.py` scans `connectors/custom/*/extractor.py`. With Connector Kit, scan for `connectors/*/connector.yaml` instead. Need to handle transition period where both patterns coexist.

6. **Keboola `_remote_attach` conditional creation.** Current `extractor.py` only creates `_remote_attach` when both `has_remote` AND `use_extension` are true. The Connector Kit runtime always calls `_write_remote_attach()` when `Cap.REMOTE` is declared. This means `_remote_attach` will be present even when the extension is unavailable (fallback to legacy client). The orchestrator handles missing extensions gracefully (logs warning, skips), so this behavioral change is safe but should be noted.

7. **Identifier validation shared module.** The `_SAFE_IDENTIFIER` regex is currently duplicated in `src/orchestrator.py`, `src/db.py`, and `cli/commands/analyst.py`. The Connector Kit adds a fourth copy. Consider extracting to a shared `src/validators.py` module in Phase 1.

---

## Appendix A: Review Findings

This spec was reviewed against the actual codebase. All findings have been addressed in the current version.

| # | Finding | Severity | Resolution |
|---|---------|----------|------------|
| 1 | `_meta` schema adds `schema_json` — breaks `tests/helpers/contract.py` exact 6-column assert | WARNING | Added to Phase 1 migration step 5 |
| 2 | `_remote_attach` 4-column schema matches all consumers | CORRECT | No action needed |
| 3 | Stable files (file_lock.py, transform.py, ws_gateway/) respected | CORRECT | No action needed |
| 4 | POC test count (29/29) is accurate | CORRECT | No action needed |
| 5 | POC doesn't test `_remote_attach` in DuckDB (only YAML) | WARNING | Added to Phase 1 migration step 6 |
| 6 | `config.secret()` method does not exist in codebase | ERROR | Fixed → `os.environ["KEBOOLA_STORAGE_TOKEN"]` |
| 7 | `self._bucket` used but never assigned in KeboolaConnector | ERROR | Fixed → `_default_bucket` + `_table_buckets` in `__init__` |
| 8 | Keboola `_remote_attach` conditional creation not replicated | WARNING | Documented in Open Question 6 |
| 9 | Custom connectors + auto-profiling in `sync.py` not addressed | WARNING | Added Section 8.3 |
| 10 | `src/db.py` is second `_remote_attach` consumer | WARNING | Added note in Section 8.2 |
| 11 | `_SAFE_IDENTIFIER` validation missing from runtime | SUGGESTION | Added `_validate_identifier()` to runtime + validation in `run()` |
| 12 | Jira `webhook.py` incompatible with queue-based streaming | WARNING | Added to Phase 3 step 3 |
