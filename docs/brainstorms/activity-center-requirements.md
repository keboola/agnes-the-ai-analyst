# Activity Center — Requirements

**Date:** 2026-05-05
**Status:** Draft, brainstorm output (pre-plan)
**Owner:** vrysanek
**Phase:** Pre-`/ce:plan`. Implementation decisions are deliberately deferred.

## Problem

Agnes already collects rich behavioral data (Claude Code session JSONLs uploaded via `agnes push`, admin actions in `audit_log`, BigQuery scans via `BqAccess`), but none of it is exposed in a form leadership can interrogate. The CEO has asked for **comprehensive per-user activity visibility** — what each analyst prompts, which tools and plugins they invoke, which queries succeed or fail, and **how good each session is** (scored). Aggregates alone don't satisfy the ask; CEO wants drill-down per user with quantitative scoring of sessions and plugins, and a way to export the full picture.

Today this requires reading raw JSONLs by hand. There is no privacy-mode toggle for analysts who want to keep a session off the wire, no per-department aggregation, no export, no scoring, and no view that distinguishes "user opened a plugin" from "user actually relied on it".

## Goals

**Ship one Activity Center that gives leadership a comprehensive per-user view, scored.** Not three phases of partial visibility — the CEO ask is unambiguous. Phasing here means iteration tracks (parallel work streams), not deferred features.

The single coherent product:

- **Per-user dashboards with chronological session replay.** Drill from "all users" → individual user → session timeline → individual session detail → individual prompt/tool call. The session detail is a step-by-step replay surface: events ordered by `event_ts`, prompt → tool calls → tool results → next prompt, with inputs/outputs visible at each step. Filter by `outcome='error'` or specific `error_class` to jump to the failure points. Cross-session chronology view shows the user's full event timeline across sessions so an admin can trace how a problem evolved (e.g. "user hit `remote_scan_too_large` three times across two sessions before figuring out the workaround"). Goal: the admin can almost replay the conditions a user faced when debugging or coaching, without having to read raw JSONL by hand.
- **Scored sessions.** Every session gets a composite productivity score (0–100) plus sub-scores (success-rate, error-density, tool-efficiency, output-density, prompt-iteration). Sub-scores are deterministic. A 5% LLM-as-judge sample adds qualitative flavor (top accomplishments / worst stuck-points of the week).
- **Scored plugins.** Composite adoption score (0–100) + sub-scores (breadth, intensity, success-rate, retention). CEO can rank plugins by actual leverage delivered, not just "how many people clicked install".
- **Scored users.** Per-user composite (0–100) + sub-scores (productivity-trend, plugin-mastery, query-cost-efficiency). Trend is more important than absolute number — leadership sees who's improving, who's stuck.
- **Failure intelligence.** Wrong-tool detection, query errors, plugin churn surfaced as a separate tile fed by the same store.
- **Privacy mode.** Per-session opt-out. Already designed below.
- **Hardening config.** Per-user prompt-level read is RBAC-gated + rate-limited + dual-approver-configurable from day one, not as a separate phase. Operator can enable instance-wide on day one or hold it back; the gating is config, not a release milestone.

**Sequencing — be honest about what ships when.** Pass 2 review pushed back on the "single coherent release" framing as rhetorical: in practice, Track A ships first (initial release); Track B and C ship as first-iteration follow-ups within weeks, not quarters. Naming this honestly avoids over-committing to a release date that the underlying plan can't honor.

- **Track A — Data + dashboards (initial release, critical path).** Extractor, schema, scoring engine, per-user / per-plugin / per-user dashboards, exports, privacy mode, disclosure flow, baseline RBAC gates. Ships first as the product face leadership opens.
- **Track B — Failure intelligence (iteration 1).** `wrong_tool` heuristic, plugin-churn dashboard, failure-rate tile. Lands when the heuristic is validated against ≥10 sample sessions; not a release blocker for Track A.
- **Track C — Hardening hardening (iteration 1, parallel with B).** Dual-approver flow, mutual-visibility digest, bulk-query rejection, compliance setup wizard. Track A ships with baseline RBAC + rate-limit + audit-log on day one; the more advanced abuse controls land alongside or shortly after.

This is the brief the CEO actually asked for. Earlier drafts split the work into three sequential phases with per-user content gated behind works-council sign-off; that framing was rejected in adversarial review as bureaucratic deferral. The defaults the OSS ships are themselves product opinions, not neutral knobs — see *Defaults are product opinions* under Privacy & legal posture for the explicit posture.

## Non-goals

- **Real-time monitoring or alerting** for the dashboard. Daily refresh is sufficient for the leadership-overview use case; expect a worst-case t+24h tail (event happens → SessionEnd → `agnes push` → next nightly extractor → next admin `agnes pull`). BigQuery cost events are the one exception worth calling out: the existing 5 GiB scan cap (`/admin/server-config`) is the cost-prevention layer, not Activity Center. Activity Center does retrospective cost attribution; it does not stop a runaway query mid-flight. If sub-day cost detection is needed later, the synchronous path is `BqAccess` writing directly to `activity_events` at scan time — out of scope for Phase 1.
- **Forking the JSONL walker.** Two existing pipelines already walk `/data/user_sessions/*.jsonl` and dedup via `session_extraction_state`: `services/corporate_memory` (knowledge extraction → `knowledge_items`) and `services/verification_detector` (verification-evidence extraction). Note: `services/session_collector` is a one-shot file *copier* (`shutil.copy2` from `/home/<user>/user/sessions/` to `/data/user_sessions/`); it does not parse JSONL contents. Activity Center becomes a third *content* consumer of the corpus. Plan-phase decision: either (a) introduce a shared walker abstraction that all three content extractors plug into (corporate_memory + verification_detector + activity_center), or (b) Activity Center runs an independent third scan with its own `activity_extraction_state` table. Option (a) is the better long-term shape but is a larger refactor than Track A alone justifies; Option (b) is the cheaper Track A landing with a refactor task in iteration backlog. Decide in plan-phase, not by accident in implementation.
- **Tracking activity outside Agnes-managed sessions.** If a user runs Claude Code without `agnes init` hooks, nothing is captured. Acceptable, with the chilling-effect caveat under *Alternatives considered* below.
- **Surveillance of private sessions.** Privacy mode (below) is a per-session opt-out the user can invoke. It is not a complete right-to-disconnect — admins still see metadata (existence, counts, durations); the session content is the only thing redacted. Documented honestly, not framed as something stronger than it is.
- **Cross-tenant analytics.** Each instance is self-contained.

## Users & primary outcomes

| User | What they need |
|------|----------------|
| **CEO / leadership** | **Comprehensive per-user activity view, scored.** Drill from all-users → individual → session → prompt. Session, plugin, and user scores with trends. Export per user / per department / all. NL prompting for ad-hoc questions, dashboards as the primary surface. |
| **Admin** | Per-user investigation any time. Plugin lifecycle audit (admin grants from `audit_log` joined to actual usage). Export per scope. RBAC gates + rate-limit + optional dual-approver — configured per instance, not as a release gate. |
| **Platform team** | Failure patterns (wrong tool used, BQ scan rejected, snapshot too large). Drives roadmap. Same data, different lens. |
| **Analyst (user)** | Visible privacy toggle they can invoke per session. Self-view of their own activity and scores — same view leadership sees of them, no asymmetry. Disclosure of what's collected, when, how long, who can see it. |

## Decisions reached

### 1. Single-release delivery (not 3 phases)
- The product ships as one coherent Activity Center. Tracks A/B/C run in parallel; Track A (data + dashboard + scoring) is the critical path, B (failure classification) joins if its heuristic is solid, C (hardening config) is always-on from day one.
- Per-user prompt-level read, dual-approver, rate-limits — all configurable on day one. Operators decide what to enable per instance; no feature is "deferred to a later phase" as a way of dodging a hard product decision now.
- Implementation will still ship in iterations (Track A first, then B/C polish) but the user-facing brief and acceptance bar describe the whole product, not a partial first cut.

### 2. Privacy mode: per-session opt-out, metadata-only
- **Default:** full session activity is extracted (prompts, tool params, results) into the activity store. Subject to retention.
- **When private:** the session's `agnes push` ships a **metadata manifest** — start/end timestamps, tool names, query target tables, durations, exit status — and the JSONL itself is *not* uploaded. The session's existence and counts are visible to admins ("user X had a 47-minute private session, 23 tool calls, 4 BQ queries"). This is the deliberate balance between trust and accountability — admins can spot anomalies without seeing content.
- **Trust model — what server enforcement actually means.** The threat model is accidental disclosure (a careless toggle, a sync race, a forgotten setting), not hostile clients. A user determined to hide content can already run Claude Code without `agnes init` hooks (acknowledged in non-goals). The server enforces the model by structurally separating the two upload paths:
  - **`POST /api/upload/sessions`** (existing) — full-JSONL upload. PAT-authed, 50 MB cap, streams to `${DATA_DIR}/user_sessions/<user_id>/`.
  - **`POST /api/upload/sessions/manifest`** (new in Phase 1) — structured JSON body validated against a pydantic schema (`session_id`, `started_at`, `ended_at`, `tool_calls: [{name, target, duration_ms, outcome}]`, `query_count`, `bq_bytes_scanned`, `exit_status`). PAT-authed. Stored as JSON sidecar at `${DATA_DIR}/user_sessions/<user_id>/<session_id>.manifest.json`.

  The client picks one endpoint per session based on the privacy toggle. The server cannot receive a "mixed" payload because the routes accept different content types and schemas — there is no third combined endpoint. The activity-center extractor reads `*.jsonl` and `*.manifest.json` from the same directory; a `*.manifest.json` shortcut-circuits any extraction attempts on a same-session JSONL (defense against a client that manages to call both endpoints — it's the manifest that wins).
- **Toggle mechanism.** `agnes private on|off|status` writes per-session state keyed by Claude Code's `$CLAUDE_SESSION_ID` if the SessionStart hook can read it, otherwise falls back to a workspace-level flag with explicit "stays on until you turn it off" semantics — documented as the actual behavior, not approximated as session-scoped. Plan-phase verification task: confirm whether `$CLAUDE_SESSION_ID` (or equivalent) is exposed to hooks; if not, the docs say "workspace-wide private until `agnes private off`" plainly.
- **No per-user persistent default in Phase 1.** Decision recorded explicitly: the toggle is per-session, defaults open, and the user re-asserts it each session. The reviewer-flagged dark-pattern risk is acknowledged and addressed by *not* claiming this is more than it is — see the disclosure language below, which describes the actual semantics rather than calling it a "first-class right". A per-user always-private option remains a Phase 3 candidate if works-council consultation or operator feedback shows the per-session friction is hurting trust.

### 3. Plugin lifecycle = grants + adoption (both)
- **Lifecycle (intent):** Admin grant/revoke events for a plugin (against a `user_group`) are already captured in `audit_log` via `app/api/access.py`. Surface those as the canonical "plugin enabled for this group" timeline. No new instrumentation needed.
- **Adoption (reality):** Tool calls in session JSONLs carry plugin identity *only when the tool name is plugin-prefixed* (MCP tools like `mcp__plugin_<server>__<tool>`, plugin-defined slash commands resolvable via the cached `marketplace_plugins` table). Built-in Claude Code tools (Read/Edit/Bash/Grep/Glob/Edit/Task), Skill invocations without a plugin manifest, and ambiguous same-named tools across marketplaces produce `plugin_name = NULL`. The "first appearance = adopted" heuristic only applies to plugin-attributable tool calls; below a coverage threshold (target: ≥70% of a plugin's expected tool surface attributable from JSONL), the adoption signal is reported as **unobservable** rather than zero. A Phase 1 implementation task validates the derivation rule against a sample of real JSONLs (≥3 known plugins: one heavily used, one zombie, one ambiguous) before any roadmap decision uses the metric.
- **Adoption persistence vs retention.** Raw events purge at 90 days, but a plugin granted >90 days ago and used at least once before the window opens would lose its first-adoption marker. To keep the headline "granted vs adopted" ratio meaningful past the retention window, the daily extractor upserts a denormalized aggregate `plugin_adoption(user_id, plugin_name, first_seen_at, last_seen_at)` that is kept past the rolling delete (aggregate-only, no payload).
- **Abandonment threshold.** "Last appearance + N days of silence = abandoned" — initial value `N = 30 days`, configurable via `instance.yaml`. Validated against ≥3 plugins in plan-phase before publishing the metric on the dashboard (one heavily used, one zombie, one ambiguous).
- Combined view answers the high-leverage question: "we granted access to plugin X to 50 users; how many actually use it monthly?" Both data sources already exist on disk; the activity-center extractor produces the join-friendly shape.

### 4. Admin UX — surfaces by persona, defaults steered toward synthesis

Five named surfaces, all on the existing `/activity-center` route. Defaults are steered toward synthesis (paragraph narrative, sub-score trends) over composite ranking, because composite-driven workforce decisions are the failure mode the OSS deliberately does not make easy.

- **`/activity-center` (overview, default landing).** Tiles: active users 7d/30d (clickable to user-list), top plugins by composite score (clickable to plugin detail), BQ bytes by department, failure-rate trend, redaction-counter anomaly tile (operator hygiene), and **the weekly leadership digest tile** (see below). The "top users by composite score" tile is *off by default*; operators flip `scoring.show_user_composite_to_admin: true` in the setup wizard to surface it. Each tile has CSV/Parquet export.
- **`/activity-center/weekly` (leadership digest, P2 review finding addressed).** A paragraph-shaped narrative synthesizing the prior week: top accomplishments by department, top friction points (failure-class trends), plugin highlights (zombies, breakouts), 1–3 recommended actions tied to the named decisions in §7. Generated nightly on Sunday from the deterministic data — *not* via LLM-as-judge (deferred to backlog) but via templated rendering of the rollups (e.g. "department X ran N queries this week, Y% success rate, top friction was `remote_scan_too_large` (Z times)"). When LLM-judge ships, this surface gets richer; until then, templated narrative is materially closer to what leadership actually opens than tile drill-down.
- **`/activity-center/users` and `/activity-center/users/<user_id>` (per-user dashboard, gated).** Available only when `per_user_audit_enabled: true` (post-wizard). All-users table with sub-scores; click → individual dashboard: session timeline (chronological, scored), plugin usage histogram, BQ cost trend, error-class breakdown, sub-score trend lines, **the disputed-count flag tile** when the analyst has flagged any score, and **a cross-session chronology view** showing the user's full event timeline across sessions (filterable by date range, error class, or plugin) so an admin can trace how a problem evolved across multiple sessions. Click a session → session detail (next bullet). Reads of `payload` trigger audit-log writes + entries in the analyst's "who looked at me" view.
- **`/activity-center/users/<user_id>/sessions/<session_id>` (session-replay surface, gated).** Step-by-step chronological reconstruction of a single session: events ordered by `event_ts`, prompt → tool calls (with params) → tool results (with outcome/error_class) → next prompt. Each step has a deep-link anchor (`#event-<event_id>`) so an admin can share a specific failure point. Filter affordances: jump-to-next-failure, jump-to-prompt-N, collapse-successful-tool-calls. For private sessions (`privacy_mode=TRUE`), the surface shows the manifest only — tool names, durations, outcomes — with content fields rendered as `<private session — content not collected>` placeholders. Same RBAC stack as per-user-detail (admin + `resource_grants(per_user_audit)` + rate-limit + dual-approver) — every render of a non-private session writes one `audit_log` row.
- **`/activity-center/plugins` and `/activity-center/plugins/<plugin>` (per-plugin dashboard).** All plugins ranked by composite score. Click → plugin detail: distinct active users trend, granted-vs-adopted ratio, success-rate trend, top users by intensity (only when `show_user_composite_to_admin: true`), abandonment list. Plugin-level data is much lower privacy weight than per-user data and is enabled by default.
- **`/me/activity` (analyst self-view).** The analyst sees: their own composite + sub-scores, session timeline, plugin usage, BQ cost, **the active composite weights and weight-version**, **a "who looked at me" list (last 30 days)**, **a "dispute this score" action per session**, **the disclosure history (versions accepted, when)**, **and the same session-replay surface for their own sessions** so they can review what they did and use it to debug their own workflow without admin involvement. This is the recourse mechanism that makes scoring legitimate to the people being scored — without it, self-view is a one-way mirror.
- **NL prompting for ad-hoc.** Activity rollup views exposed in `analytics.duckdb` (`query_mode='local'` parquet, distributed via `agnes pull` per RBAC). Per-user `activity_events.payload` is `query_mode='remote'` (server-only) so admin queries against per-user content land server-side and trigger audit + rate-limit + bulk-query rejection at the API boundary. Reviewer-flagged constraint: rollup tables ship to admin laptops via `agnes pull`, so admin queries against rollups don't generate audit-log rows — that's by design (rollups are the cross-user-aggregate surface, no per-user content involved). Audit-log discipline applies to `payload`-touching queries, not to aggregate analysis.
- Every admin-side query against per-user detail writes its own row to `audit_log`. The CEO is not exempt.

### 5. Retention: 90 days rolling, configurable
- **Basis for 90 days.** Product-team default chosen to balance recent-investigation utility with EU/CZ works-council expectations for behavioral telemetry. Not a hard requirement; operators tune to local legal context. Naming the basis here so future maintainers don't treat it as load-bearing when it isn't.
- **Raw activity events:** 90-day rolling window, then hard-deleted by a daily scheduler job. The job is idempotent and emits a row to `audit_log` on each run (`action='activity_purge'`, `result='ok'|'failed'`, `params={"deleted_rows": N}`); a missed run for >48h triggers the platform health-check warning surface used by `app/api/health.py`.
- **Daily aggregates** (per-user, per-plugin, per-department, score tables): same 90 days at initial release. Tiered retention (longer for aggregates) is in the iteration backlog — single config-key change, no schema work — `activity_center.aggregate_retention_days` defaulting to `retention_days`.
- **`plugin_adoption` aggregate** (first/last-seen markers): kept past the rolling delete since it is aggregate-only with no payload. Operators can purge a single user's row via the right-to-forget tool below.
- **Configuration:** `activity_center.retention_days: 90` in `instance.yaml`. Operators can shorten silently; extending requires re-publishing the disclosure to users (the disclosure text references the configured value at the time of acceptance).
- **Right-to-forget:** initial release ships `agnes admin activity forget --user <id>` — deletes the user's rows from `activity_events`, all daily rollups, `plugin_adoption`, and the score tables (`session_scores`, `user_scores` rows for that user; `plugin_scores` is plugin-keyed and unaffected). Single transaction. Bounded worst-case (90d) means this is rarely needed, but the tooling exists day one.

### 6. Export
- CSV and Parquet, both. CSV for spreadsheet hand-off, Parquet for further analysis pipelines.
- **`activity_export` ResourceType is not a new abstraction.** Adding it follows the established pattern in `app/resource_types.py`: one `ResourceType` enum member, one `ResourceTypeSpec` registration with a `list_blocks` delegate. No DB migration. Roughly 4–10 lines of code.
- **Export scopes** gated by `resource_grants`:
  - "all instance" export → admin group only.
  - "per department" export → group leads, via `resource_grants(resource_type='activity_export', resource_id='<group_id>')`.
  - "per user — myself" → every user, against their own user_id (no grant needed; checked at the API boundary).
  - "per user — other" → admin only, audit-logged.
- **Column inclusion in exports** is scope-dependent: `payload` is included only for "all instance" admin export and "per user — myself"; "per department" exports for group leads strip `payload` (counts, metadata, and scores only). This keeps the per-department surface aggregate-shaped even when a group lead exports raw rows.
- **Private-session rows** in exports: `privacy_mode=TRUE` rows never include `payload` regardless of scope (the column is NULL by extraction-time contract); admins exporting "all instance" still see the rows' existence and counts, matching the in-app surface.
- **Grant lifecycle.** Today, `resource_grants` has no expiry; for `activity_export` grants this is acceptable at initial release because group membership churn already revokes via Google Workspace sync. Per-grant expiry (`expires_at`) is in the iteration backlog if we see standing-grant abuse.
- **Exported file lifetime.** Once a CSV/Parquet file leaves the server, it is outside the retention contract. The export endpoint logs the export with row count + scope; the operator runbook documents that exported files inherit the operator's retention obligations.
- Every export action logs to `audit_log` with scope, row count, and target user/group (for non-aggregate scopes).

### 7. Scoring (sessions, plugins, users)

**Default visibility posture (P0 review finding addressed).** Composite per-user scores ranked across analysts on a leadership tile create a de facto performance metric — exactly the artifact Czech Labour Code §316 + GDPR Art. 88 treat as systematic monitoring of work performance, regardless of how the OSS frames it. To avoid the OSS shipping a one-click surveillance lever:

- **Per-user composite is hidden from the leadership view by default.** `activity_center.scoring.show_user_composite_to_admin: false` in `instance.yaml`. Leadership sees per-plugin scores, aggregate trends, and the analyst's *own* sub-scores when drilling down — not a ranked all-users-by-composite tile out of the box. Operators who want the ranking flip the flag after explicit acknowledgement in the compliance setup wizard.
- **Per-user composite is visible on the analyst's own self-view always.** The analyst sees their score; leadership does not by default. This breaks the symmetry-of-data claim earlier drafts made — and that's correct. Symmetry of data without symmetry of consequence is theatre. The new contract: the analyst sees what the system says about them; leadership sees aggregates and per-plugin views, with per-user content gated behind explicit operator opt-in plus per-user RBAC grants.
- **Sub-scores are the dashboard's lead, not the composite number.** The 0–100 composite is computed and stored; the dashboard tiles surface trends, qualitative narrative, and sub-score breakdowns. This is steered design — operators can change the default tile set, but the OSS doesn't make composite-driven workforce decisions the easy default.

**LLM-as-judge: deferred to iteration backlog (P0 review finding addressed).** Pass 2 surfaced three load-bearing problems: (a) judgeability — Claude scoring its own work biases toward verbose/structured output, not actually-useful work; (b) Art. 28 surface — sending session content to Anthropic creates an undisclosed third-party processor relationship requiring DPA + recipient disclosure under Art. 13/14; (c) cost path — the existing `connectors/llm/anthropic_provider.py:extract_json` does not surface usage metadata, so per-instance budget caps require an extractor-protocol extension. Together this exceeds the value of a 1-line model-written summary on a 5% sample. LLM-as-judge moves to iteration backlog: revisit when there's operator-side evidence the qualitative gap matters, the cost-metering path is built, and the disclosure language is updated. Track A scoring is deterministic-only.

**Score table retention bound (P1 review finding addressed).** Score tables are user_id-keyed behavioral data — personal data under GDPR Art. 4 even without prompt content. Initial release: `activity_center.aggregate_retention_days: 365` (default). After 365 days, score rows older than the window are hard-deleted by the same daily purge job that handles raw events. Aggregate-only framing does not override Art. 5(1)(e) storage limitation when the data is directly user-identified. `plugin_scores` is plugin-keyed and not user-identified — retention applies but the privacy weight is much lower.

**Sub-score gaming defenses (P1/P2 review finding addressed).** Each sub-score has a known game vector; the sub-score definition either includes a cross-validation that defeats the obvious game or the score doesn't ship.

Every score is a composite (0–100) plus its sub-components, both stored. Composite weights live in `instance.yaml` so operators can tune what "good" means without a code release. All deterministic scores are recomputed nightly by the extractor.

**Session score (per session)**
- *Productivity sub-score* — tool-call density (calls / minute) × success-rate, **capped at the 95th percentile of the operator's historical tool-call rate** so shell-loop gaming (`for i in {1..50}; do ...`) doesn't dominate. The cap is recomputed weekly from the previous 30 days of data.
- *Success-rate sub-score* — `ok` outcomes / total tool calls. Penalizes thrash.
- *Error-density sub-score* — inverse of error events / total events. High when sessions don't trip over `BqAccessError`s, snapshot rejections, or tool timeouts. **Errors are tool calls with `outcome='error'`; failures are errors with a populated `error_class` from the taxonomy.** Both terms now have specific meanings; do not interchange.
- *Output-density sub-score* — artifacts uploaded (`/api/upload/artifacts`) + queries whose returned rows are referenced in a subsequent prompt (cross-validation against the JSONL — uploading a dummy file or running `SELECT 1` doesn't count because no subsequent prompt references the result). Captures "stuff actually got used".
- *Prompt-iteration sub-score* — **bell-curve weighted, not inverse-linear**. Very low iteration (one-shot vibes-prompt) and very high iteration (50 retries) both score worse than 2–5 thoughtful iterations. Reviewer flag: penalizing all iteration encodes the wrong incentive — careful analysts iterate.
- *Composite* — weighted geometric mean of sub-scores, normalized 0–100. Default weights ship with the runbook. **Tool-efficiency sub-score (distinct tools / total tool calls) is dropped** — repetitive single-tool query workflows are the dominant correct pattern for an analytics platform; the original definition penalized correct behavior. Reviewer-flagged calibration miss; cut.

**Plugin score (per plugin, refreshed daily)**
- *Adoption-breadth sub-score* — distinct users in the granted population who used the plugin in the last 30 days.
- *Adoption-intensity sub-score* — invocations per active user (90th percentile capped to avoid outliers dominating).
- *Success-rate sub-score* — `ok` outcomes / total invocations of plugin tools.
- *Retention sub-score* — fraction of users who used the plugin in two consecutive 14-day windows.
- *Composite* — weighted geometric mean. Headline number on the plugin tile.

**User score (per user, refreshed daily)**
- *Productivity-trend sub-score* — slope of the user's session-score median over the last 30 days (positive = improving, negative = stuck). **Visible to the analyst on `/me/activity`; visible to leadership only when `show_user_composite_to_admin: true`.**
- *Plugin-mastery sub-score* — distinct plugins used with success-rate > **`activity_center.scoring.user.plugin_mastery_success_threshold` (default 0.75)** over 30 days. Threshold named explicitly; not left undefined at commit time.
- *Query-cost-efficiency sub-score* — useful query bytes (queries whose returned rows are referenced in a subsequent prompt within the same session — heuristic computable in deterministic SQL: prompt event within 5 minutes after a successful query event in the same session) / total BQ bytes scanned. Reviewer flagged this as borderline-LLM; the deterministic time-window heuristic is cheaper and explainable.
- *Composite* — weighted geometric mean.

**Decisions the score drives** (P1 review finding addressed). Without a named decision, scores are decoration consuming political capital. Concrete leadership actions the OSS treats as legitimate uses for the scoring data:
- Reallocate plugin grants when `plugin_scores.adoption_breadth < threshold` for 30 days (zombie plugin sunset).
- Flag `/activity-center/plugins/<plugin>` for review when granted-vs-adopted ratio < 20%.
- Surface aggregate failure-class trends to the platform team for roadmap input.
- Surface cost-per-active-analyst trend per department to the CEO for quarterly reviews.

The OSS *does not* ship a "rank analysts by composite for performance review" surface. That would be a per-deployment HR-policy decision the operator has to enable explicitly via the compliance setup wizard. Leadership uses of per-user scoring beyond the named decisions are operator policy, not OSS feature.

**Storage shape** (additions to the schema sketch below):
- `session_scores(session_id, user_id, day, composite, productivity, success_rate, error_density, output_density, prompt_iteration)` — daily rollup, kept up to `aggregate_retention_days` (default 365). LLM-judge columns deferred to iteration backlog.
- `plugin_scores(plugin_name, day, composite, adoption_breadth, adoption_intensity, success_rate, retention)` — daily.
- `user_scores(user_id, day, composite, productivity_trend, plugin_mastery, query_cost_efficiency, disputed_session_count, disputed_summary_count)` — daily. The two `disputed_*` columns surface analyst-flagged issues into the leadership view (reviewer-flagged: visibility without recourse is a dark pattern).

**Recourse and dispute (P1/P2 review finding addressed).** The analyst's `/me/activity` view is not read-only; without recourse, "symmetry" is rhetoric.
- *Weight transparency.* The analyst sees the active composite weights for their score and the weight-version timestamp.
- *Score dispute.* The analyst can flag a specific session's score as disputed via `/me/activity`. The flag writes to `audit_log` (`action='score_disputed', user_id=<self>, params={session_id, sub_score_disputed, reason}`) and increments `user_scores.disputed_session_count` for that user. Leadership view of the user shows the disputed-count as a flag tile next to the composite.
- *Symmetric mutual-visibility.* The analyst sees who has queried their `/activity-center/users/<self>` detail page in the prior 30 days (same shape as the admin mutual-visibility digest). If admins can see "who looked at whom" weekly, analysts can see "who looked at me" on their own page. Without this, the digest is a one-way mirror.
- *Right-to-recompute.* The analyst can request that an LLM-judge summary (when LLM-judge ships in iteration backlog) be regenerated or deleted from their record. Stored as a `audit_log` event; processed in the next nightly run.

These aren't polish — they're the mechanism that makes the scoring legitimate to the people being scored. Without them, the OSS ships a one-way performance-monitoring tool with self-view as window-dressing.

**Tuning vs gaming.** Visibility plus capped sub-scores plus output cross-validation reduces the obvious game vectors. It does not eliminate Goodhart's Law — once a metric is used for decisions, it gets gamed. The mitigation is structural: drop tile-level "rank by composite" surfaces by default, lead with sub-score trends and qualitative narrative when the LLM-judge eventually ships, and treat the composite as one signal among many — not the management lens. Operators who flip the composite-visible flag are explicitly opting into Goodhart territory.

## Adversarial cuts (what was deleted, why)

Adversarial review (Musk-style first-principles + "the best part is no part") removed several scope items that earlier drafts treated as load-bearing. Naming them here so future maintainers don't reintroduce them by accident.

**Cut: 3-phase delivery split.** Earlier drafts staged the work as Phase 1 (cost dashboard) → Phase 2 (failure patterns) → Phase 3 (per-user audit gated by works-council sign-off). The cut: phasing was bureaucracy, not product. CEO asked for the comprehensive view; staging it as a sequence with deferred per-user content was a way of saying "we want to build less than was asked." Replaced with iteration tracks (A: data + dashboard, B: failure classification, C: hardening config) running in parallel. Per-user content is in the initial release with RBAC + rate-limit + dual-approver-configurable from day one. Operators decide what to enable; the OSS doesn't decide for them.

**Cut: "Sharpest wedge" 0.5-phase ROI spike.** Earlier draft proposed answering the CEO's "is this paying off" question with a one-page rollup over existing data before committing to the full pipeline. The cut: CEO ask wasn't "is this paying off" in the narrow ROI sense — it was comprehensive per-user activity with scoring. A spike that answers a smaller question doesn't de-risk the actual ask; it delays it. Plan-phase will still validate corpus-size assumptions before committing to specific extractor topology, but as a 1-day spike inside the planning task, not a separate phase.

**Cut: "ROI tile" framing.** Earlier draft labeled the dashboard tile set as "answering 'is this paying off' not just 'who used what'." Misread of the brief. CEO wants who used what, scored, with drill-down. Reframed to per-user / per-plugin / per-user dashboards with composite scoring tiles.

**Cut: "DEMO" labeling on the admin page.** Pre-emptive lowering of the bar. The dashboard is the product face — ship it as the product face, not as a "we'll iterate on this later" placeholder.

**Kept: privacy mode (per-session opt-out).** Originally requested by the user, retained. Adversarial pressure-tested it against "delete it entirely — if leadership wants comprehensive visibility, why offer an off-switch?" Answer: privacy mode is the trust contract that lets leadership keep visibility everywhere except where the user explicitly objects, in exchange for not driving analysts to bypass `agnes init` entirely. Deleting the off-switch trades away more than it gains.

**Kept: works-council / DPIA / disclosure.** Adversarial cut considered: "operator's problem entirely, no documentation surface in the OSS." Rejected — the operator runbook is part of shipping the OSS responsibly; not documenting the legal surface lets a careless operator believe there isn't one. The runbook is one file; not a phase.

**Kept: schema reservation for future column-level encryption.** Adversarial cut: "premature, just don't ship the column slot." Rejected because it costs zero (one nullable column) and saves a future migration that touches a 90d-retention store. Forward-compat is cheap when it's three lines of DDL.

**Considered, deferred (not cut, just not initial-release):**

- **LLM-as-judge on every session.** Adversarial: "5% sampling is enough; full-corpus LLM scoring is expensive and judgeability concerns are real (Claude scoring its own work)." Sampling is the cap.
- **Survey-based ROI complement.** Useful, orthogonal, lower priority. Iteration backlog.
- **Cross-tenant analytics.** Out of scope by architectural design (each instance is self-contained).

### Chilling-effect risk (named, mitigated, not eliminated)

Analysts who know prompts are stored 90 days and leadership-queryable with scoring will self-censor or route around the tool. Mitigations baked into the design: per-session privacy mode is a one-command opt-out; the disclosure is explicit and acceptance-blocking; the self-view tooling is symmetric (analyst sees the same shape leadership sees); scoring is visible to the user being scored, not opaque; per-user content access is RBAC + rate-limited + dual-approver-configurable. Residual risk acknowledged: some analysts will run Claude Code without `agnes init` hooks. The OSS does not solve for that; it makes the in-scope path trustworthy.

## Privacy & legal posture

This is employee-facing telemetry in an EU/CZ context. The brainstorm output makes the following defensible by default; implementation must keep them defensible.

- **Disclosure.** First-run notice in `agnes init` describes collection, retention, scoring, how privacy mode works, and that the operator may have separately enabled per-user prompt-level inspection. Acceptance recorded server-side per user (`users.activity_disclosure_accepted_at` + `users.activity_disclosure_version`); uploads from unaccepted or stale-version users are rejected with `403 disclosure_required`. When the operator updates the disclosure (e.g. enables a new feature), the `activity_disclosure_version` bumps and users re-accept on next session start. The state machine is: `null` → first acceptance → potentially-stale → re-acceptance, with each transition logged.
- **User access to own data.** `/me/activity` self-view is symmetric in *what data exists about the user* (sessions, scores, plugin usage, BQ cost) plus the recourse mechanisms in §7 (weight transparency, score dispute, mutual-visibility on who looked at me, right-to-recompute). Symmetry of data without symmetry of agency is theatre; the new contract is symmetric data + asymmetric stakes (acknowledged) + symmetric recourse mechanisms.
- **Privacy Mode framing.** Per-session opt-out, content-only redaction, metadata still visible. Disclosure language: "When you turn private mode on for a session, the prompts and tool parameters from that session are not sent to the server. The fact that you had a session, how long it lasted, which tools you used, and which tables you queried are still recorded and counted toward your scores. The setting resets when the session ends — you turn it on each time."
- **Admin queries on per-user detail are logged.** Every read of `activity_events.payload` writes an `audit_log` row; the weekly mutual-visibility digest surfaces who looked at whom (symmetric: the analyst is also notified); rate-limit + dual-approver (default ON for ≥2-admin instances) backstop the audit trail.
- **Retention is bounded.** Raw events: 90 days, hard delete, configurable down. Score tables and `plugin_adoption`: 365 days default, hard delete, configurable down (reviewer flagged that user-keyed score data is personal data even without payload — Art. 5(1)(e) applies).
- **Operator-side compliance gating, not just documentation.** First-boot operator setup wizard at `/admin/activity-center/setup` forces the operator to acknowledge §316 / Art. 88 / Art. 35 obligations before the per-user-audit features unlock. Without the acknowledgement, the system runs in degraded mode (overview + aggregates only). The wizard records acknowledgement in `audit_log`; refusing or bypassing the wizard does not silently turn on surveillance.

### Defaults are product opinions

The defaults the OSS ships are deliberate bets, not neutral knobs. Stating them explicitly so future maintainers don't change them by accident:

| Knob | Default | Why |
|---|---|---|
| `per_user_audit_enabled` | **false** | Must be explicitly enabled via setup wizard; no silent surveillance |
| `per_user_audit_dual_approver` | **true** (when ≥2 admins) | Strongest abuse control should be opt-out, not opt-in |
| `per_user_audit.rate_limit_per_hour` | **5** | Caps unilateral corpus traversal at workday-scale, not minute-scale |
| `per_user_audit.bulk_query_max_distinct_users` | **1** | Single-user reads only on payload; cross-user goes through rollups |
| `scoring.show_user_composite_to_admin` | **false** | Composite is on the analyst's self-view; leadership sees aggregates by default |
| `scoring.llm_judge_enabled` | (n/a, deferred to backlog) | Off entirely until Art. 28 + cost-metering + judgeability concerns are addressed |
| `retention_days` (raw events) | 90 | Behavioral telemetry default, configurable down |
| `aggregate_retention_days` (score tables) | 365 | Personal data under Art. 4; bounded retention applies |
| `redaction.high_entropy_pattern_enabled` | **true with UUID exclusion** | UUIDs (8-4-4-4-12 hex) excluded from the high-entropy regex to avoid false-positive payload destruction |

Operators in less-restrictive jurisdictions can flip individual knobs; operators in stricter jurisdictions adopt the `minimum_collection: true` profile (one-line override). The OSS defaults are tuned for EU/CZ context — the most restrictive context the project knows about — not a global average.

These postures are decisions, not implementation. Plan-phase work includes a checklist verifying each is wired up before the release ships.

## Activity event shape (provisional)

Sketched here for shared vocabulary; final schema is a planning concern.

**Storage topology.** Activity Center produces a new extract under `/data/extracts/activity_center/extract.duckdb` matching the existing connector contract (`_meta` table + views, optional `data/` for parquet rollups). The `SyncOrchestrator` picks it up on its next `rebuild()` pass and surfaces the views through `analytics.duckdb` master views — same path as Keboola/BigQuery extracts. This avoids inventing a cross-DB ATTACH between `system.duckdb` and `analytics.duckdb`.

```
activity_events (90d hot, /data/extracts/activity_center/extract.duckdb)
  event_id          UUID
  user_id           VARCHAR
  session_id        VARCHAR             -- Claude Code session id when exposed to hooks; falls back to JSONL filename hash
  event_ts          TIMESTAMP
  event_type        ENUM('prompt', 'tool_call', 'tool_result', 'query', 'plugin_invoke', 'session_start', 'session_end')
  tool_name         VARCHAR             -- nullable
  plugin_name       VARCHAR             -- nullable; derived from MCP tool-name prefix or marketplace_plugins lookup. NULL for built-ins / ambiguous.
  duration_ms       INTEGER             -- nullable
  outcome           VARCHAR             -- 'ok' | 'error' | 'rejected'
  error_class       VARCHAR             -- nullable; Phase 1 vocabulary: 'bq_lib_missing', 'remote_scan_too_large', 'bq_path_not_registered', 'auth_denied', 'tool_timeout'. 'wrong_tool' is Phase 2 (heuristic TBD).
  bytes_scanned     BIGINT              -- nullable; query events. Source of truth: BqAccess (server-side), NOT JSONL — see "Data flow" below.
  rows_returned     BIGINT              -- nullable
  privacy_mode      BOOLEAN             -- TRUE when this event came from a metadata-only session
  payload           JSON                -- nullable; NULL when privacy_mode=TRUE (extraction-time contract)
```

Daily rollups (parquet, `query_mode='local'` → distributed via `agnes pull`):
- `activity_daily_user(user_id, day, prompts, tool_calls, queries, bq_bytes, failures)`
- `activity_daily_plugin(plugin_name, day, distinct_users, invocations, failures)`
- `activity_daily_department(group_id, day, prompts, tool_calls, queries, bq_bytes, failures)`

Aggregate kept past retention (no payload):
- `plugin_adoption(user_id, plugin_name, first_seen_at, last_seen_at)` — upserted by the daily extractor; survives the 90-day rolling delete so the granted-vs-adopted ratio remains computable for grants older than the retention window.

Extractor checkpoint state (avoids collision with `services/session_collector`'s `session_extraction_state`):
- `activity_extraction_state(jsonl_path VARCHAR, jsonl_hash VARCHAR, processed_at TIMESTAMP, PRIMARY KEY (jsonl_path, jsonl_hash))`

### Data flow & invariants

These are ship-blocking invariants the implementation must hold, not nice-to-haves:

1. **bytes_scanned authority.** BigQuery scan bytes are computed server-side in `app/api/query.py` / `app/api/v2_scan.py`; they are NOT in Claude Code session JSONLs. Reviewer-flagged collision: writing synchronously to `activity_events` in `extract.duckdb` from the BQ query path conflicts with the orchestrator's exclusive-write lock during `rebuild()`. Resolution: the BQ scan path writes to `audit_log` (in `system.duckdb`, where the app already writes) with `action='bq_scan', params={bytes_scanned, rows_returned, target_table, query_id}`. The nightly extractor projects matching `audit_log` rows into `activity_events` with the session_id/user_id join from the JSONL. Until that join completes, the BQ-cost data is visible only via `audit_log` queries, not the activity dashboards — acceptable for daily-refresh cadence. This avoids the cross-DB write race entirely.
2. **JSONL is source of truth for prompts/tool params.** The server stores the JSONL as `agnes push` sent it. The activity-center extractor reads it and applies the privacy filter at extract time. There is no second client-side strip happening before upload — a private session's `agnes push` sends a *manifest* (counts + tool names + targets) instead of the JSONL, never a partially-stripped JSONL. This makes reprocessing well-defined and right-to-forget straightforward.
3. **Right-to-forget cascades.** Deleting a user's events requires recomputing affected rollup rows. Scope of the cascade (reviewer-flagged: previously incomplete):
   - `activity_events`: delete rows where `user_id = <id>`.
   - `activity_daily_user`, `activity_daily_department`: delete rows where `user_id = <id>`.
   - `activity_daily_plugin`: recompute affected `(plugin_name, day)` rows from remaining users — the deleted user's contribution to plugin-level aggregates must be removed too.
   - `plugin_scores`: recompute affected `(plugin_name, day)` rows — `adoption_breadth`, `adoption_intensity`, `retention` all derive from per-user behavior; the deleted user's contribution removes cleanly only via recompute.
   - `session_scores`, `user_scores`: delete rows where `user_id = <id>`.
   - `plugin_adoption`: delete rows where `user_id = <id>`.
   The right-to-forget tool is **a transaction within `extract.duckdb`** (the activity store); the user row in `system.duckdb` is unaffected unless the operator separately deletes the account. If the recompute step fails after the delete, the next nightly extractor pass restores the rollup to a consistent state and the right-to-forget tool exits non-zero — the operator re-runs. Atomicity is single-DB, not cross-DB; the design cannot promise more than DuckDB delivers.
4. **Reprocessing.** When extraction logic changes, `DELETE FROM activity_events` + replay JSONLs in the retention window. Private-session JSONLs are not present on the server (manifest-only), so reprocessing is well-defined: those sessions stay metadata-only across schema versions. New columns added in a schema bump are NULL for replayed rows — explicitly accepted. Reviewer-flagged constraint: a future schema bump that derives *new content* from manifest fields (e.g. structured tool-target parsing) does not violate the disclosure-at-acceptance contract because the manifest fields themselves are the same data the user accepted at first run; the derived structure is interpretation, not new collection.

5. **Schema migration plan (reviewer-flagged).** v25 (next available; current `SCHEMA_VERSION = 24` per `src/db.py`) bundles the activity-center additions:
   - **New tables in `system.duckdb`**: `activity_disclosure_state` (or extend `users` with `activity_disclosure_accepted_at TIMESTAMP NULLABLE` + `activity_disclosure_version INTEGER NULLABLE`), `users.activity_disclosure_*` columns, no FKs. Setup-wizard acknowledgement table `activity_center_operator_ack(operator_id, ack_works_council BOOL, ack_dpia BOOL, ack_disclosure BOOL, acked_at TIMESTAMP)`.
   - **New column on `user_groups`**: `is_department BOOLEAN DEFAULT FALSE`. Does not collide with existing `is_system` row contract.
   - **New tables in `extract.duckdb` (the new activity_center extract)**: `activity_events`, `activity_daily_user`, `activity_daily_plugin`, `activity_daily_department`, `plugin_adoption`, `activity_extraction_state`, `session_scores`, `plugin_scores`, `user_scores`. No FKs to `system.duckdb` because cross-DB FKs are not supported by DuckDB; integrity maintained by extractor logic. `activity_events.user_id` is intentionally not a FK to `users.id` because right-to-forget may delete events without deleting the user (or vice versa).
   - **Reserved column slot**: `activity_events.payload_key_id VARCHAR NULLABLE` for future column-level encryption. Documented as forward-compat slot, NULL on initial release, populated only when iteration-backlog encryption ships.
   - **Migration ordering**: setup wizard table → `users` columns → `is_department` column → activity tables. Each step idempotent; rollback drops in reverse.
   - **Backfill**: none required for new tables. `users.activity_disclosure_accepted_at` defaults NULL, meaning every user must accept on next session start — explicit by design.

## Implementation approach (proposed, for `/ce:plan`)

Three approaches considered. Recommendation: **A→B progression based on observed corpus size.**

**A — Lazy/CTAS DuckDB over JSONLs.**
Zero new service. Scheduler runs a nightly CTAS that materializes `activity_events`, the three daily rollups, `plugin_adoption`, and the score tables directly from `read_json_auto('${DATA_DIR}/user_sessions/**/*.jsonl')` joined with the manifest sidecars. Scoring runs as additional CTAS layers on top. Privacy filter applied during the CTAS as SQL predicates. Pros: ships in days; no new service; no extractor checkpoint state; reprocessing = rerun the scheduler job; minimal new surface; scoring engine = SQL, easy to inspect and tune. Cons: full corpus rescan each night becomes painful past ~5–10M JSONL events; no incremental processing.

**B — Materialized rollups + dedicated extractor.**
Shared JSONL walker with `services/session_collector` (one walker, two extractors as plug-ins) checkpoints in `activity_extraction_state`, writes incrementally to `activity_events` + rollups + `plugin_adoption` + score tables. Pros: incremental; scales past A's painful zone; the synchronous BQ-cost-event path joins seamlessly. Cons: extractor service to maintain; LLM-as-judge sampling has to be plumbed through the extractor process.

**C — Inline extraction in upload endpoint** (rejected).
`/api/upload/sessions` extracts at receive time. Rejected for upload-latency, reprocessing fragility, uptime coupling. The narrow synchronous-write path for BQ scan-bytes (server-side, not in JSONL) survives because that data does not flow through the JSONL.

**Recommendation.** Implement A. Plan-phase 1-day spike validates corpus-size assumption against the largest expected operator deployment (back-of-envelope: 50 analysts × ~100 tool calls/day × 90 days ≈ 450K rows — trivial; 500 analysts × chatty workflow ≈ 50M rows — borderline). If A's nightly CTAS fits inside the maintenance window, ship A and add B as iteration-backlog work. If not, ship B from day one. The user-visible surface (schema, dashboards, RBAC, exports, scoring) is identical across A and B — only the extractor implementation differs. Track C hardening sits on top of either.

## Open questions for `/ce:plan`

1. **Encryption at rest — filesystem-only by default (decision recorded).** The `payload` JSON column is stored unencrypted at the DuckDB layer; operators are responsible for volume-level encryption (LUKS, GCP CMEK, equivalent). The runbook calls this out explicitly. Column-level encryption is in the iteration backlog if an instance handles regulated data or the operator's risk model demands it; the schema is forward-compatible (key-id column slot reserved in the v25 migration so a later toggle is non-breaking).
2. Where does the privacy-mode toggle UI live in the analyst workflow? CLI-only is enough for Phase 1; a status-bar widget might come later.
3. **Per-prompt redaction is a decision, not a deferred question.** Reviewer feedback (security-lens, adversarial) classifies "ship without redaction; revisit on incident" as the wrong default for an irreversible exposure class. Phase 1 ships with regex-based stripping at extract time for known-shape secrets:

   **Generic patterns:**
   - AWS-style access keys (`AKIA[0-9A-Z]{16}`) and secret keys (`(?i)aws[_-]?secret[_-]?access[_-]?key`)
   - GitHub tokens (`gh[pousr]_[A-Za-z0-9]{36,}`, `github_pat_[A-Za-z0-9_]{82}`)
   - OpenAI / Anthropic keys (`sk-[A-Za-z0-9-_]{32,}`, `sk-ant-[A-Za-z0-9-_]{32,}`)
   - JWTs (`ey[A-Za-z0-9_-]+\.ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+`)
   - Basic-auth URLs (`https?://[^/\s:]+:[^@/\s]+@`)
   - `<email>:<password>` pairs (`[^\s@]+@[^\s@]+:\S+`)
   - High-entropy strings ≥32 chars, mixed alphanumeric, no dictionary-word substring (catches secrets the curated patterns miss; accepts higher false-positive rate against UUIDs and hashes)

   **Platform-specific patterns:**
   - Keboola Storage tokens (`[a-z0-9]+-[0-9]+-[A-Za-z0-9]{40}` — tenant-stack-secret triple)
   - Atlassian PATs (`ATATT[A-Za-z0-9_-]{180,}`) and Atlassian session cookies (`cloud\.session\.token=[^;\s]+`)
   - BigQuery service-account JSON blobs: any JSON object containing a `"private_key"` field with a `-----BEGIN PRIVATE KEY-----` body — the whole object is replaced with `"<redacted:gcp-sa-json>"`

   Detection is best-effort, not exhaustive — privacy mode remains the only complete-redaction layer. The redaction module is versioned in `instance.yaml` (`activity_center.redaction.patterns`, `activity_center.redaction.high_entropy_min_length`) so operators can extend the pattern set without a code release. Each redaction action emits a counter to the existing platform metrics so unusually high redaction rates surface as anomalies.
4. **`department` = a `user_group` marked `is_department=TRUE`.** Decision recorded explicitly: a "department" is exactly a `user_group` with `is_department=TRUE`. Only `is_department` groups appear in `activity_daily_department` rollups and the "BQ bytes by department" tile. Users can belong to multiple departments; rollups are computed at `(user_id, group_id, day)` grain so a user in two departments contributes to both — totals are additive, not partitioned. The CEO sees potentially-overlapping totals (a user counted twice if they're in two departments) — this is correct for matrix organizations and documented on the tile. If a single canonical department per user is ever required, sync from a Google Workspace `department` directory attribute into a separate column on `users` rather than overloading `user_groups` further.
5. **Failure taxonomy (Track B).** Initial list: `bq_path_not_registered`, `remote_scan_too_large`, `wrong_tool` (heuristic), `tool_timeout`, `auth_denied`. The `wrong_tool` heuristic is the underspecified one — plan-phase task in Track B is to define it concretely (candidate: tool calls that returned the documented "use X instead" error from Agnes server, or tool calls followed within N seconds by a different tool against the same target).
6. **Existing `/activity-center` route.** `app/web/router.py` already has a stub `GET /activity-center` returning `activity_center.html`. Initial release reuses this route and extends it with `/users`, `/users/<user_id>`, `/plugins`, `/plugins/<plugin>` sub-routes (plan-phase task: replace stub context with live tile data; do not introduce `/admin/activity` as a parallel path).

7. **LLM-as-judge — deferred to iteration backlog.** Pass 2 review surfaced three load-bearing problems: judgeability (Claude scoring its own output biases toward verbose work), Art. 28 third-party processor surface (Anthropic must be named in disclosure + DPA executed), and cost-metering path (existing `connectors/llm/anthropic_provider.py:extract_json` doesn't surface usage data, so per-instance budget caps require an extractor protocol extension). Together these exceed the value of a 1-line model-written summary at 5% sampling. Track A ships deterministic-only scoring. Iteration backlog: revisit when (a) operator-side evidence shows the qualitative gap matters, (b) the cost-metering path is built, (c) disclosure language is updated to name Anthropic as a sub-processor, and (d) the rubric is versioned and visible to the analyst with a re-judgment-request mechanism.

8. **Track A vs Track B/C sequencing — honest commitment.** Track A (data + scoring + dashboards + privacy + baseline RBAC) ships in the initial release. Track B (failure intelligence) and Track C (advanced hardening — dual-approver, mutual-visibility digest, bulk-query rejection, setup wizard) ship as iteration 1, within weeks of Track A. The user-facing brief describes the whole product; the implementation commitment is honest about ordering.

## Success criteria

The single Activity Center release ships when all of the following are true. Tracks A/B/C are parallel work streams; A is critical path, B and C ride alongside.

### Track A — Data, scoring, dashboards (critical path)

**Data & pipeline:**
- `activity_events` populated daily from session JSONLs and manifest sidecars (extract.duckdb pattern); privacy-mode events sourced from `*.manifest.json` and metadata-only.
- Daily rollups: `activity_daily_user`, `activity_daily_plugin`, `activity_daily_department` refreshed nightly.
- `plugin_adoption` aggregate upserted nightly and survives the 90d rolling delete.
- 90d retention purge runs daily, idempotent, emits a verification row to `audit_log`; missed runs >48h surface in `app/api/health.py`.
- `activity_extraction_state` dedup table prevents collision with `services/session_collector`.
- Two upload routes ship: existing `/api/upload/sessions` (PAT-authed, 50 MB streaming JSONL); new `/api/upload/sessions/manifest` (PAT-authed, pydantic-validated JSON for privacy-mode sessions). Mixed payloads are structurally impossible.
- BQ scan-byte rows written synchronously by `BqAccess` at scan time; nightly extractor enriches with session_id/user_id when the JSONL lands.

**Scoring engine:**
- `session_scores`, `plugin_scores`, `user_scores` populated nightly. Composite + sub-scores per definitions in §7. LLM-as-judge deferred to iteration backlog (no `llm_judge_*` columns in initial release).
- Composite weights are configurable in `instance.yaml` (`activity_center.scoring.session.*`, `.plugin.*`, `.user.*`); default weights documented in the operator runbook.
- Score tables retain up to `aggregate_retention_days` (default 365), then hard-deleted by the same purge job that handles raw events. User-keyed score data is personal data under GDPR Art. 4 — the bound is non-negotiable, not "aggregate-only no retention."
- Right-to-forget cascades fully (see Data flow §3): activity_events delete + plugin_scores recompute + plugin_adoption delete + score-table user-row delete + rollup recompute, all in a single `extract.duckdb` transaction.
- **Composite per-user score is hidden from the leadership tile by default** (`scoring.show_user_composite_to_admin: false`). Sub-scores and trends remain visible on per-user drill-down (when `per_user_audit_enabled: true`); the analyst sees their own composite always on `/me/activity`.

**Admin surfaces:**
- `/activity-center` overview tiles render: active users 7d/30d, top plugins by composite score, BQ bytes by department, failure-rate trend, redaction-counter anomaly tile, **the weekly leadership digest tile** (templated narrative; richer when LLM-judge ships).
- `/activity-center/weekly` leadership digest page — paragraph-shaped synthesis of the week from rollups (Sunday-generated, Monday-morning ready).
- `/activity-center/users` and `/activity-center/users/<user_id>` per-user dashboard — *gated behind* `per_user_audit_enabled: true` (post-setup-wizard); sub-score trends, click-through to session detail with RBAC + rate-limit + dual-approver gates for `payload` access.
- `/activity-center/plugins` and `/activity-center/plugins/<plugin>` per-plugin dashboard with adoption + retention trends.
- All tiles and tables have CSV + Parquet export buttons. Export scopes per Decision 6.
- NL prompting works against `analytics.duckdb` rollup views; per-user `payload` access is `query_mode='remote'` so admin queries land server-side and trigger audit-log writes at the API boundary.

**Analyst-facing:**
- `agnes private on|off|status` toggles privacy mode; per-session mechanism (or workspace-wide fallback) verified during plan-phase 1-day spike, not assumed.
- First-run disclosure in `agnes init` describes collection, retention, scoring, and the operator's per-user-audit posture for this instance. Acceptance recorded with timestamp + version (`users.activity_disclosure_accepted_at`, `users.activity_disclosure_version`); uploads from unaccepted or stale-version users are rejected with `403 disclosure_required`.
- `/me/activity` self-view renders the analyst's own dashboard plus the recourse mechanisms: weight transparency, score-dispute action per session, "who looked at me" list (last 30d), disclosure history. Symmetric *data* + symmetric *recourse* — the rhetoric of "symmetry as trust contract" earned, not asserted.
- `agnes admin activity forget --user <id>` deletes / recomputes per the cascade in Data flow §3.

### Track B — Failure intelligence

- Failure events classified into the taxonomy: `bq_path_not_registered`, `remote_scan_too_large`, `auth_denied`, `tool_timeout`, `bq_lib_missing`, `wrong_tool` (heuristic).
- `wrong_tool` heuristic defined concretely (candidate: tool calls returning Agnes' "use X instead" guidance, or tool calls followed within N seconds by a different tool against the same target). Validated against ≥10 sample sessions before the metric is published.
- Failure-rate trend tile + a CLI report (`agnes admin activity failures --since 7d`).
- Plugin churn (granted-vs-adopted ratio): zombie plugins (granted, never adopted by anyone in the granted group), abandoned plugins (last-seen > N days). Surfaced on the `/activity-center/plugins` page.

### Track C — Hardening (paranoid defaults, configurable)

Reviewer pass 2 surfaced that earlier defaults shipped a configuration menu rather than defenses. Pass 2 flips the defaults to enforce informed consent on first deploy; operators relax for convenience after explicit acknowledgement, not before.

- **Admin role + `resource_grants(resource_type='per_user_audit', resource_id=<target_user_id>)`** — both required for any read of `activity_events.payload`. Two-of-two technical gate.
- **`activity_center.per_user_audit_enabled: true|false`** master switch in `instance.yaml`. **Defaults to `false`.** First-boot operator wizard at `/admin/activity-center/setup` requires the operator to acknowledge three checkboxes ("works-council consulted or not applicable", "DPIA completed or not applicable under Art. 35", "users disclosed and accepted") before the wizard flips this flag. Acknowledgement is recorded in `audit_log` (`action='activity_center_enabled', user_id=<operator>, params={ack_works_council, ack_dpia, ack_disclosure}`). Refusing the wizard ships degraded mode: overview tiles + aggregates work; per-user-detail returns 403 with the runbook link. This converts "operator's responsibility" from rhetoric into structure.
- **Per-user-detail read rate-limit** — `activity_center.per_user_audit.rate_limit_per_hour: 5` (default 5 user-detail queries per hour per admin). Reviewer flagged the prior 1/min default as permitting full corpus traversal in 2 hours — 5/hour caps it at hundreds of users per workday, sufficient for legitimate investigation, structurally insufficient for systematic monitoring. Breach triggers a warning row in `audit_log` and a notification to the configured alerts channel.
- **`activity_center.per_user_audit_dual_approver: true|false`** — **defaults to `true`**. Reviewer flagged the most-important intra-admin abuse control was previously default-off. When the instance has fewer admins than the dual-approver minimum (default 2), dual-approver auto-disables with an audit-log warning ("dual-approver self-disabled: only N admins on instance") so single-admin small-instance deployments aren't bricked. The disabling is logged, visible to the analyst on their detail page, and the operator can flip the dual_approver_min_admins to 1 if they explicitly want to.
- **Bulk-query detection** — `activity_center.per_user_audit.bulk_query_max_distinct_users: 1` (default 1). Any SQL statement scanning >1 distinct user_id is rejected with `403 bulk_query_rejected` and a typed error message directing the admin to rollup views. Legitimate cross-user aggregation goes through the rollup tables (which are the right shape anyway). Operators can raise the threshold; raising it requires re-acknowledging the setup wizard (sticky-default protection).
- **Mutual-visibility digest** — weekly report listing per-user-detail reads from the prior week (requester / target / timestamp). Delivered via the existing **Telegram bot infrastructure** (`services/telegram_bot`) — not email, since the OSS has no SMTP/SES dependency and adding one for this single use case is unjustified. Operators who need email can route Telegram → email at the Telegram side. **Symmetric delivery: each analyst whose record was queried also receives a Telegram notification** ("user X looked at your activity record on date Y") if they have linked their Telegram account; if not, the same information appears on `/me/activity` for self-discovery. The previous design sent the digest only to admins, which made it a one-way mirror; symmetric notification closes the loop.
- **Operator runbook** (`docs/operator/activity-center.md`) ships with the release, documenting: per-user telemetry triggers Czech Labour Code §316 / GDPR Art. 88 obligations; the setup wizard is the operator's compliance forcing function; default scoring weights and how to tune them; rate-limit and dual-approver knobs and when to flip them; *minimum-collection profile* `instance.yaml` snippet that operators can adopt as a one-line override (`activity_center.minimum_collection: true` sets retention to 30d, disables payload storage entirely, disables scoring, leaves only failure-class counts and per-department aggregates — for operators who want the failure-classification value without the surveillance surface).

### Iteration backlog (not initial release, not blocking)

- **LLM-as-judge** (deferred per §7) — qualitative tile + sampled summaries, gated behind cost-metering protocol extension, Art. 28 disclosure update, judgeability-mitigation (rubric versioning + analyst dispute mechanism + leadership-invisibility-by-default).
- **Shared JSONL walker abstraction** — refactor `corporate_memory` + `verification_detector` + `activity_center` to plug into one walker rather than triple-scanning `/data/user_sessions/`.
- **Survey-based ROI complement** — quarterly self-reported value, orthogonal to telemetry, useful triangulation.
- **Per-user persistent privacy default** — currently per-session only.
- **Column-level encryption for `payload`** — slot reserved in v25 schema (`payload_key_id`).
- **Tiered retention** — longer aggregate retention than raw, beyond the current 90/365 split.
- **Per-grant `expires_at`** on `activity_export` grants.
- **Email delivery infrastructure** — if operators consistently report Telegram-only digest is insufficient.
