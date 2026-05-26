# Smoke sign-in flow (#417) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unblock the agent-browser nightly smoke (#417) by signing the smoke session in via the existing email/password provider before navigating to protected pages.

**Architecture:** Idempotent Python seed script creates a hardcoded `e2e@example.com` Admin user; a sourced bash helper logs the agent-browser session in via the same `/auth/password/login/web` endpoint a human would; nightly workflow runs the seed between "Build + start agnes stack" and "Run smoke". No production code path is touched.

**Tech Stack:** Python 3.12 / DuckDB / argon2-cffi (existing repo deps) for the seed script; bash + agent-browser CLI for the login helper; GitHub Actions YAML for workflow wiring.

**Spec:** `docs/superpowers/specs/2026-05-25-smoke-login-flow-design.md`

---

## File map

| Action | Path | Responsibility |
|---|---|---|
| Create | `scripts/seed_e2e_user.py` | Idempotent CLI: create the e2e user with Admin membership; overwrite password hash on re-run; refuse if Admin group missing |
| Create | `tests/test_seed_e2e_user.py` | Unit coverage for seed: fresh-create, idempotent re-run, Admin-missing refusal |
| Create | `scripts/e2e/_login.sh` | Sourced helper: open `/login/password`, fill, click submit, wait for navigation |
| Modify | `scripts/e2e/smoke_catalog.sh` | One added line: `source "$(dirname "$0")/_login.sh"` |
| Modify | `scripts/e2e/smoke_admin_activity.sh` | Same one line |
| Modify | `scripts/e2e/README.md` | "Prerequisites" section pointing at the seed command |
| Modify | `.github/workflows/e2e-nightly.yml` | One added step between "Build + start agnes stack" and "Run smoke" |
| Modify | `CHANGELOG.md` | `### Internal` bullet under `[Unreleased]`, then promote to `[0.55.10]` at the end |
| Modify | `pyproject.toml` | `version = "0.55.9"` → `"0.55.10"` (release-cut, final commit) |

---

## Constants used across multiple tasks

```python
E2E_USER_EMAIL = "e2e@example.com"
E2E_USER_NAME = "E2E Smoke Test"
E2E_USER_ID = "e2e-smoke-user"  # stable id so the same row is targeted across runs
E2E_USER_PASSWORD = "E2eSmokePass!"  # dev-only; documented in spec
```

```bash
# scripts/e2e/_login.sh constants
E2E_USER_EMAIL='e2e@example.com'
E2E_USER_PASSWORD='E2eSmokePass!'
LOGIN_FORM='form[action="/auth/password/login/web"]'
```

---

## Task 1 — Seed script (TDD)

**Files:**
- Create: `scripts/seed_e2e_user.py`
- Test: `tests/test_seed_e2e_user.py`

The seed script exposes a `seed()` function (importable for testing) and a `__main__` block (for CLI). `seed()` returns `True` on success, raises `SystemExit(1)` with a clear stderr message if Admin group is absent.

- [ ] **Step 1.1: Write the first failing test — fresh-DB happy path**

Create `tests/test_seed_e2e_user.py`:

```python
"""Idempotency + safety contract tests for scripts/seed_e2e_user.py."""

from __future__ import annotations

import sys

import pytest

# scripts/ is not a Python package; load by path
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = REPO_ROOT / "scripts" / "seed_e2e_user.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_e2e_user", SEED_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seed_module():
    return _load_seed_module()


def test_seed_creates_admin_user_on_fresh_db(e2e_env, seed_module):
    """Fresh DB → user is created with password hash + Admin membership."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    seed_module.seed()

    conn = get_system_db()
    user = UserRepository(conn).get_by_email(seed_module.E2E_USER_EMAIL)
    assert user is not None
    assert user["password_hash"], "password_hash must be set"

    admin_gid = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]

    member_ids = [
        m["user_id"]
        for m in UserGroupMembersRepository(conn).list_members(admin_gid)
    ]
    assert user["id"] in member_ids
    conn.close()
```

- [ ] **Step 1.2: Run the test, see it fail with `ModuleNotFoundError` (file doesn't exist)**

```bash
.venv/bin/pytest tests/test_seed_e2e_user.py::test_seed_creates_admin_user_on_fresh_db -v
```

Expected: `FileNotFoundError` or `ModuleNotFoundError` from `spec_from_file_location` because `scripts/seed_e2e_user.py` doesn't exist yet.

- [ ] **Step 1.3: Write minimal `scripts/seed_e2e_user.py` to make the test pass**

```python
#!/usr/bin/env python3
"""Idempotent seed for the e2e smoke test user.

Creates ``e2e@example.com`` (Admin group member) with a hardcoded
dev-only password. The user exists ONLY in dev/CI containers — the
container is the privilege boundary; see
docs/superpowers/specs/2026-05-25-smoke-login-flow-design.md.

Usage:
    python scripts/seed_e2e_user.py

Exits 0 on success (whether the user was newly created or already
existed), 1 if the system Admin group is missing (DB in half-init
state — refuses to create an orphan user).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from argon2 import PasswordHasher

E2E_USER_EMAIL = "e2e@example.com"
E2E_USER_NAME = "E2E Smoke Test"
E2E_USER_ID = "e2e-smoke-user"
E2E_USER_PASSWORD = "E2eSmokePass!"


def seed() -> bool:
    """Idempotent. Returns True on success; SystemExit(1) on missing Admin group."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        admin_row = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
        ).fetchone()
        if not admin_row:
            print(
                f"error: {SYSTEM_ADMIN_GROUP!r} group not seeded — refusing to "
                "create orphan e2e user. Run the app once so the bootstrap "
                "seeds the system groups, then re-run this script.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        admin_gid = admin_row[0]

        users = UserRepository(conn)
        memberships = UserGroupMembersRepository(conn)
        password_hash = PasswordHasher().hash(E2E_USER_PASSWORD)
        existing = users.get_by_email(E2E_USER_EMAIL)
        now = datetime.now(timezone.utc)

        if existing is None:
            users.create(
                id=E2E_USER_ID,
                email=E2E_USER_EMAIL,
                name=E2E_USER_NAME,
                password_hash=password_hash,
            )
            user_id = E2E_USER_ID
        else:
            # Overwrite hash so a stale row with a forgotten password
            # doesn't leave the smoke broken.
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                [password_hash, now, existing["id"]],
            )
            user_id = existing["id"]

        # Re-assert Admin membership (no-op if already present).
        current_members = {
            m["user_id"] for m in memberships.list_members(admin_gid)
        }
        if user_id not in current_members:
            memberships.add_member(user_id, admin_gid, source="system_seed")

        print(f"seeded: {E2E_USER_EMAIL} (id={user_id}) in Admin group")
        return True
    finally:
        conn.close()


if __name__ == "__main__":
    seed()
```

- [ ] **Step 1.4: Run the test, see it pass**

```bash
.venv/bin/pytest tests/test_seed_e2e_user.py::test_seed_creates_admin_user_on_fresh_db -v
```

Expected: PASS.

If `UserGroupMembersRepository.list_members` is named differently, fix the test + script to use the actual method (grep `def ` in `src/repositories/user_group_members.py`). If the test fails because `get_system_db()` doesn't auto-create the Admin group on first open, mirror the bootstrap path used by `tests/conftest.py::seeded_app` (it calls `get_system_db()` and Admin exists by virtue of `src.db` initialization — verify with `conn.execute("SELECT name FROM user_groups").fetchall()`).

- [ ] **Step 1.5: Add idempotency test**

Append to `tests/test_seed_e2e_user.py`:

```python
def test_seed_is_idempotent(e2e_env, seed_module):
    """Running seed twice does not duplicate the user or fail."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    seed_module.seed()
    seed_module.seed()  # must not raise, must not duplicate

    conn = get_system_db()
    matches = conn.execute(
        "SELECT COUNT(*) FROM users WHERE email = ?",
        [seed_module.E2E_USER_EMAIL],
    ).fetchone()[0]
    assert matches == 1

    admin_gid = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]
    members = UserGroupMembersRepository(conn).list_members(admin_gid)
    e2e_member_rows = [m for m in members if m["user_id"] == seed_module.E2E_USER_ID]
    assert len(e2e_member_rows) == 1
    conn.close()
```

- [ ] **Step 1.6: Run and confirm it passes**

```bash
.venv/bin/pytest tests/test_seed_e2e_user.py -v
```

Expected: both tests PASS. The implementation already covers idempotency (the `if existing is None` branch + the `if user_id not in current_members` guard).

- [ ] **Step 1.7: Add Admin-missing safety test**

Append:

```python
def test_seed_refuses_when_admin_group_missing(e2e_env, seed_module):
    """If the Admin system group is absent, seed exits 1 — never an orphan user."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.users import UserRepository

    # Drop the Admin group to simulate half-init DB
    conn = get_system_db()
    conn.execute("DELETE FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP])
    conn.close()

    with pytest.raises(SystemExit) as excinfo:
        seed_module.seed()
    assert excinfo.value.code == 1

    # And no orphan user was created.
    conn = get_system_db()
    user = UserRepository(conn).get_by_email(seed_module.E2E_USER_EMAIL)
    assert user is None
    conn.close()
```

- [ ] **Step 1.8: Run all three tests and confirm**

```bash
.venv/bin/pytest tests/test_seed_e2e_user.py -v
```

Expected: 3 passed.

- [ ] **Step 1.9: Make the script executable + commit**

```bash
chmod +x scripts/seed_e2e_user.py
git add scripts/seed_e2e_user.py tests/test_seed_e2e_user.py
git commit -m "feat(scripts): seed_e2e_user — idempotent Admin seed for smoke tests"
```

---

## Task 2 — Login helper

**Files:**
- Create: `scripts/e2e/_login.sh`

- [ ] **Step 2.1: Write `scripts/e2e/_login.sh`**

```bash
#!/usr/bin/env bash
# Sourced by every scripts/e2e/smoke_*.sh after $SESSION is set, before any
# agent-browser open against a protected URL. Logs the agent-browser session
# in via the email/password form so the next page load carries the cookie.
#
# Counterpart: scripts/seed_e2e_user.py must have been run against the same
# stack first (workflow handles this; locally see scripts/e2e/README.md).

set -euo pipefail

if [[ -z "${SESSION:-}" ]]; then
  echo "::error::_login.sh requires \$SESSION to be set by the caller" >&2
  exit 2
fi
if [[ -z "${BASE_URL:-}" ]]; then
  echo "::error::_login.sh requires \$BASE_URL to be set by the caller" >&2
  exit 2
fi

E2E_USER_EMAIL='e2e@example.com'
E2E_USER_PASSWORD='E2eSmokePass!'
# Scope every selector by the unique form action — disambiguates Sign In
# from the sibling Forgot Password and Sign Up forms in login_email.html.
LOGIN_FORM='form[action="/auth/password/login/web"]'

echo "→ sign in as ${E2E_USER_EMAIL}"
agent-browser --session "$SESSION" open "${BASE_URL}/login/password"
agent-browser --session "$SESSION" fill "${LOGIN_FORM} input[name=email]"    "$E2E_USER_EMAIL"
agent-browser --session "$SESSION" fill "${LOGIN_FORM} input[name=password]" "$E2E_USER_PASSWORD"
agent-browser --session "$SESSION" click "${LOGIN_FORM} button[type=submit]"
agent-browser --session "$SESSION" wait --load networkidle
```

- [ ] **Step 2.2: Make it executable + shellcheck-clean check**

```bash
chmod +x scripts/e2e/_login.sh
# If shellcheck is installed locally, run it; otherwise skip — CI will catch.
command -v shellcheck >/dev/null 2>&1 && shellcheck scripts/e2e/_login.sh || true
```

- [ ] **Step 2.3: Commit**

```bash
git add scripts/e2e/_login.sh
git commit -m "feat(e2e): _login.sh helper — sign in via password form before smoke"
```

---

## Task 3 — Smoke scripts source the helper

**Files:**
- Modify: `scripts/e2e/smoke_catalog.sh`
- Modify: `scripts/e2e/smoke_admin_activity.sh`

The added line goes between the `trap` line and the first `agent-browser open` call (so the trap fires even if `_login.sh` exits early, and the protected URL open happens after the cookie is set).

- [ ] **Step 3.1: Edit `scripts/e2e/smoke_catalog.sh`**

After this block:

```bash
SESSION="agnes-e2e-$$"
trap 'agent-browser --session "$SESSION" close >/dev/null 2>&1 || true' EXIT
```

Insert:

```bash
# Sign the session in before hitting a protected page — /catalog otherwise
# 401-redirects to /login.
source "$(dirname "$0")/_login.sh"
```

- [ ] **Step 3.2: Edit `scripts/e2e/smoke_admin_activity.sh`**

Same insertion in the same spot.

- [ ] **Step 3.3: Commit**

```bash
git add scripts/e2e/smoke_catalog.sh scripts/e2e/smoke_admin_activity.sh
git commit -m "feat(e2e): smoke scripts source _login.sh before protected nav"
```

---

## Task 4 — Workflow integration

**Files:**
- Modify: `.github/workflows/e2e-nightly.yml`

- [ ] **Step 4.1: Add the seed step between "Build + start agnes stack" and "Run smoke"**

Insert this YAML step after the `Dump docker logs on stack failure` step (which was added by #389) and before `Run smoke ${{ matrix.script }}`:

```yaml
      - name: Seed E2E test user
        if: env.SKIP_MATRIX != '1'
        # The seed_e2e_user.py script is idempotent; running it before
        # the smoke step lets _login.sh inside the smoke scripts sign
        # the agent-browser session in. See
        # docs/superpowers/specs/2026-05-25-smoke-login-flow-design.md.
        run: docker compose exec -T app python scripts/seed_e2e_user.py
```

- [ ] **Step 4.2: actionlint locally if available**

```bash
command -v actionlint >/dev/null 2>&1 && actionlint .github/workflows/e2e-nightly.yml || echo "actionlint not installed locally — CI will run it"
```

- [ ] **Step 4.3: Commit**

```bash
git add .github/workflows/e2e-nightly.yml
git commit -m "ci(e2e-nightly): seed e2e user before smoke step"
```

---

## Task 5 — README "Prerequisites" section

**Files:**
- Modify: `scripts/e2e/README.md`

- [ ] **Step 5.1: Add a Prerequisites section between "Local development" and the rest**

Insert after the existing "Local development" code-block:

```markdown
### Prerequisites — sign-in seed

The smoke scripts authenticate against the running stack via
`/auth/password/login/web` before hitting any protected page. The
helper expects a fixed `e2e@example.com` Admin user to exist. After
`docker compose up` (or in any newly-`down -v`-ed environment), seed
it once:

\`\`\`bash
docker compose exec -T app python scripts/seed_e2e_user.py
\`\`\`

Idempotent — re-running is safe. The CI workflow does this automatically.
```

(Replace the `\`\`\`` escapes with literal triple-backticks in the actual file.)

- [ ] **Step 5.2: Commit**

```bash
git add scripts/e2e/README.md
git commit -m "docs(e2e): note the seed prerequisite for local smoke runs"
```

---

## Task 6 — Local E2E verification

**Files:** none modified.

This is the gate before pushing — confirm the whole chain works locally.

- [ ] **Step 6.1: Run the full pytest suite — all green (per CLAUDE.md)**

```bash
.venv/bin/pytest tests/ --tb=short -n auto -q
```

Expected: 5249+ passed (3 new tests added by Task 1). Fix anything that breaks before pushing.

- [ ] **Step 6.2: Start the stack locally**

```bash
touch .env  # docker-compose env_file is required
docker compose up -d --build --wait --wait-timeout 120
```

Expected: stack reports healthy; `curl -fsS http://localhost:8000/api/health` returns 200.

- [ ] **Step 6.3: Seed the e2e user**

```bash
docker compose exec -T app python scripts/seed_e2e_user.py
```

Expected stdout: `seeded: e2e@example.com (id=e2e-smoke-user) in Admin group`.

- [ ] **Step 6.4: Run both smoke scripts**

```bash
bash scripts/e2e/smoke_catalog.sh        http://localhost:8000
bash scripts/e2e/smoke_admin_activity.sh http://localhost:8000
```

Expected: both end with `✓ … smoke passed.`. If `agent-browser` is not installed, install per the README (`npm i -g agent-browser && agent-browser install`).

- [ ] **Step 6.5: Re-run the seed — confirm idempotency end-to-end**

```bash
docker compose exec -T app python scripts/seed_e2e_user.py
docker compose exec -T app python scripts/seed_e2e_user.py
```

Expected: both runs print `seeded: …`, no traceback, no duplicate rows. Optional sanity:

```bash
docker compose exec -T app python -c "from src.db import get_system_db; \
  print(get_system_db().execute(\"SELECT COUNT(*) FROM users WHERE email='e2e@example.com'\").fetchone())"
```

Expected: `(1,)`.

- [ ] **Step 6.6: Tear down**

```bash
docker compose down -v
```

- [ ] **Step 6.7: Commit nothing — this task is verification only**

If anything in Task 6 failed, go back to the relevant earlier task and fix before continuing.

---

## Task 7 — CHANGELOG + release-cut (LAST commit on the PR)

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`

Per CLAUDE.md "Release-cut belongs to the PR": this PR introduces the only `[Unreleased]` content, so the patch bump ships as the last commit.

- [ ] **Step 7.1: Edit `CHANGELOG.md` — add an `### Internal` bullet under `[Unreleased]`, then rename the section to `[0.55.10] — 2026-05-25` and add a new empty `[Unreleased]` above it**

The relevant block becomes:

```markdown
## [Unreleased]

## [0.55.10] — 2026-05-25

### Internal
- Nightly agent-browser smoke now signs in before hitting protected pages (#417, follows #389). New `scripts/seed_e2e_user.py` creates an idempotent hardcoded `e2e@example.com` Admin user in dev/CI containers; `scripts/e2e/_login.sh` (sourced by the smoke scripts) signs the agent-browser session in via `/auth/password/login/web`; the nightly workflow runs the seed between stack-up and smoke. The password is committed in the repo — acceptable because the user exists only inside an ephemeral container (`docker compose down -v` at end of every run) that has no external exposure; the container is the privilege boundary, not the credentials.

## [0.55.9] — 2026-05-25
```

- [ ] **Step 7.2: Edit `pyproject.toml`**

Change line 3 from:

```toml
version = "0.55.9"
```

to:

```toml
version = "0.55.10"
```

- [ ] **Step 7.3: Run the test suite one more time as a final sanity check**

```bash
.venv/bin/pytest tests/ --tb=short -n auto -q
```

Expected: green.

- [ ] **Step 7.4: Commit release-cut**

```bash
git add CHANGELOG.md pyproject.toml
git commit -m "release: 0.55.10 — smoke sign-in flow"
```

---

## Task 8 — Push branch and open the PR

**Files:** none modified.

- [ ] **Step 8.1: Push to a clean branch name (worktree branch has `worktree-` prefix)**

```bash
git push -u origin worktree-zs+fix-smoke-login-wall:zs/fix-smoke-login-wall
```

- [ ] **Step 8.2: Open the PR with a concrete body**

```bash
gh pr create --base main --head zs/fix-smoke-login-wall \
  --title "ci(e2e-nightly): sign smoke session in via /auth/password/login/web (#417) + release 0.55.10" \
  --body "Closes #417. Follows #389 (which unblocked the workflow far enough to surface this).

## Why

After #389 the nightly agent-browser smoke gets past \`docker compose up\` but lands on the login wall when it opens \`/catalog\` and \`/admin/activity\` — the global 401 handler in \`app/main.py:898-907\` redirects unauthenticated HTML GETs to \`/login?next=…\`, so both smoke scripts grep against a login snapshot and fail.

## Fix

Sign the smoke session in via the existing email/password provider before navigating to protected pages — no production code path touched.

- New \`scripts/seed_e2e_user.py\` — idempotent: creates \`e2e@example.com\` (Admin group) with a hardcoded dev-only password. Overwrites the hash on re-run; refuses to create an orphan user if the Admin group is missing.
- New \`scripts/e2e/_login.sh\` — sourced by both \`smoke_*.sh\`; uses agent-browser to fill the form at \`/login/password\` and POSTs against \`/auth/password/login/web\`. Selectors scoped to the form action (\`form[action='/auth/password/login/web']\`) to disambiguate the three nested forms in the tabbed login UI.
- One new step in \`.github/workflows/e2e-nightly.yml\` — runs the seed via \`docker compose exec\` between stack-up and smoke.
- Three unit tests in \`tests/test_seed_e2e_user.py\` — fresh-create, idempotency, Admin-missing-refusal.

The hardcoded password is committed in the repo. This is acceptable because the user only exists inside an ephemeral container (\`docker compose down -v\` at end of every run), the container has no external exposure, and committing the password lets a local developer reproduce the smoke setup byte-for-byte. **The container is the privilege boundary, not the credentials.**

## Spec + plan

- Spec: \`docs/superpowers/specs/2026-05-25-smoke-login-flow-design.md\`
- Plan: \`docs/superpowers/plans/2026-05-25-smoke-login-flow.md\`

## Release-cut

Patch \`0.55.9 → 0.55.10\` in the same PR per the CLAUDE.md release-cut rule.

## Verification

- Full test suite green locally (\`pytest tests/ -n auto -q\`).
- End-to-end smoke run locally — both \`smoke_catalog.sh\` and \`smoke_admin_activity.sh\` pass against a freshly-seeded \`docker compose\` stack.
- Re-running the seed twice is a no-op (verified locally)."
```

- [ ] **Step 8.3: Wait for CI; address the \`gh pr checks\` output (note: the \`release.yml\` cancelled-by-concurrency artefact from #389 will repeat for new branches — rerun the cancelled run if needed)**

```bash
gh pr view --json url --jq .url
gh pr checks
```

- [ ] **Step 8.4: Final post-merge housekeeping (out of plan but part of the routine — do not merge yet, wait for user GO)**

After merge, follow the same post-merge pattern from #389:
- Tag \`v0.55.10\` on the merge commit + create GH release.
- Manually \`gh workflow run e2e-nightly.yml\` to confirm a green nightly.
- Close #417 with a pointer to the merged PR.

---

## Self-review checklist

(Run this checklist mentally before handing off to executing-plans / subagent-driven-development.)

**Spec coverage:**
- ✅ Seed user (Section 1) → Task 1 (script + tests + commit).
- ✅ Seed mechanism (Section 2) → Task 1 + Task 4 (workflow exec line).
- ✅ Login helper (Section 3) → Task 2.
- ✅ Workflow integration (Section 4) → Task 4.
- ✅ Smoke script source line (Section 5) → Task 3.
- ✅ README (Section 6) → Task 5.
- ✅ CHANGELOG + release-cut (Section 7) → Task 7.
- ✅ Failure modes (`Admin missing`, selectors changed, cookie issues) — covered by Task 1 unit tests + Task 6 manual verification.

**Placeholder scan:** ✅ no TBDs / TODOs / "implement later" / "similar to Task N".

**Type/name consistency:**
- ✅ `E2E_USER_EMAIL` / `E2E_USER_PASSWORD` consistent in Python (Task 1) and bash (Task 2).
- ✅ `LOGIN_FORM` scope identical in bash helper and spec.
- ✅ `SYSTEM_ADMIN_GROUP` import path matches the precedent in `tests/conftest.py::seeded_app` (line 240).
- ✅ `UserGroupMembersRepository.list_members` + `add_member` — verified against `tests/conftest.py:255` usage (`add_member(user_id, group_id, source=...)`); if the actual method name differs, Step 1.4's grep-and-fix note covers it.
