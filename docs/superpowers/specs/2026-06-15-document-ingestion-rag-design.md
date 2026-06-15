# Bring-your-files: document ingestion + agent-native retrieval

**Date:** 2026-06-15
**Status:** draft design (brainstorm output), pre-implementation

## Problem

Agnes serves AI agents excellently over **registered structured tables**
(the `extract.duckdb` contract → DuckDB → `agnes query`/MCP). But a user
cannot today **bring their own files** — upload or connect a pile of
documents — and have an agent search and reason over them. The pieces that
would support it exist only in fragments: BM25 full-text search over
`knowledge_items` (`src/fts.py`), an RBAC layer (`resource_grants` +
groups), a chat/MCP surface — but there is no ingestion pipeline, no
embeddings, no document parsing, and no unified retrieval tool. Search is
per-resource and lexical-only; there is no semantic retrieval and no path
from "raw file" to "queryable knowledge".

## Goal

> A user brings their data (uploads N files, or connects a source), Agnes
> indexes it, and an LLM **through Agnes** can search it, understand it, and
> summarize over it — accurately, permission-safe, with citations.

"Work with it" in v1 means **read-only**: search, understand, summarize,
synthesize. **No write-back / actions** (no transforming files, filling
forms, or pushing to external systems) — that is a later phase.

Success is measurable:

- A high fraction of real questions answered correctly **without a human
  writing SQL or grep**, grounded in the user's files.
- **Zero permission leaks** — not "few", zero. P0 correctness invariant.
- Every answer is **citable** (source file + page/section) so it can be
  trusted and verified.
- **Most common file types** supported (explicit allowlist, §6), failing
  loudly outside it.

## Non-goals (what we deliberately do NOT build)

- **Not horizontal enterprise search ("Glean").** No crawling "all company
  knowledge", no breadth of SaaS connectors, no cross-source ACL mirroring.
  We index what the user *brings*, inside Agnes's own permission domain.
- **Not source-ACL mirroring.** Uploaded/connected files live under Agnes
  RBAC; we do not replicate Drive/Slack/etc. permission models. This removes
  the single biggest security-liability surface of a Glean-style system.
- **Not a per-format parser zoo.** One parsing layer + one vision fallback,
  not a bespoke extractor per extension (§3).
- **Not "support all files".** "Most common" is an allowlist, not a promise
  of universal coverage (§6).
- **Not write-back / agent actions** in v1 (read-only, see Goal).
- **Not a separate vector database.** Embeddings are a DuckDB column, not a
  new stateful service (§5, §7).

## Architecture: one router, three paths

```
upload / connect → ROUTER (extension + content sniff)
   │
   ├─ Tabular (CSV/TSV/XLSX/Parquet/JSON/JSONL)
   │     → load as DuckDB table (SQL-queryable, NOT embedded as text)
   │     → index a text "card" (sheet/columns/row-count/sample) for discovery
   │
   ├─ Documents (PDF/DOCX/PPTX/HTML/EPUB/RTF/ODT/MD/TXT/EML/MSG)
   │     → primary parser → Markdown + typed element tree
   │     → tables inside docs → structured table → persist as DuckDB table
   │     → low-confidence / scanned / chart page → vision-LLM fallback
   │
   ├─ Images (PNG/JPG/…) → OCR or vision caption → index the text
   │
   └─ Archives (ZIP) → unpack + recurse (depth/size limits)
```

The router by file-type **is** the core design decision. "Perfectly search a
spreadsheet" means SQL, not cosine similarity; "perfectly search a contract"
means semantic + lexical retrieval. They are different engines and must be
routed, not unified into one embedding blob.

## Domain model: Collections (not data packages)

A bring-your-files unit is a **distinct first-class entity**, not a reuse of
`data_packages`. Data packages are admin-curated bundles of *already
registered tables* (`data_packages` + `data_package_tables` → `table_registry`,
`src/repositories/data_packages.py`); they reference tables and are subscribed
to ("My Stack"). A bring-your-files unit is self-service, mixed-content, and
has a per-file processing lifecycle — a different shape:

| | Data Package | Collection (new) |
|---|---|---|
| Creator | admin (curator) | **user (self-service upload)** |
| Content | registered tables only | **mixed — documents + tabular files** |
| Lifecycle | references existing tables | **upload → parse/vision → index** (async) |
| Consumption | add to "My Stack" | **direct agent RAG** |

Forcing files → tables → package would lose the per-file processing state
(indexed / vision-processing / rejected) the UI must show.

**Naming.** User-facing noun **Collection** (the unit) inside a **Library**
section (hero eyebrow "Files"); internal code `file_corpora` / `corpus`.
Avoid "knowledge base" — it collides with Memory Domains (the "Knowledge"
eyebrow, `corporate_memory.html`). Existing UI nouns already taken: "Data"
(data packages), "Knowledge" (memory domains), "Recipe", "Marketplace".

**How they relate (reuse, not reinvent).** The router's two outputs each take
an existing path:
- **Tabular files → normal `table_registry` rows** → flow through catalog,
  RBAC, and the manifest like any other table; *may* optionally be grouped
  into a data package. No new query path.
- **Documents → `corpus_chunks`** (new), owned by the collection.

A Collection is the **self-service ingestion/ownership layer**; a data package
is the **admin curation/distribution layer** — adjacent concepts at different
altitudes, not competitors.

**New schema** (DuckDB repo + `_pg` sibling + factory + contract test in the
same change, per `CONTRIBUTING.md` sync-map; Alembic ↔ `src/db.py` ladder):
- `file_corpora`: id, slug, name, description, created_by, created_at,
  updated_at, deleted_at (soft-delete, mirroring `data_packages`).
- `corpus_files`: id, corpus_id, filename, sha256, file_type,
  size_bytes, storage_path, `processing_status`
  (pending/processing/indexed/rejected), `processing_detail` (JSON: tier,
  vision_used, error, derived `table_registry` id, chunk_count), timestamps.
- `corpus_chunks`: id, corpus_id, file_id, ordinal, text, `embedding`
  (`FLOAT[]` DuckDB / pgvector or array PG), section_path, page, bbox,
  metadata. Tabular-derived tables register into `table_registry`, linked
  back via `corpus_files.processing_detail`.

**RBAC.** New `ResourceType.COLLECTION` in `app/resource_types.py` with a
`_collection_blocks` projection (mirroring `_data_package_blocks`). Grants
`(group, collection, <id>)` via `/admin/access`; creator gets access. Every
retrieval path filters by `resource_grants` → `corpus_files`/`corpus_chunks`
join, fail-closed (the §"Permission invariant" P0).

**Upload infra is net-new.** Today only image cover-uploads exist
(`app/api/uploads.py`, admin-only, 5 MiB, content-addressed). Need:
`POST /api/collections` + `…/{id}/files` (multipart, streaming `UploadFile`,
content-addressed under `${DATA_DIR}/file_corpora/<id>/<sha256>.<ext>`, size
cap + allowlist gate at upload), plus an async ingestion job (route → Docling /
vision / tabular→DuckDB → chunk+embed → update `processing_status`) on the
existing scheduler pattern. New REST endpoints need CLI + MCP coverage (the
API-coverage ratchet).

## UI: two screens, design-system native

Both `{% extends "base_page.html" %}` (gradient hero + `{% block toolbar %}` +
`{% block page %}`), `ds.*` macros, `--ds-*` tokens only (contract guards in
`tests/test_design_system_contract.py`). Mirrors the `/catalog` +
`/marketplace` patterns so it feels native.

1. **Library list** — `/library`. Hero eyebrow "Files" / title "Your
   collections"; toolbar "+ New collection"; grid of `ds.panel` cards (name,
   file count, #queryable tables, owner, access). Mirrors `catalog.html`.
2. **Collection detail** — `/library/<slug>`. Hero (name + "N files · M
   tables · access: <group>") + "Upload files" toolbar action; a drop zone;
   a file list where each row shows a type icon + a derived-artifact line
   ("SQL table · 3 sheets" / "Document · 24 chunks" / "scanned · vision
   fallback") + a **per-file status pill** mapped to `processing_status`
   (indexed=success / processing·vision=warning / pending=info /
   rejected=danger — reuse the existing `.status-pill` pattern); and an "Ask
   this collection" entry that opens a `resource_grants`-scoped agent/RAG
   session. Rejected rows are stored but not indexed (fail-loudly per the
   allowlist, §6).

## Parsing stack

- **Primary parser: Docling** (MIT, self-hosted). Best-in-class OSS
  table/layout extraction (TableFormer), emits Markdown + a typed element
  tree we can chunk along. Self-hosted matters twice: no per-page API bill,
  and **uploaded data never leaves the deployment** (a governance
  requirement for any sensitive corpus).
- **Vision fallback: the platform's existing multimodal LLM** on the page
  image, used **only** when the parser reports low confidence, or for
  scanned PDFs / charts / chaotic layouts (~the hard 5–10%). Vision-first as
  a *default* is a cost trap (~$0.13/page); vision-first as a *fallback* is
  the cheapest way to close the quality gap, and reuses a model we already
  pay for rather than adding a new vendor.
- **Tables are never flattened into prose chunks.** A table → a DuckDB table
  (or a whole-table Markdown chunk) + an indexed description. The agent
  retrieves the description, then answers with **SQL**. A table answered by
  `SELECT` is correct; a table answered by embedding similarity over
  flattened cells is a coin flip. This is Agnes's structural advantage over
  generic "chat with your files" tools, which butcher CSV/Excel.

## Chunking

- **Structure-aware**, splitting on the document's own layout boundaries
  (headers/sections/elements from the parser's element tree), target
  ~512–1024 tokens with overlap. **Tables and code blocks are atomic** (never
  split mid-element); the section heading is prepended.
- **Contextual prefix** (Anthropic-style contextual retrieval): a one-line
  LLM-generated "this chunk is from section X of doc Y about Z" prepended
  before embedding. Cheap with prompt caching, large recall win.
- No exotic per-sentence semantic chunking — evidence shows it rarely beats
  header-aware fixed-size for the compute.
- Every chunk carries metadata: `source_file`, `page`, `element_type`,
  `section_path`, `bbox` — powering citations and "jump back to the page
  image" when text is ambiguous.

## Retrieval

- **Hybrid: BM25 (`src/fts.py`, exists) + dense embeddings.** Lexical is not
  legacy — it wins on exact identifiers (IDs, SKUs, names); hybrid beats
  either alone.
- **Embeddings live as a DuckDB column** (`FLOAT[]`), queried with
  `array_cosine_similarity`. **No separate vector database** — keeps vectors
  on the same row that gets re-indexed with the data (self-healing, RBAC
  rides the same `JOIN`).
- **Embedding provider is pluggable; default is self-hosted small**
  (`bge-small-en-v1.5`, 384-dim) — data never leaves the deployment
  (governance, consistent with the Docling rationale), free, fast at the
  dozens-of-files scale. The provider is an interface (API providers
  Voyage/OpenAI/Cohere are valid alternatives); the column width is fixed by
  the chosen model's dimension (384 for the default), so changing models is a
  re-embed, not a schema change beyond the array width.
- Exposed to the agent as **one MCP `search` tool** (alongside the existing
  catalog/schema/query tools in `cli/mcp/server.py`), returning ranked
  chunks **with citations**, plus pointers to any DuckDB tables derived from
  tabular files so the agent can pivot to SQL.

## Permission invariant (P0)

- Enforcement is **`resource_grants` on every retrieval path** — BM25,
  embedding, and DuckDB-structured — at query time, **fail-closed**. An agent
  must not be able to retrieve a chunk/row the caller's groups don't grant.
- Uploaded files belong to the uploading user / their workspace; access is
  granted via the **existing RBAC** (`app/auth/access.py`,
  `Depends(require_resource_access(...))`) on the new `ResourceType.COLLECTION`
  (see §"Domain model").
- A `checkdocumentaccess`-style audit harness: for a given user, assert the
  retrievable set equals the granted set. Treat ACL/grant correctness as a
  test-enforced invariant, not a feature.

## File-type allowlist (3 tiers)

"Most common files" is scoped explicitly; the cost curve past complex scans
is a cliff, not a slope, and silent partial failure (a half-OCR'd table that
*looks* fine) poisons retrieval worse than honest rejection.

- **Tier 1 — full support:** TXT, MD, HTML, RTF, CSV, TSV, JSON/JSONL,
  XLSX, Parquet, DOCX, PPTX, EPUB, EML/MSG, born-digital PDF.
- **Tier 2 — best-effort (vision fallback), flagged "processed via vision":**
  scanned PDFs, complex/multi-column layouts, image-heavy docs, standalone
  images.
- **Unsupported → reject at upload** with a clear message; store the raw file
  so the agent can at least acknowledge it exists. (Handwriting, CAD,
  DRM'd/password-protected, audio/video, nested archives beyond a depth
  limit, exotic legacy formats.)

## Scale: dozens now, millions later

v1 targets **dozens of files per user**. Decisions that keep the door open
for a million-file client without building for it now:

- **Retrieval:** brute-force `array_cosine_similarity` is fine at this scale.
  The seam to swap in a DuckDB `vss`/HNSW index (or an external vector store
  *only if measured necessary*) is the `search` tool's retrieval function —
  keep it behind one interface so the index strategy is swappable.
- **Ingestion:** parsing is per-file and embarrassingly parallel; the
  pipeline should be a queue of independent jobs from day one (no
  all-at-once assumption), so scaling = more workers, not a rewrite.
- **Storage:** embeddings-as-DuckDB-column holds at dozens–low-hundred-K;
  revisit only when a real corpus forces it. Do not pre-optimize.

## MVP — the thinnest end-to-end loop

Prove "bring files → agent searches them perfectly" on the easy path first:

1. **Upload + router + Tier-1 parse.** Upload a handful of files; route
   tabular → DuckDB tables, documents → Docling → Markdown/elements.
2. **Chunk + embed + BM25 index**, embeddings as DuckDB column, RBAC-scoped.
3. **`search` MCP tool** → hybrid retrieval with citations, surfaced to the
   agent; tabular pivots to SQL.
4. **Permission audit harness** green (fail-closed).

Explicitly out of MVP: vision fallback (Tier 2), OCR, connectors (upload
only), HNSW/scale work, write-back. Each is an additive slice.

## Sequenced slices after MVP

- **S1 — Vision fallback (Tier 2):** scanned/complex docs via the multimodal
  LLM on page images; confidence-gated.
- **S2 — Connectors:** "connect a source" in addition to upload (live MCP
  for sources that already expose one; indexed ingestion otherwise).
- **S3 — Scale:** swap brute-force retrieval for an indexed strategy when a
  corpus measurably needs it.
- **S4 — Richer "work":** still read-only — multi-doc synthesis, structured
  extraction across a corpus. (Write-back remains out of scope until
  separately specified.)

## Dual-backend / conventions notes (for implementation)

- Any new repository (corpus metadata, chunks, document-set registry) needs a
  **DuckDB repo + Postgres `_pg.py` sibling + factory entry + contract test**
  in the same change (per `CONTRIBUTING.md` sync-map). Embeddings-as-column
  must work on both backends (PG: `pgvector` or array; DuckDB: `FLOAT[]`).
- Schema changes need the matched **Alembic ↔ `src/db.py` migration ladder**.
- New REST endpoints need **CLI + MCP coverage** (the API-coverage ratchet)
  and an RBAC gate.
- Vendor-agnostic: parsing/retrieval are generic; no source- or
  customer-specific assumptions in code/config/docs.

## Open questions

- ~~Embedding model + dimensions~~ — DECIDED: pluggable provider, default
  self-hosted `bge-small-en-v1.5` (384-dim). See §Retrieval.
- Re-embedding/cache invalidation policy when a file is replaced.
- Corpus granularity for RBAC: per-file vs per-"document-set" grants (likely
  the latter as the `ResourceType`).
- Reranking: whether a cross-encoder reranker is worth it in v1 or an S-slice.
