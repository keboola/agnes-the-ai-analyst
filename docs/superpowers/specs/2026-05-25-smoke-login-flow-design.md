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
- Membership: `Admin` group (god-mode short-circuit per
  `src/repositories/user_groups.py:4` + `docs/RBAC.md`), so `/admin/activity`
  and any future protected HTML page is reachable without per-resource grant
  maintenance.
- Lifetime: only ever exists in dev images. The CI container is
  `docker compose down -v`-ed at the end of every nightly run, wiping the
  whole DB; outside the container the credentials grant access to nothing.

Hardcoding the password in the repo is acceptable because **the container is
the privilege boundary** — the user is Admin inside it, nothing outside it.
Committing the password (instead of stashing it in a GitHub secret) lets a
local developer reproduce the smoke setup byte-for-byte.

### 2. Seed script — `scripts/seed_e2e_user.py`

Idempotent re-runnable. Direct invocation (not `-m`) — matches the existing
`scripts/seed_corporate_memory.py` / `seed_dummy_tables.py` precedent;
`scripts/` is intentionally not a Python package.

    docker compose exec -T app python scripts/seed_e2e_user.py

Idempotency rules:

- **User absent** → create with the hashed password and Admin membership.
- **User present with same email** → overwrite the `password_hash` to the
  hardcoded value (so a stale local-dev row with a forgotten password doesn't
  leave the smoke broken). Re-assert Admin membership.
- **`Admin` group missing** (DB in a half-init state — should never happen
  on a healthy image, but defensive) → exit non-zero with a clear stderr
  message like `Admin group not seeded — refusing to create orphan user`. No
  silent broken state.

Exit codes: `0` on success (user existed or was created), `1` on
unrecoverable error.

Uses `src.repositories.users.UserRepository` for the user row and
`src.repositories.user_groups.UserGroupsRepository` for the Admin membership
(both verified — see file:line refs above). Password hashing via the same
`argon2-cffi` `PasswordHasher` used by `app/auth/providers/password.py`.

### 3. Login helper — `scripts/e2e/_login.sh`

Sourced by both `smoke_catalog.sh` and `smoke_admin_activity.sh` _after_ the
`SESSION` variable is exported and _before_ any `agent-browser open` against a
protected URL.

Target URL is `/login/password` (NOT `/login` — `/login` is a dispatcher per
`app/web/router.py:539-587` that redirects to whichever provider is
configured; the password form is at `/login/password`, served by
`app/web/router.py:596` which renders `login_email.html`).

All selectors scoped to the form's action attribute, which uniquely identifies
the Sign-In form among the three nested forms in `#signin-tab` (Sign In,
Forgot Password) and the sibling `#signup-tab`:

    LOGIN_FORM='form[action="/auth/password/login/web"]'

    agent-browser --session "$SESSION" open "${BASE_URL}/login/password"
    agent-browser --session "$SESSION" fill "${LOGIN_FORM} input[name=email]"    'e2e@example.com'
    agent-browser --session "$SESSION" fill "${LOGIN_FORM} input[name=password]" 'E2eSmokePass!'
    agent-browser --session "$SESSION" click "${LOGIN_FORM} button[type=submit]"
    agent-browser --session "$SESSION" wait --load networkidle

Why scope by form action: `login_email.html:26-46` shows the Sign-In `<form>`
with `action="/auth/password/login/web"`, sibling Forgot-Password `<form>`
with `action="/auth/password/reset"`, and presumably a Sign-Up `<form>` in
`#signup-tab`. Unscoped `button[type=submit]` would match multiple submits and
the click would be non-deterministic. The form action is the stable contract
between template and password router (`password.py:229`) — if it changes,
both ends update together.

The agent-browser session naturally inherits the `Set-Cookie` from the form
POST — no manual cookie injection needed.

### 4. Workflow changes — `.github/workflows/e2e-nightly.yml`

Insert one step between "Build + start agnes stack" and "Run smoke":

    - name: Seed E2E test user
      if: env.SKIP_MATRIX != '1'
      run: docker compose exec -T app python scripts/seed_e2e_user.py

No new env vars, no new secrets, no rerun-loop changes.

### 5. Smoke script changes

Each `scripts/e2e/smoke_*.sh` adds a single line after `SESSION=...` /
`trap ...`:

    source "$(dirname "$0")/_login.sh"

Everything below that line is unchanged.

### 6. Local-dev documentation

`scripts/e2e/README.md` gets a "Prerequisites" section noting that after
`docker compose up` the operator must run the seed once:

    docker compose exec -T app python scripts/seed_e2e_user.py

…before any `bash scripts/e2e/smoke_*.sh`.

### 7. CHANGELOG + release-cut

- `### Internal` bullet under `[Unreleased]` summarizing the seed user +
  sign-in helper, with explicit "credentials are hardcoded; container is the
  privilege boundary" justification so future readers don't think it's a
  security regression.
- Per the CLAUDE.md release-cut rule, the patch bump
  `0.55.9 → 0.55.10` (pyproject.toml + CHANGELOG rename + new empty
  `[Unreleased]`) ships in the final commit of the same PR.

## Failure modes

| Failure | Symptom | Diagnosis |
|---|---|---|
| Seed script fails (DB locked, migration mismatch, missing Admin group) | Workflow stops before "Run smoke" | Step log shows the exact Python traceback / stderr message. |
| Login form selectors change | `agent-browser fill 'form[...] input[name=email]'` exits non-zero | `set -euo pipefail` in smoke script propagates; failure points at the login step, not at a downstream assertion. |
| Login click succeeds but cookie not set (form rejected — e.g. seed step skipped or password drift) | `agent-browser open /catalog` follows the 401 redirect to `/login?next=/catalog`; `grep -qi 'Browse'` against the snapshot fails | Screenshot shows the login page, snapshot lacks "Browse"; clear hint to inspect the seed step / login click logs above. |
| Cookie expires mid-script | Same as above, mid-run | Same as above. |
| `docker compose exec` finds no `app` service | Workflow stops at seed step | Step log shows compose error. The "Dump docker logs on stack failure" step from #389 fires. |

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

1. Nightly workflow on `main`: `gh workflow run e2e-nightly.yml` → both smoke
   jobs succeed.
2. Run locally after `docker compose up` + seed: both smoke scripts pass.
3. Skipping the seed step (or seeding with wrong password): smoke fails at
   the `/catalog` `Browse` snapshot assert because the session never got a
   cookie; the artifact screenshot shows the login page. Failure log clearly
   points at the missing seed step.
4. Running the seed twice in a row: second invocation is a no-op (no
   duplicate user row, no error).

---

## Amendment 2026-05-25 — DuckDB lock + env-gate

First end-to-end dispatch on the PR branch failed in the seed step with
`_duckdb.IOException: IO Error: Could not set lock on file
"/data/state/system.duckdb": Conflicting lock is held in
/usr/local/bin/python3.13 (PID 1)`.

The spec's claim that DuckDB's cooperative file lock would block the seed
until the app writer was idle was wrong — DuckDB enforces exclusive POSIX
locks per file, not cooperative ones. `docker compose exec` runs a second
Python process inside the same container; that process cannot open the DB
because uvicorn (PID 1) is holding the writer lock.

The actual workflow recipe shipped is:

```yaml
- name: Seed E2E test user
  run: |
    docker compose stop app
    docker compose run --rm -T -e AGNES_E2E_SEED=1 app python scripts/seed_e2e_user.py
    docker compose start app
    timeout 60 sh -c 'until curl -fsS http://localhost:8000/api/health >/dev/null 2>&1; do sleep 2; done'
```

`stop` releases the lock; `compose run --rm` is a fresh container sharing
the same `data:/data` volume — it acquires the lock briefly, seeds, exits;
`start` brings the app back and we poll `/api/health` until ready.

In the same review pass:

- The seed script gained an `AGNES_E2E_SEED=1` opt-in env-gate. The seed
  module ships in the production image via `COPY . .`; without the gate,
  `docker exec` on a prod container could mint an Admin user with the
  committed password. The container-as-privilege-boundary justification
  still holds for the CI run (where the env var is set explicitly), but
  the gate documents the invariant in code and removes the
  accidental-invocation footgun.
- `scripts/e2e/_login.sh` no longer hardcodes the credentials. The
  workflow's "Export E2E credentials" step imports them from
  `scripts/seed_e2e_user.py` constants via `docker compose exec ... python
  -c '...'` and writes them to `$GITHUB_ENV`. Single source of truth, so
  the seed and the smoke helper cannot drift.
- The bare `except (VerifyMismatchError, Exception)` in the
  verify-before-rehash path narrowed to `except VerifyMismatchError`. DB
  errors and library version mismatches now propagate instead of silently
  triggering a rehash + UPDATE.
- New regression test `tests/test_login_form_action.py` pins the literal
  `action="/auth/password/login/web"` in `login_email.html` — the smoke
  helper's selector and the template can't drift apart without a CI
  failure.
