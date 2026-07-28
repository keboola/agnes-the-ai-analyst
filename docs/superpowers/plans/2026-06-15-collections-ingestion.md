# Collections Ingestion Tier-1 Implementation Plan (Slice 3)

> REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Routes through
> agnes-builder + agnes-conventions (repo-parity). TDD-first.

**Goal:** Turn an uploaded Tier-1 file into queryable knowledge: tabular files become DuckDB tables registered in `table_registry`; prose documents become `corpus_chunks` rows (text only — embeddings are Slice 4). A file's `processing_status` moves `pending → processing → indexed | rejected`.

**Architecture:** A router `ingest_file(file_id)` classifies by type and dispatches. Tabular → load into a DuckDB table (DuckDB native readers) + register in `table_registry` (linked back via `corpus_files.processing_detail.derived_table_id`). Prose → extract text (Docling if installed, else lightweight per-format fallback) → structure-aware chunks → `corpus_chunks`. A new `corpus_chunks` repo (DuckDB + PG parity) owns chunk writes. Ingestion is triggered after upload via FastAPI `BackgroundTasks` and is also directly callable (tests). **No schema migration** — `corpus_chunks` exists from Slice 1 (v77).

**Dependency decision:** **Docling is an OPTIONAL extra** (`pyproject [project.optional-dependencies] docling = ["docling"]`), NOT a core dep (it pulls torch — too heavy for the core image/CI). The text-extraction layer is an interface with a default lightweight implementation (txt/md/html plain; PDF via a light reader if available) and a Docling-backed implementation used only when importable. If neither can handle a doc → mark `rejected` with a clear reason. Never hard-import docling at module top.

---

## Tasks

### Task 1: `corpus_chunks` repository (DuckDB + PG + factory + contract test)
- Create `src/repositories/corpus_chunks.py` + `_pg.py`; factory entries + `corpus_chunks_repo()`.
- Interface: `add_many(self, chunks: list[dict]) -> int` (bulk insert; each {corpus_id, file_id, ordinal, text, section_path?, page?, bbox?, metadata?}; embedding left NULL), `list_for_file(file_id) -> list[dict]`, `list_for_corpus(corpus_id) -> list[dict]`, `delete_for_file(file_id) -> None`.
- Contract test `tests/db_pg/test_corpus_chunks_contract.py` (parametrized duckdb+pg): add_many→list round-trips; ordinal ordering preserved; delete_for_file removes; embedding column is None on read.

### Task 2: Text extraction layer (`src/ingest/text_extract.py`)
- `extract_text(path: str, file_type: str) -> ExtractResult` (ExtractResult: full_text + optional list of (section_path, text) elements). Default impl: txt/md/html/rtf plain read (+ strip HTML); PDF via a light dependency if importable else raise Unsupported. A `_docling_extract` path used when `import docling` succeeds (richer elements + tables). NEVER import docling at module top — import inside the function, guarded.
- Tests: txt/md extract; html strips tags; missing docling → fallback still works for txt/md; unextractable → raises a typed error.

### Task 3: Chunking (`src/ingest/chunking.py`)
- `chunk_text(elements_or_text, *, target_tokens=800, overlap=100) -> list[Chunk]` — structure-aware when elements present (split on section boundaries, never mid-element), else fixed-size with overlap. Each Chunk carries ordinal + section_path/page when known.
- Tests: long text → multiple ordered chunks within size bound; element list → chunks respect boundaries; empty → [].

### Task 4: Tabular ingestion (`src/ingest/tabular.py`)
- `ingest_tabular(corpus_id, file_id, storage_path, file_type) -> str` (returns derived table_registry id). Use DuckDB native readers (`read_csv_auto`/`read_parquet`/`read_json_auto`; XLSX via the excel/spatial extension or openpyxl→Arrow). Create a table `corpus_<corpus_id>_<safe_name>` (sanitized), register it in `table_registry` via the registry repo (source_type e.g. 'collection', query_mode 'local'), return the id.
- Tests: a small CSV → a registered table queryable via the analytics path; the table id is returned; sanitization handles odd filenames.

### Task 5: Ingestion router + status lifecycle (`src/ingest/runner.py`)
- `ingest_file(file_id) -> None`: load the corpus_files row; set status 'processing'; classify (reuse `src/corpus_allowlist.classify` + extension): tabular → Task 4 (set detail.derived_table_id, status 'indexed'); prose (tier1 docs) → Task 2 + 3 → corpus_chunks via Task 1 (detail.chunk_count, status 'indexed'); tier2/unsupported-here or extraction failure → status 'rejected' with detail.reason. Idempotent: re-ingest deletes prior chunks / re-registers table.
- Tests: a pending CSV row → after ingest, status 'indexed' + derived_table_id set; a pending .txt → status 'indexed' + chunks created + chunk_count set; an unextractable file → 'rejected' with reason.

### Task 6: Trigger after upload
- In `app/api/collections.py` upload handler: after creating each tier1/tier2 `pending` row, schedule `ingest_file(file_id)` via FastAPI `BackgroundTasks` (inject `background_tasks: BackgroundTasks`). Keep the request fast; status reflects progress. (Tier2/vision still just stored — Slice 5 handles vision; ingest_file marks tier2 images 'pending' or routes to a no-op until Slice 5.)
- Test: POST upload of a CSV then GET files → status becomes 'indexed' (BackgroundTasks run synchronously under TestClient).

---

## Cross-cutting
- **No migration** (corpus_chunks table from v77). If you find yourself bumping SCHEMA_VERSION, STOP — it's wrong for this slice.
- New repo → factory entry + contract test (sync-map). corpus_chunks parity is mandatory.
- `pyproject.toml`: add the `docling` optional-extra; do NOT add docling/torch to core deps. Run `uv lock` after.
- CHANGELOG `[Unreleased]` bullets (Added: Tier-1 ingestion — tabular→DuckDB tables, documents→chunks, optional Docling extra).
- Full suite before finishing: `uv run pytest tests/ --tb=short -n auto -q` (NOT `.venv/bin/pytest`). New tests + parity + the api-coverage/backend-split guards must pass. Known pre-existing failures (test_cli_snapshot_create duckdb_guard; test_mcp_server server_info subprocess) are acceptable.
- No version bump.

## Self-review
- Coverage: corpus_chunks repo+parity ✓, tabular→DuckDB ✓ (Agnes's strength), prose→chunks ✓, status lifecycle ✓, optional-Docling ✓, trigger ✓. Embeddings/hybrid search = Slice 4; vision = Slice 5.
- Types: reuses Slice-1/2 repos (corpus_files_repo add/get/set_status/delete; file_corpora_repo) + the new corpus_chunks_repo verbatim.
