# Agnes Data Apps — Wave 3: AI Authoring (KAI-mirrored) — Design

**Date:** 2026-07-23
**Status:** Draft for review
**Verified against:** `0.76.16` (waves 1+2 shipped in PR #1002, on `main`)
**Mirrors:** Keboola **KAI** data-app builder — `keboola/ui` (`apps/kai-agent`, `packages/kai-agent-sandbox`), `keboola/ai-kit` (`dataapp-developer` plugin), `keboola/mcp-server` (`tools/data_apps.py`). All KAI file references below were read directly from those repos on 2026-07-23.

## 1. Context & goal

Waves 1+2 gave Agnes the **deploy substrate** for hosted data apps: registry, apps-runner sidecar, internal git hosting with `agnes-live`, `/api/data-apps` control plane, RBAC ingress proxy at `/apps/<slug>/` with wake-on-request, `agnes app` CLI, MCP foundation tools (`data_apps_list/get/deploy/logs`), and `/apps` web pages. What waves 1+2 deliberately excluded: **the agent that authors an app from a conversation.**

The original wave-3 sketch (spec §9 of the wave-1+2 design) was "write an `agnes-data-apps` skill + 4 templates + broker endpoints from scratch." **That is now the wrong plan.** Discovery on 2026-07-23 established that Keboola already ships a complete, production, deploy-path-pluggable data-app authoring system — **KAI** — and its authoring knowledge is a **public Claude Code marketplace plugin** Agnes can ingest:

- **`keboola/ai-kit`** is a public Claude marketplace. Its **`dataapp-developer`** plugin carries the **`dataapp-development`** skill: a router with `references/` (choosing-app-type, python-js-apps, streamlit-apps, storage-access, duckdb-caching, authentication, styling-guide, styling-react-bundled, dashboard-patterns, deployment-paths, dev-workflow, kai-integration, troubleshooting, glossary) and `templates/` (streamlit, python-app, nodejs-app, python-node-app, duckdb-cache). It already covers the `keboola-config/` contract, `POST /` health check, `uv`/supervisord rules, RO-workspace + Query-Service access, DuckDB caching, and Keboola styling.
- The skill is **explicitly deployment-path-pluggable**: it abstracts the deploy target into "paths" (A: MCP-only, B: Claude Code + filesystem + MCP, C: `kbagent` CLI), with a session-start "detect available paths, ask the user" mechanism keyed off `mcp__*[Kk]eboola*` tool detection.
- **KAI itself** (`apps/kai-agent`) is a Hono + Claude Agent SDK backend that runs the LLM in an **E2B sandbox**, vendors `dataapp-development` into the sandbox, layers a Kai-specific **`dataapp-building-kai-extras`** skill on top (baked React+Vite+Tailwind+Express scaffold, preview-iframe controls, draft cadence, visual-quality voice), and deploys through **`keboola/mcp-server` `tools/data_apps.py`** (`modify_python_js_data_app`, `deploy_data_app`, `create_python_js_data_app_git_credential`, `delete_python_js_data_app_draft`, `get_data_apps`).

**Goal:** give Agnes the same authoring capability by **reusing `dataapp-development` verbatim** and **mirroring KAI's architecture**, retargeted to Agnes's own deploy substrate (waves 1+2) instead of the Keboola platform. Concretely:

1. Serve the `dataapp-developer` plugin through the Agnes marketplace (RBAC-granted).
2. Add **Agnes as a deployment path** ("Path D") to the skill.
3. Add an Agnes **extras skill** mirroring `dataapp-building-kai-extras` (scaffold-first cadence, preview iframe, draft/promote, visual-quality voice, `CLAUDE.md` context persistence).
4. Add the missing broker/MCP **tools** and the **prod+draft model** that the extras skill and preview loop depend on.

**Non-goals:** re-authoring the general skill (reuse it); Streamlit-first flows (Agnes leads with the Node.js dashboard default, same as KAI); a bespoke Agnes UI framework (KAI builds React+Vite+Tailwind+Express on the python-js runtime — we mirror that exactly, no `@keboola/design` dependency).

## 2. What we reuse verbatim vs. what we build

The load-bearing insight: **Agnes and KAI target the identical `keboola-config/` python-js runtime contract** (nginx :8888, app on :3000, `POST /` health, `uv`/supervisord). Everything the general skill teaches applies to Agnes unchanged; only the *deploy channel* differs.

| Layer | KAI | Agnes wave 3 |
|---|---|---|
| **General authoring skill** | `dataapp-development` (from `keboola/ai-kit`, vendored into the sandbox) | **same skill, same source** — served via Agnes marketplace, no fork |
| **Templates/scaffolds** | ai-kit `templates/*` + a baked React+Vite+Tailwind+Express scaffold | **same** — ai-kit templates; Agnes bakes the same scaffold shape into the chat workspace |
| **Extras skill** | `dataapp-building-kai-extras` (`keboola/ui`) | **`agnes-data-apps-extras`** — mirror, retargeted tools + Agnes preview |
| **Agent runtime** | E2B sandbox (`kai-agent-sandbox`, template `kai-agent:<hash>`) | Agnes chat E2B sandbox (already exists) + vendored skill/scaffold |
| **Deploy tools** | `mcp-server` `tools/data_apps.py` (Keboola platform) | **Agnes broker + MCP** over `/api/data-apps` (waves 1+2) — extend for drafts/modes |
| **Runtime substrate** | Keboola platform data-apps (apps-proxy) | Agnes apps-runner (waves 1+2) |
| **Iteration model** | prod + draft (external-git), draft→promote | **build the same model** on Agnes internal git |
| **Preview** | `ui_data_app_preview` / `ui_refresh` / `ui_close` / `ui_data_app_credentials` | mirror as Agnes chat tools over the ingress proxy |
| **Auth for preview** | basic-auth + apps-proxy `kai-preview` cookie | Agnes RBAC + a scoped preview grant (see §7) |
| **Context persistence** | repo-root `CLAUDE.md` `# App context (maintained by Kai)` | **same mechanism**, `# App context (maintained by Agnes)` |

Net: the two most expensive things (the authoring knowledge and the templates) are **reused, not written**. Wave 3 builds the *thin Agnes-specific layer*: Path D, the extras skill, the draft/mode tools, and the preview chat tools.

## 3. Deployment "Path D — Agnes"

The general skill's `references/deployment-paths.md` enumerates A/B/C and detects them by scanning for `mcp__*[Kk]eboola*` tools + `which kbagent`. Agnes adds **Path D**, detected by the presence of the Agnes data-app MCP tools (`data_app_*`, already shipped in wave-1+2 Task 11 — the `mcp__*` scan matches them) and/or the `agnes app` CLI.

Path D is contributed **upstream to `keboola/ai-kit`** as a new section in `deployment-paths.md` (Agnes is Keboola-OSS, so this belongs in the shared skill, not a private fork), plus the detection line. It reads, in the skill's own idiom:

- **Path D — Agnes harness (MCP + filesystem in the chat sandbox):** the app's managed git repo is hosted by Agnes at `/data-apps.git/<slug>`; deploy via the Agnes MCP tools (`data_app_deploy`, and the wave-3 additions below) or `agnes app deploy`; the running app is reachable at `/apps/<slug>/` and previewed in-chat. No Keboola-platform MCP, no `kbagent`.

The "pick one path per session" rule already handles the case where a Keboola MCP *and* Agnes tools are both present — the agent asks the user which project/harness to target.

## 4. Extras skill — `agnes-data-apps-extras`

A bundled skill in `app/initial_workspace_default/.claude/skills/` (flows into every chat session via the existing `WorkdirManager` copy + `skills_catalog` merge — the same path waves 1+2 use). It is the Agnes mirror of `dataapp-building-kai-extras`, and like it, **loads alongside `dataapp-development`, not instead of it.** Sections mirror KAI's, retargeted:

1. **Scaffold-first, custom-code-second** — bake the ai-kit `nodejs-app` scaffold (React+Vite+Tailwind+Express + `keboola-config/`) at a known path in the workspace; `cp -R` it into the managed repo before writing any real code; deploy `mode=dev` immediately to boot the container + warm `npm install` while authoring the real `src/App.tsx` + `server/index.ts` (HMR picks them up). This is verbatim KAI cadence — it exists to avoid the "first deploy races npm install → container failed to start" failure.
2. **Draft-branch discipline** — never push app code to `main`; every change goes on the draft's pinned branch; `main` only gains code at promote (see §6). Same empty-root-commit-on-`main` seeding KAI uses so the draft branch stays deletable.
3. **Preview iframe cadence** — hold the live `agnes_data_app_preview(url=…)` call until the *real* dashboard is pushed and the dev deploy reports healthy (poll `data_app_get` in ≤5s steps, never one long sleep). Then ask the user "Publish / Make changes" via `AskUserQuestion` and stop — never auto-promote.
4. **Visual-quality bar + chat voice + jargon ban** — copy KAI's rules verbatim (real React+Vite+Tailwind, charting libs via npm never CDN, no inline-HTML apps; 1–2 short sentences per chat message; never leak git/HMR/supervisord/deploy jargon to the user — say "setting up your app", not "pushing the scaffold to the draft branch").
5. **Debug via the terminal log** — the app's stdout/stderr in `data_app_logs` is the only observable signal; log row counts + redacted SQL + branch-taken, never cell values/PII.
6. **Context persistence** — the managed repo carries a root `CLAUDE.md` with a single `# App context (maintained by Agnes)` section (Purpose / Data sources / Key decisions / Iterate safely), written at promote and read back on fresh-conversation edits. Byte-for-byte KAI's mechanism.
7. **Reading Agnes data** — the scaffold ships `server/agnesQuery.ts` helpers over the app's `AGNES_TOKEN`/`AGNES_URL` (waves 1+2 injected env): `runQuery(sql)` against `/api/query`, catalog/table lookups against the catalog endpoints. (KAI's `kbcQuery.ts` hits the Keboola Query Service; ours hits the Agnes REST surface — same helper shape, different endpoint.)

The general skill (`dataapp-development`) still owns the `keboola-config/` contract, port wiring, storage-access reference, styling, and troubleshooting — the extras skill never re-derives those.

## 5. Broker + MCP tools (the deploy channel)

KAI's LLM never runs platform operations directly — it calls MCP tools that the harness executes with the real credentials. Agnes mirrors this with the **broker-ticket pattern** already used by the chat stack (`app/api/broker.py`): the sandboxed agent holds a scoped ticket, the app replays onto `/api/data-apps` under the minted user identity. Waves 1+2 shipped the read/deploy tools; wave 3 adds the draft/mode/preview set.

**KAI tool → Agnes tool** mapping:

| KAI (`mcp-server tools/data_apps.py`) | Agnes wave 3 | Notes |
|---|---|---|
| `modify_python_js_data_app` (create/update prod) | `data_app_create` / `data_app_update` | maps to `POST /api/data-apps` (exists) |
| `modify_python_js_data_app(parent_configuration_id=PROD)` (create draft) | `data_app_create_draft(slug)` | **new** — external-git draft (see §6); returns `git_clone_url` (credential-embedded) + pinned `branch` (default `init`) |
| `create_python_js_data_app_git_credential` | `data_app_mint_git_credential(slug)` | **new** — mints a fresh prod-side HTTPS git token (edit / continue-draft / lost-token recovery) |
| `deploy_data_app(mode='dev')` | `data_app_deploy(slug, mode='dev')` | **extend** existing deploy with a `mode` param — dev deploy serves the draft's pinned branch |
| `deploy_data_app(configuration_id=PROD)` (no mode) | `data_app_deploy(slug)` | prod redeploy from `main` (≈ existing deploy) |
| `delete_python_js_data_app_draft` | `data_app_delete_draft(slug)` | **new** — tears down a draft; refuses prod |
| `get_data_apps([PROD])` → prod + inline `drafts:[…]` + latest log lines | `data_app_get(slug)` | **extend** to inline `drafts` + a terminal-log tail (exists for logs; fold in) |
| `ui_data_app_preview` / `ui_refresh_data_app_preview` / `ui_close_data_app_preview` | `agnes_data_app_preview` / `…_refresh` / `…_close` | **new** chat-UI tools (§7) |
| `ui_data_app_credentials` | `agnes_data_app_credentials` | **new** — renders the shareable URL + password box as the final message element |

The parameter semantics for the reused verbs come from the KAI tool docstrings (draft create returns the credential-embedded clone URL; credential mint is against the PROD config; `mode` selects dev vs prod tree) — Agnes replicates the *contract*, not the Keboola-platform internals.

## 6. Prod + draft model on Agnes

KAI's iteration model (from `apps/kai-agent/docs/data-apps-architecture.md`): each project has **one persistent prod app** that owns the only managed git repo, and **zero-or-more short-lived drafts**. A draft is an *external-git* app whose config points the runtime at the **prod's** repo with a prod-issued HTTPS token, pinned to an iteration branch, carrying `isDraft=true` + `parentConfigurationId=<prod>`. The agent iterates on the draft (`deploy mode=dev`), then merges the branch into `main`, redeploys prod, deletes the draft.

Agnes already has the primitives (internal bare repo per app at `/data-apps.git/<slug>`, `agnes-live` ref, deploy pipeline). Wave 3 adds the draft overlay on top of the **existing `data_apps` registry** — no second repo, no copy:

- **Prod app** = the wave-1+2 `data_apps` row (`repo_mode='internal'`, owns the bare repo). Deploy serves `agnes-live` (already fast-forwarded from `main`/the deployed SHA).
- **Draft** = a lightweight registry sibling keyed by `parent_app_id`, deployed from a **pinned iteration branch** of the *same* bare repo (not a new repo). Add columns `parent_app_id TEXT`, `is_draft BOOLEAN`, `draft_branch TEXT` to `data_apps` (v97 + Alembic, dual-backend per the parity rule). A draft's ingress is `/apps/<slug>/` like any app; its deploy uses the draft branch instead of `agnes-live`.
- **`data_app_get(prod)`** inlines `drafts: [...]` by querying `parent_app_id = <prod>` — mirrors KAI's cheap draft discovery with no separate list tool.
- **Promote** = the extras-skill git flow, executed by the agent over the internal git repo: `git checkout main && git merge <draft_branch> && git push` → `data_app_deploy(prod)` (fast-forwards `agnes-live`, redeploys) → `agnes_data_app_close` → `data_app_delete_draft`. The empty-root-`main` seeding + branch-delete-not-`main` discipline from KAI carries over unchanged (our git host is the same `git http-backend` substrate).

Draft containers are subject to the same idle-reaper/auto-sleep as any app (waves 1+2), so abandoned drafts cost nothing while asleep; `delete_draft` removes the registry row + container + branch.

## 7. Preview iframe + auth

KAI shows the running dev app in an in-chat split-pane iframe, authenticated by an apps-proxy-minted `kai-preview` cookie layered over the app's basic-auth. Agnes mirrors this:

- **`agnes_data_app_preview(slug, url)`** — the chat frontend renders a split-pane iframe pointing at the draft's `/apps/<slug>/` (dev deploy). The first call (no `url`) opens the placeholder immediately (system-prompt-mandated, as in KAI); the follow-up (`url`) swaps to the live app once the real code is healthy.
- **Auth for the iframe** — the preview must load `/apps/<slug>/` without the viewer needing a separate login. Reuse the wave-1+2 ingress: the chat session already authenticates the user; mint a **scoped preview grant** (short-TTL, `data_app:<slug>`, view-only) so the iframe's requests pass the proxy's RBAC gate. This replaces KAI's cookie-over-basic-auth with Agnes's own session+grant model — no basic-auth needed because the app is private-by-default behind Agnes auth already.
- **`agnes_data_app_refresh(slug)`** — force-reload the iframe (mandatory after a dependency push or a `mode=dev` redeploy; not for ordinary `src/**`/`server/**` edits, which HMR/`tsx watch` cover).
- **`agnes_data_app_close(slug)`** — close the pane; called **before** `delete_draft` so the iframe never points at a deleted app (KAI's ordering rule).
- **`agnes_data_app_credentials(app_id)`** — renders the shareable app URL (+ password if the owner set basic-auth for external sharing) as the **final** element of the reply, no text after it. For Agnes-internal sharing this is just the `/apps/<slug>/` URL + a "grant a group in /admin/access" hint; for public sharing it mirrors KAI's credentials box.

The preview tools are chat-surface tools (rendered by the Agnes web chat), not broker/data-plane tools — they carry no Keboola-platform coupling.

## 8. What waves 1+2 already satisfy (no rebuild)

- Runtime substrate, `keboola-config/` contract, ingress proxy, wake/auto-sleep, credential-strip — done.
- Internal git hosting + `agnes-live` + push-to-deploy — done (draft branches ride the same repo).
- `/api/data-apps` create/deploy/stop/delete/logs/readiness + RBAC + audit — done.
- `data_apps_list/get/deploy/logs` MCP tools + `agnes app` CLI — done (extend get/deploy; add the draft/credential/preview tools).
- Owner-scoped service token injected as `AGNES_TOKEN` — done (the scaffold's `agnesQuery.ts` uses it).

## 9. Surfaces & parity

Per the command-UX + REST×CLI×MCP ratchet: every new `/api/data-apps` verb (draft create, mint-credential, deploy-mode, delete-draft) gets a CLI (`agnes app draft …`, `agnes app deploy --mode dev`) and an MCP tool, and the triple-surface test stays green. The preview tools are chat-only (documented exception, like other chat-surface tools). CHANGELOG bullet under Added; docs: extend `docs/DEPLOYMENT.md` "Data apps" with the authoring flow and a pointer to the ai-kit skill.

## 10. Testing

- Registry v97 draft columns: dual-backend contract test.
- Draft lifecycle: create-draft → deploy dev → promote (merge/redeploy/delete) → prod serves merged code; abandoned draft reaped; `get(prod)` inlines drafts.
- Broker tools: scoped-ticket auth on the new verbs; draft-credential mint doesn't invalidate prior tokens.
- Preview auth: scoped preview grant lets the iframe load `/apps/<slug>/`; expires; a stranger still 403s.
- Skill/scaffold: the bundled `agnes-data-apps-extras` appears in `GET /api/chat/skills`; the baked scaffold deploys `mode=dev` to a healthy container against the real runtime image (extends the wave-1+2 docker E2E).
- End-to-end (the wave-3 acceptance): a chat request "build me a dashboard over table X" → scaffold → dev deploy → real code → healthy preview → user approves → promote → prod serves it, with zero human shell access.

## 11. Rollout

| Wave | Scope | Exit criterion |
|---|---|---|
| **3A — reuse + Path D** | register `dataapp-developer` in the Agnes marketplace (RBAC-grant); upstream Path D into `keboola/ai-kit` `deployment-paths.md` + detection | an analyst in Agnes Claude Code gets the `dataapp-development` skill and, told to deploy, is routed to the Agnes tools |
| **3B — draft model + tools** | v97 draft columns; broker/MCP draft/mode/credential/delete-draft tools; `get` inlines drafts | agent can create a draft, deploy dev, promote to prod over the internal git repo, all via tools |
| **3C — extras skill + preview** | bundle `agnes-data-apps-extras`; bake the scaffold; preview chat tools + scoped preview grant; visual-quality voice | the end-to-end acceptance in §10 passes from a live chat session |
| **3D — polish** | credentials box for external sharing; Streamlit path parity; migrate-from-Streamlit helper | — |

## 12. Open questions

1. **Marketplace registration** — is the public `keboola/ai-kit` `dataapp-developer` plugin already registered in the Agnes instances, or only `ai-kit-internal` / `cf-claude-code-kit`? 3A starts with registration if not. (Operator-verifiable on the live VMs; does not block design.)
2. **Upstream vs. overlay for Path D** — contribute Path D to `keboola/ai-kit` (preferred; shared skill, benefits KAI's own multi-harness detection) vs. ship it as an Agnes-only overlay reference in the extras skill. Proposed: upstream PR, with the extras skill carrying a short pointer until it merges.
3. **Draft as registry sibling vs. config flag** — §6 models a draft as a `data_apps` row with `parent_app_id`. Alternative: a single row with an inline draft sub-state. The sibling-row approach reuses the existing ingress/reaper/RBAC per-slug machinery unchanged; confirm it doesn't inflate `/apps` listings (filter `is_draft` from the human-facing list, like KAI hides drafts from the Data Apps list).
4. **Preview auth TTL** — the scoped preview grant's lifetime vs. a long iteration session; renew on each `agnes_data_app_preview` call, or tie it to the chat session's lifetime.
