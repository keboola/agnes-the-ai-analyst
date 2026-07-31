# Global (User-Scope) Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Agnes skills + data access available in every repository a user opens, via one idempotent `agnes global enable`, per `docs/superpowers/specs/2026-07-30-global-user-scope-distribution-design.md`.

**Architecture:** Two PRs. PR 1 (Tasks 1–6) adds a shared anchored-workspace resolver and adopts it across the data-read CLI surface and the stdio MCP server, so `agnes query/pull/…` work from any cwd. PR 2 (Tasks 7–14) adds `cli/lib/user_scope.py` (marker-fenced `~/.claude/CLAUDE.md` block + user-level SessionStart hook writers), a `--target user|project` mode for the marketplace reconcile (skipping the cwd `enabledPlugins` writer in user mode), the `agnes global enable|disable|status` command group, an `agnes update` `global` step, and the docs page.

**Tech Stack:** Python 3.11+, Typer CLI, pytest (fixtures monkeypatching `subprocess.run` / `shutil.which`), the `claude` CLI as the only writer of Claude-Code-owned JSON.

## Global Constraints

Copied from the spec + repo rules; every task implicitly includes these.

- **PR boundaries:** Tasks 1–6 = PR 1; Tasks 7–14 = PR 2. The user-level hook (Task 11) and the `--target` reconcile (Task 9) MUST ship in the same release (spec §7.3) — both are in PR 2; never cherry-pick the hook forward.
- **Vendor-agnostic:** no customer names, hostnames, project IDs anywhere in code, tests, docs, commits. Placeholders only (`<agnes-host>`, `example.com`).
- **No DB change anywhere** → no DuckDB↔PG parity or migration obligations (spec §10). Do not touch `src/repositories/` or `src/db.py`.
- **Command-UX standard:** `--scope` is reserved for the frozen `auto|local|server` data-locality enumeration — the new reconcile flag is `--target user|project` (spec §7.1). New commands ship `--json`. "Not found" errors carry a next-step hint via `cli/error_render.py:render_error` typed shapes (`partial_state`, `auth_failed`).
- **User-file discipline (spec §6.4):** JSON owned by Claude Code (`enabledPlugins`, `mcpServers`) is mutated only via the `claude` CLI. Direct edits are limited to `~/.claude/CLAUDE.md` (marker splice) and the `hooks` key in user settings — both via `cli/lib/user_scope.py`, both "on anything unexpected: warn + leave the file untouched" (never back up + rebuild a user-owned file).
- **CHANGELOG:** bullets land in the same PR (Tasks 6 and 14). Release-cut is handled at merge time by the release process, not by this plan.
- **Before every push:** `.venv/bin/pytest tests/ --tb=short -n auto -q` (what CI runs). A PostToolUse hook auto-runs ruff/mypy on edited Python files — if it reformats, include the result in the same commit.
- **Hook command literal** (used twice, must match `cli/lib/hooks.py:216` exactly): `bash -c "( nohup agnes update --quiet </dev/null >/dev/null 2>&1 & ) ; true"`.
- Config keys written by this feature: `global_scope: bool` (layer enabled) and `global_hook: bool` (user-level hook wanted; false under `--no-hook`). Both via `cli/config.py::save_config` (merge-safe).

---

# Phase 1 — PR 1: anchored workspace resolution (spec §5)

### Task 1: Shared resolver `cli/lib/workspace_resolve.py`

**Files:**
- Create: `cli/lib/workspace_resolve.py`
- Test: `tests/test_workspace_resolve.py`

**Interfaces:**
- Consumes: `cli.config.get_workspace_root()` (existing; reads `~/.config/agnes/config.yaml`, honors `AGNES_CONFIG_DIR`).
- Produces: `is_workspace_shaped(p: Path) -> bool` and `resolve_data_workspace() -> Optional[Path]` — every later task imports these two names exactly.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspace_resolve.py
"""Resolver matrix for cli/lib/workspace_resolve.py (spec §5.1).

Precedence: AGNES_LOCAL_DIR env (always wins, even unshaped) →
cwd-if-shaped → workspace_root-if-shaped → None. A stale anchor
(deleted dir / unshaped) degrades to None, never to a bogus path.
"""

from pathlib import Path

import pytest

from cli.lib.workspace_resolve import is_workspace_shaped, resolve_data_workspace


def _make_shaped(p: Path, marker: str = "sentinel") -> Path:
    p.mkdir(parents=True, exist_ok=True)
    if marker == "sentinel":
        (p / ".claude").mkdir(parents=True, exist_ok=True)
        (p / ".claude" / "init-complete").write_text("x", encoding="utf-8")
    elif marker == "duckdb":
        (p / "user" / "duckdb").mkdir(parents=True, exist_ok=True)
        (p / "user" / "duckdb" / "analytics.duckdb").write_bytes(b"")
    elif marker == "parquet":
        (p / "server" / "parquet").mkdir(parents=True, exist_ok=True)
    return p


@pytest.mark.parametrize("marker", ["sentinel", "duckdb", "parquet"])
def test_is_workspace_shaped_markers(tmp_path, marker):
    assert is_workspace_shaped(_make_shaped(tmp_path / "ws", marker)) is True


def test_is_workspace_shaped_negative(tmp_path):
    plain = tmp_path / "repo"
    plain.mkdir()
    assert is_workspace_shaped(plain) is False


def test_env_wins_even_when_unshaped(tmp_path, monkeypatch):
    target = tmp_path / "env-target"
    target.mkdir()
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(target))
    monkeypatch.chdir(_make_shaped(tmp_path / "cwd-ws"))
    assert resolve_data_workspace() == target.resolve()


def test_cwd_shaped_beats_anchor(tmp_path, monkeypatch):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    anchor = _make_shaped(tmp_path / "anchor")
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    cwd = _make_shaped(tmp_path / "cwd-ws")
    monkeypatch.chdir(cwd)
    assert resolve_data_workspace() == cwd.resolve()


def test_anchor_used_when_cwd_unshaped(tmp_path, monkeypatch):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    anchor = _make_shaped(tmp_path / "anchor")
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert resolve_data_workspace() == anchor.resolve()


def test_stale_anchor_degrades_to_none(tmp_path, monkeypatch):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr(
        "cli.lib.workspace_resolve.get_workspace_root",
        lambda: str(tmp_path / "deleted-anchor"),
    )
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert resolve_data_workspace() is None


def test_no_signals_none(tmp_path, monkeypatch):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: None)
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert resolve_data_workspace() is None


def test_precedence_differs_from_update_resolver(tmp_path, monkeypatch):
    """Pin that the two resolvers are DELIBERATELY different (spec §5.1/§10):

    data reads prefer the workspace you stand in (cwd before anchor);
    `agnes update` converges the anchor (anchor before cwd). A dedup
    refactor collapsing them would change foreign-repo-safety behavior.
    """
    from cli.commands.update import _resolve_workspace as update_resolve

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    anchor = _make_shaped(tmp_path / "anchor")
    cwd = _make_shaped(tmp_path / "cwd-ws")  # sentinel marker => update sees it too
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    monkeypatch.setattr("cli.commands.update.get_workspace_root", lambda: str(anchor))
    monkeypatch.chdir(cwd)
    assert resolve_data_workspace() == cwd.resolve()   # cwd first
    assert update_resolve() == anchor.resolve()        # anchor first
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_workspace_resolve.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cli.lib.workspace_resolve'`

- [ ] **Step 3: Implement the resolver**

```python
# cli/lib/workspace_resolve.py
"""Anchored workspace resolution for data commands (spec §5.1).

Order: ``AGNES_LOCAL_DIR`` env (explicit override, always wins even when
the target is not workspace-shaped — the sandbox/runner contract) →
cwd, if workspace-shaped (preserves pre-existing behaviour for anyone
standing inside a workspace) → ``workspace_root`` config, if
workspace-shaped (the global fallback; a stale anchor degrades to None,
never to reads against a bogus path) → ``None``.

Deliberately DIFFERENT from ``cli/commands/update.py::_resolve_workspace``
(env → anchor → cwd-if-initialised): convergence must target the anchor
even when run from inside some other initialised folder, while data reads
prefer the workspace you are standing in. Pinned by
``tests/test_workspace_resolve.py::test_precedence_differs_from_update_resolver``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from cli.config import get_workspace_root


def is_workspace_shaped(p: Path) -> bool:
    """True when ``p`` looks like an Agnes workspace: the init sentinel,
    a local analytics DuckDB, or a parquet tree."""
    try:
        return (
            (p / ".claude" / "init-complete").exists()
            or (p / "user" / "duckdb" / "analytics.duckdb").exists()
            or (p / "server" / "parquet").is_dir()
        )
    except OSError:
        return False


def resolve_data_workspace() -> Optional[Path]:
    env_dir = os.environ.get("AGNES_LOCAL_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    cwd = Path.cwd()
    if is_workspace_shaped(cwd):
        return cwd.resolve()
    root = get_workspace_root()
    if root:
        anchor = Path(root)
        if is_workspace_shaped(anchor):
            return anchor.resolve()
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_workspace_resolve.py -q`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add cli/lib/workspace_resolve.py tests/test_workspace_resolve.py
git commit -m "feat(cli): shared anchored-workspace resolver for data commands"
```

### Task 2: Adopt in `agnes query` (local path)

**Files:**
- Modify: `cli/commands/query.py:152-157` (`_run_local`)
- Test: `tests/test_workspace_resolve.py` (append)

**Interfaces:**
- Consumes: `resolve_data_workspace()` from Task 1; existing `_LocalDbMissing` exception in `cli/commands/query.py`.
- Produces: no new names — behavior only.

- [ ] **Step 1: Write the failing test** (append to `tests/test_workspace_resolve.py`)

```python
def test_query_run_local_falls_back_to_anchor(tmp_path, monkeypatch):
    """From an unshaped cwd, _run_local opens the ANCHOR's DuckDB (spec §5.2)."""
    import duckdb

    from cli.commands import query as query_module

    anchor = tmp_path / "anchor"
    (anchor / "user" / "duckdb").mkdir(parents=True)
    con = duckdb.connect(str(anchor / "user" / "duckdb" / "analytics.duckdb"))
    con.execute("CREATE TABLE t AS SELECT 42 AS answer")
    con.close()

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)

    printed: list[str] = []
    monkeypatch.setattr(query_module.typer, "echo", lambda *a, **k: printed.append(str(a[0]) if a else ""))
    query_module._run_local("SELECT answer FROM t", fmt="csv", limit=10)
    assert any("42" in line for line in printed)


def test_query_run_local_none_raises_missing(tmp_path, monkeypatch):
    from cli.commands import query as query_module

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: None)
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    with pytest.raises(query_module._LocalDbMissing):
        query_module._run_local("SELECT 1", fmt="csv", limit=10)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_workspace_resolve.py -q -k query_run_local`
Expected: `test_query_run_local_falls_back_to_anchor` FAILS (raises `_LocalDbMissing` — cwd has no DB and no fallback exists yet). If `_run_local`'s signature differs (check the actual keyword names at `cli/commands/query.py:144`), adapt the test call, not the production code.

- [ ] **Step 3: Implement**

In `cli/commands/query.py` replace (currently at :154-155):

```python
    local_dir = Path(os.environ.get("AGNES_LOCAL_DIR", "."))
    db_path = local_dir / "user" / "duckdb" / "analytics.duckdb"
```

with:

```python
    from cli.lib.workspace_resolve import resolve_data_workspace

    local_dir = resolve_data_workspace()
    if local_dir is None:
        raise _LocalDbMissing()
    db_path = local_dir / "user" / "duckdb" / "analytics.duckdb"
```

(Local import mirrors the file's existing lazy-import style; the existing `if not db_path.exists(): raise _LocalDbMissing()` stays.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_workspace_resolve.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cli/commands/query.py tests/test_workspace_resolve.py
git commit -m "feat(cli): agnes query --local resolves the anchored workspace from any cwd"
```

### Task 3: Adopt in `agnes pull` + `--workspace` + no-scaffold guard

**Files:**
- Modify: `cli/commands/pull.py:37-92` (callback signature + workspace resolution)
- Test: `tests/test_workspace_resolve.py` (append)

**Interfaces:**
- Consumes: `resolve_data_workspace()`; existing `render_error` typed shapes.
- Produces: `agnes pull --workspace <dir>` option (also consumed by docs in Task 6).

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_pull_refuses_to_scaffold_foreign_cwd(tmp_path, monkeypatch):
    """No workspace anywhere -> typed error, NOTHING written into cwd (§5.2 + §8 guard)."""
    from typer.testing import CliRunner

    from cli.commands.pull import pull_app

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: None)
    monkeypatch.setenv("AGNES_SERVER", "http://localhost:9")   # never reached
    monkeypatch.setenv("AGNES_TOKEN", "t")
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)

    result = CliRunner(mix_stderr=False).invoke(pull_app, [])
    assert result.exit_code == 1
    assert "agnes init" in (result.stderr or "")
    assert not (plain / "server").exists()
    assert not (plain / "user").exists()


def test_pull_workspace_flag_wins(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cli.commands import pull as pull_module

    target = tmp_path / "explicit-ws"
    target.mkdir()
    seen: dict = {}

    def fake_run_pull(server_url, token, workspace, **kw):
        seen["workspace"] = Path(workspace)

        class R:  # minimal PullResult stand-in for the summary printer
            tables_updated = 0
            tables_removed = 0
            parquets_total = 0
            rules_count = 0
            errors: list = []
            duration_s = 0.0
            stack_sync = None

        return R()

    monkeypatch.setattr(pull_module, "run_pull", fake_run_pull)
    monkeypatch.setenv("AGNES_SERVER", "http://localhost:9")
    monkeypatch.setenv("AGNES_TOKEN", "t")
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)

    result = CliRunner().invoke(pull_module.pull_app, ["--workspace", str(target), "--quiet"])
    assert result.exit_code == 0, result.output
    assert seen["workspace"] == target.resolve()
```

Note for the implementer: open `cli/commands/pull.py` and check what the
summary printer reads off `PullResult` after `run_pull` returns; extend the
`R` stub with any additional attributes it touches so the `--quiet` path
runs clean. Adapt the stub, never the production printer.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_workspace_resolve.py -q -k pull`
Expected: FAIL (`--workspace` unknown option; foreign-cwd test currently proceeds toward a manifest fetch instead of the typed error)

- [ ] **Step 3: Implement**

In `cli/commands/pull.py`:

1. Add to the callback signature (after `skip_materialize`):

```python
    workspace_str: Optional[str] = typer.Option(
        None,
        "--workspace",
        help="Target workspace dir (default: AGNES_LOCAL_DIR, else the current dir if it is a workspace, else the anchored workspace_root).",
    ),
```

(add `from typing import Optional` to the imports)

2. Replace (currently at :89):

```python
    workspace = Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()
```

with:

```python
    if workspace_str:
        workspace = Path(workspace_str).resolve()
    else:
        from cli.lib.workspace_resolve import resolve_data_workspace

        resolved = resolve_data_workspace()
        if resolved is None:
            typer.echo(
                render_error(
                    0,
                    {
                        "detail": {
                            "kind": "partial_state",
                            "hint": (
                                "No workspace found — run `agnes init` first, or pass "
                                "--workspace <dir> / set AGNES_LOCAL_DIR. Refusing to "
                                "download data into an arbitrary directory."
                            ),
                        }
                    },
                ),
                err=True,
            )
            raise typer.Exit(1)
        workspace = resolved
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_workspace_resolve.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cli/commands/pull.py tests/test_workspace_resolve.py
git commit -m "feat(cli): agnes pull anchors to the workspace and refuses to scaffold a foreign cwd"
```

### Task 4: Adopt in snapshot/status/disk-info/explore/statusline/mark-private/self-upgrade

**Files:**
- Modify (one-line-per-site, exact locations):
  - `cli/commands/snapshot.py:32` (`_local_dir`)
  - `cli/commands/status.py:38`
  - `cli/commands/disk_info.py:13` (`_local_dir`)
  - `cli/commands/explore.py:28`
  - `cli/commands/statusline.py:280`
  - `cli/commands/mark_private.py:53`
  - `cli/commands/self_upgrade.py:617` and `:638`
- Test: `tests/test_workspace_resolve.py` (append)

**Interfaces:**
- Consumes: `resolve_data_workspace()`.
- Produces: nothing new. Rule (spec §5.2): **data-critical** sites error via their existing miss-paths; **diagnostic/display** sites use `resolve_data_workspace() or Path.cwd()` so their "nothing anywhere" behavior is byte-identical to today.

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_snapshot_local_dir_uses_anchor(tmp_path, monkeypatch):
    from cli.commands import snapshot as snapshot_module

    anchor = _make_shaped(tmp_path / "anchor")
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert snapshot_module._local_dir() == anchor.resolve()


def test_disk_info_local_dir_falls_back_to_cwd_when_nothing(tmp_path, monkeypatch):
    from cli.commands import disk_info as disk_info_module

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: None)
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert disk_info_module._local_dir() == plain.resolve()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_workspace_resolve.py -q -k "snapshot_local or disk_info_local"`
Expected: `test_snapshot_local_dir_uses_anchor` FAILS (returns the foreign cwd today)

- [ ] **Step 3: Implement — same one-line substitution at every site**

Add near the top of each of the seven files: `from cli.lib.workspace_resolve import resolve_data_workspace` (or a local import inside the function, matching each file's import style). Then replace each occurrence of

```python
Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()
```

with

```python
(resolve_data_workspace() or Path.cwd().resolve())
```

at: `snapshot.py:32`, `status.py:38`, `disk_info.py:13`, `statusline.py:280`, `mark_private.py:53`, `self_upgrade.py:617`, `self_upgrade.py:638`. In `explore.py:28` the pattern lacks `.resolve()` — replace `Path(os.environ.get("AGNES_LOCAL_DIR", "."))` with the same expression. `resolve_data_workspace()` already honors `AGNES_LOCAL_DIR` as its first step, so the env override behaves exactly as before at every site. Also update `self_upgrade.py:627-628`'s docstring sentence to: "Resolves the workspace via ``cli.lib.workspace_resolve.resolve_data_workspace()`` (env override → shaped cwd → anchored workspace_root), falling back to the current working directory."

`mark_private.py:51-53` already prefers `workspace_root` explicitly; simplify the whole conditional to a single `(resolve_data_workspace() or Path.cwd().resolve())` only if the surrounding logic reads identically afterward — otherwise substitute just the `else` branch.

- [ ] **Step 4: Run to verify pass + no regressions**

Run: `.venv/bin/pytest tests/test_workspace_resolve.py tests/ -q -n auto -k "snapshot or status or disk or explore or statusline or mark_private or self_upgrade"`
Expected: PASS (pre-existing tests for these commands keep passing because the cwd fallback preserves old behavior when nothing resolves)

- [ ] **Step 5: Commit**

```bash
git add cli/commands/snapshot.py cli/commands/status.py cli/commands/disk_info.py cli/commands/explore.py cli/commands/statusline.py cli/commands/mark_private.py cli/commands/self_upgrade.py tests/test_workspace_resolve.py
git commit -m "feat(cli): diagnostic commands resolve the anchored workspace from any cwd"
```

### Task 5: Adopt in the stdio MCP server + docstring updates

**Files:**
- Modify: `cli/mcp/server.py:321` (`query_local`), `:376` (`pull`), both tools' docstrings
- Test: `tests/test_workspace_resolve.py` (append)

**Interfaces:**
- Consumes: `resolve_data_workspace()`.
- Produces: nothing new — the MCP tools now work when spawned with cwd `/` or `$HOME` (user-scope registration, Claude Desktop).

- [ ] **Step 1: Write the failing test** (append)

```python
def test_mcp_query_local_uses_anchor(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    import duckdb

    from cli.mcp import server as mcp_server

    anchor = tmp_path / "anchor"
    (anchor / "user" / "duckdb").mkdir(parents=True)
    con = duckdb.connect(str(anchor / "user" / "duckdb" / "analytics.duckdb"))
    con.execute("CREATE TABLE t AS SELECT 7 AS n")
    con.close()

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    plain = tmp_path / "spawned-at-home"
    plain.mkdir()
    monkeypatch.chdir(plain)

    out = mcp_server.query_local("SELECT n FROM t")
    assert out["rows"] == [[7]]
```

(`query_local` is registered via `@mcp.tool()` — FastMCP keeps the plain function callable; if the decorator wraps it, call `mcp_server.query_local.fn(...)` instead — check which one imports cleanly and use that.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_workspace_resolve.py -q -k mcp_query_local`
Expected: FAIL with `FileNotFoundError: Local DuckDB not found at .../spawned-at-home/...`

- [ ] **Step 3: Implement**

In `cli/mcp/server.py`, replace at both :321 and :376:

```python
    workspace = Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()
```

with:

```python
    from cli.lib.workspace_resolve import resolve_data_workspace

    workspace = resolve_data_workspace()
    if workspace is None:
        raise FileNotFoundError(
            "No Agnes workspace found (checked AGNES_LOCAL_DIR, the current "
            "directory, and the anchored workspace_root). Run `agnes init` "
            "on this machine first."
        )
```

Then extend both tools' docstrings (agent-facing UX — spec §5.2) with one sentence each:
- `query_local`: append to the "Returns…" paragraph: `The workspace is resolved from AGNES_LOCAL_DIR, else the current directory when it is a workspace, else the workspace anchored by ``agnes init`` — so this works regardless of where the MCP client spawned the process.`
- `pull`: append the same sentence.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_workspace_resolve.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cli/mcp/server.py tests/test_workspace_resolve.py
git commit -m "feat(mcp): stdio server resolves the anchored workspace from any spawn cwd"
```

### Task 6: PR 1 docs sweep + CHANGELOG + full suite

**Files:**
- Modify: `CHANGELOG.md` (`## [Unreleased]`), `cli/commands/pull.py` (docstring), any `docs/` page found by the sweep

**Interfaces:** none — text only.

- [ ] **Step 1: Sweep for stale cwd claims**

Run: `grep -rn "AGNES_LOCAL_DIR\|current dir\|cwd" docs/ cli/commands/pull.py cli/commands/query.py --include="*.md" --include="*.py" | grep -iv test`
Update every hit that asserts data commands read "the current directory" as the final word — including `cli/commands/pull.py:34` docstring line `Refresh data from the server into ./server/parquet + ./user/duckdb.` → `Refresh data from the server into the workspace's server/parquet + user/duckdb (resolved via AGNES_LOCAL_DIR → shaped cwd → anchored workspace_root).`

- [ ] **Step 2: CHANGELOG bullets** (under `## [Unreleased]`)

```markdown
### Changed
- CLI data commands (`query --local`, `pull`, `snapshot`, `status`, `disk-info`, `explore`, `statusline`, `mark-private`) and the stdio MCP server (`query_local`, `pull`) now fall back to the workspace anchored by `agnes init` when run outside a workspace directory — Agnes data access works from any repository. `AGNES_LOCAL_DIR` still overrides; behavior inside a workspace is unchanged. `agnes pull` gains `--workspace <dir>`.

### Fixed
- `agnes pull` no longer scaffolds a `server/parquet` + `user/duckdb` tree into an arbitrary current directory when no workspace exists — it errors with a typed hint instead.
```

- [ ] **Step 3: Full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: PASS (investigate any failure in touched files; unrelated pre-existing failures: confirm on a clean base per repo policy, note in PR body)

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md docs/ cli/commands/pull.py
git commit -m "docs(cli): document anchored-workspace resolution; changelog for PR 1"
```

---

# Phase 2 — PR 2: `agnes global` + convergence + docs (spec §6–§9)

### Task 7: `cli/lib/user_scope.py` — rails block splice

**Files:**
- Create: `cli/lib/user_scope.py`
- Test: `tests/test_user_scope.py`

**Interfaces:**
- Consumes: nothing project-specific.
- Produces (exact names later tasks import):
  - `RAILS_BEGIN = "<!-- BEGIN agnes-global (managed by 'agnes global enable'; edits inside are overwritten) -->"`
  - `RAILS_END = "<!-- END agnes-global -->"`
  - `user_claude_md_path() -> Path` (`Path.home() / ".claude" / "CLAUDE.md"`)
  - `upsert_rails_block(claude_md: Path, content: str) -> str` → `"created" | "updated" | "unchanged" | "skipped_malformed"`
  - `remove_rails_block(claude_md: Path) -> str` → `"removed" | "absent" | "skipped_malformed"`
  - `rails_block_state(claude_md: Path, content: str) -> str` → `"ok" | "missing" | "drifted" | "malformed"`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_user_scope.py
"""cli/lib/user_scope.py — marker splice + user-level hook writers (spec §6.4).

Recovery philosophy under test: on anything unexpected, warn and leave the
user's file untouched. Never rebuild a user-owned file.
"""

from pathlib import Path

from cli.lib.user_scope import (
    RAILS_BEGIN,
    RAILS_END,
    rails_block_state,
    remove_rails_block,
    upsert_rails_block,
)

RAILS = "line one\nline two\n"


def test_upsert_creates_file_and_block(tmp_path):
    md = tmp_path / "CLAUDE.md"
    assert upsert_rails_block(md, RAILS) == "created"
    text = md.read_text(encoding="utf-8")
    assert text.count(RAILS_BEGIN) == 1 and text.count(RAILS_END) == 1
    assert "line one" in text
    assert rails_block_state(md, RAILS) == "ok"


def test_upsert_appends_to_existing_content_untouched(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# my own rules\ndo not touch\n", encoding="utf-8")
    assert upsert_rails_block(md, RAILS) == "created"
    text = md.read_text(encoding="utf-8")
    assert text.startswith("# my own rules\ndo not touch\n")


def test_upsert_replaces_only_inside_markers(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(
        f"before\n{RAILS_BEGIN}\nOLD CONTENT\n{RAILS_END}\nafter\n",
        encoding="utf-8",
    )
    assert upsert_rails_block(md, RAILS) == "updated"
    text = md.read_text(encoding="utf-8")
    assert "OLD CONTENT" not in text
    assert text.startswith("before\n") and text.rstrip().endswith("after")
    assert rails_block_state(md, RAILS) == "ok"


def test_upsert_idempotent(tmp_path):
    md = tmp_path / "CLAUDE.md"
    upsert_rails_block(md, RAILS)
    before = md.read_text(encoding="utf-8")
    assert upsert_rails_block(md, RAILS) == "unchanged"
    assert md.read_text(encoding="utf-8") == before


def test_duplicated_markers_leave_file_untouched(tmp_path):
    md = tmp_path / "CLAUDE.md"
    broken = f"{RAILS_BEGIN}\na\n{RAILS_END}\n{RAILS_BEGIN}\nb\n{RAILS_END}\n"
    md.write_text(broken, encoding="utf-8")
    assert upsert_rails_block(md, RAILS) == "skipped_malformed"
    assert md.read_text(encoding="utf-8") == broken
    assert rails_block_state(md, RAILS) == "malformed"


def test_unmatched_marker_leaves_file_untouched(tmp_path):
    md = tmp_path / "CLAUDE.md"
    broken = f"{RAILS_BEGIN}\nno end marker\n"
    md.write_text(broken, encoding="utf-8")
    assert upsert_rails_block(md, RAILS) == "skipped_malformed"
    assert md.read_text(encoding="utf-8") == broken


def test_remove_strips_block_exactly(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("mine\n", encoding="utf-8")
    upsert_rails_block(md, RAILS)
    assert remove_rails_block(md) == "removed"
    assert md.read_text(encoding="utf-8") == "mine\n"


def test_remove_absent_and_state_missing(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("mine\n", encoding="utf-8")
    assert remove_rails_block(md) == "absent"
    assert rails_block_state(md, RAILS) == "missing"


def test_state_drifted_on_stale_content(tmp_path):
    md = tmp_path / "CLAUDE.md"
    upsert_rails_block(md, "old rails\n")
    assert rails_block_state(md, RAILS) == "drifted"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_user_scope.py -q`
Expected: FAIL — module missing

- [ ] **Step 3: Implement**

```python
# cli/lib/user_scope.py
"""User-scope (all-repositories) writers — spec §6.4.

Two direct-edit surfaces, both governed by the same recovery philosophy:
on anything unexpected, warn to stderr and leave the user's file untouched
(never back up + rebuild a user-owned file — mirrors `cli/lib/automode.py`).

1. `~/.claude/CLAUDE.md` rails block, fenced by exact markers.
2. The `hooks` key in `~/.claude/settings.json` (Task 10) — no `claude`
   CLI exists for hook management, so the entry is merged directly using
   the workspace installer's `_OUR_COMMAND_MARKERS` contract.

Claude-Code-owned JSON (`enabledPlugins`, `mcpServers`) is NEVER written
here — the `claude` CLI is the only writer for those (spec §6.4).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RAILS_BEGIN = "<!-- BEGIN agnes-global (managed by 'agnes global enable'; edits inside are overwritten) -->"
RAILS_END = "<!-- END agnes-global -->"


def user_claude_md_path() -> Path:
    return Path.home() / ".claude" / "CLAUDE.md"


def _split_on_block(text: str) -> tuple[str, str, str] | None:
    """(before, inside, after) for exactly one well-formed marker pair;
    None when markers are absent; raises ValueError when malformed
    (duplicated or unmatched markers)."""
    begins = text.count(RAILS_BEGIN)
    ends = text.count(RAILS_END)
    if begins == 0 and ends == 0:
        return None
    if begins != 1 or ends != 1:
        raise ValueError("duplicated markers")
    start = text.index(RAILS_BEGIN)
    end = text.index(RAILS_END)
    if end < start:
        raise ValueError("END before BEGIN")
    return (
        text[:start],
        text[start + len(RAILS_BEGIN) : end],
        text[end + len(RAILS_END) :],
    )


def _render_block(content: str) -> str:
    return f"{RAILS_BEGIN}\n{content.rstrip()}\n{RAILS_END}"


def _atomic_write(path: Path, text: str) -> None:
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".claudemd.", dir=str(path.parent))
    try:
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def upsert_rails_block(claude_md: Path, content: str) -> str:
    existing = claude_md.read_text(encoding="utf-8") if claude_md.exists() else None
    block = _render_block(content)
    if existing is None:
        _atomic_write(claude_md, block + "\n")
        return "created"
    try:
        parts = _split_on_block(existing)
    except ValueError:
        print(
            f"warn: {claude_md} has duplicated/unmatched agnes-global markers; "
            "left untouched. Repair by hand, then re-run `agnes global enable`.",
            file=sys.stderr,
        )
        return "skipped_malformed"
    if parts is None:
        sep = "" if existing.endswith("\n") else "\n"
        _atomic_write(claude_md, existing + sep + block + "\n")
        return "created"
    before, inside, after = parts
    if inside.strip("\n") == content.rstrip():
        return "unchanged"
    _atomic_write(claude_md, before + block + after)
    return "updated"


def remove_rails_block(claude_md: Path) -> str:
    if not claude_md.exists():
        return "absent"
    existing = claude_md.read_text(encoding="utf-8")
    try:
        parts = _split_on_block(existing)
    except ValueError:
        print(
            f"warn: {claude_md} has duplicated/unmatched agnes-global markers; left untouched.",
            file=sys.stderr,
        )
        return "skipped_malformed"
    if parts is None:
        return "absent"
    before, _inside, after = parts
    merged = before.rstrip("\n") + ("\n" if before.strip() else "") + after.lstrip("\n")
    if not merged.strip():
        claude_md.unlink()
    else:
        _atomic_write(claude_md, merged)
    return "removed"


def rails_block_state(claude_md: Path, content: str) -> str:
    if not claude_md.exists():
        return "missing"
    try:
        parts = _split_on_block(claude_md.read_text(encoding="utf-8"))
    except ValueError:
        return "malformed"
    if parts is None:
        return "missing"
    return "ok" if parts[1].strip("\n") == content.rstrip() else "drifted"
```

Note: `test_remove_strips_block_exactly` pins the exact whitespace contract
of `remove_rails_block` (append after `mine\n` → remove → `mine\n` again).
If the first run shows an off-by-one newline, fix the join logic in
`remove_rails_block` until the round-trip is byte-identical — that
round-trip IS the §6.2 revert guarantee.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_user_scope.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add cli/lib/user_scope.py tests/test_user_scope.py
git commit -m "feat(cli): user-scope CLAUDE.md rails-block splice helpers"
```

### Task 8: Rails template `cli/templates/global_rails.md`

**Files:**
- Create: `cli/templates/global_rails.md`
- Modify: `cli/lib/user_scope.py` (add loader)
- Test: `tests/test_user_scope.py` (append)

**Interfaces:**
- Produces: `load_global_rails() -> str` in `cli/lib/user_scope.py`.

- [ ] **Step 1: Write the failing test** (append)

```python
def test_load_global_rails_compact_and_marker_free():
    from cli.lib.user_scope import RAILS_BEGIN, load_global_rails

    text = load_global_rails()
    assert 5 < len(text.splitlines()) <= 25, "rails block must stay compact (spec §13.3)"
    assert RAILS_BEGIN not in text, "template carries content only, markers are added by the splice"
    assert "agnes catalog" in text and "agnes skills show agnes-data-querying" in text
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_user_scope.py -q -k global_rails`
Expected: FAIL — `load_global_rails` undefined

- [ ] **Step 3: Implement**

Create `cli/templates/global_rails.md` (exactly this content, 20 lines):

```markdown
# Agnes — data access (available in every repository)

This machine has the Agnes CLI + MCP tools connected to the org's data
platform. When asked about org data, follow this protocol:

1. **Discover first** — `agnes catalog --json`, then `agnes schema <table>`
   and `agnes describe <table> -n 5`. Never `SELECT *` blindly.
2. **Check `query_mode`** per table: `local` → `agnes query "<SQL>"` runs
   on the laptop; `remote` → `agnes snapshot create … --estimate` first,
   or `agnes query --remote` for one-shot server-side execution;
   `server_only` → `agnes query --remote` only.
3. **Reuse snapshots** across questions; `agnes snapshot list` before
   fetching; drop with `agnes snapshot drop <name>` when done.
4. **Business metrics**: look up canonical definitions first —
   `agnes catalog --metrics` / `--show <id>`. Never invent metric SQL.

Full protocol: `agnes skills show agnes-data-querying`. Data freshness is
maintained automatically (SessionStart hook); manual refresh: `agnes update`.
```

Add to `cli/lib/user_scope.py`:

```python
def load_global_rails() -> str:
    template = Path(__file__).parent.parent / "templates" / "global_rails.md"
    return template.read_text(encoding="utf-8")
```

Check `pyproject.toml` packaging: `cli/templates/commands/` already ships in the wheel — confirm the include pattern covers `cli/templates/*.md` (look for `[tool.hatch.build]`/`package-data`/`force-include` entries mentioning `cli/templates`); if the pattern is directory-recursive nothing changes, otherwise extend it so `global_rails.md` lands in the distribution.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_user_scope.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cli/templates/global_rails.md cli/lib/user_scope.py tests/test_user_scope.py pyproject.toml
git commit -m "feat(cli): compact global rails template"
```

### Task 9: `agnes refresh-marketplace --target user|project`

**Files:**
- Modify: `cli/commands/refresh_marketplace.py` — callback (`:167`), `_reconcile_with_manifest` (`:758`), `_list_installed_agnes_plugins_in_cwd` (`:962`), the three `claude plugin …` subprocess calls (`:824-905`), the `_enable_plugins_in_workspace_settings` callsite (`:897`)
- Test: `tests/test_cli_refresh_marketplace.py` (append — reuse its `recorder`, `with_clone`, `with_token`, `claude_in_path` fixtures)

**Interfaces:**
- Consumes: existing internals named above.
- Produces:
  - `refresh_marketplace(check: bool, bootstrap: bool, target: str = "project")` — new keyword threaded end-to-end (callers in `cli/commands/update.py::_step_marketplace` keep working via the default).
  - `_list_installed_agnes_plugins(target: str) -> Optional[dict[str, str]]` — generalized lister; `_list_installed_agnes_plugins_in_cwd()` becomes a thin `return _list_installed_agnes_plugins("project")` wrapper so existing tests/callers survive.
  - `run_user_scope_reconcile(*, quiet: bool = False) -> dict[str, list[str]]` — public entry for Task 11/13: reads the existing clone manifest and reconciles with `target="user"`; returns the events dict.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli_refresh_marketplace.py`)

```python
def _user_scope_list_payload():
    return json.dumps(
        [
            {"id": "alpha@agnes", "version": "1.0.0", "projectPath": None, "scope": "user"},
            {"id": "beta@agnes", "version": "0.9.0", "projectPath": "/some/workspace", "scope": "project"},
            {"id": "other@elsewhere", "version": "2.0.0", "projectPath": None, "scope": "user"},
        ]
    )


def test_list_installed_user_target_filters_scope(recorder, claude_in_path):
    recorder.scripts.append(
        (
            ("claude", "plugin", "list", "--json"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=_user_scope_list_payload(), stderr=""),
        )
    )
    versions = rm_module._list_installed_agnes_plugins("user")
    assert versions == {"alpha": "1.0.0"}


def test_list_installed_user_target_tolerates_missing_scope_field(recorder, claude_in_path):
    # Older `claude` CLIs emit no `scope` key — user-scope rows are the ones
    # with projectPath null/absent (spec §7.1 defensive filter).
    payload = json.dumps([{"id": "alpha@agnes", "version": "1.0.0", "projectPath": None}])
    recorder.scripts.append(
        (
            ("claude", "plugin", "list", "--json"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr=""),
        )
    )
    assert rm_module._list_installed_agnes_plugins("user") == {"alpha": "1.0.0"}


def test_user_target_reconcile_uses_scope_user_and_skips_settings_writer(
    tmp_path, monkeypatch, recorder, claude_in_path, with_clone, with_token
):
    """THE §8 foreign-repo guard: user-target reconcile from a foreign cwd
    passes --scope user to every claude verb and never touches cwd/.claude."""
    (with_clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "agnes", "plugins": [{"name": "alpha", "source": "./plugins/alpha", "version": "1.1.0"}]}),
        encoding="utf-8",
    )
    recorder.scripts.append(
        (
            ("claude", "plugin", "list", "--json"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
        )
    )
    foreign = tmp_path / "foreign-repo"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    events = rm_module.run_user_scope_reconcile(quiet=True)

    install_calls = [c.cmd for c in recorder.calls if c.cmd[:3] == ["claude", "plugin", "install"]]
    assert install_calls == [["claude", "plugin", "install", "alpha@agnes", "--scope", "user"]]
    assert all("--scope project" not in " ".join(c.cmd) for c in recorder.calls)
    assert not (foreign / ".claude").exists()
    assert events["installed"] == ["alpha"]


def test_project_target_unchanged_default(recorder, claude_in_path, with_clone, with_token, tmp_path, monkeypatch):
    # Regression pin: default target still passes --scope project (workspace flow byte-for-byte).
    (with_clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "agnes", "plugins": [{"name": "alpha", "source": "./plugins/alpha", "version": "1.1.0"}]}),
        encoding="utf-8",
    )
    recorder.scripts.append(
        (
            ("claude", "plugin", "list", "--json"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
        )
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    rm_module._reconcile_with_manifest(events={"installed": [], "updated": [], "removed": [], "enabled": []})
    install_calls = [c.cmd for c in recorder.calls if c.cmd[:3] == ["claude", "plugin", "install"]]
    assert install_calls == [["claude", "plugin", "install", "alpha@agnes", "--scope", "project"]]
```

(Adapt the exact `events` dict keys to what `_reconcile_with_manifest`'s existing callers build — check the dict literal in `refresh_marketplace()`'s callback body and copy it.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_cli_refresh_marketplace.py -q -k "target or user_scope_reconcile"`
Expected: FAIL — `_list_installed_agnes_plugins` / `run_user_scope_reconcile` undefined

- [ ] **Step 3: Implement**

In `cli/commands/refresh_marketplace.py`:

1. Generalize the lister — rename the body of `_list_installed_agnes_plugins_in_cwd` to:

```python
def _list_installed_agnes_plugins(target: str = "project") -> Optional[dict[str, str]]:
```

Inside, replace the `projectPath` filter block (currently `:1000-1008`) with:

```python
        if target == "user":
            scope = entry.get("scope")
            project_path = entry.get("projectPath")
            if scope is not None:
                if scope != "user":
                    continue
            elif project_path not in (None, ""):
                # Older CLIs: no `scope` field — user rows have null projectPath.
                continue
        else:
            project_path = entry.get("projectPath")
            if not isinstance(project_path, str):
                continue
            try:
                if Path(project_path).resolve() != cwd:
                    continue
            except OSError:
                continue
```

and keep a compatibility wrapper:

```python
def _list_installed_agnes_plugins_in_cwd() -> Optional[dict[str, str]]:
    return _list_installed_agnes_plugins("project")
```

2. Thread `target` through `_reconcile_with_manifest(*, events, installed_pre=None, target: str = "project")`:
   - `installed = installed_pre if installed_pre is not None else _list_installed_agnes_plugins(target)`
   - all three subprocess verbs use `"--scope", target` instead of the literal `"--scope", "project"` (install `:826`, update — its call currently passes no scope on `plugin update`; add `"--scope", target` there too, and `--scope target` on uninstall `:880-ish`; keep warn-message strings in sync),
   - guard the settings writer callsite (`:897`):

```python
    if target == "project":
        _enable_plugins_in_workspace_settings(manifest, events=events)
```

   with a comment: `# user-target: claude plugin install --scope user records enablement itself (verified live, spec §7.1); the cwd-based writer MUST NOT run here — §8 foreign-repo guard.`

3. Add the flag to the callback (after `bootstrap`):

```python
    target: str = typer.Option(
        "project",
        "--target",
        help=(
            "Claude Code install target for stack plugins: 'project' (this "
            "workspace — the default, unchanged) or 'user' (all repositories; "
            "used by `agnes global`). Not to be confused with --scope on read "
            "commands, which selects data locality."
        ),
    ),
```

validate early (`if target not in ("project", "user"): typer.echo("error: --target must be 'project' or 'user'.", err=True); raise typer.Exit(2)`), and pass `target=target` into every `_reconcile_with_manifest` call inside the callback.

4. Public entry for the global layer:

```python
def run_user_scope_reconcile(*, quiet: bool = False) -> dict[str, list[str]]:
    """Reconcile the USER scope against the existing clone's manifest.

    No git fetch / marketplace update here — clone freshness rides the
    workspace marketplace step (`agnes update`) or `--bootstrap`. Safe to
    call from any cwd: the user target performs no cwd writes (spec §7.1).
    """
    import contextlib
    import io

    events: dict[str, list[str]] = {"installed": [], "updated": [], "removed": [], "enabled": []}
    sink = contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext()
    with sink:
        _reconcile_with_manifest(events=events, target="user")
    return events
```

(match the `events` keys to the callback's existing literal exactly).

- [ ] **Step 4: Run to verify pass + regressions**

Run: `.venv/bin/pytest tests/test_cli_refresh_marketplace.py -q -n auto`
Expected: PASS — including every pre-existing test (the default-target path is byte-identical)

- [ ] **Step 5: Commit**

```bash
git add cli/commands/refresh_marketplace.py tests/test_cli_refresh_marketplace.py
git commit -m "feat(cli): refresh-marketplace --target user|project; user target skips cwd writes"
```

### Task 10: User-level SessionStart hook writers in `cli/lib/user_scope.py`

**Files:**
- Modify: `cli/lib/user_scope.py`
- Test: `tests/test_user_scope.py` (append)

**Interfaces:**
- Consumes: `cli.lib.session_paths.user_settings_path() -> Path`; `cli.lib.hooks._OUR_COMMAND_MARKERS`.
- Produces:
  - `GLOBAL_UPDATE_HOOK_CMD = 'bash -c "( nohup agnes update --quiet </dev/null >/dev/null 2>&1 & ) ; true"'` (identical literal to `cli/lib/hooks.py:216`)
  - `install_user_session_hook() -> str` → `"installed" | "unchanged" | "skipped_malformed"`
  - `remove_user_session_hook() -> str` → `"removed" | "absent" | "skipped_malformed"`
  - `user_session_hook_state() -> str` → `"ok" | "missing" | "malformed"`

- [ ] **Step 1: Write the failing tests** (append)

```python
def _fake_user_settings(tmp_path, monkeypatch, initial: dict | str | None):
    settings = tmp_path / "user-claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(initial, dict):
        settings.write_text(json.dumps(initial), encoding="utf-8")
    elif isinstance(initial, str):
        settings.write_text(initial, encoding="utf-8")
    monkeypatch.setattr("cli.lib.user_scope.user_settings_path", lambda: settings)
    return settings


def test_install_user_hook_creates_entry_and_is_idempotent(tmp_path, monkeypatch):
    from cli.lib.user_scope import GLOBAL_UPDATE_HOOK_CMD, install_user_session_hook, user_session_hook_state

    settings = _fake_user_settings(tmp_path, monkeypatch, {"env": {"FOO": "1"}})
    assert install_user_session_hook() == "installed"
    cfg = json.loads(settings.read_text(encoding="utf-8"))
    assert cfg["env"] == {"FOO": "1"}, "foreign keys preserved"
    cmds = [h["command"] for entry in cfg["hooks"]["SessionStart"] for h in entry["hooks"]]
    assert cmds == [GLOBAL_UPDATE_HOOK_CMD]
    assert install_user_session_hook() == "unchanged"
    assert user_session_hook_state() == "ok"


def test_install_preserves_third_party_hooks(tmp_path, monkeypatch):
    from cli.lib.user_scope import install_user_session_hook, remove_user_session_hook

    third_party = {"hooks": [{"type": "command", "command": "echo hello-from-elsewhere"}]}
    settings = _fake_user_settings(tmp_path, monkeypatch, {"hooks": {"SessionStart": [third_party]}})
    install_user_session_hook()
    cfg = json.loads(settings.read_text(encoding="utf-8"))
    assert len(cfg["hooks"]["SessionStart"]) == 2
    assert remove_user_session_hook() == "removed"
    cfg = json.loads(settings.read_text(encoding="utf-8"))
    cmds = [h["command"] for entry in cfg["hooks"]["SessionStart"] for h in entry["hooks"]]
    assert cmds == ["echo hello-from-elsewhere"]


def test_corrupt_user_settings_left_untouched(tmp_path, monkeypatch):
    from cli.lib.user_scope import install_user_session_hook, remove_user_session_hook, user_session_hook_state

    settings = _fake_user_settings(tmp_path, monkeypatch, "{not json")
    assert install_user_session_hook() == "skipped_malformed"
    assert settings.read_text(encoding="utf-8") == "{not json"
    assert remove_user_session_hook() == "skipped_malformed"
    assert user_session_hook_state() == "malformed"


def test_remove_absent(tmp_path, monkeypatch):
    from cli.lib.user_scope import remove_user_session_hook, user_session_hook_state

    _fake_user_settings(tmp_path, monkeypatch, None)
    assert remove_user_session_hook() == "absent"
    assert user_session_hook_state() == "missing"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_user_scope.py -q -k hook`
Expected: FAIL — names undefined

- [ ] **Step 3: Implement** (append to `cli/lib/user_scope.py`)

```python
from cli.lib.session_paths import user_settings_path  # noqa: E402  (top of file with other imports)

GLOBAL_UPDATE_HOOK_CMD = 'bash -c "( nohup agnes update --quiet </dev/null >/dev/null 2>&1 & ) ; true"'


def _load_user_settings() -> dict | None:
    """Parsed user settings dict; {} when the file is absent; None when the
    file exists but is unreadable/not-an-object (leave-untouched signal)."""
    path = user_settings_path()
    if not path.exists():
        return {}
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cfg if isinstance(cfg, dict) else None


def _write_user_settings(cfg: dict) -> None:
    path = user_settings_path()
    _atomic_write(path, json.dumps(cfg, indent=2) + "\n")


def _is_ours(entry: dict) -> bool:
    from cli.lib.hooks import _OUR_COMMAND_MARKERS

    cmds = [h.get("command", "") for h in entry.get("hooks", []) if isinstance(h, dict)]
    return bool(cmds) and all(any(m in c for m in _OUR_COMMAND_MARKERS) for c in cmds)


def install_user_session_hook() -> str:
    cfg = _load_user_settings()
    if cfg is None:
        print(f"warn: {user_settings_path()} is not valid JSON; left untouched.", file=sys.stderr)
        return "skipped_malformed"
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print(f"warn: {user_settings_path()} `hooks` is not an object; left untouched.", file=sys.stderr)
        return "skipped_malformed"
    entries = hooks.setdefault("SessionStart", [])
    if not isinstance(entries, list):
        print(f"warn: {user_settings_path()} `hooks.SessionStart` is not a list; left untouched.", file=sys.stderr)
        return "skipped_malformed"
    ours = [e for e in entries if isinstance(e, dict) and _is_ours(e)]
    canonical = {"hooks": [{"type": "command", "command": GLOBAL_UPDATE_HOOK_CMD}]}
    if ours == [canonical]:
        return "unchanged"
    for e in ours:
        entries.remove(e)
    entries.append(canonical)
    _write_user_settings(cfg)
    return "installed"


def remove_user_session_hook() -> str:
    cfg = _load_user_settings()
    if cfg is None:
        print(f"warn: {user_settings_path()} is not valid JSON; left untouched.", file=sys.stderr)
        return "skipped_malformed"
    entries = cfg.get("hooks", {}).get("SessionStart") if isinstance(cfg.get("hooks"), dict) else None
    if not isinstance(entries, list):
        return "absent"
    ours = [e for e in entries if isinstance(e, dict) and _is_ours(e)]
    if not ours:
        return "absent"
    for e in ours:
        entries.remove(e)
    _write_user_settings(cfg)
    return "removed"


def user_session_hook_state() -> str:
    cfg = _load_user_settings()
    if cfg is None:
        return "malformed"
    entries = cfg.get("hooks", {}).get("SessionStart") if isinstance(cfg.get("hooks"), dict) else None
    if not isinstance(entries, list):
        return "missing"
    return "ok" if any(isinstance(e, dict) and _is_ours(e) for e in entries) else "missing"
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_user_scope.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cli/lib/user_scope.py tests/test_user_scope.py
git commit -m "feat(cli): user-level SessionStart hook writers (marker-matched, leave-untouched recovery)"
```

### Task 11: `agnes global enable` (+ registration in `cli/main.py`)

**Files:**
- Create: `cli/commands/global_scope.py` (module name differs from the command — `global` is a Python keyword)
- Modify: `cli/main.py` (one `add_typer` line, alongside the block at `:284-330`)
- Test: `tests/test_cli_global_scope.py`

**Interfaces:**
- Consumes: `run_user_scope_reconcile` (Task 9), `upsert_rails_block`/`load_global_rails`/`user_claude_md_path`/`install_user_session_hook` (Tasks 7/8/10), `cli.config.save_config`/`load_config`/`get_token`, `cli.client.api_get`, `cli.commands.refresh_marketplace._claude_base_cmd` + `CLONE_DIR` + `refresh_marketplace` (bootstrap reuse), `cli.error_render.render_error`.
- Produces:
  - `global_app` Typer group registered as `agnes global`.
  - `enable(no_hook: bool, force: bool, as_json: bool)` command.
  - `_mcp_entry_state() -> str` (`"ours" | "foreign" | "absent"`), `MCP_SERVER_NAME = "agnes"` — reused by Tasks 12/13.
  - `run_convergence(*, want_hook: bool, force: bool, report: list[dict]) -> None` — the shared engine `enable` and Task 13's `_step_global` both call.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_global_scope.py
"""`agnes global` command group (spec §6). All state isolated: AGNES_CONFIG_DIR
+ patched user paths + recorded subprocess.run — no real `claude`, no network."""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cli.commands.global_scope as gs_module


class _Recorder:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.scripts: list[tuple[tuple[str, ...], subprocess.CompletedProcess]] = []

    def run(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        for prefix, scripted in sorted(self.scripts, key=lambda s: -len(s[0])):
            if tuple(cmd[: len(prefix)]) == prefix:
                return scripted
        return subprocess.CompletedProcess(args=list(cmd), returncode=0, stdout="", stderr="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "agnes-cfg"
    cfg_dir.mkdir()
    (cfg_dir / "token.json").write_text(json.dumps({"access_token": "pat", "email": "t@example.com"}), encoding="utf-8")
    (cfg_dir / "config.yaml").write_text("server: http://localhost:9\nworkspace_root: " + str(tmp_path / "ws") + "\n", encoding="utf-8")
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))

    claude_md = tmp_path / "user-claude" / "CLAUDE.md"
    settings = tmp_path / "user-claude" / "settings.json"
    monkeypatch.setattr("cli.lib.user_scope.user_claude_md_path", lambda: claude_md)
    monkeypatch.setattr("cli.lib.user_scope.user_settings_path", lambda: settings)

    clone = tmp_path / "marketplace"
    (clone / ".git").mkdir(parents=True)
    (clone / ".claude-plugin").mkdir(parents=True)
    (clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "agnes", "plugins": [{"name": "alpha", "source": "./plugins/alpha", "version": "1.0.0"}]}),
        encoding="utf-8",
    )
    import cli.commands.refresh_marketplace as rm_module

    monkeypatch.setattr(rm_module, "CLONE_DIR", clone)

    rec = _Recorder()
    monkeypatch.setattr(rm_module.subprocess, "run", rec.run)
    monkeypatch.setattr(gs_module.subprocess, "run", rec.run)
    monkeypatch.setattr(rm_module.shutil, "which", lambda n: "claude" if n == "claude" else None)
    monkeypatch.setattr(gs_module.shutil, "which", lambda n: {"claude": "claude", "agnes": "/fake/bin/agnes"}.get(n))
    rec.scripts.append(
        (("claude", "plugin", "list", "--json"), subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""))
    )
    rec.scripts.append(
        (("claude", "mcp", "get"), subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="not found"))
    )
    monkeypatch.setattr(gs_module, "_verify_credentials", lambda: True)
    return {"rec": rec, "claude_md": claude_md, "settings": settings, "cfg_dir": cfg_dir}


def test_enable_converges_all_artifacts(env):
    result = CliRunner().invoke(gs_module.global_app, ["enable"])
    assert result.exit_code == 0, result.output
    rec = env["rec"]
    assert ["claude", "plugin", "install", "alpha@agnes", "--scope", "user"] in rec.calls
    mcp_adds = [c for c in rec.calls if c[:3] == ["claude", "mcp", "add"]]
    assert mcp_adds and "--scope" in mcp_adds[0] and "user" in mcp_adds[0] and mcp_adds[0][-1] == "mcp"
    assert env["claude_md"].exists()
    cfg = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert cfg["hooks"]["SessionStart"], "hook installed by default (spec §13.2)"
    import yaml

    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf["global_scope"] is True and conf["global_hook"] is True


def test_enable_no_hook(env):
    result = CliRunner().invoke(gs_module.global_app, ["enable", "--no-hook"])
    assert result.exit_code == 0, result.output
    assert not env["settings"].exists() or "hooks" not in json.loads(env["settings"].read_text(encoding="utf-8"))
    import yaml

    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf["global_hook"] is False


def test_enable_twice_idempotent(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    md_before = env["claude_md"].read_text(encoding="utf-8")
    settings_before = env["settings"].read_text(encoding="utf-8")
    result = CliRunner().invoke(gs_module.global_app, ["enable"])
    assert result.exit_code == 0
    assert env["claude_md"].read_text(encoding="utf-8") == md_before
    assert env["settings"].read_text(encoding="utf-8") == settings_before


def test_enable_json_report(env):
    result = CliRunner().invoke(gs_module.global_app, ["enable", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    stages = {row["stage"] for row in doc["report"]}
    assert {"plugins", "mcp", "rails", "hook", "flag"} <= stages
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_cli_global_scope.py -q`
Expected: FAIL — module missing

- [ ] **Step 3: Implement `cli/commands/global_scope.py`**

```python
"""`agnes global` — the user-scope (all-repositories) Agnes layer (spec §6).

`enable` idempotently converges five artifacts; `disable` reverts exactly
what enable wrote; `status` reports each artifact. Module is named
`global_scope` because `global` is a Python keyword; the command name is
registered as `global` in cli/main.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get
from cli.config import get_token, load_config, save_config
from cli.error_render import render_error
from cli.lib.user_scope import (
    install_user_session_hook,
    load_global_rails,
    rails_block_state,
    remove_rails_block,
    remove_user_session_hook,
    upsert_rails_block,
    user_claude_md_path,
    user_session_hook_state,
)

global_app = typer.Typer(help="Manage the user-scope (all-repositories) Agnes layer")

MCP_SERVER_NAME = "agnes"


def _verify_credentials() -> bool:
    """One cheap authenticated probe — same as `agnes init` step 2."""
    try:
        return api_get("/api/catalog/tables").status_code == 200
    except Exception:
        return False


def _claude_cmd() -> Optional[list[str]]:
    from cli.commands.refresh_marketplace import _claude_base_cmd

    return _claude_base_cmd()


def _agnes_binary() -> str:
    return shutil.which("agnes") or sys.argv[0]


def _mcp_entry_state() -> str:
    """'ours' | 'foreign' | 'absent' — via `claude mcp get agnes`."""
    base = _claude_cmd()
    if base is None:
        return "absent"
    result = subprocess.run(
        [*base, "mcp", "get", MCP_SERVER_NAME],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return "absent"
    out = result.stdout or ""
    # Ours == a stdio entry whose args end with the `mcp` subcommand of an
    # agnes binary. `claude mcp get` prints "Command: <path>" and "Args: mcp".
    return "ours" if ("agnes" in out and "Args: mcp" in out) else "foreign"


def run_convergence(*, want_hook: bool, force: bool, report: list[dict]) -> None:
    """Shared engine for `enable` and `agnes update`'s `global` step.

    Every step is check-then-act; failures are recorded, never raised.
    """
    from cli.commands.refresh_marketplace import CLONE_DIR, run_user_scope_reconcile

    # 1 — marketplace clone + plugins (user scope; no cwd writes, spec §7.1)
    if not (CLONE_DIR / ".git").is_dir():
        # Spec §6.1 step 1: reuse the existing bootstrap path (clone +
        # `claude plugin marketplace add` + first reconcile), then fall
        # through — the reconcile below is an idempotent no-op after it.
        from cli.commands.refresh_marketplace import refresh_marketplace

        try:
            refresh_marketplace(check=False, bootstrap=True, target="user")
            report.append({"stage": "marketplace", "status": "bootstrapped", "detail": str(CLONE_DIR)})
        except typer.Exit as exc:
            report.append({"stage": "marketplace", "status": "error", "detail": f"bootstrap exit={getattr(exc, 'exit_code', 1)}"})
    if not (CLONE_DIR / ".git").is_dir():
        report.append({"stage": "plugins", "status": "skipped", "detail": f"no marketplace clone at {CLONE_DIR}"})
    else:
        try:
            events = run_user_scope_reconcile(quiet=True)
            changed = sum(len(v) for v in events.values())
            report.append({"stage": "plugins", "status": "reconciled" if changed else "ok", "detail": json.dumps({k: v for k, v in events.items() if v}) if changed else "user-scope plugins current"})
        except Exception as exc:  # noqa: BLE001 — convergence must not abort
            report.append({"stage": "plugins", "status": "error", "detail": str(exc)})

    # 2 — MCP entry (via the claude CLI only — spec §6.4)
    state = _mcp_entry_state()
    if state == "ours":
        report.append({"stage": "mcp", "status": "ok", "detail": "user-scope stdio entry present"})
    elif state == "foreign" and not force:
        report.append({"stage": "mcp", "status": "skipped", "detail": f"an MCP server named '{MCP_SERVER_NAME}' exists and is not ours; re-run with --force to replace"})
    else:
        base = _claude_cmd()
        if base is None:
            report.append({"stage": "mcp", "status": "error", "detail": "`claude` CLI not on PATH"})
        else:
            if state == "foreign":
                subprocess.run([*base, "mcp", "remove", MCP_SERVER_NAME, "-s", "user"], capture_output=True, text=True, check=False)
            result = subprocess.run(
                [*base, "mcp", "add", "--scope", "user", MCP_SERVER_NAME, "--", _agnes_binary(), "mcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            report.append({"stage": "mcp", "status": "added" if result.returncode == 0 else "error", "detail": (result.stderr or result.stdout or "").strip()[:200] or "registered"})

    # 3 — rails block
    outcome = upsert_rails_block(user_claude_md_path(), load_global_rails())
    report.append({"stage": "rails", "status": outcome, "detail": str(user_claude_md_path())})

    # 4 — user-level SessionStart hook (default ON, spec §13.2)
    if want_hook:
        report.append({"stage": "hook", "status": install_user_session_hook(), "detail": "SessionStart -> detached `agnes update --quiet`"})
    else:
        report.append({"stage": "hook", "status": "skipped", "detail": "--no-hook / global_hook=false"})


@global_app.command("enable")
def enable(
    no_hook: bool = typer.Option(False, "--no-hook", help="Do not install the user-level SessionStart update hook."),
    force: bool = typer.Option(False, "--force", help="Replace a foreign MCP server entry named 'agnes'."),
    as_json: bool = typer.Option(False, "--json", help="Emit a single JSON report."),
):
    """Enable Agnes in every repository: user-scope plugins, MCP entry,
    rails block, SessionStart hook, config flag. Idempotent."""
    if _claude_cmd() is None:
        typer.echo(render_error(0, {"detail": {"kind": "partial_state", "hint": "`claude` CLI not found on PATH — install Claude Code first."}}), err=True)
        raise typer.Exit(1)
    if not get_token() or not _verify_credentials():
        typer.echo(render_error(401, {"detail": {"kind": "auth_failed", "hint": "No working Agnes credentials. Run `agnes auth login` or `agnes init` first."}}), err=True)
        raise typer.Exit(1)

    if not load_config().get("workspace_root"):
        typer.echo("note: no anchored workspace yet — local-data MCP tools resolve nothing until `agnes init` runs.", err=True)

    report: list[dict] = []
    run_convergence(want_hook=not no_hook, force=force, report=report)
    save_config({"global_scope": True, "global_hook": not no_hook})
    report.append({"stage": "flag", "status": "ok", "detail": "global_scope=true"})

    if as_json:
        typer.echo(json.dumps({"report": report}))
    else:
        for row in report:
            typer.echo(f"  {row['stage']:<8} {row['status']:<12} {row['detail']}")
        typer.echo("Global layer enabled — restart Claude Code sessions to pick it up.")
```

Register in `cli/main.py` (append after the `:330` block):

```python
from cli.commands.global_scope import global_app  # with the other command imports

app.add_typer(global_app, name="global")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_cli_global_scope.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cli/commands/global_scope.py cli/main.py tests/test_cli_global_scope.py
git commit -m "feat(cli): agnes global enable — user-scope plugins, MCP, rails, hook"
```

### Task 12: `agnes global disable` + `agnes global status`

**Files:**
- Modify: `cli/commands/global_scope.py`
- Test: `tests/test_cli_global_scope.py` (append)

**Interfaces:**
- Consumes: Task 11's helpers + Task 7/10 removers.
- Produces: `disable(as_json)`, `status(as_json)` commands.

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_disable_reverts_exactly(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="agnes:\n  Command: /fake/bin/agnes\n  Args: mcp\n", stderr=""),
        ),
    )
    result = CliRunner().invoke(gs_module.global_app, ["disable"])
    assert result.exit_code == 0, result.output
    rec = env["rec"]
    assert ["claude", "plugin", "uninstall", "alpha@agnes", "--scope", "user"] in rec.calls
    assert any(c[:3] == ["claude", "mcp", "remove"] and "user" in c for c in rec.calls)
    assert not env["claude_md"].exists() or "agnes-global" not in env["claude_md"].read_text(encoding="utf-8")
    cfg = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert not any(cfg.get("hooks", {}).get("SessionStart", []))
    import yaml

    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf["global_scope"] is False


def test_disable_leaves_foreign_mcp_entry(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="agnes:\n  Command: /usr/bin/somethingelse\n  Args: serve\n", stderr=""),
        ),
    )
    result = CliRunner().invoke(gs_module.global_app, ["disable"])
    assert result.exit_code == 0
    assert not any(c[:3] == ["claude", "mcp", "remove"] for c in env["rec"].calls)


def test_status_reports_each_artifact(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    result = CliRunner().invoke(gs_module.global_app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    rows = {row["artifact"]: row["state"] for row in doc["artifacts"]}
    assert rows["rails"] == "ok"
    assert rows["hook"] == "ok"
    assert rows["flag"] == "ok"
    assert "plugins" in rows and "mcp" in rows
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_cli_global_scope.py -q -k "disable or status"`
Expected: FAIL — commands missing

- [ ] **Step 3: Implement** (append to `cli/commands/global_scope.py`)

```python
@global_app.command("disable")
def disable(
    as_json: bool = typer.Option(False, "--json", help="Emit a single JSON report."),
):
    """Revert exactly what `enable` wrote. Marketplace registration and the
    clone stay (the workspace flow uses them too, spec §6.2)."""
    report: list[dict] = []

    from cli.commands.refresh_marketplace import _list_installed_agnes_plugins

    base = _claude_cmd()
    installed = _list_installed_agnes_plugins("user") if base else None
    if installed:
        removed: list[str] = []
        for name in sorted(installed):
            result = subprocess.run(
                [*base, "plugin", "uninstall", f"{name}@agnes", "--scope", "user"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode == 0:
                removed.append(name)
        report.append({"stage": "plugins", "status": "removed", "detail": ", ".join(removed) or "none"})
    else:
        report.append({"stage": "plugins", "status": "ok", "detail": "no user-scope stack plugins installed"})

    state = _mcp_entry_state()
    if state == "ours" and base:
        subprocess.run([*base, "mcp", "remove", MCP_SERVER_NAME, "-s", "user"], capture_output=True, text=True, check=False)
        report.append({"stage": "mcp", "status": "removed", "detail": "user-scope entry removed"})
    elif state == "foreign":
        report.append({"stage": "mcp", "status": "skipped", "detail": "entry named 'agnes' is not ours — left in place"})
    else:
        report.append({"stage": "mcp", "status": "ok", "detail": "no entry"})

    report.append({"stage": "rails", "status": remove_rails_block(user_claude_md_path()), "detail": str(user_claude_md_path())})
    report.append({"stage": "hook", "status": remove_user_session_hook(), "detail": "user-level SessionStart"})
    save_config({"global_scope": False, "global_hook": False})
    report.append({"stage": "flag", "status": "ok", "detail": "global_scope=false"})

    if as_json:
        typer.echo(json.dumps({"report": report}))
    else:
        for row in report:
            typer.echo(f"  {row['stage']:<8} {row['status']:<12} {row['detail']}")


@global_app.command("status")
def status(
    as_json: bool = typer.Option(False, "--json", help="Emit a single JSON document."),
):
    """One row per artifact: ok | missing | drifted | … with the repair hint
    (`agnes global enable` re-runs convergence)."""
    from cli.commands.refresh_marketplace import CLONE_DIR, _list_installed_agnes_plugins, _read_marketplace_plugin_versions

    cfg = load_config()
    artifacts: list[dict] = []

    manifest = _read_marketplace_plugin_versions() if (CLONE_DIR / ".git").is_dir() else None
    installed = _list_installed_agnes_plugins("user") if _claude_cmd() else None
    if manifest is None or installed is None:
        artifacts.append({"artifact": "plugins", "state": "unknown", "detail": "marketplace clone or `claude` CLI unavailable"})
    else:
        missing = sorted(set(manifest) - set(installed))
        drifted = sorted(n for n in manifest if n in installed and installed[n] != manifest[n])
        state = "ok" if not missing and not drifted else "drifted"
        artifacts.append({"artifact": "plugins", "state": state, "detail": f"{len(installed)}/{len(manifest)} user-scope; missing: {missing or '—'}; stale: {drifted or '—'}"})

    mcp_state = _mcp_entry_state()
    artifacts.append({"artifact": "mcp", "state": {"ours": "ok", "foreign": "drifted", "absent": "missing"}[mcp_state], "detail": f"`claude mcp get {MCP_SERVER_NAME}` -> {mcp_state}"})
    artifacts.append({"artifact": "rails", "state": rails_block_state(user_claude_md_path(), load_global_rails()), "detail": str(user_claude_md_path())})
    hook_state = user_session_hook_state()
    if not cfg.get("global_hook", False) and hook_state == "missing":
        hook_state = "disabled"
    artifacts.append({"artifact": "hook", "state": "ok" if hook_state == "ok" else hook_state, "detail": "user-level SessionStart"})
    artifacts.append({"artifact": "flag", "state": "ok" if cfg.get("global_scope") else "missing", "detail": f"global_scope={bool(cfg.get('global_scope'))}, global_hook={bool(cfg.get('global_hook'))}"})

    if as_json:
        typer.echo(json.dumps({"artifacts": artifacts}))
    else:
        for row in artifacts:
            typer.echo(f"  {row['artifact']:<8} {row['state']:<10} {row['detail']}")
        if any(r["state"] not in ("ok", "disabled") for r in artifacts):
            typer.echo("Repair: agnes global enable")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_cli_global_scope.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cli/commands/global_scope.py tests/test_cli_global_scope.py
git commit -m "feat(cli): agnes global disable/status"
```

### Task 13: `agnes update` step `global`

**Files:**
- Modify: `cli/commands/update.py` — add `_step_global` + one `_run_step` call **after** the workspace `if/else` block (after line ~705, outside the `os.chdir(workspace)` scope — the global step is workspace-independent and must also run when `workspace is None`)
- Test: `tests/test_cli_update.py` (append)

**Interfaces:**
- Consumes: `run_convergence` + `load_config` (Task 11); `_run_step` (existing).
- Produces: report stage `"global"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli_update.py`; mirror that file's existing style for invoking `update` and reading the report — check how its tests build config/monkeypatch steps and follow the same pattern)

```python
def test_update_skips_global_step_when_flag_off(tmp_path, monkeypatch):
    import cli.commands.update as update_module

    called = []
    monkeypatch.setattr(
        "cli.commands.global_scope.run_convergence",
        lambda **kw: called.append(kw),
    )
    monkeypatch.setattr(update_module, "load_config", lambda: {"server": "http://localhost:9"})
    report: list[dict] = []
    update_module._step_global(report=report, quiet=True)
    assert called == []
    assert report and report[0]["status"] == "skipped"


def test_update_runs_global_step_when_flag_on(monkeypatch):
    import cli.commands.update as update_module

    called = []
    monkeypatch.setattr(
        "cli.commands.global_scope.run_convergence",
        lambda **kw: called.append(kw),
    )
    monkeypatch.setattr(
        update_module,
        "load_config",
        lambda: {"server": "http://localhost:9", "global_scope": True, "global_hook": True},
    )
    report: list[dict] = []
    update_module._step_global(report=report, quiet=True)
    assert len(called) == 1
    assert called[0]["want_hook"] is True and called[0]["force"] is False
```

(`load_config` may not be imported in `update.py` yet — the implementation below imports it; patch wherever the implementation reads it, adjusting the monkeypatch target to the actual import site.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_cli_update.py -q -k global_step`
Expected: FAIL — `_step_global` undefined

- [ ] **Step 3: Implement** (in `cli/commands/update.py`, next to `_step_marketplace`)

```python
def _step_global(*, report: list[dict], quiet: bool = False) -> None:
    """Converge the user-scope layer (spec §7.2). Workspace-independent —
    runs OUTSIDE the workspace chdir block and also when no workspace
    exists. Gated on the `global_scope` config flag; `global_hook: false`
    (set by `agnes global enable --no-hook`) keeps the hook un-asserted."""
    import contextlib
    import io

    from cli.config import load_config

    cfg = load_config()
    if not cfg.get("global_scope"):
        report.append({"stage": "global", "status": "skipped", "detail": "global_scope not enabled"})
        return
    from cli.commands.global_scope import run_convergence

    sub_report: list[dict] = []
    sink = contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext()
    with sink:
        run_convergence(want_hook=bool(cfg.get("global_hook", False)), force=False, report=sub_report)
    bad = [r for r in sub_report if r.get("status") == "error"]
    report.append(
        {
            "stage": "global",
            "status": "error" if bad else "ok",
            "detail": "; ".join(f"{r['stage']}={r['status']}" for r in sub_report),
        }
    )
```

Wire it in `run()` — after the workspace `if/else` block closes (i.e. after the `finally: os.chdir(prev_cwd)` block and its `else` branch end, before the `entry = {` report finalization at `:709`):

```python
        # User-scope layer (spec §7.2) — workspace-independent by design:
        # runs from the LAUNCHING cwd (safe: the user target performs no
        # cwd writes) and also when workspace is None.
        _run_step("global", lambda: _step_global(report=report, quiet=step_quiet), report)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_cli_update.py -q`
Expected: PASS (new + all pre-existing update tests — the step is a no-op when the flag is absent, so existing report-shape assertions gain one `skipped` row at most; if any existing test asserts an exact report length, extend that assertion)

- [ ] **Step 5: Commit**

```bash
git add cli/commands/update.py tests/test_cli_update.py
git commit -m "feat(cli): agnes update converges the user-scope layer when enabled"
```

### Task 14: Docs + CHANGELOG + full suite (PR 2 close-out)

**Files:**
- Create: `docs/global-distribution.md`
- Modify: `docs/README.md` (index link), `CLAUDE.md` (one pointer line in "Local sync & Claude Code hooks"), `CHANGELOG.md`

**Interfaces:** none — text only.

- [ ] **Step 1: Write `docs/global-distribution.md`**

```markdown
# Global distribution — Agnes in every repository

Make Agnes skills and data access available in **all** repositories on a
machine, not just the analyst workspace. Three audiences, three recipes.

## Engineer with the Agnes CLI (recommended)

Prerequisite: `agnes init` has run once on this machine (any workspace).

    agnes global enable

Idempotently converges five user-scope artifacts:

| Artifact | Where | What it does |
|---|---|---|
| Stack plugins | `claude plugin install <p>@agnes --scope user` | skills/commands from your stack load in every repo |
| MCP server | `claude mcp add --scope user agnes -- <agnes> mcp` | catalog/schema/query/query_local tools everywhere (first tools appear a few seconds into a fresh session — the stdio server has a short cold start) |
| Rails block | `~/.claude/CLAUDE.md` (marker-fenced) | the data-querying protocol in every session |
| SessionStart hook | `~/.claude/settings.json` | a detached `agnes update --quiet` keeps data + plugins fresh from any repo (skip with `--no-hook`) |
| Config flag | `~/.config/agnes/config.yaml` | `agnes update` re-converges the layer on every run |

Check with `agnes global status` (add `--json` for scripting); remove with
`agnes global disable` — it reverts exactly what enable wrote and never
touches your other marketplaces, MCP servers, or hooks.

Privacy: session transcripts are uploaded ONLY from the anchored analyst
workspace. Sessions in your other repositories are never pushed, with or
without the global layer.

## Machine without the CLI — remote MCP

    claude mcp add --scope user --transport http agnes https://<agnes-host>/api/mcp/http

Claude Code will report "Needs authentication" — open any session and run
`/mcp` to complete the OAuth consent in your browser (sign in with your
Agnes account). You get the full RBAC-filtered server-side tool set; no
local parquets, no CLI required.

## Operator — fleet-wide default

Managed settings (MDM-deployed `managed-settings.json`, or server-managed
settings) can force the layer for every engineer:

    {
      "extraKnownMarketplaces": {
        "agnes": {"source": {"source": "git", "url": "https://<agnes-host>/marketplace.git"}}
      },
      "enabledPlugins": {"<plugin>@agnes": true},
      "strictKnownMarketplaces": false
    }

plus a managed MCP entry pointing at `https://<agnes-host>/api/mcp/http`.
Set `strictKnownMarketplaces: true` to lock plugin sources to the list
above. Consult the Claude Code managed-settings documentation for
deployment paths per OS.
```

- [ ] **Step 2: Index + pointer + CHANGELOG**

- `docs/README.md`: add under the appropriate section: `- [Global distribution](global-distribution.md) — Agnes skills + data in every repository (user-scope layer, remote MCP, fleet managed settings)`
- Root `CLAUDE.md`, end of the "Local sync & Claude Code hooks" section, one line: `Engineers who work across many repos can enable the user-scope layer — skills + data everywhere — with `agnes global enable`; see [docs/global-distribution.md](docs/global-distribution.md).`
- `CHANGELOG.md` under `### Added`:

```markdown
- `agnes global enable|disable|status` — user-scope (all-repositories) layer: stack plugins installed with `claude plugin install --scope user`, an `agnes` user-scope stdio MCP entry, a marker-fenced rails block in `~/.claude/CLAUDE.md`, and (by default; `--no-hook` opts out) a user-level SessionStart hook running the detached `agnes update`. `agnes update` re-converges the layer via a new `global` step, and `agnes refresh-marketplace` gains `--target user|project` (the `user` target performs no per-repository writes). New docs page: `docs/global-distribution.md`.
```

- [ ] **Step 3: Full suite + repo verification loop**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q` — must pass.
Then run the repo's own gate: `python scripts/verify_syncmap.py` (the sync-map check must stay green — the new `--target` flag is enumerated, not boolean, so `check_scope_flags` passes; if it flags anything, fix per its message).

- [ ] **Step 4: Commit**

```bash
git add docs/global-distribution.md docs/README.md CLAUDE.md CHANGELOG.md
git commit -m "docs: global distribution guide; changelog for agnes global"
```

---

## Final verification (both PRs)

- [ ] Full suite green: `.venv/bin/pytest tests/ --tb=short -n auto -q`
- [ ] `scripts/verify_syncmap.py` green
- [ ] Manual smoke on the dev machine (mirrors the spec §3.1 evidence): from a non-workspace directory run `agnes query --local "SELECT 1"` (PR 1) and `agnes global status --json` (PR 2)
- [ ] `/agnes-review` on each PR's diff before requesting merge (repo's mandatory review loop)
- [ ] Grep both diffs for customer-specific tokens before opening PRs (vendor-agnostic rule)
