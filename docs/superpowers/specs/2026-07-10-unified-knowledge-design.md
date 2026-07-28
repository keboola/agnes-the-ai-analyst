# Unified knowledge: ingest, single-query search, local packaging, maintained digests

**Date:** 2026-07-10
**Status:** approved design (brainstorm output), pre-implementation
**Issues:** #795 (parent) — #796, #797, #798, #799
**Builds on:** `2026-06-15-document-ingestion-rag-design.md` (Collections, router,
hybrid retrieval), `2026-07-08-llm-first-ingestion-design.md` (honest statuses,
LLM-first ingestion, E2B sandbox)

## Problem

An agent today has to know *which* source to ask — documents live in
Collections, corporate know-how in the knowledge base, structured data in the
catalog — and each has its own separate search. Answers are slow and
hit-or-miss, and unstructured knowledge never leaves the server, so nothing
works offline. Issue #795 asks for the qmd-style experience: package docs +
wiki + architecture notes into a dataset that is **queryable locally with one
query across everything**, shipped **without per-source credentials**.

Much of the substrate exists. Collections ingest per-file uploads into
SQL tables + searchable chunks with hybrid (lexical + vector) retrieval and
RBAC; `agnes pull` distributes RBAC-filtered parquets to laptops with the
Agnes PAT as the only credential; the corporate-memory bundle already delivers
markdown into `.claude/rules/` at session start; the scheduler already
recomputes materialized tables on change. This design composes those pieces
and fills four gaps — one slice per sub-issue.

## Goal

1. A whole knowledge dump (e.g. a Confluence space export) can be ingested
   into a Collection in one action (#796).
2. One query fans out over documents + knowledge base + table catalog and
   returns a single ranked, cited result list (#797).
3. The knowledge travels to the analyst laptop via `agnes pull` and the same
   search works locally/offline; the only credential anywhere is the existing
   Agnes PAT (#798).
4. Admin-defined digest documents regenerate automatically when their sources
   change and land in the agent's context at session start (#799).

**Embeddings are a requirement, not an optional nicety.** Deployments should
run with the embedding model installed so "vector" in the title is real; the
lexical-only degradation path remains only as a fallback, never the intended
state. Deliverable includes making that easy (image variant or documented
install), not just possible.

## Non-goals

- **No live source connectors in v1** — no Confluence/wiki API integration
  (goes against the credential-free theme and is the largest lift). Git-repo
  knowledge sources are a natural later slice (the nightly-clone pattern
  already exists in `src/marketplace.py`) but are out of scope here.
- **No standalone export artifact** consumable without the Agnes CLI. The
  distribution channel is `agnes pull`; a portable qmd-style single-file
  export can be a later slice behind the same packaging seam.
- **No new vector store, no HNSW, no reranker** — brute-force hybrid retrieval
  stays, per the 2026-06-15 design's scale posture.
- **No changes to the three underlying search engines** beyond what K2's thin
  fan-out layer needs; they remain independently usable.

## Slice K1 — Bundle ingest (#796)

Extend the existing per-file upload to accept archives; no new concepts.

- Add `zip` to the corpus allowlist (`src/corpus_allowlist.py`).
- New router branch `src/ingest/bundle.py`: safe unpack (zip-slip guard,
  depth / file-count / total-size limits), then each inner file becomes its
  own `corpus_files` row routed through the existing `ingest_file` — tabular
  to SQL tables, prose to chunks, images to the vision path, exactly as if
  uploaded individually.
- New nullable `corpus_files.parent_file_id` column linking children to the
  archive row (Alembic ↔ `src/db.py` ladder, both repos, contract test).
- **Confluence export normalizer** (detection-based, not a new format tier):
  when the zip matches the Confluence HTML/XML export layout, strip
  navigation boilerplate and map page titles into `section_path`; content
  then flows through the existing HTML extractor. Anything else in the
  archive takes the normal per-extension route; hard files inside a bundle
  fall to the LLM-first path (2026-07-08 spec) once that ships.
- Archive row status aggregates its children: `indexed` if ≥1 child indexed,
  else `needs_review` with a reason — consistent with the status-honesty
  rules. Child rows keep their own per-file statuses.
- Surfaces unchanged: the existing upload endpoint / CLI / MCP simply accept
  a zip. Library UI shows the archive row with a child summary.

## Slice K2 — `knowledge_search`: one query across everything (#797)

A thin fan-out module (`src/search/unified.py`) over three existing engines,
merging their results — no engine is modified:

1. **Collections chunks** — existing hybrid retrieval
   (`src/ingest/retrieval.py:search`, IDF-weighted lexical + cosine).
2. **Knowledge base** — existing `knowledge_items` search (DuckDB FTS/BM25 via
   `src/fts.py`; PG `ts_rank` sibling), through the knowledge repo.
3. **Table catalog cards** — lexical match over `table_registry`
   name/description/columns. A table hit returns a pivot hint ("structured
   data — query it with SQL via `agnes query`"), never flattened rows. This
   keeps the router principle from the 2026-06-15 design: tables are answered
   by SQL, not similarity.

Merging: min-max normalize scores within each source, interleave, return
top-k. Each hit is typed (`chunk | knowledge | table`) and carries a citation
(file + page/section, knowledge-item ref, or table id + schema pointer).

RBAC is fail-closed per source: collection grants for chunks, memory-domain
grants for knowledge items, `can_access_table` for catalog cards. Empty grant
set for a source → that source contributes nothing; never "search all".

Surfaces (API-coverage ratchet): `GET /api/knowledge/search` + MCP tool
`knowledge_search` + `agnes search`. The existing `collections_search`
remains for scoped searches.

## Slice K3 — Credential-free local packaging (#798)

Ride the existing `agnes pull` channel; the Agnes PAT stays the only
credential end-to-end.

- **Server:** build a per-collection artifact `knowledge.duckdb` (chunks +
  embeddings for that corpus) when corpus content changes; content-hash it
  and list it in the sync manifest next to tables. RBAC = the existing
  collection grant — the manifest only lists artifacts for granted corpora,
  mirroring how tables are filtered today.
- **Client:** `agnes pull` downloads changed artifacts to
  `user/knowledge/<corpus>.duckdb` (hash-verified, atomic promotion, pruned
  on de-authorization — same lifecycle as parquets). The FTS index is rebuilt
  locally after download (cheaper than shipping it; `src/fts.py` pattern).
- **Local search:** `agnes search --local` (and the MCP tool when the server
  is unreachable) runs the same hybrid scoring against local artifacts.
  Vector scoring requires the embedding model locally (the query itself must
  be embedded with the same model) — available via the `agnes[embeddings]`
  extra (~130 MB model). Without it, local search degrades to lexical-only:
  the same rule as the server, no special logic.
- Digest artifacts from K4 and the existing corporate-memory bundle already
  travel this channel as markdown; K3 adds only the chunk/vector payload.

## Slice K4 — Maintained digests (#799)

Scout-pattern merged artifacts: a few key markdown files, always current,
loaded into agent context at session start.

- New table `knowledge_artifacts`: `id`, `slug`, `title`, `instructions`
  (the standing prompt, e.g. "maintain an overview of our architecture"),
  `source_corpus_ids`, `output_md`, `source_fingerprint`, `generated_at`,
  `model`, `status`. Both backends + contract test, migration ladders.
- **Scheduler pass** modeled on `_run_materialized_pass`: fingerprint the
  source corpora's chunk content; only when the fingerprint changed, run an
  LLM job to regenerate the digest. Budget/timeout knobs mirror the
  `ingest.llm.*` family; concurrency 1.
- **Failure semantics:** generation failed / budget exhausted / no API key →
  keep the previous `output_md`, mark the artifact visibly stale
  (`status` + reason). Never a silent failure, never a half-written digest.
- **Distribution:** the manifest lists digests as assets; `agnes pull`
  writes `.claude/rules/ka_<slug>.md` — the same delivery as the
  corporate-memory `km_*.md` bundle, so the agent has the digest in context
  from the first second of a session.
- Admin CRUD for artifact definitions: REST + `/admin` UI + CLI + MCP
  (ratchet), grants via the standard `resource_grants` mechanism.

## Sequencing

K1 → K2 → K3 → K4, one PR (and one implementation plan) per slice. K2 does
not depend on K1 (parallelizable); K3 depends on K2's search module for the
local path; K4 depends on K3's manifest/asset plumbing.

## Cross-cutting conventions (per CONTRIBUTING.md sync-map)

- DuckDB repo + `_pg.py` sibling + factory entry + cross-engine contract test
  in the same PR for every new/changed repository.
- Alembic ↔ `src/db.py` migration ladders land together.
- Every new REST endpoint ships CLI + MCP coverage (API-coverage ratchet) and
  an RBAC gate (`require_admin` / `require_resource_access`).
- CHANGELOG bullet per PR; vendor-agnostic content only (Confluence appears
  solely as a detected export format, no customer specifics).
- Full test suite (`.venv/bin/pytest tests/ --tb=short -n auto -q`) before
  every push.

## Testing (per slice)

- **K1:** unit — unpack guards (zip-slip, limits), Confluence-layout
  detection/normalization, status aggregation; integration — a fixture zip
  (mixed tabular + prose + junk) ingests end-to-end with honest per-child
  statuses; contract — `parent_file_id` on both backends.
- **K2:** unit — per-source normalization and interleave (deterministic
  ordering), typed hits, RBAC fail-closed per source (a caller with no grants
  on one source still gets the other two); API/CLI/MCP surface tests.
- **K3:** artifact build/hash on content change; pull download/verify/prune
  lifecycle against a stub server; local search parity — the same query over
  the same corpus returns the same ranking server-side and locally (with and
  without the embedding model).
- **K4:** fingerprint short-circuit (unchanged sources → no LLM call, using a
  fake agent); failure semantics (stale, never silent); delivery — pulled
  digest lands as `.claude/rules/ka_<slug>.md`.

## Open questions (deferred, not blockers)

- Embedding-model packaging: separate Docker image variant vs documented
  install step — decide in K3 when the local model need is concrete.
- Whether `knowledge_items` should eventually gain embeddings so source 2 of
  K2 is also semantic (today it is FTS-only). Cheap once K2's seam exists.
- Portable single-file export (qmd-style, outside Agnes) — later slice behind
  K3's packaging seam if demand appears.
