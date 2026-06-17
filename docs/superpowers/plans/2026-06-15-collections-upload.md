# Collections Upload Implementation Plan (Slice 2)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use checkbox (`- [ ]`).
>
> **Adaptation note:** Implementation routes through `agnes-builder` + `agnes-conventions`
> playbooks (`references/{endpoint-rbac,repo-parity}.md`), which enforce TDD micro-steps,
> DuckDB↔PG parity, and the REST×CLI×MCP API-coverage ratchet. Tasks give exact files,
> interfaces, RBAC gates, and test intent; defer per-line TDD cadence to those playbooks.

**Goal:** A collection can be created (admin), files uploaded into it (members), and listed — over REST, with CLI + MCP coverage, all RBAC-gated and fail-closed. No ingestion yet (uploaded files land with `processing_status='pending'`).

**Architecture:** A new `app/api/collections.py` router (CRUD + file upload/list/delete) reaching the `file_corpora_repo()` / `corpus_files_repo()` factories from Slice 1. Content-addressed file storage mirroring `app/api/uploads.py`. An extension→tier allowlist gates uploads at the door. Matching CLI commands and MCP tools satisfy the API-coverage ratchet. No schema migration (all tables exist from Slice 1, v77).

**Tech Stack:** FastAPI (`UploadFile`, multipart), `app/auth/access.py` RBAC, `cli/` (CLI), `cli/mcp/server.py` + `app/api/mcp_http.py` (MCP), pytest `TestClient`.

**RBAC model (v1 decision):** Collection **create/delete** = `require_admin` (admin curates + grants a group via the existing `/admin/access`, Slice 1's `ResourceType.COLLECTION`). File **upload/list/delete** and collection **read** = `require_resource_access(ResourceType.COLLECTION, "{collection_id}")` — any member of a granted group can upload into and read a collection. Per-user self-service ownership is a later refinement (needs a per-user grant model); documented, not built here.

---

## File structure

- Create: `app/api/collections.py` — the router.
- Create: `src/file_storage.py` (or `app/api/_corpus_storage.py`) — content-addressed write/delete helper.
- Create: `src/corpus_allowlist.py` — extension→tier classification + cap constants.
- Modify: `app/main.py` — register the router (merge-magnet).
- Create: `cli/commands/collections.py` — CLI; Modify CLI registry (merge-magnet).
- Modify: `cli/mcp/server.py` + `app/api/mcp_http.py` — MCP tools (merge-magnet).
- Create tests: `tests/test_corpus_allowlist.py`, `tests/test_file_storage.py`, `tests/test_api_collections.py`, CLI + MCP coverage assertions.

---

### Task 1: Extension→tier allowlist (`src/corpus_allowlist.py`)

**Interface:**
```python
TIER1_EXTENSIONS: set[str]   # txt, md, html, rtf, csv, tsv, json, jsonl, xlsx, parquet, docx, pptx, epub, eml, msg, pdf
TIER2_EXTENSIONS: set[str]   # png, jpg, jpeg, tif, tiff  (vision/OCR — stored now, processed in Slice 5)
MAX_UPLOAD_BYTES: int        # e.g. 100 * 1024 * 1024

def classify(filename: str) -> Optional[str]:   # "tier1" | "tier2" | None (unsupported → reject)
```
- [ ] Test first: `classify("a.pdf")=="tier1"`, `classify("a.PNG")=="tier2"` (case-insensitive), `classify("a.dwg") is None`, no-extension → None.
- [ ] Implement; commit. CHANGELOG bullet in report (Added).

### Task 2: Content-addressed storage helper (`src/file_storage.py`)

**Interface (mirror `app/api/uploads.py` content-addressing):**
```python
async def store_corpus_file(corpus_id: str, filename: str, upload: UploadFile) -> StoredFile
# streams to ${DATA_DIR}/file_corpora/<corpus_id>/<sha256>.<ext>, enforces MAX_UPLOAD_BYTES,
# returns StoredFile(sha256, storage_path, size_bytes, ext); idempotent on identical content.
def delete_corpus_file(storage_path: str) -> None
```
- [ ] Test first: storing bytes returns a path under DATA_DIR with the sha256; same content → same path (idempotent); oversize → raises; path is inside the corpus dir (no traversal from filename).
- [ ] Implement (read `app/api/uploads.py` for the streaming + sha256 + cap pattern); commit.

### Task 3: REST router (`app/api/collections.py`) + registration

**Endpoints (all JSON except upload=multipart):**
```
POST   /api/collections                      require_admin            body {name, slug?, description?} -> {id,...}
GET    /api/collections                       auth (RBAC-filtered list of accessible collections)
GET    /api/collections/{collection_id}       require_resource_access(COLLECTION,"{collection_id}")  -> collection + files[]
DELETE /api/collections/{collection_id}       require_admin            (soft_delete)
POST   /api/collections/{collection_id}/files require_resource_access  multipart files[]; allowlist gate; -> [{file_id,status,...}]
GET    /api/collections/{collection_id}/files require_resource_access  -> files[] with processing_status
DELETE /api/collections/{collection_id}/files/{file_id} require_resource_access
```
- Upload handler: for each file → `classify()`; unsupported → HTTP 422 `unsupported_file_type` (clear message, list allowed) and (per spec) store the raw bytes but record `processing_status='rejected'` with `processing_detail={'reason':'unsupported_type'}`; tier1/tier2 → `store_corpus_file()` + `corpus_files_repo().add(...)` status `pending`.
- GET list of collections must be RBAC-filtered (use the same access check the projection uses; non-admins see only granted collections; admins see all). Fail-closed.
- [ ] Tests first (`tests/test_api_collections.py`, `TestClient`): admin creates a collection; non-admin create → 403; member uploads a tier1 file → 200, row pending; upload `.dwg` → 422 rejected; non-member GET detail → 403; member GET lists the file with status. (Seed grants via the RBAC test helpers used by other `app/api/` tests.)
- [ ] Implement router; register in `app/main.py` (follow an existing `app.include_router(...)` there).
- [ ] Commit. CHANGELOG bullet (Added: Collections upload API).

### Task 4: CLI commands (`cli/commands/collections.py`) + registration

Mirror an existing command module (e.g. `cli/commands/catalog.py`) and its registration.
```
agnes collections create --name ... [--description ...]
agnes collections list
agnes collections show <id>
agnes collections upload <id> <path...>      # multipart POST per file
agnes collections rm <id>
```
- [ ] Test first (CLI invocation against a TestClient/server fixture as other CLI tests do): create→list shows it; upload→show lists the file.
- [ ] Implement + register; commit. CHANGELOG bullet.

### Task 5: MCP tools (API-coverage ratchet)

The REST×CLI×MCP ratchet (CI gate) requires every new `/api/*` endpoint be invocable via CLI (Task 4) **and** exposed as an MCP tool. Add MCP tools mirroring the management endpoints in `cli/mcp/server.py` and `app/api/mcp_http.py` (follow how `catalog`/`query` tools are registered):
```
collections_list()          -> accessible collections
collection_get(collection_id)-> detail + files
```
(Upload via MCP is out of scope — MCP tools are read/JSON; the ratchet's coverage map should map the upload endpoint to the CLI `upload` command. Verify the ratchet config/test accepts CLI-only coverage for multipart upload, or add the endpoint to the documented exception list with a one-line `log`/comment — do NOT silently grandfather.)
- [ ] Run the API-coverage test; make new endpoints pass (CLI + MCP) or be explicitly mapped. Commit. CHANGELOG bullet.

---

## Cross-cutting

- **No migration** in this slice (schema is v77 from Slice 1).
- **Merge magnets:** `app/main.py`, the CLI registry, `cli/mcp/server.py`/`app/api/mcp_http.py` — if built by a parallel team, these are magnet files (single builder avoids the conflict).
- **Full suite before finishing:** `uv run pytest tests/ --tb=short -n auto -q` (note: this repo uses `uv run pytest`, not `.venv/bin/pytest`). Must be green excluding the known pre-existing PG-needs-live-server / xdist-marketplace failures; the new `app/api/` tests and the API-coverage ratchet must pass.
- **No version bump** (release-cut decided separately). CHANGELOG `[Unreleased]` bullets only.

## Self-review

- Spec coverage: upload (multipart, content-addressed ✓), allowlist gate + reject (✓), status lifecycle pending/rejected (✓), RBAC fail-closed (✓), CLI+MCP coverage (✓). Ingestion/embeddings/UI are later slices.
- RBAC decision (admin-create / member-upload) documented; consistent with `data_packages` + Slice-1 `ResourceType.COLLECTION`.
- Types: uses Slice-1 repos `file_corpora_repo()` (create/get/get_by_slug/list/soft_delete) + `corpus_files_repo()` (add/get/list_for_corpus/set_status) verbatim.
