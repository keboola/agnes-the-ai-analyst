# Data Apps Wave 3C — Extras Skill + Preview Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the KAI-mirrored AI-authoring loop for Agnes data apps. Wave 3B shipped the prod+draft **deploy substrate** (registry columns v99, `create-draft` / `git-credential` / `dev`-mode-deploy / `delete-draft` endpoints, their MCP tools, the broker `data_apps` scope). 3C ships the **agent-facing layer that makes the loop usable from a chat conversation**: the bundled `agnes-data-apps-extras` skill, the baked React+Vite+Tailwind+Express scaffold, the in-chat preview iframe tools + scoped preview auth, the marketplace registration of the reused `dataapp-developer` plugin, and "Path D — Agnes" in the shared skill. The exit criterion is the §10 end-to-end acceptance: a chat request "build me a dashboard over table X" → scaffold → dev deploy → real code → healthy in-chat preview → user approves → promote → prod serves it, zero human shell access.

**Architecture:** No new schema, no migration — 3C is deliberately migration-free (the draft model landed in 3B/v99, and the scoped preview grant reuses the existing `access_tokens` table's `scopes` + `expires_at` columns rather than adding a TTL column to the durable `resource_grants` model). New work is: (1) content assets bundled into `app/initial_workspace_default/.claude/skills/` + a baked scaffold tree, both flowing into every chat sandbox via the existing `WorkdirManager` copy + `skills_catalog` merge; (2) four **chat-surface** MCP tools (`agnes_data_app_preview` / `…_refresh` / `…_close` / `…_credentials`) that emit render directives the web chat special-cases into a split-pane iframe; (3) a short-TTL `data-app-preview:<slug>` scoped token minted server-side and honored by the ingress proxy so the iframe loads `/apps/<slug>/` without a cross-origin login; (4) marketplace registration + "Path D" documentation.

**Tech Stack:** FastAPI, httpx, FastMCP, itsdangerous/JWT (reusing `access_tokens`), the upstream `data-app-python-js` runtime image, React+Vite+Tailwind+Express (baked scaffold), vanilla JS chat frontend (`app/web/static/js/chat.js`), Claude Code skill format.

**Spec:** `docs/superpowers/specs/2026-07-23-data-apps-wave3-ai-authoring-design.md` — read §3 (Path D), §4 (extras skill sections 1–7), §7 (preview iframe + auth), §11 (open questions). §5/§6 (draft tools + prod/draft model) are **already shipped in 3B (0.76.30)** — this plan consumes them, does not rebuild them.

**Prior plan (style/format template):** `docs/superpowers/plans/2026-07-23-data-apps-wave3b-draft-model.md`.

---

## Prerequisites (hard dependency — fixed on `zs/data-apps-hosting-enable`, PR #1065)

A live E2E on 2026-07-27 confirmed the 3B draft model works end-to-end at the **control-plane** level (create draft → `deploy mode=dev` → registry row + branch + config all correct), but the **container actually serving the app** is blocked by two wave-1+2 hosting gaps. 3C's preview acceptance (§10 — a *healthy* iframe) cannot pass until both are fixed. These are **out of scope for this plan** — they are fixed on branch `zs/data-apps-hosting-enable` (PR #1065) and must merge before Task 8's E2E can go green.

- **P1 — apps-runner docker-socket permission.** The `apps-runner` sidecar runs as the image's non-root uid 999 with no `group_add` for the docker-socket gid → `PermissionError(13)` → runner can't create containers → proxy 502. Fixed on the prerequisite branch (compose `group_add: ${DOCKER_GID}`).
- **P2 — runtime container clone credential.** The runtime image only credentials HTTPS clone URLs, not Agnes's internal `http://app:8000` backend → `could not read Username` → crash-loop → proxy 502 `container_unreachable`. Fixed on the prerequisite branch (`build_config_json` embeds the token in the repository URL).

**Declared dependency, not re-planned here.** Tasks 1–7 build and unit/integration-test in parallel with the prerequisite — they don't touch the runner or the runtime clone path. Only **Task 8's docker E2E "healthy preview" assertion** requires P1+P2 merged; `xfail` those container/preview asserts until the prerequisite merges, keep the control-plane asserts hard. Do **not** copy the P1/P2 fixes into this branch.

---

## Open questions from spec §11 — resolutions

**Q1 — Marketplace registration status of the public `keboola/ai-kit` `dataapp-developer` plugin.** *Register it as part of 3C (Task 6).* Instances currently register `ai-kit-internal` and `cf-claude-code-kit`, not the public `dataapp-developer` plugin. 3C treats registration as an operator setup step: a `POST /api/marketplaces` runbook + a `resource_grants` row for the author group. Vendor-agnostic; captured as a runbook in `docs/DEPLOYMENT.md` (not OSS code). **Register the public plugin; grant to the author group, not blanket `Everyone`.**

**Q2 — Path D: upstream vs. Agnes-only overlay.** *Both, in order.* Upstream "Path D — Agnes harness" to `keboola/ai-kit` `references/deployment-paths.md` + detection line (preferred — shared skill), AND ship a short interim overlay in `agnes-data-apps-extras` (Task 1) so Agnes works standalone immediately. Overlay shrinks to a one-line pointer once the upstream PR merges. **Do not fork the general skill.**

**Q3 — Draft as registry sibling vs. config flag.** *Settled and shipped in 3B* (`parent_app_id`/`is_draft`/`draft_branch` at v99). No 3C decision. 3C only previews a draft's `/apps/<draft_slug>/`.

**Q4 — Preview-auth TTL.** *Short-TTL scoped token in `access_tokens`, renewed per preview call, capped to the chat session; no new schema.* Do NOT add a TTL column to `resource_grants` (pollutes durable RBAC + forces a dual-backend migration for transient state). Mint a `data-app-preview:<slug>` scoped token in `access_tokens` (already has `scopes` + `expires_at`), mirroring the enforced `data-app-git:<slug>` scope from 3B. **Default TTL 30 min, renewed on every `agnes_data_app_preview`, revoked at `SessionEnd` alongside broker tickets.** Deliver as a cookie scoped to the app origin (`Path=/apps/<slug>/`), never a URL query param (privacy rule). The proxy's `_can_view` gains a branch accepting a valid, unexpired preview token whose scope slug matches.

---

## Global Constraints

- **No migration in 3C.** Schema stays v99. The preview grant reuses `access_tokens` (`create()` already takes `scopes` + `expires_at`) — nothing serializes last, all tasks parallelize.
- **Dual-backend discipline** for any repo method touched: no new `access_tokens` method without its `_pg.py` twin + a `tests/db_pg/` contract assertion in the same task.
- **Surfaces / triple-surface ratchet.** Preview tools are the documented chat-only exception (spec §9): no REST route, no CLI → they join only the MCP allowlists (`test_mcp_tool_parity.py`, `test_mcp_http.py`) + `FOUNDATION_TOOL_NAMES`; no `_COHORT`/`_EXEMPT` entry (they expose no `/api/*`). The one new REST route — `POST /api/data-apps/{slug}/preview-grant` — is chat-surface-internal; add to `_EXEMPT` in `test_documentation_api_triple_surface.py` with reason `"preview-grant mints the in-chat iframe cookie for the web chat surface; chat-only, no CLI/MCP analogue (spec §7)"`. No other grandfather growth.
- **Design-system contract.** Preview-pane HTML/CSS must pass `test_design_system_contract.py` (no bare `:root{}`, no raw `#hex`, no `var(--primary)` — use `var(--ds-*)`; page CSS in a block).
- **Feature flag.** Every new server handler calls `_feature_gate()` first; preview MCP tools return a friendly "disabled" payload, not 500, when off.
- **Vendor-agnostic.** Scaffold + extras skill + Path D text ship in the public repo: placeholders only. The `keboola/ai-kit` reference is fine (public Keboola OSS) — frame as "the upstream `dataapp-development` skill".
- **No AI attribution. `.venv/bin/pytest` (ruff at `/opt/homebrew/bin/ruff`). Guard `uv.lock`.**
- **CHANGELOG discipline** — one `### Added` bullet (Task 7); release-cut is the last commit if this PR lands the only `[Unreleased]` content (patch bump by default).

## File Structure

```
app/initial_workspace_default/.claude/skills/agnes-data-apps-extras/SKILL.md   # NEW — extras skill (first bundled skill)
app/initial_workspace_default/.claude/skills/agnes-data-apps-extras/references/ # NEW — path-d.md, agnes-query.md, promote-flow.md
app/initial_workspace_default/scaffolds/nodejs-dashboard/                        # NEW — baked React+Vite+Tailwind+Express scaffold
  ├── keboola-config/                                                            #   runtime contract (nginx :8888, app :3000, POST / health)
  ├── src/App.tsx, src/main.tsx, index.html, vite.config.ts, tailwind.config.js
  ├── server/index.ts, server/agnesQuery.ts                                      #   Express + Agnes REST helpers over AGNES_TOKEN/AGNES_URL
  ├── package.json, supervisord.conf, CLAUDE.md.tmpl
app/api/mcp/foundation_tools.py     # +4 chat-surface MCP tools; +names in FOUNDATION_TOOL_NAMES
app/api/data_apps.py                # +POST /{slug}/preview-grant; +_mint_preview_token helper
app/api/data_apps_proxy.py          # +_can_view accepts a valid data-app-preview:<slug> token/cookie
app/auth/access.py OR app/auth/jwt.py  # preview-token verify path (mirror the data-app-git scope guard)
app/chat/manager.py                 # revoke preview tokens at SessionEnd (alongside ticket revoke)
app/web/static/js/chat.js           # render preview/refresh/close/credentials as a split-pane iframe
app/web/static/css/chat.css         # preview pane styles (ds vars only)
docs/DEPLOYMENT.md                  # authoring flow + marketplace-registration runbook + Path D pointer
CHANGELOG.md                        # one Added bullet + release cut
tests/test_chat_skills_catalog.py, tests/test_chat_skills_endpoint.py  # extras skill appears in catalog
tests/test_data_apps_scaffold.py    # NEW — scaffold shape/health contract
tests/test_data_apps_preview.py     # NEW — preview tools + preview-grant auth
tests/test_data_apps_proxy.py       # preview-token accept / expiry / stranger-403
tests/test_mcp_tool_parity.py, tests/test_mcp_http.py                  # +4 tool names
tests/test_documentation_api_triple_surface.py                         # +preview-grant in _EXEMPT
tests/test_data_apps_e2e_docker.py  # extend: scaffold→dev deploy→healthy preview→promote (gated on P1/P2)
```

## Parallelization map (for `/agnes-build` decomposition)

No migration → nothing serializes last. Coupling groups:

- **Group A — content (independent):** Task 1 (extras skill), Task 2 (baked scaffold), Task 6 (marketplace runbook + Path D doc). Three parallel workers.
- **Group B — preview backend (coupled: token → proxy):** Task 5 (preview-grant token + proxy accept) precedes Task 4's tool returning a usable cookie; build in one worktree or Task 4 stubs the token call.
- **Group C — preview frontend (depends on Task 4's payload shape only):** Task 3 (chat.js/chat.css) needs the render-directive JSON shape fixed below.
- **Task 7** (docs/CHANGELOG/ratchet/release) folds last. **Task 8** (E2E) runs after 1–7 integrate, gates on P1/P2.

**Fixed render-directive contract (so Task 3 and Task 4 build independently).** Each preview MCP tool returns a JSON object the runner forwards verbatim into the `tool_result` frame; `chat.js` switches on `render`:

```json
{ "render": "data_app_preview", "slug": "<draft_slug>", "url": "/apps/<draft_slug>/" | null, "preview_cookie": "<name>=<val>; Max-Age=1800; Path=/apps/<draft_slug>/; SameSite=Lax" }
{ "render": "data_app_preview_refresh", "slug": "<draft_slug>" }
{ "render": "data_app_preview_close", "slug": "<draft_slug>" }
{ "render": "data_app_credentials", "slug": "<slug>", "url": "<share_url>", "password": "<basic-auth pw or null>" }
```

`url: null` on the first `preview` call opens the placeholder pane immediately (spec §7 mandate); the follow-up with a real `url` swaps to the live app.

---

### Task 1: Extras skill `agnes-data-apps-extras` (bundled) + Path D overlay

**Files:** Create `app/initial_workspace_default/.claude/skills/agnes-data-apps-extras/SKILL.md` + `references/{path-d.md,agnes-query.md,promote-flow.md}`. Test: `tests/test_chat_skills_catalog.py`, `tests/test_chat_skills_endpoint.py`.

**Interfaces:** Consumes the bundled-skills path — `app/chat/skills_catalog.py::list_bundled_skills` reads `app/initial_workspace_default/.claude/skills/<name>/SKILL.md`; `WorkdirManager` copies the tree into every session. This is the **first** bundled skill (dir doesn't exist yet), so `list_bundled_skills`'s "missing dir is normal" branch stops firing. Produces a skill (valid frontmatter `name: agnes-data-apps-extras`, one-line `description`) that loads **alongside** `dataapp-development`, mirroring `dataapp-building-kai-extras`, sections retargeted (spec §4, 1–7): (1) scaffold-first cadence — `cp -R` the baked `nodejs-dashboard` scaffold into the managed repo before any real code, `data_app_deploy(mode=dev)` immediately to boot + warm `npm install`; (2) draft-branch discipline — never push app code to `main`; (3) preview cadence — hold `agnes_data_app_preview(url=…)` until real dashboard pushed + dev deploy healthy (poll `data_app_get` in ≤5s steps), then `AskUserQuestion` "Publish / Make changes", never auto-promote; (4) visual-quality bar + chat voice + jargon ban; (5) debug via `data_app_logs` only (row counts + redacted SQL, never PII); (6) context persistence — root `CLAUDE.md` `# App context (maintained by Agnes)` written at promote, read on fresh-conversation edits; (7) reading Agnes data via `server/agnesQuery.ts` over `AGNES_TOKEN`/`AGNES_URL`. `references/path-d.md` — the Q2 interim overlay. `references/promote-flow.md` — the exact 3B-tool-supported promote sequence.

- [ ] **Step 1: Failing catalog test** asserting `agnes-data-apps-extras` in `list_bundled_skills(BUNDLED_TEMPLATE_DIR)` with `source == "bundled"` + non-empty description.
- [ ] **Step 2: Run to fail** (dir missing).
- [ ] **Step 3: Write the skill + references.** Keep `SKILL.md` a router (≤ ~2 screens) deferring to `references/` and the general `dataapp-development` skill for the `keboola-config/` contract, ports, storage-access, styling, troubleshooting.
- [ ] **Step 4: Run** the catalog + endpoint tests → PASS; verify a WorkdirManager session-prep test still copies `.claude/skills/`.
- [ ] **Step 5: Commit** — `feat(data-apps): bundled agnes-data-apps-extras authoring skill + Path D overlay`

---

### Task 2: Baked `nodejs-dashboard` scaffold + `agnesQuery.ts`

**Files:** Create `app/initial_workspace_default/scaffolds/nodejs-dashboard/**`. Test: `tests/test_data_apps_scaffold.py` (NEW).

**Interfaces:** Consumes the runtime contract (nginx :8888, app :3000, `POST /` health, `uv`/supervisord) — same contract 3B/wave-2 deploy. Agnes mirror of KAI's baked React+Vite+Tailwind+Express scaffold (spec §2/§4.1), **no `@keboola/design` dep**. Produces a self-contained scaffold the extras skill `cp -R`s: `keboola-config/` present; `server/index.ts` serves the built SPA + `POST /` health; `server/agnesQuery.ts` → `runQuery(sql)` = `POST {AGNES_URL}/api/query` with `Authorization: Bearer ${AGNES_TOKEN}` + catalog/table lookups. `CLAUDE.md.tmpl` carries the empty `# App context (maintained by Agnes)` skeleton. `package.json` pins chart libs via npm (never CDN). Location `scaffolds/` (sibling to `.claude/`) so `skills_catalog` doesn't parse it as a skill but `WorkdirManager` still copies it.

- [ ] **Step 1: Failing scaffold contract test** — `keboola-config/` dir, `server/agnesQuery.ts` file, `supervisord.conf`, `package.json` has `vite`+`tailwindcss`, no `cdn.` in `index.html`; `agnesQuery.ts` references `AGNES_TOKEN`/`AGNES_URL`/`/api/query`.
- [ ] **Step 2: Run to fail.**
- [ ] **Step 3: Write the scaffold** — minimal but real: `App.tsx` calls a `/api/data` Express route backed by `agnesQuery.runQuery`; Vite build served by Express; supervisord runs Express :3000 behind nginx :8888. Vendor-agnostic.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(data-apps): baked nodejs-dashboard scaffold + agnesQuery helpers`

---

### Task 3: Chat frontend — split-pane preview iframe rendering

**Files:** Modify `app/web/static/js/chat.js`, `app/web/static/css/chat.css`. Test: design-system contract + manual DOM verify.

**Interfaces:** Consumes the fixed render-directive contract. `chat.js` already routes `tool_call`/`tool_result` frames (`renderToolCallStart`, `renderToolCallEnd`). Add a switch on `result.render` for the four preview directives driving a persistent split-pane iframe: `data_app_preview` `url:null` → placeholder pane; with `url` → set `iframe.src`, and if `preview_cookie` present set it on the app origin **before** loading (never token-in-URL); `_refresh` → reload; `_close` → tear down; `data_app_credentials` → shareable URL (+ password if present) as the final reply element. CSS `.cloud-chat-preview-pane` uses only `var(--ds-*)`.

- [ ] **Step 1: Implement** the four render branches + pane lifecycle + CSS.
- [ ] **Step 2: Design-system gate** → PASS.
- [ ] **Step 3: Manual verify** per the acceptance checklist (pane opens placeholder → swaps to `/apps/<slug>/` → refresh → close).
- [ ] **Step 4: Commit** — `feat(data-apps): in-chat split-pane preview iframe rendering`

---

### Task 4: Preview chat-surface MCP tools

**Files:** Modify `app/api/mcp/foundation_tools.py` (+4 tools; +`FOUNDATION_TOOL_NAMES`). Test: `tests/test_data_apps_preview.py` (NEW), `tests/test_mcp_tool_parity.py`, `tests/test_mcp_http.py`.

**Interfaces:** Consumes `data_apps_repo().get_by_slug`, the Task-5 `POST /{slug}/preview-grant`, `_app_url`, feature-gate (disabled → friendly payload). Produces four tools returning the fixed render directives (no CLI): `agnes_data_app_preview(slug, url="")` — empty `url` → placeholder directive; real `url` → calls preview-grant to mint the token, returns directive with `url` + `preview_cookie`. `agnes_data_app_refresh(slug)`, `agnes_data_app_close(slug)` (extras skill calls close **before** `data_app_delete_draft`), `agnes_data_app_credentials(slug)` (password only if owner set basic-auth; else null + URL + "grant a group in /admin/access" hint). Add the four names to `FOUNDATION_TOOL_NAMES`.

- [ ] **Step 1: MCP allowlist tests first** — add the four names to `test_mcp_tool_parity.py` + `test_mcp_http.py` → FAIL.
- [ ] **Step 2: Tool behavior test** — placeholder-then-live; credentials terminal render.
- [ ] **Step 3: Implement** after `data_app_logs`, mirroring the existing `data_app_*` closure shape (call preview-grant via the broker `data_apps`-scoped `base_url`/`headers_fn`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(data-apps): preview/refresh/close/credentials chat-surface MCP tools`

---

### Task 5: Scoped preview grant — `POST /{slug}/preview-grant` + proxy accept (no migration)

**Files:** Modify `app/api/data_apps.py` (+`_mint_preview_token`, +`POST /{slug}/preview-grant`), `app/api/data_apps_proxy.py` (`_can_view` accepts the preview token/cookie), `app/auth/` (verify path mirroring the `data-app-git:<slug>` guard), `app/chat/manager.py` (revoke at SessionEnd). Test: `tests/test_data_apps_preview.py`, `tests/test_data_apps_proxy.py`, `tests/test_documentation_api_triple_surface.py`.

**Interfaces:** Consumes `access_token_repo().create(..., scopes="data-app-preview:<slug>", expires_at=now+30m)` (existing dual-backend factory, no new column), `create_access_token(..., extra_claims={"scope": ...})`, `_get_row_or_404`, `_require_owner_or_admin`, `_feature_gate`. Reuses the 3B `data-app-git:<slug>` enforcement pattern. Produces `_mint_preview_token(row, ttl_s=1800) -> (token, set_cookie_header)` (cookie `Path=/apps/<slug>/; SameSite=Lax; HttpOnly` on the app origin); `POST /api/data-apps/{slug}/preview-grant` (owner/Admin/viewer, feature-gated) → `{"preview_cookie": ..., "expires_at": ...}`, **`_EXEMPT`** in the ratchet; `_can_view` branch authorizing a valid unexpired scope-matching preview token (view-only), rejected on the JSON control-plane API like `data-app-git`; SessionEnd revoke alongside `ticket_repo().revoke_session` (or rely on 30-min `expires_at` backstop + best-effort revoke).

- [ ] **Step 1: Failing proxy tests** — preview token authorizes `/apps/dash/`; expired → 401/403; scoped to slug (token for `dash` rejected on `/apps/other/`); rejected on `/api/data-apps/dash` control plane.
- [ ] **Step 2: Run to fail.**
- [ ] **Step 3: Implement** mint helper + endpoint + `_can_view` branch + scope guard + SessionEnd revoke.
- [ ] **Step 4: Ratchet** — add `preview-grant` to `_EXEMPT` with the reason string.
- [ ] **Step 5: Run** → PASS.
- [ ] **Step 6: Commit** — `feat(data-apps): scoped short-TTL preview grant + proxy iframe auth`

---

### Task 6: Marketplace registration runbook + Path D upstream (Q1 + Q2)

**Files:** Modify `docs/DEPLOYMENT.md`. External: upstream PR to `keboola/ai-kit` `deployment-paths.md`.

**Interfaces:** Produces a `docs/DEPLOYMENT.md` "Data apps → AI authoring" subsection: how an operator registers the public `keboola/ai-kit` `dataapp-developer` plugin — `POST /api/marketplaces` with the git URL (SSRF-guarded, cloned nightly per `app/api/marketplaces.py`), then a `resource_grants` row for the author group. Vendor-agnostic. Plus the Path D upstream PR (a "Path D — Agnes harness" section + detection line keyed off `data_app_*` MCP tools), tracked externally; the Task-1 overlay makes Agnes work standalone until it merges.

- [ ] **Step 1: Write the runbook** — exact `POST /api/marketplaces` body (from `app/api/marketplaces.py::create_marketplace`) + the grant step.
- [ ] **Step 2: Open the upstream `keboola/ai-kit` PR** for Path D; reference its URL in the CHANGELOG bullet.
- [ ] **Step 3: Commit** — `docs(data-apps): AI-authoring marketplace-registration runbook + Path D pointer`

---

### Task 7: CHANGELOG + docs fold + ratchet green + release cut

**Files:** Modify `CHANGELOG.md`, `docs/DEPLOYMENT.md`, `pyproject.toml`. Test: full suite.

**Interfaces:** One `### Added` CHANGELOG bullet; the DEPLOYMENT.md authoring-flow paragraph; the release-cut commit (version bump + `[Unreleased]`→`[X.Y.Z]` rename + fresh `[Unreleased]`) as the **last** commit if this PR lands the only `[Unreleased]` content (patch bump by default).

- [ ] **Step 1: CHANGELOG** `### Added` — "Data Apps: in-chat AI authoring loop" (scaffold → preview → promote with no shell access; the extras skill + baked scaffold + `agnes_data_app_preview`/`_refresh`/`_close`/`_credentials` + short-TTL scoped preview grant).
- [ ] **Step 2: DEPLOYMENT.md** authoring-flow paragraph.
- [ ] **Step 3: Full suite** — `.venv/bin/pytest tests/ --tb=short -n auto -q` → green modulo documented pre-existing failures; confirm the ratchet + MCP + design-system + skills-catalog tests pass.
- [ ] **Step 4: Release cut** (last commit).
- [ ] **Step 5: Commit** — `feat(data-apps): AI-authoring loop — docs, CHANGELOG, release cut`

---

### Task 8: End-to-end acceptance (gated on prerequisite P1/P2 = PR #1065)

**Files:** Modify `tests/test_data_apps_e2e_docker.py`. Test: the docker-compose E2E harness (`profile: apps`, `data_apps.enabled=true`).

**Interfaces:** Consumes everything above + the **merged** P1/P2 fixes. Produces the spec §10 assertion extended onto the wave-1+2 docker E2E: seed a prod app → `create-draft` → `cp -R` the baked scaffold onto the draft branch → `data_app_deploy(mode=dev)` → poll `data_app_get` until the draft container is **healthy** (`POST /` 200 through the proxy) → author a trivial change → preview URL loads through the proxy with a preview token → promote (merge → prod redeploy → close → delete-draft) → **prod** `/apps/<slug>/` serves the merged code → the abandoned-draft reaper removes an untouched draft.

- [ ] **Step 1: Write/extend** the full lifecycle. Guard the "healthy container" + "preview URL 200" asserts behind P1/P2: if the prerequisite is not merged, `pytest.mark.xfail(reason="blocked on zs/data-apps-hosting-enable P1/P2", strict=False)`; keep the control-plane asserts (draft row, branch, config.json branch, promote merges `main`, delete-draft removes row+branch) as hard asserts.
- [ ] **Step 2: Run** (requires docker) — control-plane green now; container/preview asserts flip to hard-pass once P1/P2 merge.
- [ ] **Step 3: Manual acceptance checklist** — a live chat session on an instance with P1/P2 + the `dataapp-developer` plugin registered: "build me a dashboard over table X" → scaffold + placeholder preview → real code + healthy preview swap → `AskUserQuestion` Publish → promote → prod serves it; no shell access, no leaked git/deploy jargon.
- [ ] **Step 4: Commit** — `test(data-apps): end-to-end AI-authoring acceptance (container asserts gated on hosting-enable)`
