# Data Apps Wave 3B — Draft Model + Deploy Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the KAI-style prod+draft iteration model to Agnes data apps — a draft is a registry sibling of a prod app, deployed from a pinned iteration branch of the prod app's own git repo, with broker/MCP/CLI tools to create drafts, mint git credentials, deploy in `dev` mode, delete drafts, and inline drafts into `get`.

**Architecture:** Extend the existing `data_apps` registry (waves 1+2) with three columns (`parent_app_id`, `is_draft`, `draft_branch`) at schema v98. A draft shares the parent's internal bare git repo (`/data-apps.git/<parent_slug>`) but its container clones a pinned branch instead of `agnes-live`. New control-plane endpoints hang off the existing `/api/data-apps` router; the sandboxed chat agent reaches them through the existing broker-ticket replay pattern under a new `data_apps` scope.

**Tech Stack:** FastAPI, DuckDB + Postgres (dual backend), Alembic, git http-backend, httpx, typer CLI, FastMCP.

**Spec:** `docs/superpowers/specs/2026-07-23-data-apps-wave3-ai-authoring-design.md` — read §5 (tools), §6 (prod+draft model), §9 (surfaces). Preview chat tools (§7) are wave 3C, NOT in this plan.

## Global Constraints

- Dual-backend discipline: every `src/repositories/data_apps.py` method gets the matching `data_apps_pg.py` method in the SAME task, plus a `tests/db_pg/test_data_apps_contract.py` assertion. Reach repos only via `data_apps_repo()`.
- Schema: `SCHEMA_VERSION` is currently **97** (`corpus_files.path`). This plan is **v98**. The DuckDB ladder in `src/db.py` (`_v97_to_v98` + BOTH call sites) and the Alembic migration `migrations/versions/0045_data_apps_drafts_v98.py` (`down_revision = "0044_corpus_files_path_v97"`) move together in one task. `tests/test_db_schema_version.py` is the gate.
- Every new `/api/*` route lands in `_COHORT` (with a CLI command + MCP tool) or `_EXEMPT` (with a reason) in `tests/test_documentation_api_triple_surface.py`. No growth of the grandfather list.
- New broker endpoints follow the ticket-replay pattern in `app/api/broker.py` (`require_broker_ticket` + `_require_scope` + `_replay`); the agent gets a new `data_apps` ticket scope minted at spawn.
- Vendor-agnostic; no AI attribution in commits; `.venv/bin/pytest` (ruff at `/opt/homebrew/bin/ruff`, not in venv); guard `uv.lock` (`git checkout -- uv.lock` if it churns, never commit).
- Feature flag: every new handler calls `_feature_gate()` first (404 `data_apps_disabled` when disabled).
- Draft slug convention: `<parent_slug>--<branch>` truncated/validated to `SLUG_RE`; drafts are private-by-default like any app and filtered from human-facing `/apps` listings by `is_draft`.

## File Structure

```
src/db.py                                    # v98 step + _DATA_APPS_CREATE_SQL cols + 2 ladder sites
migrations/versions/0045_data_apps_drafts_v98.py
src/repositories/data_apps.py                # +create_draft, list_drafts; +3 cols in _COLS/create
src/repositories/data_apps_pg.py             # PG twins
src/data_apps/spec.py                        # build_config_json: draft branch override
src/data_apps/git_repos.py                   # +ensure_branch, +delete_branch
app/api/data_apps.py                         # +draft/credential/mode/delete-draft endpoints; get inlines drafts
app/api/broker.py                            # +data_apps-scope replay is generic; no new endpoint if reused
app/chat/manager.py                          # +mint "data_apps" ticket at spawn
app/api/mcp/foundation_tools.py              # +4 MCP tools
cli/commands/data_apps.py                    # +draft/credential CLI; deploy --mode
tests/test_data_apps_api.py                  # endpoint tests
tests/db_pg/test_data_apps_contract.py       # draft column contract
tests/test_documentation_api_triple_surface.py  # ratchet entries
```

---

### Task 1: Registry v98 — draft columns + repo methods

**Files:**
- Modify: `src/db.py` (SCHEMA_VERSION 97→98, `_DATA_APPS_CREATE_SQL` +3 cols, `_v97_to_v98` step, 2 ladder sites)
- Create: `migrations/versions/0045_data_apps_drafts_v98.py`
- Modify: `src/repositories/data_apps.py`, `src/repositories/data_apps_pg.py`
- Test: `tests/test_data_apps_repo.py`, `tests/db_pg/test_data_apps_contract.py`

**Interfaces:**
- Produces on `data_apps_repo()`:
  - `create_draft(*, parent_app_id: str, slug: str, branch: str, owner_user_id: str, idle_timeout_s: int = 1800, sleep_mode: str = "recreate") -> str` — inserts a row with `is_draft=True`, `parent_app_id`, `draft_branch=branch`, `repo_mode='internal'`, `name=<parent name> (draft)`; returns `app_<uuid12>`.
  - `list_drafts(parent_app_id: str) -> List[dict]`
  - existing `get/list/delete` now return/accept the 3 new columns; `list()` gains `include_drafts: bool = True` (human callers pass `False`).
- New columns: `parent_app_id VARCHAR DEFAULT ''`, `is_draft BOOLEAN DEFAULT FALSE`, `draft_branch VARCHAR DEFAULT ''`.

- [ ] **Step 1: Failing repo test** — `tests/test_data_apps_repo.py`:

```python
class TestDrafts:
    def test_create_draft_and_list(self, repo):
        parent = repo.create(slug="dash", name="Dash", owner_user_id="u1")
        did = repo.create_draft(parent_app_id=parent, slug="dash--init",
                                branch="init", owner_user_id="u1")
        assert did.startswith("app_")
        d = repo.get(did)
        assert d["is_draft"] is True
        assert d["parent_app_id"] == parent
        assert d["draft_branch"] == "init"
        assert [r["id"] for r in repo.list_drafts(parent)] == [did]

    def test_list_excludes_drafts_when_asked(self, repo):
        p = repo.create(slug="p", name="P", owner_user_id="u1")
        repo.create_draft(parent_app_id=p, slug="p--x", branch="x", owner_user_id="u1")
        slugs_all = {r["slug"] for r in repo.list()}
        slugs_prod = {r["slug"] for r in repo.list(include_drafts=False)}
        assert "p--x" in slugs_all and "p--x" not in slugs_prod
        assert "p" in slugs_prod
```

- [ ] **Step 2: Run to verify fail** — `.venv/bin/pytest tests/test_data_apps_repo.py::TestDrafts -x -q` → FAIL (`create_draft` missing / no `is_draft` column).

- [ ] **Step 3: Schema `src/db.py`** — set `SCHEMA_VERSION = 98`. Append 3 columns to `_DATA_APPS_CREATE_SQL` (after `sleep_mode`, before `service_token_id` is fine — order only matters for `_COLS`; keep new cols last before the timestamps for clarity):

```sql
    service_token_id VARCHAR DEFAULT '',
    parent_app_id   VARCHAR DEFAULT '',
    is_draft        BOOLEAN DEFAULT FALSE,
    draft_branch    VARCHAR DEFAULT '',
    last_request_at TIMESTAMP,
```

Add the step function after `_v96_to_v97`:

```python
def _v97_to_v98(conn: duckdb.DuckDBPyConnection) -> None:
    """v97→v98: data_apps draft model — parent_app_id, is_draft, draft_branch."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info('data_apps')").fetchall()}
    if "parent_app_id" not in cols:
        conn.execute("ALTER TABLE data_apps ADD COLUMN parent_app_id VARCHAR DEFAULT ''")
    if "is_draft" not in cols:
        conn.execute("ALTER TABLE data_apps ADD COLUMN is_draft BOOLEAN DEFAULT FALSE")
    if "draft_branch" not in cols:
        conn.execute("ALTER TABLE data_apps ADD COLUMN draft_branch VARCHAR DEFAULT ''")
    conn.execute("UPDATE schema_version SET version = 98")
```

Wire BOTH ladder sites exactly like `_v96_to_v97`: in the fresh-install/sequential branch add `_v97_to_v98(conn)` after the `_v96_to_v97(conn)` line; in the `if current < N` branch add `if current < 98: _v97_to_v98(conn)` after the `if current < 97` block.

- [ ] **Step 4: Alembic** — `migrations/versions/0045_data_apps_drafts_v98.py`:

```python
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0045_data_apps_drafts_v98"
down_revision: Union[str, None] = "0044_corpus_files_path_v97"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("data_apps", sa.Column("parent_app_id", sa.String(), server_default=""))
    op.add_column("data_apps", sa.Column("is_draft", sa.Boolean(), server_default=sa.text("false")))
    op.add_column("data_apps", sa.Column("draft_branch", sa.String(), server_default=""))


def downgrade() -> None:
    op.drop_column("data_apps", "draft_branch")
    op.drop_column("data_apps", "is_draft")
    op.drop_column("data_apps", "parent_app_id")
```

- [ ] **Step 5: DuckDB repo** — in `src/repositories/data_apps.py`, add the 3 columns to `_COLS` (insert `"parent_app_id", "is_draft", "draft_branch"` right after `"service_token_id"`), add `include_drafts` to `list()`, and add the two methods:

```python
def list(self, *, owner_user_id=None, state=None, include_drafts=True, limit=1000):
    clauses, params = [], []
    if owner_user_id is not None:
        clauses.append("owner_user_id = ?"); params.append(owner_user_id)
    if state is not None:
        clauses.append("state = ?"); params.append(state)
    if not include_drafts:
        clauses.append("is_draft = FALSE")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = self.conn.execute(
        f"SELECT {self._SELECT} FROM data_apps {where} ORDER BY created_at DESC LIMIT ?",
        params).fetchall()
    return [dict(zip(self._COLS, r)) for r in rows]

def create_draft(self, *, parent_app_id, slug, branch, owner_user_id,
                 idle_timeout_s=1800, sleep_mode="recreate"):
    app_id = "app_" + uuid4().hex[:12]
    self.conn.execute(
        "INSERT INTO data_apps"
        "(id, slug, name, owner_user_id, repo_mode, parent_app_id, is_draft,"
        " draft_branch, idle_timeout_s, sleep_mode) "
        "VALUES (?, ?, ?, ?, 'internal', ?, TRUE, ?, ?, ?)",
        [app_id, slug, f"{slug} (draft)", owner_user_id, parent_app_id,
         branch, idle_timeout_s, sleep_mode])
    return app_id

def list_drafts(self, parent_app_id):
    rows = self.conn.execute(
        f"SELECT {self._SELECT} FROM data_apps WHERE parent_app_id = ? "
        "AND is_draft = TRUE ORDER BY created_at DESC", [parent_app_id]).fetchall()
    return [dict(zip(self._COLS, r)) for r in rows]
```

(Match the existing `list()` shape — it already builds `clauses`/`params`; only the `include_drafts` clause is new.)

- [ ] **Step 6: PG twin** — mirror in `src/repositories/data_apps_pg.py` with `sa.text()` named params. PG uses `SELECT *`, so `get`/`list` pick up the new columns automatically; add `include_drafts` to the `list` WHERE-builder (`AND is_draft = false`), and add `create_draft` (`... is_draft, ...) VALUES (..., true, ...)`) + `list_drafts` (`WHERE parent_app_id = :pid AND is_draft = true`). Boolean literals are `true`/`false` in PG text SQL.

- [ ] **Step 7: Contract test** — in `tests/db_pg/test_data_apps_contract.py`, add a backend-parametrized test:

```python
def test_draft_lifecycle(repo):
    p = repo.create(slug="cp", name="CP", owner_user_id="u1")
    d = repo.create_draft(parent_app_id=p, slug="cp--i", branch="i", owner_user_id="u1")
    got = repo.get(d)
    assert got["is_draft"] in (True, 1)   # duckdb bool vs pg bool
    assert got["parent_app_id"] == p
    assert got["draft_branch"] == "i"
    assert [r["id"] for r in repo.list_drafts(p)] == [d]
    assert "cp--i" not in {r["slug"] for r in repo.list(include_drafts=False)}
```

Normalize the bool with `bool(got["is_draft"])` in the assert if the backends differ.

- [ ] **Step 8: Run gates** — `.venv/bin/pytest tests/test_data_apps_repo.py tests/db_pg/test_data_apps_contract.py tests/test_db_schema_version.py tests/test_repository_registry.py -q` → PASS.

- [ ] **Step 9: Commit**

```bash
git add src/db.py migrations/versions/0045_data_apps_drafts_v98.py \
  src/repositories/data_apps.py src/repositories/data_apps_pg.py \
  tests/test_data_apps_repo.py tests/db_pg/test_data_apps_contract.py
git commit -m "feat(data-apps): draft-model registry columns and repo methods (v98)"
```

---

### Task 2: Draft branch in `build_config_json` + git branch helpers

**Files:**
- Modify: `src/data_apps/spec.py`, `src/data_apps/git_repos.py`
- Test: `tests/test_data_apps_spec.py`, `tests/test_data_apps_git.py`

**Interfaces:**
- Consumes: Task 1 rows (draft rows have `is_draft=True`, `parent_app_id`, `draft_branch`).
- Produces:
  - `build_config_json(app_row, *, secrets, clone_url, clone_token)` — UNCHANGED signature; behavior: when `app_row.get("is_draft")` is truthy, the internal git block uses `branch = app_row["draft_branch"]` instead of `LIVE_BRANCH`, and `clone_url` still points at the PARENT repo (the caller passes the parent's clone URL).
  - `ensure_branch(slug: str, branch: str, base: str = "main") -> None` in `git_repos.py` — creates `refs/heads/<branch>` at `base`'s commit if absent; no-op if it exists. Raises `ValueError` on invalid slug (via `repo_path`).
  - `delete_branch(slug: str, branch: str) -> None` — deletes `refs/heads/<branch>`; no-op if absent; refuses `main`/`agnes-live` (`ValueError`).

- [ ] **Step 1: Failing spec test** — `tests/test_data_apps_spec.py`:

```python
def test_config_json_draft_uses_pinned_branch():
    from src.data_apps.spec import build_config_json
    row = {"repo_mode": "internal", "is_draft": True, "draft_branch": "init",
           "slug": "d--init"}
    cfg = build_config_json(row, secrets={},
                            clone_url="http://app:8000/data-apps.git/d",
                            clone_token="PAT")
    assert cfg["dataApp"]["git"]["branch"] == "init"
    assert cfg["dataApp"]["git"]["repository"].endswith("/data-apps.git/d")
    assert cfg["dataApp"]["git"]["#password"] == "PAT"


def test_config_json_prod_still_agnes_live():
    from src.data_apps.spec import build_config_json
    row = {"repo_mode": "internal", "slug": "d"}
    cfg = build_config_json(row, secrets={}, clone_url="http://x/data-apps.git/d",
                            clone_token="PAT")
    assert cfg["dataApp"]["git"]["branch"] == "agnes-live"
```

- [ ] **Step 2: Run to fail** — `.venv/bin/pytest tests/test_data_apps_spec.py -k draft -q` → FAIL.

- [ ] **Step 3: Implement** — in `src/data_apps/spec.py::build_config_json`, change the internal branch:

```python
    if app_row["repo_mode"] == "internal":
        branch = app_row["draft_branch"] if app_row.get("is_draft") else LIVE_BRANCH
        git = {"repository": clone_url, "branch": branch,
               "username": "agnes", "#password": clone_token}
```

(external branch unchanged.)

- [ ] **Step 4: Failing git test** — `tests/test_data_apps_git.py`:

```python
def test_ensure_and_delete_branch(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import subprocess
    from src.data_apps.git_repos import init_app_repo, ensure_branch, delete_branch, resolve_ref
    init_app_repo("g")
    # seed main with one commit
    work = tmp_path / "w"
    subprocess.run(["git", "clone", str(tmp_path / "apps" / "git" / "g.git"), str(work)],
                   check=True, capture_output=True)
    (work / "f").write_text("x")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "c"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "origin", "HEAD:main"],
                   check=True, capture_output=True)
    ensure_branch("g", "init", base="main")
    assert resolve_ref("g", "init") == resolve_ref("g", "main")
    ensure_branch("g", "init")  # idempotent, no raise
    delete_branch("g", "init")
    assert resolve_ref("g", "init") is None
    import pytest
    with pytest.raises(ValueError):
        delete_branch("g", "main")
```

- [ ] **Step 5: Implement git helpers** — append to `src/data_apps/git_repos.py`:

```python
def ensure_branch(slug: str, branch: str, base: str = "main") -> None:
    p = repo_path(slug)  # validates slug
    if resolve_ref(slug, branch) is not None:
        return
    target = resolve_ref(slug, base)
    if not target:
        raise ValueError(f"base ref {base!r} not found in app repo {slug}")
    subprocess.run(["git", "-C", str(p), "update-ref", f"refs/heads/{branch}", target],
                   check=True, capture_output=True)


def delete_branch(slug: str, branch: str) -> None:
    if branch in ("main", "agnes-live"):
        raise ValueError(f"refusing to delete protected branch {branch!r}")
    p = repo_path(slug)  # validates slug
    if resolve_ref(slug, branch) is None:
        return
    subprocess.run(["git", "-C", str(p), "update-ref", "-d", f"refs/heads/{branch}"],
                   check=True, capture_output=True)
```

- [ ] **Step 6: Run** — `.venv/bin/pytest tests/test_data_apps_spec.py tests/test_data_apps_git.py -q` → PASS.

- [ ] **Step 7: Commit**

```bash
git add src/data_apps/spec.py src/data_apps/git_repos.py tests/test_data_apps_spec.py tests/test_data_apps_git.py
git commit -m "feat(data-apps): draft-branch config.json + git branch helpers"
```

---

### Task 3: Git-credential mint helper + `POST /{slug}/git-credential`

**Files:**
- Modify: `app/api/data_apps.py`
- Test: `tests/test_data_apps_api.py`

**Interfaces:**
- Consumes: `_mint_service_token(slug, owner)` (exists, L236), `access_token_repo()`, `_get_row_or_404`, `_require_owner_or_admin`.
- Produces:
  - module-level `def _mint_git_credential(row: dict) -> str` — mints a PAT for the app owner scoped `data-app-git:<slug>`, returns a clone URL `http://app:8000/data-apps.git/<slug>` with the token embedded as `agnes:<jwt>@` basic-auth. Does NOT store it as the service token (it is the agent's push credential, independent of the container's runtime token).
  - `POST /api/data-apps/{slug}/git-credential` → `{"git_clone_url": "<url>"}` (owner/Admin, feature-gated).

- [ ] **Step 1: Failing test** — `tests/test_data_apps_api.py`:

```python
class TestGitCredential:
    def test_mint_git_credential(self, client_as_user, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/sapp/git-credential")
        assert r.status_code == 200, r.text
        url = r.json()["git_clone_url"]
        assert "/data-apps.git/sapp" in url
        assert "@" in url and url.startswith("http")

    def test_git_credential_stranger_403(self, client_as_other_user, seeded_repo_with_commit):
        assert client_as_other_user.post("/api/data-apps/sapp/git-credential").status_code == 403
```

- [ ] **Step 2: Run to fail** — `.venv/bin/pytest tests/test_data_apps_api.py::TestGitCredential -q` → FAIL (404 no route).

- [ ] **Step 3: Implement** — in `app/api/data_apps.py` add the helper near `_mint_service_token`:

```python
def _mint_git_credential(row: dict) -> str:
    owner = users_repo().get_by_id(row["owner_user_id"])
    if not owner:
        raise OwnerNotFoundError(row["owner_user_id"])
    slug = row["slug"]
    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(
        user_id=owner["id"], email=owner["email"], token_id=token_id, typ="pat",
        extra_claims={"scope": f"data-app-git:{slug}"})
    access_token_repo().create(
        id=token_id, user_id=owner["id"], name=f"data-app-git:{slug}",
        token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8], expires_at=None)
    return f"{AGNES_INTERNAL_URL.replace('://', f'://agnes:{jwt_token}@')}/data-apps.git/{slug}"
```

and the endpoint (near `deploy_data_app`):

```python
@router.post("/{slug}/git-credential")
async def mint_git_credential(slug: str, user: dict = Depends(get_current_user),
                              conn: duckdb.DuckDBPyConnection = Depends(_get_db)):
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)
    try:
        url = _mint_git_credential(row)
    except OwnerNotFoundError:
        raise HTTPException(status_code=500, detail="owner_not_found")
    _audit(conn, user["id"], "data_app.git_credential", f"data_app:{slug}", {})
    return {"git_clone_url": url}
```

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/test_data_apps_api.py::TestGitCredential -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/data_apps.py tests/test_data_apps_api.py
git commit -m "feat(data-apps): mint per-app git push credential"
```

---

### Task 4: Create-draft endpoint

**Files:**
- Modify: `app/api/data_apps.py`
- Test: `tests/test_data_apps_api.py`

**Interfaces:**
- Consumes: `data_apps_repo().create_draft` (Task 1), `ensure_branch` (Task 2), `_mint_git_credential` (Task 3), `_get_row_or_404`, `_require_owner_or_admin`, `SLUG_RE`.
- Produces:
  - `CreateDraftRequest(BaseModel)`: `branch: str = "init"`.
  - `POST /api/data-apps/{slug}/drafts` → creates a draft of the prod app `<slug>`, ensures the branch exists on the prod repo, returns `{"id", "slug", "branch", "git_clone_url"}`. 400 `parent_is_draft` if `<slug>` is itself a draft; 400 `invalid_branch` if branch fails a `^[a-z0-9][a-z0-9._/-]{0,60}$` check; 409 `slug_exists` on collision.
  - Draft slug = `f"{parent_slug}--{branch}"` lowercased, non-`SLUG_RE` chars replaced with `-`, truncated to 40 chars, validated against `SLUG_RE` (400 `invalid_slug` if it still fails).

- [ ] **Step 1: Failing test**:

```python
class TestDrafts:
    def test_create_draft(self, client_as_user, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["branch"] == "init"
        assert body["slug"].startswith("sapp--")
        assert "/data-apps.git/sapp" in body["git_clone_url"]
        # draft is inlined under the prod app (Task 6), and hidden from list
        listed = {a["slug"] for a in client_as_user.get("/api/data-apps").json()}
        assert body["slug"] not in listed

    def test_draft_of_draft_rejected(self, client_as_user, seeded_repo_with_commit):
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "a"}).json()
        r = client_as_user.post(f"/api/data-apps/{d['slug']}/drafts", json={"branch": "b"})
        assert r.status_code == 400 and r.json()["detail"] == "parent_is_draft"
```

(The "hidden from list" assert depends on Task 6 wiring `list(include_drafts=False)` into the `GET /api/data-apps` handler — if Task 6 lands after this, keep the assert but expect it to pass once Task 6 is in; if running strictly in order, split that assert into a Task 6 test. Implementer: put the list-hiding change in Task 6 and keep only the 201/branch/slug asserts here.)

Corrected Step-1 test (order-safe — drop the list assert; it moves to Task 6):

```python
class TestDrafts:
    def test_create_draft(self, client_as_user, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["branch"] == "init"
        assert body["slug"].startswith("sapp--")
        assert "/data-apps.git/sapp" in body["git_clone_url"]

    def test_draft_of_draft_rejected(self, client_as_user, seeded_repo_with_commit):
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "a"}).json()
        r = client_as_user.post(f"/api/data-apps/{d['slug']}/drafts", json={"branch": "b"})
        assert r.status_code == 400 and r.json()["detail"] == "parent_is_draft"
```

- [ ] **Step 2: Run to fail** — `.venv/bin/pytest tests/test_data_apps_api.py::TestDrafts -q` → FAIL.

- [ ] **Step 3: Implement** — add the model + a slug helper + endpoint in `app/api/data_apps.py`:

```python
import re as _re  # if not already imported; SLUG_RE already comes from spec

_BRANCH_RE = _re.compile(r"^[a-z0-9][a-z0-9._/-]{0,60}$")

class CreateDraftRequest(BaseModel):
    branch: str = "init"


def _draft_slug(parent_slug: str, branch: str) -> str:
    raw = f"{parent_slug}--{branch}".lower()
    cleaned = _re.sub(r"[^a-z0-9-]", "-", raw)[:40].strip("-")
    if not SLUG_RE.match(cleaned):
        raise HTTPException(status_code=400, detail="invalid_slug")
    return cleaned


@router.post("/{slug}/drafts", status_code=201)
async def create_draft(slug: str, payload: CreateDraftRequest,
                       user: dict = Depends(get_current_user),
                       conn: duckdb.DuckDBPyConnection = Depends(_get_db)):
    _feature_gate()
    parent = _get_row_or_404(slug)
    _require_owner_or_admin(user, parent)
    if parent.get("is_draft"):
        raise HTTPException(status_code=400, detail="parent_is_draft")
    if not _BRANCH_RE.match(payload.branch):
        raise HTTPException(status_code=400, detail="invalid_branch")
    draft_slug = _draft_slug(slug, payload.branch)
    from src.data_apps.git_repos import ensure_branch
    ensure_branch(slug, payload.branch, base="main")
    repo = data_apps_repo()
    try:
        draft_id = repo.create_draft(parent_app_id=parent["id"], slug=draft_slug,
                                     branch=payload.branch, owner_user_id=parent["owner_user_id"])
    except duckdb.ConstraintException:
        raise HTTPException(status_code=409, detail="slug_exists")
    draft_row = repo.get(draft_id)
    git_url = _mint_git_credential(parent)  # push credential is against the PROD repo
    _audit(conn, user["id"], "data_app.draft_create", f"data_app:{draft_slug}",
           {"parent": slug, "branch": payload.branch})
    return {"id": draft_id, "slug": draft_slug, "branch": payload.branch,
            "git_clone_url": git_url}
```

(Note: `main` must exist on the prod repo — `ensure_branch(base="main")` raises `ValueError` if the repo is empty. The `_seed_app_with_commit` fixture pushes `HEAD:main`, so tests are fine; real prod apps get `main` from their first deploy. If `ensure_branch` raises `ValueError`, map it to 409 `deploy_empty_repo` — wrap the call.)

Wrap `ensure_branch`:

```python
    try:
        ensure_branch(slug, payload.branch, base="main")
    except ValueError:
        raise HTTPException(status_code=409, detail="parent_has_no_main")
```

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/test_data_apps_api.py::TestDrafts -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/data_apps.py tests/test_data_apps_api.py
git commit -m "feat(data-apps): create-draft endpoint"
```

---

### Task 5: Deploy `mode` param (dev deploys the draft branch)

**Files:**
- Modify: `app/api/data_apps.py`
- Test: `tests/test_data_apps_api.py`

**Interfaces:**
- Consumes: `DeployRequest` (extend), `redeploy_current`, `fast_forward_live`, draft rows.
- Produces: `DeployRequest` gains `mode: Optional[str] = None`. Behavior:
  - `mode == "dev"`: the target MUST be a draft row (400 `dev_requires_draft` otherwise). Skip `fast_forward_live` (the draft's container clones its pinned `draft_branch` directly — `build_config_json` already selects it via `is_draft`). Deploy via `redeploy_current(row)`; `record_deploy(row["id"], "")`; state → running. The runner's config_dir/container name are per-slug, so a draft gets its own container.
  - `mode` unset / `"prod"`: existing behavior (internal → `fast_forward_live`; external → no ff), rejected 400 `prod_on_draft` if the row `is_draft`.

- [ ] **Step 1: Failing test**:

```python
def test_deploy_dev_mode_on_draft(self, client_as_user, fake_runner, seeded_repo_with_commit):
    d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
    r = client_as_user.post(f"/api/data-apps/{d['slug']}/deploy", json={"mode": "dev"})
    assert r.status_code == 200, r.text
    slug, spec, cfg = fake_runner.up_calls[-1]
    assert slug == d["slug"]
    assert cfg["dataApp"]["git"]["branch"] == "init"        # draft branch, not agnes-live
    assert cfg["dataApp"]["git"]["repository"].endswith("/data-apps.git/sapp")  # PARENT repo

def test_deploy_dev_requires_draft(self, client_as_user, fake_runner, seeded_repo_with_commit):
    r = client_as_user.post("/api/data-apps/sapp/deploy", json={"mode": "dev"})
    assert r.status_code == 400 and r.json()["detail"] == "dev_requires_draft"

def test_deploy_prod_on_draft_rejected(self, client_as_user, fake_runner, seeded_repo_with_commit):
    d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
    r = client_as_user.post(f"/api/data-apps/{d['slug']}/deploy", json={})
    assert r.status_code == 400 and r.json()["detail"] == "prod_on_draft"
```

**Critical for the dev-mode config:** the draft's container must clone the PARENT's repo (`/data-apps.git/<parent_slug>`), not a repo named after the draft slug (drafts have no repo of their own). `redeploy_current` builds `clone_url = f"{AGNES_INTERNAL_URL}/data-apps.git/{slug}"` from `row["slug"]`. For a draft, override the clone slug to the parent's slug. Implement by resolving the parent slug inside `redeploy_current` when `row["is_draft"]`.

- [ ] **Step 2: Run to fail** — FAIL.

- [ ] **Step 3: Implement** — two changes in `app/api/data_apps.py`.

(a) `redeploy_current` clone-url uses the parent repo for drafts:

```python
def redeploy_current(row: dict) -> None:
    slug = row["slug"]
    repo = data_apps_repo()
    ...
    # drafts share the parent app's git repo; clone from there, on the pinned branch
    repo_slug = slug
    if row.get("is_draft") and row.get("parent_app_id"):
        parent = repo.get(row["parent_app_id"])
        if parent:
            repo_slug = parent["slug"]
    clone_url = f"{AGNES_INTERNAL_URL}/data-apps.git/{repo_slug}"
    ...
```

(b) `DeployRequest` + `deploy_data_app` branch on mode:

```python
class DeployRequest(BaseModel):
    sha: Optional[str] = None
    mode: Optional[str] = None
```

```python
    if payload.mode == "dev":
        if not row.get("is_draft"):
            raise HTTPException(status_code=400, detail="dev_requires_draft")
        sha = ""
    elif row.get("is_draft"):
        raise HTTPException(status_code=400, detail="prod_on_draft")
    elif row["repo_mode"] == "external":
        if payload.sha:
            raise HTTPException(status_code=400, detail="external_repo_sha_unsupported")
        sha = ""
    else:
        try:
            sha = fast_forward_live(slug, payload.sha)
        except ValueError as exc:
            if "no commits to deploy" in str(exc):
                raise HTTPException(status_code=409, detail="deploy_empty_repo")
            raise HTTPException(status_code=400, detail=str(exc))
    # ... unchanged redeploy_current + record_deploy + set_state ...
```

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/test_data_apps_api.py -k "deploy" -q` → PASS (existing deploy tests unaffected: prod path unchanged).

- [ ] **Step 5: Commit**

```bash
git add app/api/data_apps.py tests/test_data_apps_api.py
git commit -m "feat(data-apps): dev-mode deploy serves the draft branch"
```

---

### Task 6: Delete-draft endpoint + `get` inlines drafts + hide drafts from list

**Files:**
- Modify: `app/api/data_apps.py`
- Test: `tests/test_data_apps_api.py`

**Interfaces:**
- Consumes: `list_drafts`, `delete_branch` (Task 2), `_serialize`, existing `delete_data_app` internals (`_revoke_service_token`, runner stop, rmtree).
- Produces:
  - `DELETE /api/data-apps/{slug}/drafts/{draft_slug}` → tears down a draft: owner/Admin of the PARENT; 400 `not_a_draft` if `<draft_slug>` isn't a draft; 404 if unknown; deletes the container (runner stop recreate, best-effort), the registry row, and the draft branch on the parent repo. Returns 204.
  - `GET /api/data-apps/{slug}` response gains `"drafts": [ {id, slug, branch, state, url}, ... ]` for prod apps (empty for drafts).
  - `GET /api/data-apps` (list) passes `include_drafts=False` so drafts never appear in the human-facing/CLI list.

- [ ] **Step 1: Failing tests**:

```python
def test_get_inlines_drafts(self, client_as_user, seeded_repo_with_commit):
    d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
    detail = client_as_user.get("/api/data-apps/sapp").json()
    assert any(x["slug"] == d["slug"] and x["branch"] == "init" for x in detail["drafts"])

def test_list_hides_drafts(self, client_as_user, seeded_repo_with_commit):
    d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
    slugs = {a["slug"] for a in client_as_user.get("/api/data-apps").json()}
    assert "sapp" in slugs and d["slug"] not in slugs

def test_delete_draft(self, client_as_user, fake_runner, seeded_repo_with_commit):
    d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
    r = client_as_user.delete(f"/api/data-apps/sapp/drafts/{d['slug']}")
    assert r.status_code == 204, r.text
    assert client_as_user.get(f"/api/data-apps/{d['slug']}").status_code == 404
    detail = client_as_user.get("/api/data-apps/sapp").json()
    assert detail["drafts"] == []

def test_delete_draft_rejects_non_draft(self, client_as_user, seeded_repo_with_commit):
    # deleting the prod slug through the draft route is a 400
    r = client_as_user.delete("/api/data-apps/sapp/drafts/sapp")
    assert r.status_code == 400 and r.json()["detail"] == "not_a_draft"
```

- [ ] **Step 2: Run to fail** — FAIL.

- [ ] **Step 3: Implement** — three edits in `app/api/data_apps.py`.

(a) list handler passes `include_drafts=False` (find the `GET ""` list endpoint; change its `data_apps_repo().list(...)` call to include `include_drafts=False`).

(b) `get` detail handler inlines drafts (find `GET /{slug}`; after building the serialized row, if not a draft, attach):

```python
    out = _serialize(row)
    if not row.get("is_draft"):
        out["drafts"] = [
            {"id": d["id"], "slug": d["slug"], "branch": d["draft_branch"],
             "state": d["state"], "url": _app_url(d["slug"], _effective_config())}
            for d in data_apps_repo().list_drafts(row["id"])
        ]
    return out
```

(c) delete-draft endpoint (reuse the teardown the existing `delete_data_app` does — factor the shared body into a helper `_teardown_app(row)` if `delete_data_app` inlines it; otherwise call the same steps):

```python
@router.delete("/{slug}/drafts/{draft_slug}", status_code=204)
async def delete_draft(slug: str, draft_slug: str,
                       user: dict = Depends(get_current_user),
                       conn: duckdb.DuckDBPyConnection = Depends(_get_db)):
    _feature_gate()
    parent = _get_row_or_404(slug)
    _require_owner_or_admin(user, parent)
    repo = data_apps_repo()
    draft = repo.get_by_slug(draft_slug)
    if draft is None:
        raise HTTPException(status_code=404, detail="data_app_not_found")
    if not draft.get("is_draft") or draft.get("parent_app_id") != parent["id"]:
        raise HTTPException(status_code=400, detail="not_a_draft")
    # best-effort container teardown (mirror delete_data_app)
    try:
        _runner().stop(draft_slug, mode="recreate")
    except (RunnerUnavailable, RunnerError):
        pass
    _revoke_service_token(draft)
    from src.data_apps.git_repos import delete_branch
    try:
        delete_branch(slug, draft["draft_branch"])
    except ValueError:
        pass
    repo.delete(draft["id"])
    # remove the draft's config dir (its own per-slug dir; the git repo stays on the parent)
    _rmtree_config_dir(draft_slug)
    _audit(conn, user["id"], "data_app.draft_delete", f"data_app:{draft_slug}",
           {"parent": slug})
```

(If `delete_data_app` inlines the rmtree/`_revoke_service_token` rather than exposing helpers, extract `_rmtree_config_dir(slug)` and reuse; keep `delete_data_app` behavior identical.)

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/test_data_apps_api.py -k "draft or list or delete" -q` → PASS. Also run the full `tests/test_data_apps_api.py` to confirm the list-hiding change didn't break existing list tests (they seed only prod apps, so `include_drafts=False` is a no-op for them).

- [ ] **Step 5: Commit**

```bash
git add app/api/data_apps.py tests/test_data_apps_api.py
git commit -m "feat(data-apps): delete-draft, inline drafts in get, hide drafts from list"
```

---

### Task 7: Broker `data_apps` scope + spawn wiring

**Files:**
- Modify: `app/chat/manager.py` (mint ticket), `app/api/broker.py` (verify generic replay covers `/api/data-apps`)
- Test: `tests/test_broker.py` (or the existing broker test module — `grep -rln "require_broker_ticket\|/api/broker" tests/`)

**Interfaces:**
- Consumes: `ticket_repo().mint(session_id, scope)`, `_replay`, `_mint_identity_jwt`.
- Produces: a `data_apps`-scoped ticket minted at chat spawn; a broker route `POST /api/broker/data-apps` that replays the sandboxed agent's request onto `/api/data-apps/*` under the minted identity. The agent uses this instead of a raw PAT.

- [ ] **Step 1: Read `app/api/broker.py`** — confirm whether `/api/broker/agnes-api` already replays arbitrary `/api/*` paths (it replays `body["path"]` + `body["body"]`). If it does and only gates on scope `"main"`, the cleanest wave-3B move is a **twin endpoint** `POST /api/broker/data-apps` gated on scope `"data_apps"` that reuses `_replay` but restricts the replayed path prefix to `/api/data-apps`. Write the failing test first:

```python
def test_broker_data_apps_scope(broker_env):
    # broker_env mints a data_apps-scoped ticket and returns (client, ticket)
    client, ticket = broker_env
    r = client.post("/api/broker/data-apps",
                    headers={"Authorization": f"Bearer {ticket}"},
                    json={"path": "/api/data-apps", "method": "GET"})
    assert r.status_code == 200

def test_broker_data_apps_wrong_scope_rejected(broker_env_main_scope):
    client, ticket = broker_env_main_scope
    r = client.post("/api/broker/data-apps",
                    headers={"Authorization": f"Bearer {ticket}"},
                    json={"path": "/api/data-apps", "method": "GET"})
    assert r.status_code == 401 and r.json()["detail"] == "ticket_scope_mismatch"

def test_broker_data_apps_path_confined(broker_env):
    client, ticket = broker_env
    r = client.post("/api/broker/data-apps",
                    headers={"Authorization": f"Bearer {ticket}"},
                    json={"path": "/api/admin/users", "method": "GET"})
    assert r.status_code == 403 and r.json()["detail"] == "path_not_allowed"
```

(Model `broker_env` on the existing broker test's ticket-minting fixture — `grep` the broker test file for how it mints a ticket + builds the client.)

- [ ] **Step 2: Run to fail** — FAIL (no `/api/broker/data-apps`).

- [ ] **Step 3: Implement** — in `app/api/broker.py`, add:

```python
@router.post("/data-apps")
async def data_apps_broker(request: Request,
                           row: Dict[str, Any] = Depends(require_broker_ticket)) -> Response:
    """Replay a sandboxed agent's data-apps request under the ticket identity."""
    _require_scope(row, "data_apps")
    body = await request.json()
    path = (body.get("path") or "")
    if not path.startswith("/api/data-apps"):
        raise HTTPException(status_code=403, detail="path_not_allowed")
    resp = await _replay(request, row, body)
    return _to_response(resp)
```

- [ ] **Step 4: Wire spawn** — in `app/chat/manager.py`, next to the existing `main`/`mcp` mints (~L1609), add:

```python
        data_apps = ticket_repo().mint(live.chat_id, "data_apps")
```

and thread `data_apps` into wherever `main`/`mcp` tickets are handed to the sandbox env/config (follow the exact plumbing the `mcp` ticket uses — it's injected into the sandbox as an env var / passed to the runner; mirror it as `AGNES_DATA_APPS_TICKET`).

- [ ] **Step 5: Run** — the broker tests + `.venv/bin/pytest tests/ -k "broker" -q` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api/broker.py app/chat/manager.py tests/
git commit -m "feat(data-apps): broker data_apps scope for sandboxed authoring"
```

---

### Task 8: MCP tools + CLI + ratchet + docs/CHANGELOG

**Files:**
- Modify: `app/api/mcp/foundation_tools.py`, `cli/commands/data_apps.py`, `cli/main.py` (if a new sub-group), `tests/test_documentation_api_triple_surface.py`, `tests/test_mcp_tool_parity.py`, `tests/test_mcp_http.py`, `CHANGELOG.md`, `docs/DEPLOYMENT.md`
- Test: `tests/test_cli_data_apps.py`, the MCP parity tests

**Interfaces:**
- Produces MCP tools (mirror `data_app_deploy`'s shape): `data_app_create_draft(slug, branch="init")`, `data_app_delete_draft(slug, draft_slug)`, `data_app_git_credential(slug)`, and extend `data_app_deploy(slug, sha="", mode="")` to pass `mode`. CLI: `agnes app draft create <slug> [--branch]`, `agnes app draft delete <slug> <draft_slug>`, `agnes app git-credential <slug>`, `agnes app deploy <slug> --mode dev`.

- [ ] **Step 1: MCP parity test first** — add the 3 new tool names to the expected lists in `tests/test_mcp_tool_parity.py` AND `tests/test_mcp_http.py` (the second static allowlist, per the wave-1+2 finding). Run → FAIL.

- [ ] **Step 2: Implement MCP tools** — in `app/api/mcp/foundation_tools.py`, after `data_app_logs`, mirror the `data_app_deploy` closure shape:

```python
    @mcp.tool()
    async def data_app_create_draft(slug: str, branch: str = "init") -> dict:
        """Create a draft of a prod data app on an iteration branch (owner/Admin).
        Returns the draft slug + a git_clone_url with an embedded push credential."""
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base_url}/api/data-apps/{slug}/drafts",
                             headers=headers_fn(), json={"branch": branch}, timeout=60)
            r.raise_for_status(); return r.json()

    @mcp.tool()
    async def data_app_delete_draft(slug: str, draft_slug: str) -> dict:
        """Tear down a draft of prod app <slug> (owner/Admin)."""
        async with httpx.AsyncClient() as c:
            r = await c.request("DELETE", f"{base_url}/api/data-apps/{slug}/drafts/{draft_slug}",
                                headers=headers_fn(), timeout=60)
            r.raise_for_status(); return {"status": "deleted"}

    @mcp.tool()
    async def data_app_git_credential(slug: str) -> dict:
        """Mint a fresh git push credential (clone URL) for a data app (owner/Admin)."""
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base_url}/api/data-apps/{slug}/git-credential",
                             headers=headers_fn(), timeout=30)
            r.raise_for_status(); return r.json()
```

Extend `data_app_deploy`:

```python
    @mcp.tool()
    async def data_app_deploy(slug: str, sha: str = "", mode: str = "") -> dict:
        """Deploy a data app. mode='dev' deploys a draft's branch; empty deploys prod."""
        payload: dict = {}
        if sha: payload["sha"] = sha
        if mode: payload["mode"] = mode
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{base_url}/api/data-apps/{slug}/deploy",
                             headers=headers_fn(), json=payload, timeout=60)
            r.raise_for_status(); return r.json()
```

Add the 3 new names to the `FOUNDATION_TOOL_NAMES` list at the top of the file.

- [ ] **Step 3: CLI** — in `cli/commands/data_apps.py` add a `draft` sub-typer + `git-credential` command, and `--mode` on deploy. Add the new detail codes (`parent_is_draft`, `invalid_branch`, `dev_requires_draft`, `prod_on_draft`, `not_a_draft`, `parent_has_no_main`, `path_not_allowed`) to `_ERROR_MESSAGES`:

```python
draft_app = typer.Typer(help="Manage data-app drafts")
data_apps_app.add_typer(draft_app, name="draft")

@draft_app.command("create")
def draft_create(slug: str = typer.Argument(...), branch: str = typer.Option("init", "--branch"),
                 json: bool = typer.Option(False, "--json")):
    resp = api_post(f"/api/data-apps/{slug}/drafts", json={"branch": branch})
    if resp.status_code == 404: _not_found(slug)
    if resp.status_code != 201: _fail(resp)
    _emit(resp.json(), json)

@draft_app.command("delete")
def draft_delete(slug: str = typer.Argument(...), draft_slug: str = typer.Argument(...)):
    resp = api_delete(f"/api/data-apps/{slug}/drafts/{draft_slug}")
    if resp.status_code != 204: _fail(resp)
    typer.echo(f"Deleted draft {draft_slug}")

@data_apps_app.command("git-credential")
def git_credential(slug: str = typer.Argument(...), json: bool = typer.Option(False, "--json")):
    resp = api_post(f"/api/data-apps/{slug}/git-credential")
    if resp.status_code == 404: _not_found(slug)
    if resp.status_code != 200: _fail(resp)
    _emit(resp.json(), json)
```

Deploy `--mode` (edit the existing `deploy_app`):

```python
@data_apps_app.command("deploy")
def deploy_app(slug: str = typer.Argument(...), sha: str = typer.Option(None, "--sha"),
               mode: str = typer.Option(None, "--mode"), json: bool = typer.Option(False, "--json")):
    payload = {}
    if sha: payload["sha"] = sha
    if mode: payload["mode"] = mode
    resp = api_post(f"/api/data-apps/{slug}/deploy", json=payload)
    if resp.status_code == 404: _not_found(slug)
    if resp.status_code != 200: _fail(resp)
    _emit(resp.json(), json)
```

(If there's no `_emit` helper, follow the file's existing echo/`--json` pattern from `show`.)

- [ ] **Step 4: Ratchet** — in `tests/test_documentation_api_triple_surface.py`: move `/api/data-apps/{slug}/deploy` stays in `_COHORT` (still has CLI+MCP); add new `_COHORT` entries for the routes that now have all three surfaces:

```python
    "/api/data-apps/{slug}/drafts": ("app draft create", "data_app_create_draft"),
    "/api/data-apps/{slug}/drafts/{draft_slug}": ("app draft delete", "data_app_delete_draft"),
    "/api/data-apps/{slug}/git-credential": ("app git-credential", "data_app_git_credential"),
```

The broker route `/api/broker/data-apps` is broker-internal — add to `_EXEMPT` with a reason (`"broker replay surface for the sandboxed authoring agent; not a user-facing API"`).

- [ ] **Step 5: Run all** — `.venv/bin/pytest tests/test_mcp_tool_parity.py tests/test_mcp_http.py tests/test_documentation_api_triple_surface.py tests/test_cli_data_apps.py -q` → PASS.

- [ ] **Step 6: Docs + CHANGELOG** — CHANGELOG under `### Added`:

```markdown
- Data Apps: prod + draft iteration model — create a draft on an iteration branch
  (`agnes app draft create`), deploy it in `dev` mode, then promote by merging into
  `main`; drafts share the prod app's git repo and are hidden from the app list.
  New MCP tools (`data_app_create_draft`, `data_app_delete_draft`, `data_app_git_credential`)
  and a broker `data_apps` scope let the chat agent author apps end-to-end.
```

`docs/DEPLOYMENT.md` "Data apps" section: add a short "Draft iteration" paragraph pointing at the ai-kit `dataapp-development` skill.

- [ ] **Step 7: Full suite + commit**

```bash
.venv/bin/pytest tests/ --tb=short -n auto -q   # expect green modulo the documented pre-existing failures
git add app/api/mcp/foundation_tools.py cli/commands/data_apps.py cli/main.py \
  tests/test_documentation_api_triple_surface.py tests/test_mcp_tool_parity.py \
  tests/test_mcp_http.py tests/test_cli_data_apps.py CHANGELOG.md docs/DEPLOYMENT.md
git commit -m "feat(data-apps): draft/credential MCP tools, CLI, ratchet, docs"
```

---

## Self-review notes (applied)

- Spec §6 draft model ↔ Tasks 1 (columns) + 2 (branch config/git) + 4 (create) + 5 (dev deploy) + 6 (delete/inline). Draft = registry sibling sharing the parent repo, deployed from a pinned branch — matches "no second repo, no copy."
- Spec §5 tool table ↔ Tasks 3 (git-credential), 4 (create-draft), 5 (deploy mode), 6 (delete-draft, get inlines drafts), 8 (MCP/CLI). `data_app_update` and preview tools are out of 3B scope (preview = 3C).
- Spec §9 surfaces ↔ Task 8 ratchet + CLI + MCP; broker replay ↔ Task 7 (mirrors `app/api/broker.py` `_replay` + a `data_apps` scope, the spec's "broker-ticket pattern").
- Schema correction applied: data-apps drafts are **v98** (v97 is `corpus_files.path`); Alembic `0045`, `down_revision 0044_corpus_files_path_v97`.
- Type consistency: `create_draft(*, parent_app_id, slug, branch, owner_user_id, …)`, `list_drafts(parent_app_id)`, `_mint_git_credential(row) -> str`, `ensure_branch(slug, branch, base="main")`, `delete_branch(slug, branch)`, `DeployRequest.mode`, draft slug `<parent>--<branch>` — used consistently across tasks.
- Promote flow (merge draft→main, redeploy prod, delete draft) is executed by the **agent over git + existing deploy/delete-draft tools** (spec §6) — no new endpoint needed; it's exercised end-to-end in wave 3C's acceptance test, not here.
- Out of 3B scope (deferred to 3C): preview chat tools (`agnes_data_app_preview/refresh/close/credentials`), scoped preview grant, the `agnes-data-apps-extras` skill, scaffold baking, marketplace registration.
