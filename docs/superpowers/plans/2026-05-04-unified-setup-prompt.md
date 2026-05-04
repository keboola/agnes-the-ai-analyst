# Unified `/setup` Prompt — Implementation Plan

**Branch:** `zs/clean-analyst-bootstrap-spec` (PR #173)
**Goal:** Collapse the dual admin/analyst bash setup-prompt architecture into a single unified flow that's RBAC-resolved per user.

## Summary of chosen approach

Collapse `_resolve_analyst_lines` and the admin layout in `app/web/setup_instructions.py` into a single `resolve_lines()` whose content is gated by booleans (`has_marketplace`, `has_skills`, `has_ca`). `agnes init` becomes mandatory for everyone (the workspace-rails delivery mechanism), so the unified flow always emits: TLS trust → install CLI → `agnes init` → preflight (`git --version` + `claude --version`) → marketplace/plugins (iff `plugin_install_names` non-empty) → skills (always emits) → diagnose → confirm. RBAC resolution stays in `compute_default_agent_prompt`, but unconditionally — admin/non-admin alike pass through `resolve_allowed_plugins`; only users with grants get the marketplace block. PAT scope is unified to `general` 90 d for ALL callers (no scope-by-role split — see decision below). The `?role=` query param and admin tile both go away. The `welcome_template` admin override remains a single text blob (no DB schema change, no role-aware UI affordance).

## PAT scope decision — uniform `general` 90d for everyone, NO new endpoint

Original plan proposed a new `POST /auth/tokens/issue-for-setup` endpoint that would inspect `user.is_admin` and mint `general`/90d for admins, `bootstrap-analyst`/1h for non-admins. After review:

- **No real security benefit**: non-admin users can ALREADY mint their own `general` 90d PATs via the existing `POST /auth/tokens` route from the `/tokens` UI. There's no admin gate on `general` scope. So routing the install button through a "role-locked" endpoint is ceremony without security value.

- **Bootstrap-analyst 1h scope is broken for the install flow**: an analyst pasting the prompt at 10:00 mints a PAT expiring at 11:00. They run `agnes init` at 10:30 (saves PAT to `~/.config/agnes/token.json`). At 11:30 they open Claude Code; SessionStart hook runs `agnes pull` → 401 because the saved PAT expired. `agnes init` does not re-mint a long-lived token internally. So bootstrap-analyst PATs are effectively single-use-then-broken in this flow.

Decision: install button mints `general` scope, `expires_in_days=90` for everyone. Single `fetch('/auth/tokens', { method: 'POST', body: JSON.stringify({ name: 'agnes-install-...', expires_in_days: 90 }) })` in JS. The `bootstrap-analyst` scope + clamp logic stays in the codebase (still useful for future flows, e.g. a one-shot CI bootstrap), just not invoked from `/setup`. Tracked as separate cleanup issue: redesign or retire `bootstrap-analyst` scope.

## Legacy `?role=admin` URL — no redirect needed

`?role=` query parameter was introduced in this PR (not on `main`). No production URLs reference it; bookmarks/runbooks don't exist yet. Just remove the param from the route signature; no `RedirectResponse` shim required.

## Tasks

### Task 1 — Drop `role` parameter from `setup_instructions.resolve_lines`
**Files:** `app/web/setup_instructions.py`
**What:** Remove `role: Literal["analyst", "admin"]` parameter from `resolve_lines` and `render_setup_instructions`. Delete `_resolve_analyst_lines`, `_analyst_init_lines`, `_analyst_finale_lines` entirely. Move `agnes init` step into a new helper `_init_lines(server_url_placeholder)` that always emits. Reuse `_finale_lines` (no parallel analyst version once layouts merge).
**Tests deleted:** `tests/test_setup_instructions_analyst.py` (entire file).
**Tests rewritten:** `tests/test_setup_instructions.py` — drop `role=` kwarg from any `resolve_lines(...)` calls; update admin layout assertions for unified numbering (Task 3).
**Commit:** `refactor(setup-instructions): drop role param; collapse analyst/admin into one layout`
**LOC budget:** ~150 (deletion-heavy).

### Task 2 — Adopt unified step layout
**Files:** `app/web/setup_instructions.py`
**What:** Recompose `resolve_lines` to always emit:
- 0 (optional): TLS trust block
- 1: Install CLI
- 2: `agnes init --server-url ... --token ...` (NEW position — was admin's step 2/3 login+verify; analyst's step 2-3 init+catalog)
- 3: `agnes catalog` smoke verify (drop the admin-only `agnes auth whoami` — `agnes init` already verifies the PAT against `/api/catalog/tables`)
- 4 (iff has_marketplace): Pre-flight: `git --version` AND `claude --version` (Task 4)
- 5 (iff has_marketplace): Marketplace + plugins
- 6: Diagnose
- 7 (iff has_skills, default True): Skills
- 8 (or earliest): Confirm

Renumbering helper: `_step_numbers(*, has_marketplace, has_skills)` returns the dict so helpers don't reimplement renumber logic. Update `_finale_lines` bullets to be conditional on `has_marketplace`/`has_skills`/`has_ca`.
**Tests rewritten:** `tests/test_setup_instructions.py::test_resolve_lines_no_plugins_keeps_six_step_layout` — rename + update assertions. Unified no-plugin layout: 1 install, 2 init, 3 catalog, 4 diagnose, 5 skills, 6 confirm. With plugins: 1, 2, 3, 4 preflight, 5 marketplace, 6 diagnose, 7 skills, 8 confirm.
**Commit:** `refactor(setup-instructions): unified layout with mandatory agnes init`
**LOC budget:** ~120.

### Task 3 — Add `claude --version` to the pre-flight check
**Files:** `app/web/setup_instructions.py` (`_git_check_block` → `_preflight_block`)
**What:** Rename `_git_check_block` to `_preflight_block`. Inside, after `git --version`, add `claude --version || { ... install hint ... }`. Install hint: `npm i -g @anthropic-ai/claude-code` or directs the user to the platform installer (link to `https://docs.claude.com/claude-code`). Keep section header "Make sure git and claude are installed (required for the marketplace clone)". Step number stays parameterized.
**Tests added:** `tests/test_setup_instructions.py::test_preflight_checks_both_git_and_claude`.
**Commit:** `feat(setup-instructions): preflight checks both git and claude`
**LOC budget:** ~40.

### Task 4 — Drop `role` from `compute_default_agent_prompt`; resolve plugins unconditionally
**Files:** `src/welcome_template.py`
**What:** Remove `role: Literal["analyst", "admin"] = "admin"` parameter from `compute_default_agent_prompt`. Always run `marketplace_filter.resolve_allowed_plugins(conn, user)` (currently gated on `role == "admin"`). Function returns `[]` for users with no grants — that's already the analyst case. Remove `role` from `render_agent_prompt_banner`'s tail (the `role = "admin" if user.is_admin else "analyst"` block deletes entirely).
**Tests rewritten:** `tests/test_welcome_template_renderer.py` — drop role-aware distinction. Update assertions to reflect unified output: `agnes init` always present, `agnes auth import-token` never present (replaced by init), `claude plugin marketplace add` only when caller has plugin grants.
**Commit:** `refactor(welcome-template): drop role param; resolve plugins per-user unconditionally`
**LOC budget:** ~60.

### Task 5 — Strip `?role=` from `/setup` route; remove silent admin-downgrade
**Files:** `app/web/router.py`
**What:** Remove `role: Literal["analyst", "admin"] = Query(default="analyst")` from `setup_page`. Delete silent-downgrade block. Drop `role` from `compute_default_agent_prompt(...)` calls. Drop `role` from template ctx. **No redirect needed** — `?role=` was introduced in this PR, no existing URLs reference it.
**Tests deleted:** `tests/test_setup_page_roles.py` — entire file.
**Tests added:** `tests/test_setup_page_unified.py` — two small tests: `test_setup_page_renders_unified_layout`, `test_setup_page_renders_marketplace_for_user_with_grants`.
**Commit:** `refactor(setup-page): drop role query param`
**LOC budget:** ~70.

### Task 6 — Drop the admin tile and JS scope ternary from `install.html`
**Files:** `app/web/templates/install.html`
**What:** Delete role-tiles `<nav>` block. Drop `_show_admin_tile` flag. Delete `const ROLE = {{ role | tojson }};` line. Replace `tokenBody = ROLE === "analyst" ? {scope: "bootstrap-analyst", ttl_seconds: 3600} : {expires_in_days: 90}` ternary with a single body: `{name: defaultTokenName(), expires_in_days: 90}`. Continues to call existing `POST /auth/tokens` endpoint — no new endpoint needed (see PAT scope decision above). Keep "Valid 90 days" copy as-is (true for everyone now).
**Tests rewritten:** `tests/test_web_ui.py::test_install_preview_*` — drop `?role=admin` from URLs; admin caller now sees unified layout. `tests/test_setup_page_roles.py::test_setup_page_analyst_js_uses_bootstrap_scope` and `test_setup_page_admin_js_uses_general_scope` are deleted as part of Task 5.
**Commit:** `refactor(install.html): single tile, single PAT-mint body shape`
**LOC budget:** ~50.

### Task 7 — Audit `_build_context` and dashboard-CTA path
**Files:** `app/web/router.py` (`_build_context`)
**What:** `_build_context` calls `render_agent_prompt_banner(conn, user=user, server_url=ctx_server_url)` already. Once `render_agent_prompt_banner` is role-free (Task 4), this just works. Verify the no-conn fallback path still works: passes `plugin_install_names=[]`, anonymous visitors see no-marketplace shape — same as today. **Audit only; if no edits needed, skip the commit.**
**LOC budget:** 0 net (audit only).

### Task 8 — Delete dead test infrastructure
**Files:** `tests/test_setup_instructions_analyst.py` (delete), `tests/test_setup_page_roles.py` (delete)
**What:** Confirm no other tests `import` from these modules. If `tests/fixtures/analyst_bootstrap.py` references analyst-specific paths, audit and update.
**Commit:** `chore(tests): drop split-flow test files; covered by unified suite`
**LOC budget:** -358 (file deletions).

### Task 9 — CHANGELOG entry under `## [Unreleased]`
**Files:** `CHANGELOG.md`
**What:** Add bullets:
- Under `### Changed`: `**BREAKING** /setup is now a single unified flow regardless of caller's role. The ?role= query parameter (introduced in this PR) is removed before merge — no migration needed. The admin tile is gone. PAT scope is uniform: every install-page mint uses scope=general with expires_in_days=90, calling the existing POST /auth/tokens endpoint. The bootstrap-analyst 1h-clamped scope is no longer used from /setup (see open issue for redesign). The marketplace + plugins block is emitted only when the caller has plugin grants in resource_grants. agnes init is now part of every setup flow (admin and analyst alike) — it's the workspace-rails delivery mechanism.`
- Under `### Added`: pre-flight check now verifies `claude --version` in addition to `git --version`.
- Under `### Removed`: `_resolve_analyst_lines` helper, `role` parameter on `compute_default_agent_prompt` and `resolve_lines`, `?role=` query param on `/setup`, admin tile in `install.html`.
**Commit:** `docs(changelog): unified /setup flow under Unreleased`
**LOC budget:** ~30.

### Task 10 — Final smoke test + invariant pin
**Files:** none (verification) + `tests/test_setup_instructions.py`
**What:** Verify orthogonal commits NOT regressed:
- `agnes init --token` ContextVar override (commit `8784f10a`) — confirm unified flow's emitted `agnes init` line still passes `--token`.
- Sub-agent's stale-`da` cleanup (commit `8233c3e3`) — verify unified prompt has no `da` verbs.
**Tests added:** `tests/test_setup_instructions.py::test_unified_flow_uses_only_agnes_verbs` — `assert "da " not in resolve_lines(...)` (with space delimiter to avoid false-positive on `Darwin`/`adapter`).
**Commit:** `test(setup-instructions): pin no-legacy-da-verbs invariant`
**LOC budget:** ~25.

## Test impact summary

| File | Action | Reason |
|---|---|---|
| `tests/test_setup_instructions_analyst.py` | **DELETE (81 LOC)** | Dual-layout assertions; unified path makes them moot |
| `tests/test_setup_page_roles.py` | **DELETE (277 LOC)** | All eight tests assert role-branching that's gone |
| `tests/test_setup_instructions.py` | **REWRITE** | Drop `role=` kwargs; update step-number assertions; add preflight + no-da-verbs tests |
| `tests/test_welcome_template_renderer.py` | **REWRITE** | Drop role-aware tests; assert unified default with conditional marketplace |
| `tests/test_welcome_template_api.py` | **NO CHANGE** | API surface unchanged |
| `tests/test_tokens_bootstrap_scope.py` | **NO CHANGE** | Underlying clamp logic preserved (no longer used from /setup, but kept for future reuse) |
| `tests/test_setup_page_unified.py` | **NEW** | Cover single tile, no `?role=` param |
| `tests/test_web_ui.py::test_install_preview_*` | **REWRITE** | Drop `?role=admin` from URLs |

## Resolved questions

All five open questions from the original Plan-agent draft have been resolved:
1. **Skills always-on** — yes, no `has_skills` boolean.
2. **`agnes init` workspace dir guard** — out of scope; user opted to drop. Documented assumption is "paste in your workspace dir" (no enforcement).
3. **PAT mint endpoint** — no new endpoint; uniform `general` 90 d for everyone via existing `/auth/tokens` (see PAT scope decision section).
4. **`?role=admin` redirect** — moot, `?role=` introduced in this PR, no production URLs to migrate.
5. **Admin override copy** — no doc note; admin/analyst split deferred entirely (the codebase no longer encourages role-split UX).

## Out-of-scope follow-ups (file as separate issues after merge)

- Bootstrap-analyst scope is now unused from `/setup`. Either retire it or fix the design hole (1 h clamp breaks `agnes pull` after the install window). Tracked as separate issue.
- Workspace-dir guard in `agnes init` — refuse-to-clobber-non-empty-home heuristic. Orthogonal to setup prompt.
