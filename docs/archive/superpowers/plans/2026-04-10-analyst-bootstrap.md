# Analyst Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `da analyst setup` command that onboards analysts to a remote Agnes instance — connects, downloads data, initializes local DuckDB, generates CLAUDE.md.

**Architecture:** New top-level Typer command `da analyst` with `setup` subcommand. Uses existing `cli/client.py` HTTP helpers. Generates CLAUDE.md from template with instance-specific placeholders.

**Tech Stack:** Typer, httpx (via cli/client.py), DuckDB, Rich (progress bars), Jinja2-style string substitution

**Spec:** `docs/superpowers/specs/2026-04-10-porting-internal-features-design.md` — Section 2

**Depends on:** Business Metrics plan (Task 5 — metrics API must exist for Step 4)

---

### Task 1: CLAUDE.md Template

**Files:**
- Create: `config/claude_md_template.txt`

- [ ] **Step 1: Create the template file**

Create `config/claude_md_template.txt`:

```
# {instance_name} — AI Data Analyst

This workspace is connected to {server_url}.

## Rules
- Before computing any business metric: run `da metrics show <category>/<name>`
- For current schema: read `data/metadata/schema.json`
- Do not use DESCRIBE/SHOW COLUMNS — read metadata files instead
- Save work output to `user/artifacts/`
- Sync data regularly with `da sync`

## Metrics Workflow
1. `da metrics list` — find the relevant metric
2. `da metrics show revenue/mrr` — read SQL and business rules
3. Use the canonical SQL from the metric definition, adapt to the question
4. Never invent metric calculations — always check existing definitions first

## Data Sync
- `da sync` — download current data from server
- `da sync --docs-only` — just metadata and metrics (fast refresh)
- `da sync --upload-only` — upload sessions and local notes to server
- Data on the server refreshes every {sync_interval}

## Directory Structure
- `data/` — read-only data downloaded from server
  - `data/parquet/` — table data in Parquet format
  - `data/duckdb/` — local analytics DuckDB database
  - `data/metadata/` — profiles, schema, metrics cache
- `user/` — your workspace (persistent across syncs)
  - `user/artifacts/` — analysis outputs, reports, charts
  - `user/sessions/` — Claude Code session logs
- `.claude/CLAUDE.local.md` — your personal notes (never overwritten, uploaded on sync)
```

- [ ] **Step 2: Commit**

```bash
git add config/claude_md_template.txt
git commit -m "feat: add CLAUDE.md template for analyst bootstrap"
```

---

### Task 2: `da analyst setup` — Core Command

**Files:**
- Create: `cli/commands/analyst.py`
- Modify: `cli/main.py` (register analyst_app)
- Test: `tests/test_cli.py` (help test)
- Test: `tests/test_analyst_bootstrap.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_cli.py` in `TestCLIHelp`:

```python
    def test_analyst_help(self):
        result = runner.invoke(app, ["analyst", "--help"])
        assert result.exit_code == 0
        assert "setup" in result.output
```

Create `tests/test_analyst_bootstrap.py`:

```python
"""Tests for analyst bootstrap flow."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path / "local"))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "local").mkdir()
    monkeypatch.chdir(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    yield tmp_path / "workspace"


class TestDetectExistingProject:
    def test_detects_existing_claude_md(self, tmp_workspace):
        (tmp_workspace / "CLAUDE.md").write_text("# Acme — AI Data Analyst\n")
        result = runner.invoke(app, ["analyst", "setup", "--server-url", "http://localhost:8000"])
        assert "already set up" in result.output.lower() or result.exit_code == 0

    def test_no_detection_with_force(self, tmp_workspace):
        (tmp_workspace / "CLAUDE.md").write_text("# Acme — AI Data Analyst\n")
        with patch("cli.commands.analyst._connect_to_instance") as mock_connect:
            mock_connect.side_effect = SystemExit(1)  # Will fail at connect step
            result = runner.invoke(app, ["analyst", "setup", "--force",
                                         "--server-url", "http://localhost:8000"])
            # Should have passed detection and attempted connect
            mock_connect.assert_called_once()


class TestCreateWorkspace:
    def test_creates_directory_structure(self, tmp_workspace):
        from cli.commands.analyst import _create_workspace
        _create_workspace(tmp_workspace)
        assert (tmp_workspace / "data" / "parquet").is_dir()
        assert (tmp_workspace / "data" / "duckdb").is_dir()
        assert (tmp_workspace / "data" / "metadata").is_dir()
        assert (tmp_workspace / "user" / "artifacts").is_dir()
        assert (tmp_workspace / "user" / "sessions").is_dir()
        assert (tmp_workspace / ".claude").is_dir()


class TestGenerateClaudeMd:
    def test_generates_from_template(self, tmp_workspace):
        from cli.commands.analyst import _generate_claude_md
        _generate_claude_md(
            workspace=tmp_workspace,
            instance_name="Acme Analytics",
            server_url="https://data.acme.com",
            sync_interval="15 minutes",
        )
        claude_md = tmp_workspace / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text()
        assert "Acme Analytics" in content
        assert "https://data.acme.com" in content
        assert "15 minutes" in content

    def test_creates_claude_local_md(self, tmp_workspace):
        from cli.commands.analyst import _generate_claude_md
        (tmp_workspace / ".claude").mkdir(parents=True, exist_ok=True)
        _generate_claude_md(
            workspace=tmp_workspace,
            instance_name="Test",
            server_url="http://localhost",
            sync_interval="1 hour",
        )
        assert (tmp_workspace / ".claude" / "CLAUDE.local.md").exists()

    def test_does_not_overwrite_existing_local_md(self, tmp_workspace):
        (tmp_workspace / ".claude").mkdir(parents=True, exist_ok=True)
        local_md = tmp_workspace / ".claude" / "CLAUDE.local.md"
        local_md.write_text("my notes")
        from cli.commands.analyst import _generate_claude_md
        _generate_claude_md(
            workspace=tmp_workspace,
            instance_name="Test",
            server_url="http://localhost",
            sync_interval="1 hour",
        )
        assert local_md.read_text() == "my notes"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_analyst_bootstrap.py -v`
Expected: FAIL — `No such command 'analyst'`

- [ ] **Step 3: Implement `cli/commands/analyst.py`**

Create `cli/commands/analyst.py`:

```python
"""Analyst commands — da analyst."""

import json
import logging
from pathlib import Path
from typing import Optional

import typer

logger = logging.getLogger(__name__)

analyst_app = typer.Typer(help="Analyst workspace — setup, connect to a remote instance")

AGNES_IDENTIFIER = "AI Data Analyst"


@analyst_app.command("setup")
def setup(
    server_url: str = typer.Option(None, "--server-url", "-s", help="Agnes instance URL"),
    force: bool = typer.Option(False, "--force", help="Re-run from scratch, clean partial state"),
):
    """Set up a local analyst workspace connected to a remote Agnes instance."""
    workspace = Path.cwd()

    # Step 1: Detect existing project
    if not force:
        claude_md = workspace / "CLAUDE.md"
        if claude_md.exists() and AGNES_IDENTIFIER in claude_md.read_text():
            typer.echo("Project already set up. Use 'da sync' to refresh data, or --force to re-setup.")
            return

    # Step 2: Connect
    if not server_url:
        server_url = typer.prompt("Agnes instance URL (e.g., https://data.acme.com)")

    token = _connect_to_instance(server_url)

    # Step 3: Create workspace
    _create_workspace(workspace)

    # Step 4: Download schema and metrics
    _download_metadata(workspace, server_url, token)

    # Step 5: Download data
    table_count = _download_data(workspace, server_url, token)

    # Step 6: Initialize DuckDB
    row_count = _initialize_duckdb(workspace)

    # Step 7: Generate CLAUDE.md
    instance_name = _get_instance_name(server_url, token)
    _generate_claude_md(
        workspace=workspace,
        instance_name=instance_name,
        server_url=server_url,
        sync_interval="15 minutes",
    )

    # Step 8: Verify
    typer.echo(f"\nSetup complete. {table_count} tables, {row_count} total rows.")
    typer.echo("Start analyzing with Claude Code, or run 'da sync' to refresh data.")


def _connect_to_instance(server_url: str) -> str:
    """Connect to Agnes instance, authenticate, return JWT token."""
    import httpx

    # Health check
    try:
        resp = httpx.get(f"{server_url}/api/health", timeout=10)
        resp.raise_for_status()
    except Exception as e:
        typer.echo(f"Cannot reach {server_url}: {e}", err=True)
        raise typer.Exit(1)

    # Authenticate
    email = typer.prompt("Email")
    password = typer.prompt("Password", hide_input=True)

    try:
        resp = httpx.post(
            f"{server_url}/auth/token",
            data={"username": email, "password": password},
            timeout=10,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            typer.echo("Authentication failed: no token in response", err=True)
            raise typer.Exit(1)
    except httpx.HTTPStatusError as e:
        typer.echo(f"Authentication failed: {e.response.text}", err=True)
        raise typer.Exit(1)

    # Save credentials
    from cli.config import save_config
    save_config({"server_url": server_url, "token": token})
    typer.echo(f"Connected to {server_url}")
    return token


def _create_workspace(workspace: Path) -> None:
    """Create analyst directory structure."""
    dirs = [
        workspace / "data" / "parquet",
        workspace / "data" / "duckdb",
        workspace / "data" / "metadata",
        workspace / "user" / "artifacts",
        workspace / "user" / "sessions",
        workspace / ".claude",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def _download_metadata(workspace: Path, server_url: str, token: str) -> None:
    """Download table list and metrics to local cache."""
    import httpx

    headers = {"Authorization": f"Bearer {token}"}
    metadata_dir = workspace / "data" / "metadata"

    # Tables
    try:
        resp = httpx.get(f"{server_url}/api/catalog/tables", headers=headers, timeout=30)
        resp.raise_for_status()
        (metadata_dir / "tables.json").write_text(json.dumps(resp.json(), indent=2))
        typer.echo(f"Downloaded table catalog ({resp.json().get('count', '?')} tables)")
    except Exception as e:
        typer.echo(f"Warning: could not download table catalog: {e}", err=True)

    # Metrics
    try:
        resp = httpx.get(f"{server_url}/api/metrics", headers=headers, timeout=30)
        resp.raise_for_status()
        (metadata_dir / "metrics.json").write_text(json.dumps(resp.json(), indent=2))
        typer.echo(f"Downloaded metrics ({resp.json().get('count', '?')} metrics)")
    except Exception as e:
        typer.echo(f"Warning: could not download metrics: {e}", err=True)


def _download_data(workspace: Path, server_url: str, token: str) -> int:
    """Download parquet files for all accessible tables. Returns count."""
    import httpx

    metadata_dir = workspace / "data" / "metadata"
    parquet_dir = workspace / "data" / "parquet"

    tables_file = metadata_dir / "tables.json"
    if not tables_file.exists():
        return 0

    tables_data = json.loads(tables_file.read_text())
    tables = tables_data.get("tables", [])
    count = 0

    for table in tables:
        tid = table["id"]
        target = parquet_dir / f"{tid}.parquet"

        # Resume: skip if already downloaded
        if target.exists() and target.stat().st_size > 0:
            count += 1
            continue

        try:
            with httpx.Client(base_url=server_url, headers={"Authorization": f"Bearer {token}"},
                              timeout=300) as client:
                with client.stream("GET", f"/api/data/{tid}/download") as resp:
                    if resp.status_code == 404:
                        continue
                    resp.raise_for_status()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "wb") as f:
                        for chunk in resp.iter_bytes(65536):
                            f.write(chunk)
            count += 1
            typer.echo(f"  Downloaded {tid}")
        except Exception as e:
            typer.echo(f"  Failed {tid}: {e}", err=True)

    typer.echo(f"Downloaded {count}/{len(tables)} tables")
    return count


def _initialize_duckdb(workspace: Path) -> int:
    """Create local analytics.duckdb with views over downloaded parquets. Returns total rows."""
    import duckdb

    parquet_dir = workspace / "data" / "parquet"
    db_path = workspace / "data" / "duckdb" / "analytics.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(db_path))
    total_rows = 0

    for pq in sorted(parquet_dir.glob("*.parquet")):
        view_name = pq.stem
        try:
            conn.execute(f"CREATE OR REPLACE VIEW \"{view_name}\" AS SELECT * FROM read_parquet('{pq}')")
            row_count = conn.execute(f"SELECT count(*) FROM \"{view_name}\"").fetchone()[0]
            total_rows += row_count
        except Exception as e:
            logger.warning("Could not create view for %s: %s", pq.name, e)

    conn.close()
    typer.echo(f"Initialized DuckDB with {len(list(parquet_dir.glob('*.parquet')))} views")
    return total_rows


def _get_instance_name(server_url: str, token: str) -> str:
    """Get instance name from server, fallback to URL hostname."""
    import httpx
    try:
        resp = httpx.get(f"{server_url}/api/health", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        data = resp.json()
        return data.get("instance_name", server_url.split("//")[-1].split("/")[0])
    except Exception:
        return server_url.split("//")[-1].split("/")[0]


def _generate_claude_md(
    workspace: Path,
    instance_name: str,
    server_url: str,
    sync_interval: str,
) -> None:
    """Generate CLAUDE.md from template."""
    template_path = Path(__file__).parent.parent.parent / "config" / "claude_md_template.txt"
    if template_path.exists():
        template = template_path.read_text()
    else:
        # Inline fallback
        template = "# {instance_name} — AI Data Analyst\n\nConnected to {server_url}.\n"

    content = template.replace("{instance_name}", instance_name)
    content = content.replace("{server_url}", server_url)
    content = content.replace("{sync_interval}", sync_interval)

    (workspace / "CLAUDE.md").write_text(content)

    # Create CLAUDE.local.md if it doesn't exist
    local_md = workspace / ".claude" / "CLAUDE.local.md"
    local_md.parent.mkdir(parents=True, exist_ok=True)
    if not local_md.exists():
        local_md.write_text("# Personal Notes\n\nAdd your learnings and insights here.\n")
```

Register in `cli/main.py`:

```python
from cli.commands.analyst import analyst_app
# ...
app.add_typer(analyst_app, name="analyst")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_analyst_bootstrap.py -v && pytest tests/test_cli.py::TestCLIHelp::test_analyst_help -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add cli/commands/analyst.py cli/main.py config/claude_md_template.txt tests/test_analyst_bootstrap.py tests/test_cli.py
git commit -m "feat: add da analyst setup command with bootstrap flow"
```

---

### Task 3: Returning-Session Detection

**Files:**
- Modify: `cli/commands/analyst.py` (add `da analyst status` command)
- Test: `tests/test_analyst_bootstrap.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_analyst_bootstrap.py`:

```python
import time

class TestReturningSession:
    def test_stale_data_warning(self, tmp_workspace):
        from cli.commands.analyst import _check_data_freshness
        metadata_dir = tmp_workspace / "data" / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        # Write last_sync.json with old timestamp
        import json
        from datetime import datetime, timezone, timedelta
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        (metadata_dir / "last_sync.json").write_text(json.dumps({"last_sync": old_time}))
        result = _check_data_freshness(tmp_workspace)
        assert result == "stale"

    def test_fresh_data_ok(self, tmp_workspace):
        from cli.commands.analyst import _check_data_freshness
        metadata_dir = tmp_workspace / "data" / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        import json
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        (metadata_dir / "last_sync.json").write_text(json.dumps({"last_sync": now}))
        result = _check_data_freshness(tmp_workspace)
        assert result == "fresh"

    def test_no_data_returns_missing(self, tmp_workspace):
        from cli.commands.analyst import _check_data_freshness
        result = _check_data_freshness(tmp_workspace)
        assert result == "missing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_analyst_bootstrap.py::TestReturningSession -v`
Expected: FAIL — `_check_data_freshness` not found

- [ ] **Step 3: Implement freshness check and status command**

Add to `cli/commands/analyst.py`:

```python
@analyst_app.command("status")
def status():
    """Check workspace status and data freshness."""
    workspace = Path.cwd()

    claude_md = workspace / "CLAUDE.md"
    if not claude_md.exists() or AGNES_IDENTIFIER not in claude_md.read_text():
        typer.echo("No analyst workspace detected. Run 'da analyst setup' first.")
        raise typer.Exit(1)

    freshness = _check_data_freshness(workspace)
    if freshness == "stale":
        typer.echo("Data is stale (>24h old). Run 'da sync' to refresh.")
    elif freshness == "missing":
        typer.echo("No data found. Run 'da analyst setup' to download data.")
    else:
        typer.echo("Data is fresh.")


def _check_data_freshness(workspace: Path) -> str:
    """Check data freshness. Returns 'fresh', 'stale', or 'missing'."""
    last_sync_file = workspace / "data" / "metadata" / "last_sync.json"
    if not last_sync_file.exists():
        return "missing"

    try:
        data = json.loads(last_sync_file.read_text())
        last_sync_str = data.get("last_sync")
        if not last_sync_str:
            return "missing"

        from datetime import datetime, timezone, timedelta
        last_sync = datetime.fromisoformat(last_sync_str)
        if last_sync.tzinfo is None:
            last_sync = last_sync.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - last_sync
        if age > timedelta(hours=24):
            return "stale"
        return "fresh"
    except Exception:
        return "missing"
```

Also update `_download_metadata` to write `last_sync.json`:

At the end of the `_download_metadata` function, add:

```python
    # Record sync timestamp
    from datetime import datetime, timezone
    sync_record = {"last_sync": datetime.now(timezone.utc).isoformat(), "server_url": server_url}
    (metadata_dir / "last_sync.json").write_text(json.dumps(sync_record))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_analyst_bootstrap.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add cli/commands/analyst.py tests/test_analyst_bootstrap.py
git commit -m "feat: add da analyst status and returning-session freshness check"
```

---

### Task 4: Final Integration

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --timeout=60`
Expected: ALL PASS

- [ ] **Step 2: Commit if any fixes needed**

```bash
git add -A
git commit -m "fix: address analyst bootstrap integration issues"
```
