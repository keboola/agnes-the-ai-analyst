# Grounded Analyst Workspace — Port Specification

**Date:** 2026-08-13
**Status:** Approved direction; waves execute via per-wave plans in `docs/superpowers/plans/`.
**Design source:** the upstream Keboola "Business User AI Workspace" PRD (2026-07-23) and its
shipped reference implementation. This spec transplants that product into Agnes **at full
fidelity**: same capabilities, same UI contracts, same interaction grammar — expressed in
Agnes's stack (FastAPI + Jinja2 + vanilla JS + `--ds-*` design system) and Agnes's existing
primitives (chat runner, MCP foundation tools, `metric_definitions` + `glossary_terms`,
hosted data apps, agent profiles).

**Fidelity bar (what "identical" means here):** for every capability below, the *rendered
behavior* — states, transitions, badge semantics, popover content, chart interactions,
empty states, error/streaming fallbacks — must match the reference implementation.
The *code* is Agnes-native. Where the reference made a deliberate trade-off (heuristic
validation, LLM-chosen refs, prompt-enforced conventions), we replicate the trade-off, not
an "improved" variant — north-star upgrades stay deferred exactly as upstream deferred them.
Where the reference stubbed or mocked (activity feed, thumbnails), we replicate the stub
**unless** Agnes already has the primitive for the real thing at equal-or-lower cost; each
such divergence is called out inline and needs no further approval.

**Architectural simplification (stated once, applies throughout):** upstream gates the
workspace per *project* (feature flag / URL override) because it lives inside a
data-engineering IDE. Agnes has no such dual identity — the whole instance IS the
business-user surface. Therefore: no per-project gating, no `?aiWorkspace` override, no
sessionStorage flag. The gate is the instance-level redesign experience, and after Wave 0
there is no other experience. Upstream's "workspace shell" == Agnes's rail chrome.

---

## Wave 0 — Retire the classic experience (BREAKING)

The redesign (rail chrome + paper theme + redesigned pages) becomes the only experience.
This removes the dual-surface tax from every subsequent wave: no more `ui_layout == 'rail'`
branches in chat.html, no `*_legacy.html` freezes, no default-parity guard.

- Default `experience` flips to `redesign`; the `classic` value is removed (startup warning
  + fallback to redesign if configured). `AGNES_UI_LAYOUT=topnav` likewise retired.
- Delete: all `*_legacy.html` templates + their router branches, `_app_header.html` topnav
  chrome, `js/tour_legacy.js`, `_tour_legacy.html`, the `ui_layout != 'rail'` branches of
  `chat.html`, `_chat_welcome_cards_legacy.html`.
- Rewrite `tests/test_ui_layout_theme.py`: the "default look never changes" contract is
  deliberately retired; new contract = rail+paper render as the only chrome, `experience`
  knob tolerated-but-inert for `classic`.
- **BREAKING** changelog entry: instances that never opted in (e.g. any fleet still on
  default chrome) change look on upgrade. Release notes must say exactly that.

Plan: `docs/superpowers/plans/2026-08-14-workspace-w0-legacy-retirement.md`.

---

## Wave 1 — Grounding & citations (the trust engine)

**Product (fidelity target).** Every data answer carries evidence. Query results render a
tri-state trust badge — **Validated** (green check; SQL matched governed semantic-layer
objects, no violations), **Check** (amber warning; matched but with rule violations or
`valid: false`), or **no badge at all** (ad-hoc SQL; absence-of-badge is deliberate
anti-noise). The result detail modal gains a "Semantic layer" section listing the models,
metrics, and datasets the query touched plus a violations list. Inline `[[q:ref]]` markers
in assistant prose render as clickable shield chips that open the exact query (SQL + result
+ grounding) behind a stated number. A missing/unresolved ref degrades to plain
`[[q:ref]]` text (mid-stream state), never breaks the message.

**Protocol (verbatim from the reference).**
1. Agent picks a short `query_ref` per claim (`q1`, `q2`, … lowercase alphanumeric).
2. Calls `validate_semantic_query(sql_query, query_ref)` BEFORE running the query.
3. Calls the query tool with the SAME SQL and SAME `query_ref`.
4. Cites in prose immediately after the stated figure: `[[q:<ref>]]`.
5. Re-running a corrected version of the same logical query REUSES the ref (ref identifies
   the claim, not the attempt).

**Server (Agnes mapping).**
- `app/api/mcp/foundation_tools.py`: add optional `query_ref: str = ""` passthrough to the
  existing `query` tool (echoed in output, otherwise inert), and a new
  `validate_semantic_query(sql_query: str, query_ref: str = "") -> dict` tool.
- Validation is **heuristic string matching** (replicating the reference's declared
  best-effort semantics): normalize the SQL (whitespace-collapse, lowercase), then detect
  - used metrics: `metric_definitions` rows whose name or definition-SQL fragments appear
    in the query;
  - used datasets: `table_registry` rows whose table name appears in FROM/JOIN position;
  - models: distinct `glossary_terms.model_uuid` / metric provenance where available,
    else a single synthetic model representing the instance registry;
  - violations: business-rule strings attached to matched metrics (severity `info|warn`),
    e.g. a metric definition's documented constraints that the query text contradicts.
- Output contract — **identical shape** to the reference (the UI parser is written against
  it):

```json
{
  "query_ref": "q1",
  "validation_auto_detected": {
    "valid": true,
    "semantic_models": [{"id": "...", "name": "..."}],
    "used_metrics": [{"id": "...", "name": "...", "description": "...", "sql": "...", "dataset": "..."}],
    "used_datasets": [{"id": "...", "name": "...", "tableId": "..."}],
    "violations": [{"severity": "warn", "message": "..."}],
    "summary": "..."
  }
}
```

- Tri-state derivation (client-side, exact reference logic): `violations.length > 0 || !valid`
  → (`used_metrics`+`used_datasets` nonempty ? `violations` : `ad-hoc`); else nonempty
  → `validated`, empty → `ad-hoc`.
- MCP tool-set parity: update `tests/test_mcp_http.py`'s pinned tool set and
  `tests/test_mcp_tool_parity.py` in the same PR (both backends of the sync-map row).

**Client (`app/web/static/js/chat.js`).**
- Build per-message grounding resolver + **conversation-wide** citation registry from
  `tool_call` frames (they already carry `args` = tool input) joined to their
  `tool_result` frames via `tool_use_id`; `validate_semantic_query` results arrive as JSON
  text → `JSON.parse` behind a shape guard. Join order: by `query_ref`, fallback by
  normalized SQL (`sql.replace(/\s+/g,' ').trim().toLowerCase()`). Citation registry is
  chat-wide and last-write-wins on ref; grounding badge resolves per-message (two scopes,
  replicated deliberately).
- Render: badge on the query-result block; "Semantic layer" section + violations in the
  result modal; `[[q:ref]]` → chip via a post-markdown DOM pass (Agnes uses marked, not
  remark: the citation rewrite happens on the rendered container, only inside text nodes,
  after `renderMarkdownSafe`). All injected content goes through the sanitizer.
- Streaming: an unresolved ref renders as literal `[[q:ref]]` text and upgrades to a chip
  when its query completes (same graceful-degrade as the reference).

**Prompt.** The analyst rails (Wave 4) carry the validate→run→cite protocol text. Until
Wave 4 lands, the tool exists but nothing drives it — acceptable; waves are independently
shippable.

**Replicated caveats (do NOT fix in this wave):** refs are LLM-chosen and can dangle;
validation is regex-grade; `confidence` stays reserved-unimplemented; the chat-wide
registry collapses a reused ref to the latest query.

---

## Wave 2 — Glossary term tooltips

**Product.** When the agent uses a term defined in `glossary_terms`, the term renders with
a dashed underline + info icon; clicking opens a popover: "What is {concept}?", the
glossary definition (markdown, nested-rendered), and a "Show semantic layer definition"
link deep-linking to `/catalog/semantics?search=<concept>`. Coverage is prompt-driven;
only glossary-defined terms may be annotated.

**Annotation grammar (verbatim).**
`[Concept](https://example.com/)<!--sem-ref{"concept":"…","summary":"…","model":"<uuid>","type":"glossary"}-->`
— href is an ignored placeholder; metadata rides the comment. The parser also accepts the
legacy `kb-doc` marker name.

**Robustness invariants (port exactly — each guards an observed LLM failure):**
- Pre-parse normalization: variant terminators (`→`, `->`, `–>`, `—>`, one-or-two
  typographic dashes) are repaired to `-->` **only when immediately preceded by `"` or
  `}`** (so a `->` inside a summary can't truncate); literal newlines inside the JSON are
  escaped to `\n`.
- JSON parse retries with truncation-repair suffixes `''`, `'}'`, `'"}'`.
- **A matched annotation comment NEVER reaches rendered output** — stripped even when its
  metadata is unusable (then the link stays a plain link). Unterminated comment mid-stream
  = leave for a later render pass.
- Metadata is carried per-link (`data-doc-*` attributes on the anchor), never keyed by
  href (glossary terms share one placeholder href; href-keying collides).
- Works in prose, headings, list items, and table cells (where the comment arrives as
  literal text, not parsed HTML).

**Agnes mapping.** Implemented as a pre-parse text normalization + post-render DOM pass in
`chat.js` (marked pipeline, not remark): before `renderMarkdownSafe`, normalize + extract
annotations positionally; after render, tag the corresponding anchors with `data-doc-*`
and strip comments. Popover = existing tooltip/popover component styled to the reference
anatomy (title, markdown body, footer link). Nested markdown in the popover body goes
through `renderMarkdownSafe`.

**Replicated caveats:** agent-supplied `summary`/`model` are trusted (verbatim-copy is a
prompt instruction only); wrong/missing model UUID falls back to the semantics page root.

---

## Wave 3 — Inline Vega-Lite charts

**Product.** A ```` ```vega-lite ```` fence renders an interactive chart inline (SVG,
tooltips). Incomplete JSON while streaming → "Building chart…" skeleton placeholder.
Parse/render failure → `<pre>` fallback with the raw spec. Charts are artifacts with
stable identity: registry keyed on top-level `spec.name`, conversation-wide, newest wins;
a superseded version collapses to a compact one-line reference — `"{title}" updated —
latest version below` (down-arrow icon). Clicking a data mark (never axis/legend/title)
opens a small anchored popover summarizing the datum with a **"Chat about this"** button
that seeds a grounded follow-up message. Identity is convention-based (prompt instructs
name reuse); no runtime-unique fallback — replicated as-is.

**Security decisions (load-bearing; port verbatim):**
- Vega expression evaluation via **AST interpreter** (`ast: true`) — no `eval`/`Function`.
- **Blocking loader**: `load`/`sanitize`/`http`/`file` all reject — a spec cannot fetch or
  exfiltrate anything; data must be inlined in `data.values`.
- `renderer: 'svg'`; embed actions: export only (no editor/source/compiled).
- Libraries vendored under `app/web/static/vendor/` (CSP: no CDN), lazy-loaded on first
  vega-lite fence exactly like `mermaid.min.js`.

**Theme:** the reference's chart config (Inter, neutral axis/grid colors, 6-color
categorical range, transparent background, title anchor start) re-expressed by resolving
`--ds-*` tokens at render time so charts follow blue/paper themes.

**Prompt directive (Wave 4 carries it):** inline `data.values` only (never `data.url`),
≤~100 rows, stable `"name"` slug, reuse the SAME name to edit / new name only for a
genuinely different chart, concise `"title"`, `"width": "container"`; prefer a chart when
the shape (trend/ranking/distribution/part-to-whole) is the point; Mermaid stays for
structural diagrams; data apps for full dashboards.

---

## Wave 4 — Analyst profile (prompt + toolset)

**Product.** The default web-chat persona becomes the analyst: proactively grounds answers
(drives Waves 1–3 without being asked), constrained to a read-only-plus-data-apps toolset,
never claims capabilities beyond it.

**Agnes mapping.** Unlike upstream (general prompt + authoritative override section — a
trade-off they explicitly regretted), Agnes composes the session `CLAUDE.md` at spawn
time (`app/chat/workdir.py` rails), so we get the **north-star variant for free**: a
purpose-built analyst rails document containing
- the complete-toolset enumeration ("never claim capabilities beyond this") adapted to
  Agnes's foundation tools (catalog/schema/describe/query + semantic/glossary tools +
  data-app tools + feedback);
- the answer-from-verified-queries rule (no figures from memory; every quantitative claim
  = a just-run, cited query);
- the validate→run→cite `query_ref` protocol (Wave 1 text, verbatim);
- the glossary `sem-ref` annotation protocol (Wave 2 grammar, verbatim, including the
  "verbatim summary / only glossary-defined terms / ASCII `-->` terminator" rules);
- the proactive charting directive (Wave 3 text, verbatim).
Tool constraint enforcement rides the existing agent-scope machinery (broker-enforced
`AgentPrincipal`) — stronger than upstream's SDK `disallowedTools`; no new mechanism.

**Replicated caveat:** grounding/glossary/charting remain prompt-driven behaviors — not
enforced. Reliability = model compliance; that is the shipped reference reality too.

---

## Wave 5 — Workspace home (new-chat landing)

**Product.** A genuinely-new chat (not merely empty while history loads) shows: personal
greeting; the composer; **domain-grouped analytical starter questions** (chips per
business theme, 1–5 natural-language questions each; clicking seeds the chat); a
**"Your data models"** panel (per-model dataset/metric counts, covered sources, uncovered
list with "Review & add"); **"Saved dashboards"** (top data apps by last change,
newest-first); **"Latest activity"**. Home→conversation is one animated grid: rows
`messages / greeting / input / home-body` transition `0fr auto auto 1fr` ↔
`1fr auto auto 0fr` (500ms ease-out) so the composer slides from centered-under-greeting
to docked-at-bottom.

**Suggestions endpoint.** `POST /api/chat/analytical-suggestions`: small/fast model,
forced tool-use (JSON schema output), 30s timeout, ≤5 categories; prefers
`metric_definitions`/`glossary_terms` language when present, falls back to exploratory
table-derived questions; the request carries a structural summary only (names +
descriptions + counts, no row data). Client appends one deterministic **"Explore
unmodeled"** category when uncovered tables exist (never dropped by the cap). Any backend
failure → HTTP 200 with empty categories; client renders none (no retry). Cache hard per
session keyed on coverage counts.

**Agnes divergences (stated):** "Latest activity" — upstream shipped a mock with a visible
"Mockup" badge; Agnes has real events (`audit_log`, sessions) — ship the real feed behind
the same UI anatomy instead of replicating the mock. "Saved dashboards" reads the
data-apps registry. Coverage reuses the `admin_semantic_layer_coverage` computation via a
non-admin endpoint (RBAC-filtered to the caller's grants).

---

## Wave 6 — Dashboards page + in-workspace data-app view

**Product.** A "Dashboards" rail destination: searchable card grid of the instance's data
apps — preview thumbnail slot (deterministic faux-SVG keyed on app id; real capture
deferred, replicating upstream), name, status dot (Running / Stopped / Unfinished),
"Edited <relative>". Ordered by last change, newest first. Clicking opens the app **inside
the workspace**: split view with live app iframe + chat alongside for follow-ups; a
conversational path ("show me the churn dashboard") opens it via a deterministic tool (no
LLM turn). Published apps expose an external-link affordance.

**Agnes mapping.** Agnes hosts apps itself (`src/data_apps/` + apps_runner), so the
upstream system-draft/dev-deploy machinery is unnecessary — the in-workspace iframe is the
app's internal URL with the existing auth. The split view is a chat-page mode (iframe pane
+ chat column) mirroring the reference's preview panel behavior: view-mode hides
publish-to-production affordances, shows external-link. The current-context prompt hint
("the user is currently viewing data app X — don't reopen it; changes follow the draft
flow") ports into the session context block.

---

## Wave 7 — Semantic Layer page (user-facing)

**Product.** A "Semantic Layer" rail destination for every user: browse models/metrics/
datasets/glossary with counts and coverage (which sources are covered/uncovered), search,
and `?search=<concept>` deep links (the Wave 2 popover's footer target). Editing stays
admin-gated (`/admin/semantic-layer` remains the CRUD surface); the user page is
read-first — Agnes's RBAC posture, diverging from upstream's shipped full-CRUD (their own
spec originally said read-only; we keep the original contract).

**Agnes mapping.** Extend `/catalog/semantics` (already the Definitions surface) to the
full reference anatomy: model list → tabbed detail (Datasets / Metrics / Glossary),
coverage flags, search param. Deep-linkable (URL-backed, like the reference's SL
destination — unlike its ephemeral chat/dashboards local state, which we also replicate:
Dashboards is not deep-linkable).

---

## Delivery

- One wave = one PR = one plan file; `verify-agnes-change` loop + `/agnes-review` per PR;
  CHANGELOG bullet per PR; Wave 0 carries the BREAKING entry.
- Order: 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 (4 activates 1–3 end-to-end; 5–7 consume them).
- Each wave's plan is written only after the previous wave merges (file/line references
  stay true).
- Blast radius watch: every wave touches `chat.js` — after Wave 0 there is exactly one
  chat surface, and its regression suite (`tests/test_design_system_contract.py`, chat E2E)
  gates each PR.
