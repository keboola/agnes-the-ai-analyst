# Collections Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Adaptation note:** This repo routes implementation through `agnes-builder`
> + the `agnes-conventions` playbooks (`references/{migration,repo-parity,endpoint-rbac}.md`),
> which enforce the TDD micro-steps (write failing test → run → implement → run → commit),
> DuckDB↔PG parity, and the migration ladder. Tasks below give exact DDL, signatures,
> file paths, and test intent; defer the per-line TDD cadence to those playbooks.

**Goal:** Land the schema, repositories (DuckDB↔PG parity), and RBAC resource type for "Collections" (bring-your-files), so a collection can be created and made grantable — the foundation every later slice builds on.

**Architecture:** Three new system-DB tables (`file_corpora`, `corpus_files`, `corpus_chunks`) created in a single schema migration (DuckDB ladder + Alembic, in lockstep). Two repositories with DuckDB + Postgres siblings reached through the factory, covered by cross-engine contract tests. A new `ResourceType.COLLECTION` with a `list_blocks` projection so `/admin/access` can grant collections. `corpus_chunks` (with a 384-dim embedding column) is created now but its repo is deferred to the Retrieval slice — the one-migration-per-build-run constraint means all schema lands here.

**Tech Stack:** Python, DuckDB (`src/db.py` ladder), Alembic (`migrations/versions/`), FastAPI RBAC (`app/resource_types.py`, `app/auth/access.py`), pytest (`tests/db_pg/` contract tests).

---

## File structure

- `src/db.py` — bump `SCHEMA_VERSION` 76→77; add 3 `CREATE TABLE` to the fresh-install schema; add `_v76_to_v77` migration fn + wire dispatch.
- `migrations/versions/0024_collections_v77.py` — Alembic parity revision (3 tables).
- `tests/test_db_schema_version.py` — bump the version assertion.
- `src/repositories/file_corpora.py` + `_pg.py` — Collection (corpus) CRUD.
- `src/repositories/corpus_files.py` + `_pg.py` — per-file rows + status lifecycle.
- `src/repositories/__init__.py` — two factory registry entries + `*_repo()` functions.
- `tests/db_pg/test_file_corpora_contract.py`, `tests/db_pg/test_corpus_files_contract.py` — cross-engine contract tests.
- `app/resource_types.py` — `ResourceType.COLLECTION` + `_collection_blocks` + `ResourceTypeSpec` registration.
- `CHANGELOG.md` — `[Unreleased]` bullet (folded by the integrator; each task emits its bullet in its report).

---

## Table DDL (canonical — used by both Task 1 sub-paths)

```sql
-- file_corpora: a Collection (self-service container of uploaded files)
CREATE TABLE IF NOT EXISTS file_corpora (
    id VARCHAR PRIMARY KEY,
    slug VARCHAR UNIQUE NOT NULL,
    name VARCHAR NOT NULL,
    description VARCHAR,
    created_by VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp,
    deleted_at TIMESTAMP
);

-- corpus_files: one row per uploaded file + its processing lifecycle
CREATE TABLE IF NOT EXISTS corpus_files (
    id VARCHAR PRIMARY KEY,
    corpus_id VARCHAR NOT NULL,
    filename VARCHAR NOT NULL,
    sha256 VARCHAR NOT NULL,
    file_type VARCHAR,
    size_bytes BIGINT,
    storage_path VARCHAR,
    processing_status VARCHAR NOT NULL DEFAULT 'pending',  -- pending|processing|indexed|rejected
    processing_detail VARCHAR,  -- JSON: {tier, vision_used, error, derived_table_id, chunk_count}
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

-- corpus_chunks: prose-document chunks + embedding (repo deferred to Retrieval slice)
CREATE TABLE IF NOT EXISTS corpus_chunks (
    id VARCHAR PRIMARY KEY,
    corpus_id VARCHAR NOT NULL,
    file_id VARCHAR NOT NULL,
    ordinal INTEGER,
    text VARCHAR,
    embedding FLOAT[384],          -- DuckDB fixed-size array; PG: float8[] (pgvector later)
    section_path VARCHAR,
    page INTEGER,
    bbox VARCHAR,                  -- JSON
    metadata VARCHAR,             -- JSON
    created_at TIMESTAMP DEFAULT current_timestamp
);
```

> Embedding column: DuckDB `FLOAT[384]` (queried later with `array_cosine_similarity`).
> PG side uses `sa.ARRAY(sa.Float)` (float8[]) for now — pgvector `vector(384)` is a
> Retrieval-slice option, not a foundation dependency. Nullable; populated at ingestion.

---

### Task 1: Schema migration (DuckDB ladder + Alembic) — THE migration task

> This is the single migration task; `/agnes-build` serializes it last. Follow
> `agnes-conventions/references/migration.md`.

**Files:**
- Modify: `src/db.py` (`SCHEMA_VERSION` at line 50; fresh-install schema block ~line 5165; upgrade dispatch ~line 5450)
- Create: `migrations/versions/0024_collections_v77.py`
- Modify: `tests/test_db_schema_version.py`

- [ ] **Step 1 — failing test:** add `test_collections_tables_exist_on_fresh_db` in `tests/test_db_schema_version.py` (or a new `tests/test_collections_schema.py`): open a fresh system DB, assert `file_corpora`, `corpus_files`, `corpus_chunks` are present (`SELECT * FROM information_schema.tables` / DuckDB `duckdb_tables()`), and `SCHEMA_VERSION == 77`.
- [ ] **Step 2 — run, expect FAIL.** `.venv/bin/pytest tests/test_collections_schema.py -v`
- [ ] **Step 3 — implement DuckDB:**
  - Add the three `CREATE TABLE IF NOT EXISTS` (DDL above) to the fresh-install schema block.
  - Add `_v76_to_v77(conn)` mirroring `_v75_to_v76` (src/db.py:4965): the same three `CREATE TABLE IF NOT EXISTS` + `conn.execute("UPDATE schema_version SET version = 77")`.
  - Wire dispatch: in the upgrade path add `if current < 77: _v76_to_v77(conn)`; ensure fresh-install path reaches it.
  - Bump `SCHEMA_VERSION = 77` (line 50).
- [ ] **Step 4 — implement Alembic** `migrations/versions/0024_collections_v77.py`: `revision="0024_collections_v77"`, `down_revision="0023_store_entity_votes_v76"`; `upgrade()` creates the three tables via `op.create_table(...)` (embedding as `sa.Column("embedding", sa.ARRAY(sa.Float()), nullable=True)`); `downgrade()` drops them in reverse.
- [ ] **Step 5 — bump version test:** in `tests/test_db_schema_version.py` raise the assertion to `>= 77`.
- [ ] **Step 6 — run, expect PASS:** `.venv/bin/pytest tests/test_collections_schema.py tests/test_db_schema_version.py -v`
- [ ] **Step 7 — commit** (`feat: collections schema v77 (file_corpora/corpus_files/corpus_chunks)`); emit CHANGELOG bullet in report (Internal: `Schema v77 / Alembic 0024_collections_v77`).

---

### Task 2: `file_corpora` repository (DuckDB + PG + factory + contract test)

> Follow `agnes-conventions/references/repo-parity.md`. Template:
> `src/repositories/data_packages.py` (+ `_pg.py`).

**Files:**
- Create: `src/repositories/file_corpora.py`, `src/repositories/file_corpora_pg.py`
- Modify: `src/repositories/__init__.py`
- Create: `tests/db_pg/test_file_corpora_contract.py`

**Interface (both backends, identical signatures):**
```python
class FileCorporaRepository:           # _pg: FileCorporaPgRepository
    def create(self, *, name: str, slug: str, description: Optional[str],
               created_by: str) -> str: ...           # returns generated id "col_..."
    def get(self, corpus_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]: ...
    def get_by_slug(self, slug: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]: ...
    def list(self, *, search: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]: ...
    def soft_delete(self, corpus_id: str) -> None: ... # sets deleted_at
```

- [ ] **Step 1 — contract test first** `tests/db_pg/test_file_corpora_contract.py`: copy the `@pytest.fixture(params=["duckdb","pg"])` harness from `test_data_packages_contract.py`; assert `create→get` round-trips the same shape on both backends, `list` excludes soft-deleted, `get_by_slug` works, ids are unique.
- [ ] **Step 2 — run, expect FAIL** (`.venv/bin/pytest tests/db_pg/test_file_corpora_contract.py -v`).
- [ ] **Step 3 — implement DuckDB repo** mirroring `data_packages.py` shape (`_COLS`, `_SELECT`, id gen e.g. `"col_" + secrets.token_hex(8)`).
- [ ] **Step 4 — implement `_pg.py` sibling** (SQLAlchemy `Engine`, bound params).
- [ ] **Step 5 — factory:** add to `_REGISTRY` in `src/repositories/__init__.py`:
  ```python
  "file_corpora": {DUCKDB: ("src.repositories.file_corpora", "FileCorporaRepository"),
                   PG: ("src.repositories.file_corpora_pg", "FileCorporaPgRepository")},
  ```
  and `def file_corpora_repo() -> Any: return _build("file_corpora")`.
- [ ] **Step 6 — run, expect PASS** on both backends.
- [ ] **Step 7 — commit** (`feat: file_corpora repo (duckdb+pg parity)`); CHANGELOG bullet (Added: Collections — corpus repository).

---

### Task 3: `corpus_files` repository (DuckDB + PG + factory + contract test)

> Same playbook as Task 2.

**Files:**
- Create: `src/repositories/corpus_files.py`, `src/repositories/corpus_files_pg.py`
- Modify: `src/repositories/__init__.py`
- Create: `tests/db_pg/test_corpus_files_contract.py`

**Interface:**
```python
class CorpusFilesRepository:           # _pg: CorpusFilesPgRepository
    def add(self, *, corpus_id: str, filename: str, sha256: str,
            file_type: Optional[str], size_bytes: Optional[int],
            storage_path: Optional[str]) -> str: ...   # status defaults 'pending'
    def get(self, file_id: str) -> Optional[Dict[str, Any]]: ...
    def list_for_corpus(self, corpus_id: str) -> List[Dict[str, Any]]: ...
    def set_status(self, file_id: str, *, status: str,
                   detail: Optional[Dict[str, Any]] = None) -> None: ...  # detail -> JSON
```

- [ ] **Step 1 — contract test first:** create→list_for_corpus returns the row; `set_status('indexed', {'tier':1,'chunk_count':12})` round-trips `processing_status` + JSON `processing_detail` on both backends; status defaults to `'pending'`.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — DuckDB repo** (JSON via `json.dumps`/parse, mirroring `_JSON_LIST_COLS` handling in `data_packages.py`).
- [ ] **Step 4 — `_pg.py` sibling** (`CAST(:detail AS JSONB)`).
- [ ] **Step 5 — factory** entry `"corpus_files"` + `corpus_files_repo()`.
- [ ] **Step 6 — run, expect PASS.**
- [ ] **Step 7 — commit** (`feat: corpus_files repo + status lifecycle (duckdb+pg)`); CHANGELOG bullet.

---

### Task 4: `ResourceType.COLLECTION` + RBAC projection

> Follow `agnes-conventions/references/endpoint-rbac.md`. Template:
> `_data_package_blocks` (app/resource_types.py:190).

**Files:**
- Modify: `app/resource_types.py`
- Create/modify: `tests/test_resource_types.py` (or the existing resource-types test)

- [ ] **Step 1 — failing test:** assert `ResourceType.COLLECTION.value == "collection"`; assert the `/admin/access` block projection returns a `"Collections"` block whose items carry `resource_id`, `name`, `slug` for non-deleted `file_corpora` (seed one row, call the registered `list_blocks`).
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement:**
  - Add `COLLECTION = "collection"` to the `ResourceType` StrEnum (app/resource_types.py:36).
  - Add `_collection_blocks(conn)` mirroring `_data_package_blocks` (SELECT id, slug, name, description FROM `file_corpora` WHERE `deleted_at IS NULL` ORDER BY name → one `"Collections"` block).
  - Register a `ResourceTypeSpec(key=ResourceType.COLLECTION, display_name="Collections", description="User-uploaded file collections", id_format="<corpus_id>", list_blocks=_collection_blocks)` in the `RESOURCE_TYPES` registry.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit** (`feat: COLLECTION resource type + access projection`); CHANGELOG bullet (Added).

---

## Cross-cutting (integrator / release)

- **CHANGELOG:** builders do NOT edit `CHANGELOG.md`; each emits its bullet in its report; the integrator folds them under `[Unreleased]` (Added: Collections foundation — schema, repos, RBAC; Internal: schema v77 / Alembic 0024).
- **Full suite before push:** `.venv/bin/pytest tests/ --tb=short -n auto -q` (incl. `tests/db_pg/` parity + `tests/test_db_schema_version.py` + `tests/test_backend_split_guard.py`).
- **Release-cut** is decided separately at PR time per CLAUDE.md (do not auto-bump version here).

## Self-review (done)

- **Spec coverage:** schema (file_corpora/corpus_files/corpus_chunks ✓), embedding column 384-dim ✓, RBAC ResourceType.COLLECTION ✓, DuckDB↔PG parity ✓, migration ladder ✓. Upload/ingestion/retrieval/UI are later slices (out of this plan by design).
- **Placeholders:** DDL, signatures, factory keys, migration mechanics all concrete; per-line TDD delegated to agnes-conventions (declared in header).
- **Type consistency:** repo method names (`create/get/get_by_slug/list/soft_delete`; `add/get/list_for_corpus/set_status`) and table/column names match the DDL and the spec's domain-model section.
