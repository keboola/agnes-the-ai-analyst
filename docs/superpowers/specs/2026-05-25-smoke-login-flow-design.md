# E2E nightly smoke — sign-in flow design

Date: 2026-05-25
Issue: [#417](https://github.com/keboola/agnes-the-ai-analyst/issues/417)
Follows: [#389](https://github.com/keboola/agnes-the-ai-analyst/pull/389) (root-cause fix that uncovered this)

## Problem

The agent-browser smoke scripts under `scripts/e2e/` open `/catalog` and
`/admin/activity` and grep their snapshots for expected UI markers. But the
app's global 401 handler (`app/main.py:898-907`) redirects unauthenticated HTML
GETs to `/login?next=…`, so the smoke scripts land on the login page and fail
their next assertion. This was masked for weeks because the workflow died
earlier on a missing `.env`; #389 unblocked that and surfaced this.

## Approach

Smoke scripts sign in via the existing email/password provider before
navigating to protected pages. No production code path is touched. Three
alternatives were considered (see _Alternatives considered_ at the end);
sign-in-flow was chosen over an env-gated auth bypass to keep prod code paths
unchanged and to exercise the real auth surface end-to-end.

## Components

### 1. E2E seed user

- Email: `e2e@example.com`
- Password: hardcoded dev-only — `E2eSmokePass!`
- Membership: `Admin` group (god-mode short-circuit per `docs/RBAC.md`, so
  `/admin/activity` and any future protected page is reachable without
  per-resource grant maintenance).
- Lifetime: only ever exists in the ephemeral CI container (`docker compose
  down -v` between every run wipes it). No external exposure.

Hardcoded credentials are acceptable because the user has zero privileges
outside the throwaway container, and committing them lets a local developer
reproduce the smoke setup byte-for-byte. No GitHub secret involved.

### 2. Seed script — `scripts/seed_e2e_user.py`

Idempotent — running twice is a no-op (creates user only if absent, ensures
Admin membership only if missing). Uses `UserRepository` + `UserGroupsRepository`
via the same DuckDB connection the app uses. Exits 0 on success, non-zero with
a clear stderr message on failure.

Invoked from the workflow via:

    docker compose exec -T app python -m scripts.seed_e2e_user

The `-T` disables TTY (no pty in CI). Local developers run the same command
before invoking a smoke script.

### 3. Login helper — `scripts/e2e/_login.sh`

Sourced by both `smoke_catalog.sh` and `smoke_admin_activity.sh` _after_ the
`SESSION` variable is exported and _before_ any `agent-browser open` against a
protected URL.

    agent-browser --session "$SESSION" open "${BASE_URL}/login"
    agent-browser --session "$SESSION" fill 'input[name=email]' 'e2e@example.com'
    agent-browser --session "$SESSION" fill 'input[name=password]' 'E2eSmokePass!'
    agent-browser --session "$SESSION" click 'button[type=submit]'
    agent-browser --session "$SESSION" wait --load networkidle

The agent-browser session naturally inherits the cookie set by the form POST —
no manual cookie injection needed. Selectors (`input[name=email]`,
`input[name=password]`, `button[type=submit]`) are stable because they map to
`app/web/templates/login_email.html:26-46`, which uses the actual form-field
names the password provider reads (`name="email"`, `name="password"` —
verified in source).

If a future redesign of the login form breaks these selectors, that's the
right time to discover it: login is a critical surface, and a smoke failure
that points at the login step is far easier to diagnose than a downstream
assertion against half-rendered content.

### 4. Workflow changes — `.github/workflows/e2e-nightly.yml`

Insert one step between "Build + start agnes stack" and "Run smoke":

    - name: Seed E2E test user
      if: env.SKIP_MATRIX != '1'
      run: docker compose exec -T app python -m scripts.seed_e2e_user

No new env vars, no new secrets, no rerun-loop changes.

### 5. Smoke script changes

Each `scripts/e2e/smoke_*.sh` adds a single line after `SESSION=...` /
`trap ...`:

    source "$(dirname "$0")/_login.sh"

Everything below that line is unchanged.

### 6. Local-dev documentation

`scripts/e2e/README.md` gets a "Prerequisites" section noting the seed
command must run once after `docker compose up` and before the first
smoke script.

## Failure modes

| Failure | Symptom | Diagnosis |
|---|---|---|
| Seed script fails (DB locked, migration mismatch) | Workflow stops before "Run smoke" | Step log shows the exact Python traceback. |
| Login form selectors change | `agent-browser fill 'input[name=email]'` exits non-zero | `set -euo pipefail` in smoke script propagates; failure points at the login step, not at a downstream assertion. |
| Login cookie expires mid-script | A later `agent-browser open` redirects to `/login` | Existing snapshot-assert behavior — clear failure message. |
| `docker compose exec` finds no `app` service | Workflow stops at seed step | Step log shows compose error. The new `Dump docker logs on stack failure` step from #389 runs (if `failure()` triggers). |

## Non-goals

- Testing the login flow itself (covered by `tests/test_auth_*`).
- Multi-user smoke scenarios (one Admin user is enough for current smoke
  coverage).
- Testing OAuth / Google / magic-link flows.
- Persistent test data — every nightly run starts from a clean container.

## Alternatives considered

**Env-gated auth bypass** (`AGNES_E2E_BYPASS_AUTH=1` in 401 handler).
Smaller diff (1 file in `app/`), zero smoke-script changes. Rejected because
it adds a production code path that bypasses auth — even with hard env-gate
+ boot warning, reviewers (Devin/humans) understandably balk, and a
misconfigured `.env` in any deployment that picks the env var up would be a
security incident. The sign-in path tests the real auth surface and has no
prod code path at all.

**Dev-VM target** (run smoke against a long-running deployed instance with a
prior auth token). Rejected because (a) couples OSS repo CI to private
customer infra, violating the "vendor-agnostic OSS" rule in `CLAUDE.md`,
(b) shared mutable env produces flaky/non-reproducible failures, (c)
introduces a secret-rotation chore.

## Acceptance

- Nightly workflow on `main`: `gh workflow run e2e-nightly.yml` → both smoke
  jobs succeed.
- Run locally after `docker compose up` + seed: both smoke scripts pass.
- Removing the seed step: both smoke scripts fail at the login step with a
  clear `Invalid email or password` artifact in the screenshot.
