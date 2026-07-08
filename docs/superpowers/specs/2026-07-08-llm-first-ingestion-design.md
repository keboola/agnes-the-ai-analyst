# LLM-first collection ingestion (+ ingestion status honesty)

**Date:** 2026-07-08
**Status:** approved design (brainstorm output), pre-implementation
**Builds on:** `2026-06-15-document-ingestion-rag-design.md` (router: tabular / documents / images; vision fallback)

## Problem

Two field-observed failures in the Collections ingestion pipeline
(`src/ingest/`):

1. **PDF rejected on default images.** `text_extract.py` supports a
   lightweight `pypdf` fallback when the heavy `docling` extra is absent —
   but `pypdf` is not a core dependency, so default deployments reject every
   PDF with *"PDF text extraction needs the 'docling' extra or pypdf"*.
2. **Dishonest `indexed` status.** A real-world 15-sheet finance workbook
   (title rows, pivot sheets, slicers, 34k-row journal with mixed-type
   columns) went through `ingest_tabular` → DuckDB `read_xlsx()` picked the
   first parsable sheet → **0 rows, 1 column** → the file was marked
   `indexed` and the empty derived table registered. Search over the
   collection returns nothing, and the UI gives no hint anything failed.

The deterministic single-call reader fundamentally cannot handle messy
real-world workbooks/documents. Per the 2026-06-15 spec's own non-goal we do
not want a per-format parser zoo either. Decision: route hard formats to an
**LLM agent that picks its own tools** — the approach the platform already
trusts for chat.

## Decisions (approved)

- **LLM is the primary orchestrator** for non-trivial formats, not a
  fallback. Trivial formats stay deterministic (LLM would only burn tokens).
- **Execution in an E2B sandbox** (same provider/template plumbing as chat).
  Uploaded files are untrusted input; the agent gets full tool freedom
  *inside* the sandbox and zero Agnes credentials, so prompt injection from
  file content has nowhere to escalate.

## Part A — status honesty + pypdf (ship first, independent PR)

1. Add `pypdf` to **core** dependencies (small, pure-python). `docling`
   stays an opt-in extra.
2. `indexed` requires non-empty output: a tabular ingest that produces an
   empty table (0 rows), and a document/image ingest that produces 0 chunks,
   transitions to a new **`needs_review`** status instead, with
   `processing_detail.reason` (e.g. `extraction produced empty table`).
   Status set: `pending → processing → indexed | needs_review | rejected`.
3. **UI**: Library file cards visibly badge `rejected` / `needs_review`
   with the reason (today the status is silently swallowed, so a broken
   ingest looks like broken search).
4. **Re-ingest**: `POST /api/collections/{id}/files/{file_id}/reingest`
   (admin) + a button on the file card. Unblocks backfill of files ingested
   before this fix. CLI + MCP coverage per the API-coverage gate.

## Part B — LLM-first ingestion

### Routing

```
upload → ext/content sniff
  ├─ trivial (csv/tsv/txt/md/jsonl/parquet) → deterministic path (today's)
  └─ everything else (xlsx/xls, pdf, docx, pptx, images, zip, …)
        → LLM ingestion job (E2B sandbox)
        → llm disabled/keys missing → deterministic path + honest statuses
```

`ingest.llm.enabled` in `instance.yaml` (default **false** — OSS instances
without keys keep working unchanged). Reuses chat's provider config
(`chat.provider=e2b` template plumbing) but with its own budget knobs.

### Agent contract

Sandbox receives: the file (read-only mount/copy), a task prompt, and
nothing else — no Agnes tokens, no network access to the Agnes API. It may
install and use any tool it wants (pandas, openpyxl, pdfplumber, vision on
page renders, …). It must write `/out`:

- `tables/*.parquet` — extracted tables (multi-sheet workbook → one per
  meaningful sheet; pivot/slicer/technical sheets skipped)
- `chunks/*.jsonl` — text chunks, each with `text` + `citation`
  (`filename`, `sheet|page|section`)
- `manifest.json` — per-output provenance: source region, tool used,
  confidence, row/chunk counts; plus an overall summary

The server **validates** the manifest against a schema and enforces limits
(output size, table/chunk counts) before importing anything. Import goes
through the existing paths — parquet via the `ingest_tabular` registration
flow (derived tables, `_meta`, `table_registry`), chunks via
`_chunk_embed_store` — so RBAC, catalog, and retrieval behave exactly as
for deterministic ingests.

### Guardrails

- `ingest.llm.per_file_budget_usd`, `ingest.llm.daily_budget_usd`,
  `ingest.llm.timeout_seconds`, concurrency 1.
- Budget/timeout exhausted or agent failed validation → `needs_review`
  with reason — never a silent no-op, never a half-import.
- Outcome (tool used, cost, duration) recorded in `processing_detail`.

### Failure semantics

| Outcome | Status |
|---|---|
| valid manifest, ≥1 non-empty output | `indexed` |
| agent ran, outputs empty/invalid | `needs_review` (+reason) |
| budget/timeout/sandbox error | `needs_review` (+reason) |
| format on neither path's allowlist | `rejected` |

### Testing

- Unit: runner status transitions (incl. empty-output → `needs_review`)
  with a fake agent; manifest validator (schema + limits) property cases.
- Contract: `corpus_files` status set extended in DuckDB + PG repos in the
  same change, cross-engine contract test extended.
- E2E: one gated scenario (env-gated like the chat E2E) — upload a messy
  workbook fixture, assert per-sheet tables + honest statuses.
- Fixture: a synthetic multi-sheet workbook reproducing the failure class
  (title rows, pivot sheet, mixed-type column).

## Non-goals

- No write-back/actions from the ingestion agent (read-only extraction).
- No change to retrieval/search itself.
- No per-format parser zoo growth on the deterministic path — it stays
  frozen at today's formats + `pypdf`.
- No new vector store; outputs land in the existing DuckDB structures.
