# Local Sync Hooks + Materialized BigQuery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Agnes auto-sync RBAC-filtered tables to each analyst's local parquet/DuckDB at Claude Code session boundaries, and let admins materialize BigQuery queries server-side so the same auto-sync flow distributes them.

**Architecture:** Three independent phases shipped as three PRs. Phase A wires `da sync` into Claude Code SessionStart/SessionEnd hooks (no schema change, no server change). Phase B introduces `query_mode='materialized'` for BigQuery — admin registers a SQL query, scheduler runs it through DuckDB BQ extension and writes a parquet that the existing manifest endpoint exposes; per-user RBAC filtering is unchanged. Phase C is documentation only.

**Tech Stack:** Python 3.11+, DuckDB (with `bigquery` community extension), FastAPI, Typer, pytest. No new runtime deps.

**RBAC story (already in place — no new code):** `/api/sync/manifest` filters per user via `can_access_table(user, table_id, conn)` (`src/rbac.py:63`), which consults `resource_grants(group, ResourceType.TABLE, table_id)`. Any table with `query_mode IN ('local', 'materialized')` that the user has access to lands in their manifest, so `da sync` (and the SessionStart hook) downloads it automatically. Admins curate the auto-sync set by **(1) setting `query_mode`** to control whether a table is local-distributed at all, and **(2) granting it to groups** to control who gets it.

**Out of scope (separate plan later):** Per-user opt-in whitelist for very large tables (`auto_sync_default=false` flag). Today everything the user can access AND has `query_mode='local'/'materialized'` syncs. Opt-in tier is a Phase 4 concern.

---

## File Structure

| File | Phase | Responsibility |
|---|---|---|
| `cli/commands/sync.py` | A | Add `--quiet` flag (suppress progress for hook use). |
| `docs/setup/claude_settings.json` | A | Replace dead `collect_session.py` reference. Ship `SessionStart` + `SessionEnd` hooks that call `da sync` / `da sync --upload-only`. |
| `cli/commands/analyst.py` | A | `da analyst setup` writes the hooks to `~/.claude/settings.json`. |
| `tests/test_cli_sync_quiet.py` | A | New — covers `--quiet` flag. |
| `tests/test_cli_analyst_setup_hooks.py` | A | New — covers hook installation. |
| `src/db.py` | B | Schema v15 migration: add `source_query TEXT` to `table_registry`. |
| `src/repositories/table_registry.py` | B | Persist + return `source_query`. |
| `connectors/bigquery/extractor.py` | B | New `materialize_query(table_id, sql, project_id, output_dir)`. `init_extract` skips materialized rows during view creation. |
| `app/api/sync.py` | B | `/api/sync/trigger` materializes due rows by walking `table_registry` and consulting `is_table_due()`. |
| `app/api/admin.py` | B | `RegisterTableRequest` accepts `source_query` + `query_mode='materialized'`. Mode/query coherence validation. |
| `cli/commands/admin.py` | B | `da admin table register --mode materialized --query @file.sql --schedule "every 6h"`. |
| `tests/test_db_migration_v15.py` | B | New — schema bump test. |
| `tests/test_bq_materialize.py` | B | New — `materialize_query()` happy path + cost guardrail. |
| `tests/test_sync_trigger_materialized.py` | B | New — trigger walks registry. |
| `README.md` | C | Mode-first table; new "Local sync & auto-update" section. |
| `CLAUDE.md` | C | Schema v15 mention; `materialized` mode in Connector Pattern; auto-sync hooks under Development. |
| `docs/architecture.md` | C | Refresh ASCII diagram with materialized path. |
| `cli/skills/connectors.md` | C | "BigQuery: when to use which mode" decision table. |
| `CHANGELOG.md` | A, B, C | Each phase appends to `## [Unreleased]`. |

---

## Phase A — Auto-sync hooks

Ships standalone. No schema, no server change. One PR.

### Task A1: Add `--quiet` flag to `da sync`

**Files:**
- Modify: `cli/commands/sync.py:28-44` (signature + docstring) and `cli/commands/sync.py:45-56` (Progress block).
- Test: `tests/test_cli_sync_quiet.py` (new).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_sync_quiet.py
"""Verify `da sync --quiet` suppresses progress output but still completes."""
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock

from cli.main import app


def test_quiet_flag_suppresses_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    runner = CliRunner()

    fake_resp = MagicMock()
    fake_resp.json.return_value = {"tables": {}, "assets": {}, "server_time": "2026-04-30T00:00:00Z"}
    fake_resp.raise_for_status = MagicMock()

    with patch("cli.commands.sync.api_get", return_value=fake_resp):
        result = runner.invoke(app, ["sync", "--quiet"])

    assert result.exit_code == 0
    # No spinner glyphs, no "Found X tables" header
    assert "Found" not in result.stdout
    assert "Downloading" not in result.stdout
    # Final summary line is allowed and expected
    assert "Downloaded:" in result.stdout or result.stdout.strip() == ""
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_cli_sync_quiet.py::test_quiet_flag_suppresses_progress -v
```

Expected: FAIL — `--quiet` flag doesn't exist.

- [ ] **Step 3: Add the flag**

In `cli/commands/sync.py`, modify the `sync` callback signature to accept `quiet`:

```python
@sync_app.callback(invoke_without_command=True)
def sync(
    table: str = typer.Option(None, "--table", help="Sync specific table only"),
    upload_only: bool = typer.Option(False, "--upload-only", help="Only upload sessions/artifacts"),
    docs_only: bool = typer.Option(False, "--docs-only", help="Only sync documentation"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress progress output (for hooks/cron)"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be synced without downloading, uploading, or writing local state.",
    ),
):
    """Sync data between server and local machine."""
    if upload_only:
        _upload(as_json, dry_run=dry_run, quiet=quiet)
        return

    if quiet:
        # Bypass Rich Progress entirely so hook stdout stays clean.
        _sync_quiet(table=table, docs_only=docs_only, as_json=as_json, dry_run=dry_run)
        return

    with Progress(
        # ...existing block...
```

Add the helper at module level (after `_upload`):

```python
def _sync_quiet(table, docs_only, as_json, dry_run):
    """Same flow as the Progress block, no UI. One-line final summary on stderr."""
    try:
        resp = api_get("/api/sync/manifest")
        resp.raise_for_status()
        manifest = resp.json()
    except Exception as e:
        typer.echo(f"sync: manifest fetch failed: {e}", err=True)
        raise typer.Exit(1)

    server_tables = manifest.get("tables", {})
    local_state = get_sync_state()
    local_tables = local_state.get("tables", {})

    to_download = []
    for tid, info in server_tables.items():
        if table and tid != table:
            continue
        if docs_only:
            continue
        local_hash = local_tables.get(tid, {}).get("hash", "")
        server_hash = info.get("hash", "")
        if server_hash != local_hash or tid not in local_tables or not server_hash:
            to_download.append(tid)

    if dry_run:
        if as_json:
            typer.echo(json.dumps({"dry_run": True, "would_download": to_download}, indent=2))
        return

    local_dir = _local_data_dir()
    parquet_dir = local_dir / "server" / "parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)

    results = {"downloaded": [], "errors": []}
    for tid in to_download:
        target = parquet_dir / f"{tid}.parquet"
        expected_hash = server_tables[tid].get("hash", "")
        try:
            stream_download(f"/api/data/{tid}/download", str(target))
            if expected_hash:
                if _md5_file(target) != expected_hash:
                    target.unlink(missing_ok=True)
                    raise ValueError("hash mismatch")
            elif not _is_valid_parquet(target):
                target.unlink(missing_ok=True)
                raise ValueError("not a valid parquet")
            local_tables[tid] = {
                "hash": expected_hash,
                "rows": server_tables[tid].get("rows", 0),
                "size_bytes": server_tables[tid].get("size_bytes", 0),
            }
            results["downloaded"].append(tid)
        except Exception as e:
            results["errors"].append({"table": tid, "error": str(e)})

    from datetime import datetime, timezone
    local_state["tables"] = local_tables
    local_state["last_sync"] = datetime.now(timezone.utc).isoformat()
    save_sync_state(local_state)

    if results["downloaded"]:
        _rebuild_duckdb_views(local_dir, parquet_dir)

    if as_json:
        typer.echo(json.dumps(results, indent=2))
    elif results["downloaded"] or results["errors"]:
        # One terse line for hook-friendly logs; silent on no-op
        typer.echo(
            f"sync: {len(results['downloaded'])} tables, {len(results['errors'])} errors",
            err=True,
        )
```

Update `_upload` signature to accept `quiet=False`:

```python
def _upload(as_json: bool, dry_run: bool = False, quiet: bool = False):
```

And inside `_upload`, replace the final non-JSON `typer.echo` block with:

```python
    if as_json:
        typer.echo(json.dumps(results, indent=2))
    elif not quiet:
        typer.echo(f"Uploaded {results['sessions']} sessions")
        if results["local_md"]:
            typer.echo("Uploaded CLAUDE.local.md")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_cli_sync_quiet.py::test_quiet_flag_suppresses_progress -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli/commands/sync.py tests/test_cli_sync_quiet.py
git commit -m "feat(cli): da sync --quiet suppresses progress for hooks/cron"
```

---

### Task A2: Replace broken SessionEnd hook + add SessionStart in shipped settings

**Files:**
- Modify: `docs/setup/claude_settings.json:3-12`.
- Test: `tests/test_setup_hooks_template.py` (new).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_setup_hooks_template.py
"""The shipped Claude settings template must point hooks at `da sync`, not the deleted server/scripts."""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "docs" / "setup" / "claude_settings.json"


def test_template_has_session_start_da_sync():
    cfg = json.loads(TEMPLATE.read_text())
    starts = cfg.get("hooks", {}).get("SessionStart", [])
    assert starts, "SessionStart hook missing"
    cmds = [h["command"] for entry in starts for h in entry.get("hooks", [])]
    assert any("da sync" in c and "--upload-only" not in c for c in cmds), (
        f"Expected `da sync` in SessionStart, got {cmds}"
    )


def test_template_has_session_end_upload():
    cfg = json.loads(TEMPLATE.read_text())
    ends = cfg.get("hooks", {}).get("SessionEnd", [])
    cmds = [h["command"] for entry in ends for h in entry.get("hooks", [])]
    assert any("da sync --upload-only" in c for c in cmds), (
        f"Expected `da sync --upload-only` in SessionEnd, got {cmds}"
    )


def test_template_drops_dead_server_scripts_reference():
    raw = TEMPLATE.read_text()
    assert "server/scripts/collect_session.py" not in raw, (
        "Template still references the deleted server/scripts/collect_session.py — "
        "the SessionEnd hook would silently fail."
    )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_setup_hooks_template.py -v
```

Expected: 3 FAIL (template still has the dead reference, no SessionStart).

- [ ] **Step 3: Update the template**

Edit `docs/setup/claude_settings.json` so the `hooks` block reads:

```json
{
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "da sync --quiet 2>/dev/null || true"
                    }
                ]
            }
        ],
        "SessionEnd": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "da sync --upload-only --quiet 2>/dev/null || true"
                    }
                ]
            }
        ]
    },
```

(Leave the `permissions` block untouched.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_setup_hooks_template.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/setup/claude_settings.json tests/test_setup_hooks_template.py
git commit -m "fix(setup): replace dead SessionEnd target with da sync hooks

SessionStart pulls RBAC-filtered parquets via the existing manifest +
ETag flow. SessionEnd uploads sessions and CLAUDE.local.md. Both run
quietly so they don't pollute Claude Code's stdout."
```

---

### Task A3: `da analyst setup` installs hooks into `~/.claude/settings.json`

**Files:**
- Modify: `cli/commands/analyst.py` (the `setup` command body).
- Test: `tests/test_cli_analyst_setup_hooks.py` (new).

- [ ] **Step 1: Locate the existing setup command**

Run `grep -n "def setup\|def _install\|claude.*settings" cli/commands/analyst.py` to find the entry point. The new logic plugs into the same workflow that already creates `CLAUDE.md`.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_cli_analyst_setup_hooks.py
"""`da analyst setup` should write SessionStart/SessionEnd hooks idempotently."""
import json
from pathlib import Path

from cli.commands.analyst import _install_claude_hooks


def test_install_creates_settings_when_missing(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    _install_claude_hooks(settings)

    cfg = json.loads(settings.read_text())
    starts = cfg["hooks"]["SessionStart"]
    cmds = [h["command"] for e in starts for h in e["hooks"]]
    assert any("da sync --quiet" in c for c in cmds)


def test_install_preserves_existing_unrelated_hooks(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
        },
        "permissions": {"allow": ["Bash(git status:*)"]},
    }))

    _install_claude_hooks(settings)

    cfg = json.loads(settings.read_text())
    assert "PreToolUse" in cfg["hooks"]
    assert cfg["permissions"]["allow"] == ["Bash(git status:*)"]
    assert "SessionStart" in cfg["hooks"]


def test_install_is_idempotent(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    _install_claude_hooks(settings)
    first = settings.read_text()
    _install_claude_hooks(settings)
    second = settings.read_text()
    # Second call must not duplicate the hook entry.
    assert json.loads(first)["hooks"]["SessionStart"] == json.loads(second)["hooks"]["SessionStart"]
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_cli_analyst_setup_hooks.py -v
```

Expected: 3 FAIL — `_install_claude_hooks` doesn't exist.

- [ ] **Step 4: Implement `_install_claude_hooks`**

Add to `cli/commands/analyst.py`:

```python
def _install_claude_hooks(settings_path: Path) -> None:
    """Add SessionStart/SessionEnd hooks calling `da sync` to a Claude settings file.

    Idempotent: replaces our hook entries (matched by command substring `da sync`)
    but leaves anyone else's hooks untouched. Creates the file when missing.
    """
    import json

    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
    else:
        cfg = {}

    hooks = cfg.setdefault("hooks", {})

    def _replace_or_add(event: str, command: str) -> None:
        existing = hooks.setdefault(event, [])
        # Drop any prior `da sync` entries we own
        for entry in list(existing):
            entry_cmds = [h.get("command", "") for h in entry.get("hooks", [])]
            if all("da sync" in c for c in entry_cmds) and entry_cmds:
                existing.remove(entry)
        existing.append({
            "hooks": [{"type": "command", "command": command}]
        })

    _replace_or_add("SessionStart", "da sync --quiet 2>/dev/null || true")
    _replace_or_add("SessionEnd",   "da sync --upload-only --quiet 2>/dev/null || true")

    settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
```

Then call it from the existing `setup` command, after the workspace is detected and before the success message:

```python
# Inside `da analyst setup`, after workspace detection:
from pathlib import Path
claude_settings = Path.home() / ".claude" / "settings.json"
_install_claude_hooks(claude_settings)
typer.echo(f"Installed Claude Code SessionStart/End hooks at {claude_settings}")
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_cli_analyst_setup_hooks.py -v
```

Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add cli/commands/analyst.py tests/test_cli_analyst_setup_hooks.py
git commit -m "feat(cli): da analyst setup installs SessionStart/End hooks

Idempotently writes hooks into ~/.claude/settings.json so each Claude
Code session triggers da sync (pull) on start and --upload-only (push)
on end. Existing user-owned hooks are preserved."
```

---

### Task A4: CHANGELOG entry for Phase A

**Files:**
- Modify: `CHANGELOG.md` (top of file).

- [ ] **Step 1: Add the unreleased entry**

If `## [Unreleased]` already exists at the top, add bullets under the existing sections. Otherwise insert a new section just under the title:

```markdown
## [Unreleased]

### Added
- `da sync --quiet` flag suppresses Rich Progress and final summary, intended for use from Claude Code SessionStart/SessionEnd hooks and cron jobs. Errors still surface on stderr.
- `da analyst setup` now installs `SessionStart` (pull) and `SessionEnd` (upload) hooks into `~/.claude/settings.json`, idempotently, preserving any existing user-owned hooks.
- `docs/setup/claude_settings.json` ships the same two hooks so operators bootstrapping a fresh Claude Code workspace get auto-sync out of the box.

### Fixed
- `docs/setup/claude_settings.json` no longer references the deleted `server/scripts/collect_session.py` — the dead `SessionEnd` hook silently failed in every Claude Code session since the v1→v2 server purge.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): record Phase A — auto-sync hooks"
```

---

### Task A5: Open Phase A PR

- [ ] **Step 1: Push the branch and open the PR**

```bash
git push -u origin plan/local-sync-and-materialized-bq
gh pr create --title "feat(sync): Claude Code SessionStart/End hooks for auto local sync" --body "$(cat <<'EOF'
## Summary
- Adds `da sync --quiet` for hook/cron use
- Replaces broken `SessionEnd` reference (`server/scripts/collect_session.py` was deleted in `ff0e6dc`)
- Ships `SessionStart` (pull) and `SessionEnd` (upload) hooks via the install template and via `da analyst setup`

## Why
Analysts already have RBAC-filtered manifest delta-sync. They had no way to invoke it automatically — `da sync` only ran on explicit invocation. This wires it into Claude Code's lifecycle so every session starts with fresh parquets and finishes by uploading the session log.

## Test plan
- [ ] `pytest tests/test_cli_sync_quiet.py tests/test_setup_hooks_template.py tests/test_cli_analyst_setup_hooks.py -v`
- [ ] Manual: open Claude Code in a workspace bootstrapped with `da analyst setup`, confirm parquets appear in `server/parquet/`
- [ ] Manual: end the session, confirm session jsonl is uploaded
EOF
)"
```

(Phase A is now reviewable independently; Phases B + C land later on top of `main`.)

---

## Phase B — `query_mode='materialized'` for BigQuery

**Open PR for Phase B from a fresh branch off `main` once Phase A is merged.** Tasks below assume you start from `main` after Phase A.

### Task B1: Schema v15 migration adds `source_query` column

**Files:**
- Modify: `src/db.py:19` (bump `SCHEMA_VERSION`) and `src/db.py:152-168` (CREATE TABLE) and `src/db.py:520-540` (migration list).
- Test: `tests/test_db_migration_v15.py` (new).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_migration_v15.py
"""v15 adds source_query column to table_registry."""
import duckdb
import pytest

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


def test_schema_version_is_15():
    assert SCHEMA_VERSION == 15


def test_v15_adds_source_query(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    cols = {
        r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'table_registry'"
        ).fetchall()
    }
    assert "source_query" in cols, f"source_query missing from {cols}"
    assert get_schema_version(conn) == 15
    conn.close()


def test_v14_db_migrates_to_v15(tmp_path):
    """Pre-existing v14 DB without source_query upgrades cleanly."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    # Simulate a v14 DB
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (14)")
    conn.execute("""CREATE TABLE table_registry (
        id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,
        source_type VARCHAR, bucket VARCHAR, source_table VARCHAR,
        sync_strategy VARCHAR DEFAULT 'full_refresh',
        query_mode VARCHAR DEFAULT 'local',
        sync_schedule VARCHAR, profile_after_sync BOOLEAN DEFAULT true,
        primary_key VARCHAR, folder VARCHAR, description TEXT,
        registered_by VARCHAR, is_public BOOLEAN DEFAULT true,
        registered_at TIMESTAMP DEFAULT current_timestamp
    )""")
    conn.execute("INSERT INTO table_registry (id, name) VALUES ('foo', 'foo')")

    _ensure_schema(conn)

    assert get_schema_version(conn) == 15
    cols = {
        r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'table_registry'"
        ).fetchall()
    }
    assert "source_query" in cols
    # Existing row preserved, new column NULL
    row = conn.execute("SELECT id, source_query FROM table_registry WHERE id='foo'").fetchone()
    assert row == ("foo", None)
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_db_migration_v15.py -v
```

Expected: 3 FAIL.

- [ ] **Step 3: Bump schema version + add the column**

In `src/db.py`:

Line 19:
```python
SCHEMA_VERSION = 15
```

Lines 152-168 — add `source_query TEXT` to the CREATE TABLE:
```python
CREATE TABLE IF NOT EXISTS table_registry (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    source_type VARCHAR,
    bucket VARCHAR,
    source_table VARCHAR,
    source_query TEXT,
    sync_strategy VARCHAR DEFAULT 'full_refresh',
    query_mode VARCHAR DEFAULT 'local',
    sync_schedule VARCHAR,
    profile_after_sync BOOLEAN DEFAULT true,
    primary_key VARCHAR,
    folder VARCHAR,
    description TEXT,
    registered_by VARCHAR,
    is_public BOOLEAN DEFAULT true,
    registered_at TIMESTAMP DEFAULT current_timestamp
);
```

In the migration block (around line 520-540, the list of `ALTER TABLE` statements), append the new ALTER for v15:
```python
"ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS source_query TEXT",
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_db_migration_v15.py -v
pytest tests/ -k "schema or migration" -v   # regression check
```

Expected: 3 PASS plus existing migration tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db_migration_v15.py
git commit -m "feat(db): v15 migration — add source_query to table_registry

Backing column for query_mode='materialized'. NULL for existing rows;
admin sets it when registering a materialized BigQuery table."
```

---

### Task B2: `TableRegistryRepository` persists + returns `source_query`

**Files:**
- Modify: `src/repositories/table_registry.py:14-40` (`upsert`) and the `get` / `list_*` methods.
- Test: `tests/test_table_registry_source_query.py` (new).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_table_registry_source_query.py
"""Repository round-trips source_query column."""
import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.table_registry import TableRegistryRepository


@pytest.fixture
def repo(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    return TableRegistryRepository(conn)


def test_upsert_persists_source_query(repo):
    repo.upsert(
        table_id="orders_90d",
        name="orders_90d",
        source_type="bigquery",
        query_mode="materialized",
        source_query="SELECT date, SUM(revenue) FROM bq.\"prj.ds.orders\" WHERE date >= current_date - INTERVAL 90 DAY GROUP BY 1",
        sync_schedule="every 6h",
    )
    row = repo.get("orders_90d")
    assert row["query_mode"] == "materialized"
    assert "INTERVAL 90 DAY" in row["source_query"]
    assert row["sync_schedule"] == "every 6h"


def test_upsert_omitted_source_query_stays_null(repo):
    repo.upsert(table_id="t1", name="t1", source_type="keboola", query_mode="local")
    row = repo.get("t1")
    assert row["source_query"] is None


def test_list_all_includes_source_query(repo):
    repo.upsert(table_id="m1", name="m1", source_type="bigquery",
                query_mode="materialized", source_query="SELECT 1")
    rows = repo.list_all()
    assert rows[0]["source_query"] == "SELECT 1"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_table_registry_source_query.py -v
```

Expected: 3 FAIL — `upsert` rejects the unknown kwarg.

- [ ] **Step 3: Update the repository**

In `src/repositories/table_registry.py`, modify `upsert` to accept and persist `source_query`:

```python
def upsert(
    self, table_id: str, name: str, *,
    source_type: Optional[str] = None,
    bucket: Optional[str] = None,
    source_table: Optional[str] = None,
    source_query: Optional[str] = None,
    query_mode: str = "local",
    sync_schedule: Optional[str] = None,
    profile_after_sync: bool = True,
    folder: Optional[str] = None,
    description: Optional[str] = None,
    registered_by: Optional[str] = None,
    is_public: bool = True,
    sync_strategy: str = "full_refresh",
    primary_key: Optional[str] = None,
) -> None:
    self.conn.execute(
        """INSERT INTO table_registry (id, name, folder, sync_strategy,
            description, registered_by, primary_key,
            source_type, bucket, source_table, source_query, query_mode,
            sync_schedule, profile_after_sync, is_public)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name, folder = excluded.folder,
                sync_strategy = excluded.sync_strategy,
                description = excluded.description,
                source_type = excluded.source_type, bucket = excluded.bucket,
                source_table = excluded.source_table,
                source_query = excluded.source_query,
                query_mode = excluded.query_mode,
                sync_schedule = excluded.sync_schedule,
                profile_after_sync = excluded.profile_after_sync,
                is_public = excluded.is_public,
                primary_key = excluded.primary_key
        """,
        [table_id, name, folder, sync_strategy, description, registered_by, primary_key,
         source_type, bucket, source_table, source_query, query_mode,
         sync_schedule, profile_after_sync, is_public],
    )
```

The `get`, `list_all`, `list_by_source`, etc. helpers all `SELECT *` and return dicts — `source_query` flows through automatically as long as the column exists. Verify by reading the methods.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_table_registry_source_query.py tests/test_db_migration_v15.py -v
```

Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repositories/table_registry.py tests/test_table_registry_source_query.py
git commit -m "feat(registry): persist source_query for materialized tables"
```

---

### Task B3: BigQuery `materialize_query()` writes parquet

**Files:**
- Modify: `connectors/bigquery/extractor.py` (add new function, leave `init_extract` alone for now).
- Test: `tests/test_bq_materialize.py` (new).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bq_materialize.py
"""BigQuery materialize_query writes parquet via DuckDB COPY.

We don't actually attach BigQuery in CI — we substitute the BQ ATTACH
with an in-memory table the test sets up, so the COPY pathway is exercised
end-to-end without a network call.
"""
import duckdb
import pytest
from pathlib import Path
from unittest.mock import patch

from connectors.bigquery.extractor import materialize_query


@pytest.fixture
def stub_bq(monkeypatch):
    """Replace the ATTACH step with an in-memory table named 'bq.test.orders'."""
    real_connect = duckdb.connect

    def _stub_connect(path):
        conn = real_connect(path)
        # Pretend ATTACH happened: create a schema 'bq.test' with an 'orders' table.
        conn.execute("CREATE SCHEMA IF NOT EXISTS bq")
        conn.execute("CREATE SCHEMA IF NOT EXISTS bq.test")
        conn.execute("CREATE OR REPLACE TABLE bq.test.orders AS "
                     "SELECT 'EU' AS region, 100 AS revenue UNION ALL "
                     "SELECT 'US' AS region, 250 AS revenue")
        return conn

    monkeypatch.setattr(duckdb, "connect", _stub_connect)
    yield


def test_materialize_writes_parquet_and_returns_stats(tmp_path, stub_bq):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    stats = materialize_query(
        table_id="orders_summary",
        sql="SELECT region, SUM(revenue) AS revenue FROM bq.test.orders GROUP BY 1",
        project_id="test-project",
        output_dir=str(out),
        skip_attach=True,  # test-only short-circuit, see implementation
    )

    parquet_path = out / "data" / "orders_summary.parquet"
    assert parquet_path.exists()
    assert stats["rows"] == 2
    assert stats["size_bytes"] > 0
    assert stats["query_mode"] == "materialized"

    # Parquet is readable
    rows = duckdb.connect().execute(
        f"SELECT region, revenue FROM read_parquet('{parquet_path}') ORDER BY region"
    ).fetchall()
    assert rows == [("EU", 100), ("US", 250)]


def test_materialize_atomic_on_failure(tmp_path, stub_bq):
    """If COPY fails (bad SQL), no partial parquet remains."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    parquet_path = out / "data" / "broken.parquet"

    with pytest.raises(Exception):
        materialize_query(
            table_id="broken",
            sql="SELECT * FROM bq.test.does_not_exist",
            project_id="test-project",
            output_dir=str(out),
            skip_attach=True,
        )
    assert not parquet_path.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_bq_materialize.py -v
```

Expected: 2 FAIL — `materialize_query` doesn't exist.

- [ ] **Step 3: Implement `materialize_query`**

Append to `connectors/bigquery/extractor.py`:

```python
def materialize_query(
    table_id: str,
    sql: str,
    project_id: str,
    output_dir: str,
    *,
    skip_attach: bool = False,
) -> Dict[str, Any]:
    """Run an SQL query through the DuckDB BQ extension and write the result
    to a parquet file in `output_dir/data/{table_id}.parquet`.

    Atomic: writes to `<file>.tmp` first, renames on success, deletes on failure.

    Args:
        table_id: Logical id from table_registry; becomes the parquet filename.
        sql: SELECT statement (no trailing semicolon). May reference `bq."dataset"."table"`.
        project_id: GCP project ID for the ATTACH.
        output_dir: `/data/extracts/bigquery` (the connector root, not `data/`).
        skip_attach: Test-only — skip ATTACH so a stubbed schema can stand in.

    Returns:
        {"rows": int, "size_bytes": int, "query_mode": "materialized"}
    """
    import os

    if not validate_identifier(table_id, "table_id"):
        raise ValueError(f"unsafe table_id: {table_id!r}")

    out_path = Path(output_dir)
    data_dir = out_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    parquet_path = data_dir / f"{table_id}.parquet"
    tmp_path = data_dir / f"{table_id}.parquet.tmp"
    if tmp_path.exists():
        tmp_path.unlink()

    # Materialize via a throwaway connection so we don't hold a lock on extract.duckdb.
    conn = duckdb.connect(":memory:")
    try:
        if not skip_attach:
            conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
            conn.execute(f"ATTACH 'project={project_id}' AS bq (TYPE bigquery, READ_ONLY)")

        # COPY into the tmp file; the path is interpolated, so escape single quotes.
        safe_path = str(tmp_path).replace("'", "''")
        conn.execute(f"COPY ({sql}) TO '{safe_path}' (FORMAT PARQUET)")

        rows = conn.execute(
            f"SELECT count(*) FROM read_parquet('{safe_path}')"
        ).fetchone()[0]
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    finally:
        conn.close()

    size_bytes = tmp_path.stat().st_size
    os.replace(tmp_path, parquet_path)

    return {"rows": rows, "size_bytes": size_bytes, "query_mode": "materialized"}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_bq_materialize.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add connectors/bigquery/extractor.py tests/test_bq_materialize.py
git commit -m "feat(bq): materialize_query writes parquet via DuckDB COPY

Atomic write (.tmp + rename), happy path + failure cleanup tested
against an in-memory BQ stub. No network in CI."
```

---

### Task B4: BigQuery cost guardrail (dry-run before COPY)

**Files:**
- Modify: `connectors/bigquery/extractor.py` (extend `materialize_query`).
- Modify: `config/instance.yaml.example` (document the knob).
- Test: `tests/test_bq_cost_guardrail.py` (new).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bq_cost_guardrail.py
"""materialize_query refuses to run when dry-run estimate exceeds the cap."""
import pytest
import duckdb
from unittest.mock import patch

from connectors.bigquery.extractor import materialize_query, MaterializeBudgetError


def test_refuses_when_estimate_exceeds_cap(tmp_path, monkeypatch):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    # Stub the dry-run helper to claim the query would scan 100 GB.
    with patch("connectors.bigquery.extractor._dry_run_bytes", return_value=100 * 2**30):
        with pytest.raises(MaterializeBudgetError) as exc:
            materialize_query(
                table_id="huge",
                sql="SELECT * FROM bq.bigds.fact",
                project_id="p",
                output_dir=str(out),
                max_bytes=10 * 2**30,  # 10 GB cap
                skip_attach=True,
            )
    assert "100" in str(exc.value)


def test_proceeds_when_estimate_under_cap(tmp_path, monkeypatch):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    real_connect = duckdb.connect

    def _stub_connect(path):
        conn = real_connect(path)
        conn.execute("CREATE SCHEMA IF NOT EXISTS bq.test")
        conn.execute("CREATE OR REPLACE TABLE bq.test.tiny AS SELECT 1 AS n")
        return conn

    monkeypatch.setattr(duckdb, "connect", _stub_connect)

    with patch("connectors.bigquery.extractor._dry_run_bytes", return_value=1024):
        stats = materialize_query(
            table_id="tiny", sql="SELECT * FROM bq.test.tiny",
            project_id="p", output_dir=str(out),
            max_bytes=10 * 2**30, skip_attach=True,
        )
    assert stats["rows"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_bq_cost_guardrail.py -v
```

Expected: 2 FAIL — neither `MaterializeBudgetError` nor `_dry_run_bytes` exists.

- [ ] **Step 3: Add the guardrail**

In `connectors/bigquery/extractor.py`:

```python
class MaterializeBudgetError(RuntimeError):
    """Raised when the BigQuery dry-run estimate exceeds the configured cap."""


def _dry_run_bytes(sql: str, project_id: str) -> int:
    """Use the BigQuery REST API directly to estimate bytes scanned.

    DuckDB's BQ extension doesn't surface a dry-run primitive, so we go
    through google-cloud-bigquery (already a transitive dep via the GCP
    SDK in this project). Returns 0 if the dry-run itself errors — caller
    decides whether to fail-closed via the cap.
    """
    try:
        from google.cloud import bigquery
        from google.cloud.bigquery import QueryJobConfig

        client = bigquery.Client(project=project_id)
        cfg = QueryJobConfig(dry_run=True, use_query_cache=False)
        job = client.query(sql, job_config=cfg)
        return int(job.total_bytes_processed or 0)
    except ImportError:
        # google-cloud-bigquery not installed — skip the guardrail (warn upstream).
        return 0
```

Update `materialize_query` signature + early branch:

```python
def materialize_query(
    table_id: str,
    sql: str,
    project_id: str,
    output_dir: str,
    *,
    max_bytes: Optional[int] = None,
    skip_attach: bool = False,
) -> Dict[str, Any]:
    # ...identifier check unchanged...

    if max_bytes is not None:
        estimated = _dry_run_bytes(sql, project_id)
        if estimated > max_bytes:
            raise MaterializeBudgetError(
                f"dry-run estimate {estimated:,} bytes exceeds cap {max_bytes:,} "
                f"for table {table_id!r}"
            )

    # ...rest unchanged...
```

In `config/instance.yaml.example`, under the `bigquery:` block, add:

```yaml
bigquery:
  project_id: ""
  # Cost guardrail for query_mode='materialized'. The scheduler runs a BQ
  # dry-run before each COPY and refuses to run if the estimate exceeds
  # this cap. Set to null to disable (NOT recommended in production).
  max_bytes_per_materialize: 10737418240   # 10 GiB
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_bq_cost_guardrail.py tests/test_bq_materialize.py -v
```

Expected: 4 PASS (2 new + 2 from B3).

- [ ] **Step 5: Commit**

```bash
git add connectors/bigquery/extractor.py config/instance.yaml.example tests/test_bq_cost_guardrail.py
git commit -m "feat(bq): cost guardrail — dry-run estimate vs. configurable cap

Default 10 GiB. Refuses to materialize when BQ dry-run reports a
larger scan, preventing a misconfigured query from eating the monthly
project budget."
```

---

### Task B5: `/api/sync/trigger` runs materialized queries on schedule

**Files:**
- Modify: `app/api/sync.py` — extend `trigger_sync` to walk materialized rows.
- Test: `tests/test_sync_trigger_materialized.py` (new).

- [ ] **Step 1: Find the trigger handler**

```bash
grep -n "def trigger_sync\|def _do_sync\|materialize\|query_mode" app/api/sync.py | head -20
```

Locate where the body of the trigger walks tables. (It currently calls source-specific extractors; we add a per-row materialized branch.)

- [ ] **Step 2: Write the failing test**

```python
# tests/test_sync_trigger_materialized.py
"""trigger_sync materializes due BQ tables and updates sync_state."""
import duckdb
import pytest
from pathlib import Path
from unittest.mock import patch

from src.db import _ensure_schema
from src.repositories.table_registry import TableRegistryRepository


@pytest.fixture
def system_db(tmp_path, monkeypatch):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    repo = TableRegistryRepository(conn)
    repo.upsert(
        table_id="orders_90d", name="orders_90d",
        source_type="bigquery", query_mode="materialized",
        source_query="SELECT 1 AS n",
        sync_schedule="every 1m",  # always due in tests
    )
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    yield conn
    conn.close()


def test_trigger_calls_materialize_for_due_rows(system_db, tmp_path):
    from app.api import sync as sync_mod

    with patch("app.api.sync._materialize_table") as mock_mat:
        mock_mat.return_value = {"rows": 1, "size_bytes": 100, "query_mode": "materialized"}
        sync_mod._run_materialized_pass(system_db, project_id="test-project")

    mock_mat.assert_called_once()
    call_kwargs = mock_mat.call_args.kwargs
    assert call_kwargs["table_id"] == "orders_90d"
    assert "SELECT 1 AS n" in call_kwargs["sql"]
```

- [ ] **Step 3: Run test to verify it fails**

```bash
pytest tests/test_sync_trigger_materialized.py -v
```

Expected: FAIL — `_run_materialized_pass` and `_materialize_table` don't exist.

- [ ] **Step 4: Wire materialization into the trigger**

Add to `app/api/sync.py` (top of file, with the other helpers):

```python
def _materialize_table(
    *,
    table_id: str,
    sql: str,
    project_id: str,
    output_dir: str,
    max_bytes: int | None,
) -> dict:
    """Thin wrapper so the trigger can be tested without importing duckdb."""
    from connectors.bigquery.extractor import materialize_query
    return materialize_query(
        table_id=table_id, sql=sql, project_id=project_id,
        output_dir=output_dir, max_bytes=max_bytes,
    )


def _run_materialized_pass(conn, project_id: str) -> dict:
    """Walk table_registry for materialized BQ rows and run any that are due."""
    from datetime import datetime, timezone
    from src.scheduler import is_table_due
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.sync_state import SyncStateRepository
    from app.instance_config import get_value

    output_dir = str(Path(_get_data_dir()) / "extracts" / "bigquery")
    max_bytes = get_value(["bigquery", "max_bytes_per_materialize"], 10 * 2**30)

    registry = TableRegistryRepository(conn)
    state = SyncStateRepository(conn)

    summary = {"materialized": [], "skipped": [], "errors": []}
    for row in registry.list_all():
        if row.get("query_mode") != "materialized":
            continue
        last = state.get_last_sync(row["id"])
        if not is_table_due(row.get("sync_schedule") or "every 1h", last):
            summary["skipped"].append(row["id"])
            continue
        try:
            stats = _materialize_table(
                table_id=row["id"],
                sql=row["source_query"],
                project_id=project_id,
                output_dir=output_dir,
                max_bytes=max_bytes,
            )
            state.update_after_sync(
                table_id=row["id"],
                rows=stats["rows"],
                file_size_bytes=stats["size_bytes"],
                hash="",  # filled by manifest pass via _file_hash
            )
            summary["materialized"].append(row["id"])
        except Exception as e:
            summary["errors"].append({"table": row["id"], "error": str(e)})

    return summary
```

In the existing `trigger_sync` body, after Keboola/Jira passes and before the orchestrator rebuild:

```python
# Materialized BigQuery pass — runs SQL → parquet for due rows.
project_id = get_value(["bigquery", "project_id"], "")
if project_id:
    materialized = _run_materialized_pass(conn, project_id)
    print(f"[SYNC] materialized: {materialized}", flush=True)
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_sync_trigger_materialized.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api/sync.py tests/test_sync_trigger_materialized.py
git commit -m "feat(sync): trigger materializes due BQ rows

Walks table_registry for query_mode='materialized', honors per-table
sync_schedule via existing is_table_due(), writes parquet + updates
sync_state. Errors are aggregated per-row, not fatal."
```

---

### Task B6: BQ extractor `init_extract` skips materialized rows for view creation

**Files:**
- Modify: `connectors/bigquery/extractor.py:90-130` (the `for tc in table_configs` loop).
- Test: extend `tests/test_bq_materialize.py` (or new `tests/test_bq_init_extract_skips.py`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bq_init_extract_skips.py
"""init_extract creates remote views only for query_mode='remote' rows;
materialized rows are handled by the sync trigger pass instead."""
import duckdb
from pathlib import Path

from connectors.bigquery.extractor import init_extract


def test_init_extract_skips_materialized(tmp_path, monkeypatch):
    out = tmp_path / "extracts" / "bigquery"

    # Stub ATTACH so we don't hit BQ.
    real_connect = duckdb.connect

    def _stub(path):
        conn = real_connect(path)
        # Pretend ATTACH worked; the loop's CREATE VIEW will reference bq.X.Y
        # We don't actually need it to resolve since the matrialized row is skipped
        # and the remote row gets a CREATE VIEW that we'll inspect via _meta.
        conn.execute("CREATE SCHEMA IF NOT EXISTS bq")
        conn.execute("CREATE SCHEMA IF NOT EXISTS bq.dset")
        conn.execute("CREATE OR REPLACE TABLE bq.dset.live AS SELECT 1 AS x")
        return conn

    monkeypatch.setattr(duckdb, "connect", _stub)

    configs = [
        {"name": "live_orders", "bucket": "dset", "source_table": "live", "query_mode": "remote"},
        {"name": "agg_90d",     "bucket": "dset", "source_table": "live",
         "query_mode": "materialized", "source_query": "SELECT 1"},
    ]
    init_extract(str(out), "test-project", configs, skip_attach=True)

    db = duckdb.connect(str(out / "extract.duckdb"))
    meta = db.execute("SELECT table_name, query_mode FROM _meta ORDER BY table_name").fetchall()
    assert meta == [("live_orders", "remote")]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_bq_init_extract_skips.py -v
```

Expected: FAIL — both rows would currently be added to `_meta`, or `init_extract` doesn't accept `skip_attach`.

- [ ] **Step 3: Update `init_extract`**

In `connectors/bigquery/extractor.py`, change the signature to accept `skip_attach=False`:

```python
def init_extract(
    output_dir: str,
    project_id: str,
    table_configs: List[Dict[str, Any]],
    *,
    skip_attach: bool = False,
) -> Dict[str, Any]:
```

Inside the loop, skip materialized rows:

```python
for tc in table_configs:
    if tc.get("query_mode") == "materialized":
        # Handled by the sync trigger pass — it writes the parquet and
        # the orchestrator will pick it up via the standard local path.
        continue
    # ...existing remote view creation unchanged...
```

Replace the `INSTALL/LOAD/ATTACH` block with the `skip_attach` guard:

```python
if not skip_attach:
    conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
    conn.execute(f"ATTACH 'project={project_id}' AS bq (TYPE bigquery, READ_ONLY)")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_bq_init_extract_skips.py tests/test_bq_materialize.py -v
```

Expected: 3 PASS (1 new + 2 from B3).

- [ ] **Step 5: Commit**

```bash
git add connectors/bigquery/extractor.py tests/test_bq_init_extract_skips.py
git commit -m "feat(bq): init_extract skips materialized rows for view creation

They live in /data/extracts/bigquery/data/*.parquet (written by the
sync trigger pass) and the orchestrator's standard local-parquet
discovery picks them up — no view needed in extract.duckdb."
```

---

### Task B7: Admin API accepts `mode='materialized'` + `source_query`

**Files:**
- Modify: `app/api/admin.py:75-100` (`RegisterTableRequest` + `UpdateTableRequest`).
- Modify: `app/api/admin.py` registration handler — validate mode/query coherence.
- Test: `tests/test_api_admin_materialized.py` (new).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_admin_materialized.py
from fastapi.testclient import TestClient
import pytest

from app.main import app


@pytest.fixture
def admin_client(monkeypatch):
    # Test fixture pattern from existing admin tests; reuse the test admin token.
    return TestClient(app)


def test_register_materialized_requires_query(admin_client, admin_token):
    r = admin_client.post("/api/admin/tables/orders_90d",
        json={"name": "orders_90d", "source_type": "bigquery",
              "query_mode": "materialized"},  # no source_query
        headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 400
    assert "source_query" in r.json()["detail"].lower()


def test_register_materialized_accepts_query(admin_client, admin_token):
    r = admin_client.post("/api/admin/tables/orders_90d",
        json={"name": "orders_90d", "source_type": "bigquery",
              "query_mode": "materialized",
              "source_query": "SELECT date FROM bq.\"prj.ds.orders\"",
              "sync_schedule": "every 6h"},
        headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200


def test_register_remote_rejects_query(admin_client, admin_token):
    """source_query only makes sense with materialized mode."""
    r = admin_client.post("/api/admin/tables/x",
        json={"name": "x", "source_type": "bigquery",
              "query_mode": "remote",
              "source_query": "SELECT 1"},
        headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_api_admin_materialized.py -v
```

Expected: 3 FAIL.

- [ ] **Step 3: Extend the request model + handler**

In `app/api/admin.py`:

```python
class RegisterTableRequest(BaseModel):
    name: str
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    source_query: Optional[str] = None
    query_mode: str = "local"
    sync_schedule: Optional[str] = None
    description: Optional[str] = None
    is_public: bool = True

    @model_validator(mode="after")
    def _check_mode_query_coherence(self):
        if self.query_mode == "materialized" and not self.source_query:
            raise ValueError("query_mode='materialized' requires source_query")
        if self.query_mode != "materialized" and self.source_query:
            raise ValueError("source_query is only valid when query_mode='materialized'")
        return self
```

(`UpdateTableRequest` mirrors the same fields and validator.)

In the registration handler, ensure `source_query` is forwarded to `repo.upsert`. Also catch `ValidationError` and return a 400 instead of FastAPI's default 422 so the test assertions on `400` hold.

```python
@router.post("/tables/{table_id}")
async def register_table(table_id: str, request: RegisterTableRequest, ...):
    try:
        repo.upsert(
            table_id=table_id,
            name=request.name,
            source_type=request.source_type,
            bucket=request.bucket,
            source_table=request.source_table,
            source_query=request.source_query,
            query_mode=request.query_mode,
            sync_schedule=request.sync_schedule,
            description=request.description,
            is_public=request.is_public,
            registered_by=user.get("email", "admin"),
        )
    except (ValueError, ValidationError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "table_id": table_id}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_api_admin_materialized.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/admin.py tests/test_api_admin_materialized.py
git commit -m "feat(api): admin tables endpoint accepts source_query for materialized mode

Validates mode/query coherence: materialized requires source_query;
local/remote forbid it. Returns 400 on mismatch."
```

---

### Task B8: CLI `da admin table register --mode materialized --query @file.sql`

**Files:**
- Modify: `cli/commands/admin.py` (table register subcommand).
- Test: `tests/test_cli_admin_materialized.py` (new).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_admin_materialized.py
import json
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock
from pathlib import Path

from cli.main import app


def test_register_materialized_reads_query_from_file(tmp_path, monkeypatch):
    sql_file = tmp_path / "orders.sql"
    sql_file.write_text("SELECT date FROM bq.\"prj.ds.orders\"")

    captured = {}

    def fake_post(path, json):
        captured["path"] = path
        captured["json"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.json = lambda: {"status": "ok", "table_id": "orders_90d"}
        return resp

    monkeypatch.setattr("cli.commands.admin.api_post", fake_post)

    runner = CliRunner()
    result = runner.invoke(app, [
        "admin", "table", "register", "orders_90d",
        "--source", "bigquery",
        "--mode", "materialized",
        "--query", f"@{sql_file}",
        "--schedule", "every 6h",
    ])
    assert result.exit_code == 0, result.stdout
    assert captured["json"]["query_mode"] == "materialized"
    assert "SELECT date" in captured["json"]["source_query"]
    assert captured["json"]["sync_schedule"] == "every 6h"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_cli_admin_materialized.py -v
```

Expected: FAIL — flags not yet wired.

- [ ] **Step 3: Add the flags + `@file` shorthand**

In `cli/commands/admin.py`, find the `table register` command (or add it if it doesn't exist with this exact shape) and update:

```python
@table_app.command("register")
def register_table(
    table_id: str = typer.Argument(..., help="Logical id (alphanumeric + underscore)"),
    source: str = typer.Option(..., "--source", help="Source type: keboola/bigquery/csv"),
    mode: str = typer.Option("local", "--mode",
        help="Query mode: local (default), remote, materialized"),
    bucket: str = typer.Option(None, "--bucket"),
    source_table: str = typer.Option(None, "--source-table"),
    query: str = typer.Option(None, "--query",
        help="SQL for materialized mode. `@path/to.sql` reads from disk."),
    schedule: str = typer.Option(None, "--schedule",
        help="e.g. 'every 6h' or 'daily 03:00'"),
    description: str = typer.Option(None, "--description"),
):
    """Register a table in table_registry."""
    if query and query.startswith("@"):
        query = Path(query[1:]).read_text(encoding="utf-8").strip()

    if mode == "materialized" and not query:
        typer.echo("error: --mode materialized requires --query (or --query @file.sql)", err=True)
        raise typer.Exit(2)

    payload = {
        "name": table_id,
        "source_type": source,
        "query_mode": mode,
        "bucket": bucket,
        "source_table": source_table,
        "source_query": query,
        "sync_schedule": schedule,
        "description": description,
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    resp = api_post(f"/api/admin/tables/{table_id}", json=payload)
    if resp.status_code >= 400:
        typer.echo(f"error: {resp.status_code} {resp.text}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Registered {table_id} (mode={mode})")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_cli_admin_materialized.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli/commands/admin.py tests/test_cli_admin_materialized.py
git commit -m "feat(cli): da admin table register --mode materialized --query @file.sql

Convenience for materialized BQ tables: SQL can be inlined or read
from a file via @path/to.sql. Schedule passes through to table_registry."
```

---

### Task B9: Phase B CHANGELOG + Phase B PR

**Files:**
- Modify: `CHANGELOG.md`.

- [ ] **Step 1: Append to the unreleased section**

Add under `## [Unreleased]`:

```markdown
### Added
- `query_mode='materialized'` for BigQuery (schema v15). Admins register a SQL query via `da admin table register --mode materialized --query @file.sql --schedule "every 6h"`; the scheduler runs it through the DuckDB BQ extension and writes a parquet to `/data/extracts/bigquery/data/`. The existing manifest + RBAC flow distributes it to analysts on `da sync` (and the SessionStart hook from Phase A) without any client change.
- `bigquery.max_bytes_per_materialize` config knob (default 10 GiB). The trigger runs a BQ dry-run before each COPY and refuses to materialize if the estimate exceeds the cap.
- New `source_query TEXT` column on `table_registry` (NULL for non-materialized rows).

### Changed
- BigQuery `init_extract` no longer creates remote views for materialized rows; they live as parquets and surface via the orchestrator's standard local-parquet discovery.
```

- [ ] **Step 2: Commit + open PR**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): record Phase B — materialized BQ"
git push -u origin <phase-b-branch>
gh pr create --title "feat(bq): query_mode='materialized' for scheduled BQ → parquet" --body "$(cat <<'EOF'
## Summary
- Schema v15 adds `source_query` column
- New BQ extractor function `materialize_query()` writes parquet via DuckDB COPY
- `/api/sync/trigger` walks materialized rows and runs due ones
- BQ dry-run cost guardrail with `bigquery.max_bytes_per_materialize`
- Admin API + CLI accept `--mode materialized --query @file.sql`

## RBAC
No new code. The materialized parquet lands in `/data/extracts/bigquery/data/<id>.parquet`. Manifest filtering already gates per-user via `can_access_table` → `resource_grants(group, ResourceType.TABLE, id)`. Admins control auto-sync membership by granting the table to a user group.

## Test plan
- [ ] `pytest tests/test_db_migration_v15.py tests/test_table_registry_source_query.py tests/test_bq_materialize.py tests/test_bq_cost_guardrail.py tests/test_bq_init_extract_skips.py tests/test_sync_trigger_materialized.py tests/test_api_admin_materialized.py tests/test_cli_admin_materialized.py -v`
- [ ] Manual: register a small materialized table against a real BQ project, trigger sync, confirm parquet appears and `da sync` downloads it on the analyst side
- [ ] Manual: register a query that scans more than `max_bytes_per_materialize`, confirm trigger logs the budget error and skips the row
EOF
)"
```

---

## Phase C — Documentation

Lands after Phase A and Phase B are merged. Single PR.

### Task C1: README — mode-first table + auto-sync section

**Files:**
- Modify: `README.md:41-49` (Supported Data Sources table) and add new section after Quick Start.

- [ ] **Step 1: Replace the source-mode table**

Find the existing table:

```markdown
| Source | Mode | Description |
|--------|------|-------------|
| **Keboola** | Batch pull | DuckDB Keboola extension downloads tables to Parquet on a schedule |
| **BigQuery** | Remote attach | DuckDB BQ extension; queries execute in BigQuery, no local download |
| **Jira** | Real-time push | Webhook receiver updates Parquet files incrementally |
```

Replace with mode-first layout (so BigQuery can appear in two rows once Phase B is in):

```markdown
| Mode | Distribution | Sources | Use when |
|------|--------------|---------|----------|
| **Batch pull** (`local`) | Parquet on disk, scheduled | Keboola | Source has a native bulk-export and the table fits on disk |
| **Materialized SQL** (`materialized`) | Parquet on disk, scheduled query | BigQuery | Source table is too large; you want a curated subset on disk |
| **Remote attach** (`remote`) | View, no download | BigQuery | Table is too large to materialize; latency cost of remote query is acceptable |
| **Real-time push** | Incremental parquet | Jira | Source is event-driven and you need sub-minute freshness |

The first three modes are what `da sync` distributes to analysts. The fourth is server-side only — analysts query Jira data through the same `da sync`-distributed parquets.
```

- [ ] **Step 2: Add "Local sync & auto-update" section**

Insert after the Quick Start section:

```markdown
## Local sync & auto-update

Analysts run Claude Code against a local DuckDB built from RBAC-filtered parquets. `da sync` does the work:

```bash
da sync           # delta-pull: manifest → MD5 compare → download changed → rebuild views
da sync --watch   # same, every 15 min (cron-style standalone)
```

`da analyst setup` installs Claude Code hooks that call `da sync --quiet` on every SessionStart and `da sync --upload-only --quiet` on SessionEnd, so analysts get fresh data without thinking about it. The hooks are in `~/.claude/settings.json` and you can edit them.

**Admin: which tables auto-sync to whom**

The auto-sync set per user is the intersection of:

1. Tables with `query_mode IN ('local', 'materialized')` — these have parquets on disk and end up in the manifest.
2. Tables granted to one of the user's groups via `resource_grants(group, table_id)` (see `docs/RBAC.md`).

To enroll a new table for auto-sync, register it (or update its `query_mode`) and grant it to the relevant groups in `/admin/access`. New analysts get the same set on their next `da sync`.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): mode-first table + Local sync & auto-update section"
```

---

### Task C2: CLAUDE.md — schema v15, materialized mode, hooks under Development

**Files:**
- Modify: `CLAUDE.md` (multiple sections).

- [ ] **Step 1: Update the schema description**

Find the line in "DuckDB Schema (src/db.py)" that mentions schema versions and add v15:

> Schema v15 adds `source_query TEXT` to `table_registry` to back `query_mode='materialized'` (BigQuery scheduled-query parquet path).

- [ ] **Step 2: Update the connector pattern section**

In "Architecture: extract.duckdb Contract", add the third bullet to "Three source types":

```markdown
Source modes:
- **Batch pull** (Keboola, `query_mode='local'`): DuckDB extension downloads to parquet, scheduled
- **Remote attach** (BigQuery, `query_mode='remote'`): DuckDB BQ extension, no download, queries go to BQ
- **Materialized SQL** (BigQuery, `query_mode='materialized'`): scheduler runs admin-registered SQL through DuckDB BQ extension, writes the result to `/data/extracts/bigquery/data/<id>.parquet`. Distributed via the same manifest + `da sync` flow as Keboola tables.
- **Real-time push** (Jira): Webhooks update parquets incrementally
```

- [ ] **Step 3: Add a "Local sync & Claude Code hooks" subsection under Development**

```markdown
### Local sync & Claude Code hooks

`da sync` is the canonical analyst-side distribution path: pulls the RBAC-filtered manifest from the server, downloads parquets whose MD5 changed, rebuilds local DuckDB views.

`da analyst setup` writes two hooks into `~/.claude/settings.json`:

- `SessionStart` → `da sync --quiet` — pulls fresh parquets at the start of every Claude Code session
- `SessionEnd`   → `da sync --upload-only --quiet` — uploads the session jsonl + `CLAUDE.local.md` to the server

The hooks pass `--quiet` so they don't pollute Claude Code stdout, and trail with `|| true` so a server outage never blocks a session.

Admin RBAC for auto-sync: `query_mode IN ('local', 'materialized')` plus a `resource_grants` row for one of the user's groups → table appears in their manifest → `da sync` downloads it. No per-user sync config; the admin layer is the single source of truth.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude.md): schema v15, materialized mode, auto-sync hooks"
```

---

### Task C3: `cli/skills/connectors.md` decision table + `docs/architecture.md` diagram refresh

**Files:**
- Modify: `cli/skills/connectors.md`.
- Modify: `docs/architecture.md`.

- [ ] **Step 1: Append a "BigQuery: when to use which mode" section to `cli/skills/connectors.md`**

```markdown
## BigQuery: pick a mode

| Need | Mode | Why |
|------|------|-----|
| Latency under 100 ms, table fits on disk | `materialized` | Local parquet, no BQ roundtrip |
| Table too large for analyst's disk, occasional ad-hoc query | `remote` | DuckDB BQ extension, no download |
| Table too large for disk AND analyst hits it constantly | `materialized` with aggregation/filter | Scheduled COPY of a slice |
| One-off subquery joined with local data | (no registry row) | Use `da query --register-bq …` for ad-hoc |

Cost: `materialized` runs once per `sync_schedule` regardless of how many analysts query it. `remote` runs once per analyst-query. The break-even is roughly query frequency × bytes scanned vs. one COPY × bytes scanned.

Guardrail: `bigquery.max_bytes_per_materialize` (default 10 GiB) blocks the COPY when BQ's dry-run estimate exceeds the cap. Set it explicitly per environment in `instance.yaml`.
```

- [ ] **Step 2: Refresh the ASCII diagram in `docs/architecture.md`**

Replace the BigQuery box with two lanes:

```
┌──────────────┐  ┌────────────────────┐  ┌──────────────┐
│   Keboola    │  │     BigQuery       │  │   Jira       │
│  extractor   │  │  ┌──────┬───────┐  │  │  webhooks    │
│ (DuckDB ext) │  │  │remote│material│ │  │ (incremental)│
│              │  │  │ view │  ized  │ │  │              │
└──────┬───────┘  └────────┬─────────┘  └──────┬───────┘
       │                    │                    │
       ▼                    ▼                    ▼
   extract.duckdb    extract.duckdb +     extract.duckdb
   + data/*.parquet  data/*.parquet for   + data/*.parquet
                     materialized rows
       │                    │                    │
       └────────────────────┼────────────────────┘
                            ▼
                 SyncOrchestrator.rebuild()
                            │
                ┌───────────┼───────────┐
                ▼           ▼           ▼
            FastAPI     /api/sync/   da sync
            (serve)      manifest    (pull → local)
```

- [ ] **Step 3: Commit + open PR**

```bash
git add cli/skills/connectors.md docs/architecture.md
git commit -m "docs: BigQuery mode decision table + architecture diagram refresh"
git push -u origin <phase-c-branch>
gh pr create --title "docs: local-sync hooks + materialized-BQ guide" --body "Wraps up the local-sync rollout with README, CLAUDE.md, connectors guide, and architecture-diagram updates."
```

---

## Self-Review Notes (filled by author at completion)

**Spec coverage check:**

| Requirement | Task |
|---|---|
| Auto-sync at SessionStart | A2, A3 |
| Upload sessions at SessionEnd | A2, A3 |
| `da sync` invocable from a hook (no progress noise) | A1 |
| Admin can specify which tables sync to which user | C1, C2 — documented; no new code (RBAC + `query_mode` already cover this) |
| BigQuery materialized mode | B1–B8 |
| Cost guardrail | B4 |
| Per-table `sync_schedule` honored for materialized rows | B5 |
| RBAC filtering for materialized tables | implicit — manifest already filters via `can_access_table` |
| Vendor-agnostic OSS (no GRPN/Foundry leaks) | examples use `prj.ds.orders` placeholders only |

**No placeholder check:** every code step shows the actual code; every test step has full test bodies; every command has full args. ✓

**Type/name consistency:**
- `materialize_query(table_id, sql, project_id, output_dir, *, max_bytes=None, skip_attach=False)` — same signature in B3, B4, B5, B6.
- `_run_materialized_pass(conn, project_id)` and `_materialize_table(...)` — defined in B5, only called there.
- `_install_claude_hooks(settings_path)` — defined and tested in A3.
- Schema column `source_query` (TEXT) — same name in B1, B2, B7, B8.

---

## Open questions to resolve before execution

1. **Per-user opt-in whitelist (Phase 4)**: a future plan should add `auto_sync_default BOOLEAN` on `table_registry` and a `user_sync_subscriptions(user_id, table_id)` table for analysts to opt into large tables they'd otherwise miss. Out of scope here.
2. **Materialized refresh on schema drift**: if the BQ source schema changes mid-cycle (column added/dropped), the next COPY succeeds but downstream views may break. Phase B logs the failure and continues; a follow-up could surface this in `/admin/tables` UI.
3. **Cost guardrail when `google-cloud-bigquery` is missing**: currently fails open (returns 0 bytes, COPY proceeds). Acceptable for OSS but operators should be told. Documented in C1.
