# Metadata Writer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add column metadata management — discover basetypes/descriptions, store in DuckDB, push back to Keboola Storage API.

**Architecture:** `column_metadata` table (created in schema v4 by the metrics plan). New `ColumnMetadataRepository` following `table_registry.py` pattern. CLI subcommands under `da admin metadata`. API endpoints under `/api/admin/metadata/`. Keboola push uses Storage API v2.

**Tech Stack:** DuckDB, FastAPI, Typer, httpx (for Keboola API push), PyArrow (for schema introspection)

**Spec:** `docs/superpowers/specs/2026-04-10-porting-internal-features-design.md` — Section 3

**Depends on:** Business Metrics plan (Task 1 — schema v4 creates `column_metadata` table)

---

### Task 1: ColumnMetadataRepository

**Files:**
- Create: `src/repositories/column_metadata.py`
- Test: `tests/test_column_metadata.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_column_metadata.py`:

```python
"""Tests for ColumnMetadataRepository."""

import os
import json
from pathlib import Path

import pytest
import duckdb


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db
    conn = get_system_db()
    yield conn
    conn.close()


class TestColumnMetadataCreate:
    def test_save_single_column(self, db_conn):
        from src.repositories.column_metadata import ColumnMetadataRepository
        repo = ColumnMetadataRepository(db_conn)
        repo.save("orders", "total_amount", basetype="NUMERIC", description="Order total in USD")
        result = repo.get("orders", "total_amount")
        assert result is not None
        assert result["basetype"] == "NUMERIC"
        assert result["description"] == "Order total in USD"
        assert result["confidence"] == "manual"

    def test_upsert_overwrites(self, db_conn):
        from src.repositories.column_metadata import ColumnMetadataRepository
        repo = ColumnMetadataRepository(db_conn)
        repo.save("orders", "total_amount", basetype="NUMERIC", description="v1")
        repo.save("orders", "total_amount", basetype="FLOAT", description="v2")
        result = repo.get("orders", "total_amount")
        assert result["basetype"] == "FLOAT"
        assert result["description"] == "v2"


class TestColumnMetadataRead:
    def test_list_for_table(self, db_conn):
        from src.repositories.column_metadata import ColumnMetadataRepository
        repo = ColumnMetadataRepository(db_conn)
        repo.save("orders", "id", basetype="STRING")
        repo.save("orders", "total", basetype="NUMERIC")
        repo.save("users", "email", basetype="STRING")
        results = repo.list_for_table("orders")
        assert len(results) == 2
        names = {r["column_name"] for r in results}
        assert names == {"id", "total"}

    def test_get_missing(self, db_conn):
        from src.repositories.column_metadata import ColumnMetadataRepository
        repo = ColumnMetadataRepository(db_conn)
        assert repo.get("x", "y") is None


class TestColumnMetadataDelete:
    def test_delete_column(self, db_conn):
        from src.repositories.column_metadata import ColumnMetadataRepository
        repo = ColumnMetadataRepository(db_conn)
        repo.save("orders", "total", basetype="NUMERIC")
        assert repo.delete("orders", "total") is True
        assert repo.get("orders", "total") is None

    def test_delete_missing(self, db_conn):
        from src.repositories.column_metadata import ColumnMetadataRepository
        repo = ColumnMetadataRepository(db_conn)
        assert repo.delete("x", "y") is False


class TestColumnMetadataProposal:
    def test_import_proposal(self, db_conn, tmp_path):
        from src.repositories.column_metadata import ColumnMetadataRepository
        repo = ColumnMetadataRepository(db_conn)

        proposal = {
            "project": {"name": "sales"},
            "generated_at": "2026-04-10T12:00:00",
            "tables": {
                "orders": {
                    "columns": {
                        "id": {"basetype": "STRING", "description": "Order ID", "confidence": "high"},
                        "total": {"basetype": "NUMERIC", "description": "Total amount", "confidence": "medium"},
                    }
                }
            },
        }
        proposal_path = tmp_path / "proposal.json"
        proposal_path.write_text(json.dumps(proposal))

        count = repo.import_proposal(proposal_path)
        assert count == 2
        assert repo.get("orders", "id")["basetype"] == "STRING"
        assert repo.get("orders", "total")["confidence"] == "medium"

    def test_import_proposal_sets_source(self, db_conn, tmp_path):
        from src.repositories.column_metadata import ColumnMetadataRepository
        repo = ColumnMetadataRepository(db_conn)

        proposal = {
            "tables": {
                "orders": {
                    "columns": {
                        "id": {"basetype": "STRING", "description": "test", "confidence": "high"},
                    }
                }
            },
        }
        (tmp_path / "p.json").write_text(json.dumps(proposal))
        repo.import_proposal(tmp_path / "p.json")
        assert repo.get("orders", "id")["source"] == "ai_enrichment"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_column_metadata.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement ColumnMetadataRepository**

Create `src/repositories/column_metadata.py`:

```python
"""Repository for column metadata (descriptions, basetypes)."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

logger = logging.getLogger(__name__)


class ColumnMetadataRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def save(self, table_id: str, column_name: str,
             basetype: Optional[str] = None,
             description: Optional[str] = None,
             confidence: str = "manual",
             source: str = "manual") -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO column_metadata (table_id, column_name, basetype, description, confidence, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (table_id, column_name) DO UPDATE SET
                basetype = excluded.basetype,
                description = excluded.description,
                confidence = excluded.confidence,
                source = excluded.source,
                updated_at = excluded.updated_at""",
            [table_id, column_name, basetype, description, confidence, source, now],
        )
        return self.get(table_id, column_name)

    def get(self, table_id: str, column_name: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM column_metadata WHERE table_id = ? AND column_name = ?",
            [table_id, column_name],
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def list_for_table(self, table_id: str) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM column_metadata WHERE table_id = ? ORDER BY column_name",
            [table_id],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def delete(self, table_id: str, column_name: str) -> bool:
        existing = self.get(table_id, column_name)
        if not existing:
            return False
        self.conn.execute(
            "DELETE FROM column_metadata WHERE table_id = ? AND column_name = ?",
            [table_id, column_name],
        )
        return True

    def import_proposal(self, proposal_path) -> int:
        """Import a metadata proposal JSON file. Returns count of columns imported."""
        path = Path(proposal_path)
        data = json.loads(path.read_text())
        count = 0

        tables = data.get("tables", {})
        for table_id, table_data in tables.items():
            columns = table_data.get("columns", {})
            for col_name, col_data in columns.items():
                self.save(
                    table_id=table_id,
                    column_name=col_name,
                    basetype=col_data.get("basetype"),
                    description=col_data.get("description"),
                    confidence=col_data.get("confidence", "medium"),
                    source="ai_enrichment",
                )
                count += 1

        return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_column_metadata.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/repositories/column_metadata.py tests/test_column_metadata.py
git commit -m "feat: add ColumnMetadataRepository with CRUD and proposal import"
```

---

### Task 2: CLI Subcommands `da admin metadata`

**Files:**
- Modify: `cli/commands/admin.py` (add metadata subcommands)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_cli.py` in `TestCLIHelp`:

```python
    def test_admin_metadata_help(self):
        result = runner.invoke(app, ["admin", "metadata-show", "--help"])
        assert result.exit_code == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py::TestCLIHelp::test_admin_metadata_help -v`
Expected: FAIL — `No such command 'metadata-show'`

- [ ] **Step 3: Add metadata commands to admin.py**

Add to `cli/commands/admin.py`:

```python
@admin_app.command("metadata-show")
def metadata_show(
    table_id: str = typer.Argument(..., help="Table ID"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Show column metadata for a table."""
    resp = api_get(f"/api/admin/metadata/{table_id}")
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    columns = resp.json().get("columns", [])
    if as_json:
        typer.echo(json.dumps(columns, indent=2))
    else:
        if not columns:
            typer.echo(f"No metadata for table '{table_id}'")
            return
        typer.echo(f"\n  Metadata for {table_id}:")
        for c in columns:
            desc = c.get("description", "-")
            typer.echo(f"    {c['column_name']:30s} {c.get('basetype', '?'):12s} {desc}")


@admin_app.command("metadata-apply")
def metadata_apply(
    proposal_path: str = typer.Argument(..., help="Path to proposal JSON file"),
    push_to_source: bool = typer.Option(False, "--push-to-source", help="Push to Keboola Storage API"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show changes without applying"),
):
    """Apply a metadata proposal (JSON) to DuckDB and optionally push to source."""
    from pathlib import Path

    path = Path(proposal_path)
    if not path.exists():
        typer.echo(f"File not found: {proposal_path}", err=True)
        raise typer.Exit(1)

    import json as json_mod
    data = json_mod.loads(path.read_text())
    tables = data.get("tables", {})

    if dry_run:
        for table_id, td in tables.items():
            for col, cd in td.get("columns", {}).items():
                typer.echo(f"  {table_id}.{col}: {cd.get('basetype', '?')} — {cd.get('description', '-')}")
        typer.echo(f"\nDry run: {sum(len(td.get('columns', {})) for td in tables.values())} columns would be applied")
        return

    from src.db import get_system_db
    from src.repositories.column_metadata import ColumnMetadataRepository

    conn = get_system_db()
    try:
        repo = ColumnMetadataRepository(conn)
        count = repo.import_proposal(path)
        typer.echo(f"Applied {count} column metadata entries to DuckDB")
    finally:
        conn.close()

    if push_to_source:
        resp = api_post(f"/api/admin/metadata/push", json={"proposal_path": str(path)})
        if resp.status_code == 200:
            typer.echo("Pushed metadata to source system")
        else:
            typer.echo(f"Push failed: {resp.json().get('detail', resp.text)}", err=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py::TestCLIHelp::test_admin_metadata_help -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cli/commands/admin.py tests/test_cli.py
git commit -m "feat: add da admin metadata-show and metadata-apply commands"
```

---

### Task 3: API Endpoints

**Files:**
- Create: `app/api/metadata.py`
- Modify: `app/main.py` (register router)
- Test: `tests/test_api.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_api.py`:

```python
class TestMetadataAPI:
    def test_get_metadata_empty(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.get("/api/admin/metadata/orders",
                          headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        assert resp.json()["columns"] == []

    def test_save_and_get_metadata(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/admin/metadata/orders",
            json={"columns": [
                {"column_name": "id", "basetype": "STRING", "description": "Order ID"},
                {"column_name": "total", "basetype": "NUMERIC", "description": "Total amount"},
            ]},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

        resp = client.get("/api/admin/metadata/orders",
                          headers={"Authorization": f"Bearer {admin_token}"})
        assert len(resp.json()["columns"]) == 2

    def test_analyst_cannot_save_metadata(self, seeded_client):
        client, _, analyst_token = seeded_client
        resp = client.post(
            "/api/admin/metadata/orders",
            json={"columns": [{"column_name": "id", "basetype": "STRING"}]},
            headers={"Authorization": f"Bearer {analyst_token}"},
        )
        assert resp.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py::TestMetadataAPI -v`
Expected: FAIL — 404 on `/api/admin/metadata/orders`

- [ ] **Step 3: Implement API router**

Create `app/api/metadata.py`:

```python
"""Column metadata API endpoints."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.dependencies import get_current_user, require_admin, _get_db
from src.repositories.column_metadata import ColumnMetadataRepository

router = APIRouter(tags=["metadata"])


class ColumnMetadataItem(BaseModel):
    column_name: str
    basetype: Optional[str] = None
    description: Optional[str] = None
    confidence: str = "manual"


class ColumnMetadataSave(BaseModel):
    columns: List[ColumnMetadataItem]


@router.get("/api/admin/metadata/{table_id}")
async def get_table_metadata(
    table_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = ColumnMetadataRepository(conn)
    columns = repo.list_for_table(table_id)
    return {"table_id": table_id, "columns": columns}


@router.post("/api/admin/metadata/{table_id}")
async def save_table_metadata(
    table_id: str,
    body: ColumnMetadataSave,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = ColumnMetadataRepository(conn)
    for col in body.columns:
        repo.save(
            table_id=table_id,
            column_name=col.column_name,
            basetype=col.basetype,
            description=col.description,
            confidence=col.confidence,
            source="api",
        )
    return {"status": "ok", "table_id": table_id, "count": len(body.columns)}


@router.post("/api/admin/metadata/{table_id}/push")
async def push_metadata_to_source(
    table_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Push column metadata to the source system (Keboola only)."""
    from src.repositories.table_registry import TableRegistryRepository
    table_repo = TableRegistryRepository(conn)
    table = table_repo.get(table_id)

    if not table:
        raise HTTPException(status_code=404, detail=f"Table not found: {table_id}")
    if table.get("source_type") != "keboola":
        raise HTTPException(status_code=400, detail="Push only supported for Keboola source tables")

    meta_repo = ColumnMetadataRepository(conn)
    columns = meta_repo.list_for_table(table_id)
    if not columns:
        raise HTTPException(status_code=400, detail="No metadata to push")

    # Build Keboola API payload
    import os
    import httpx

    stack_url = os.environ.get("KBC_STACK_URL", "")
    token = os.environ.get("KBC_STORAGE_TOKEN", "")
    if not stack_url or not token:
        raise HTTPException(status_code=400, detail="KBC_STACK_URL and KBC_STORAGE_TOKEN must be set")

    source_table = table.get("source_table", table_id)
    columns_metadata = {}
    for col in columns:
        entries = []
        if col.get("basetype"):
            entries.append({"key": "KBC.datatype.basetype", "value": col["basetype"]})
        if col.get("description"):
            entries.append({"key": "KBC.description", "value": col["description"]})
        if entries:
            columns_metadata[col["column_name"]] = entries

    try:
        resp = httpx.post(
            f"{stack_url}/v2/storage/tables/{source_table}/metadata",
            headers={"X-StorageApi-Token": token},
            json={"provider": "ai-metadata-enrichment", "columnsMetadata": columns_metadata},
            timeout=30,
        )
        resp.raise_for_status()
        return {"status": "pushed", "table_id": table_id, "columns": len(columns_metadata)}
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Keboola API error: {e.response.text}")
```

Register in `app/main.py`:

```python
from app.api.metadata import router as metadata_router
# ... (add near other router imports)

# In create_app(), add before web_router:
app.include_router(metadata_router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api.py::TestMetadataAPI -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/metadata.py app/main.py tests/test_api.py
git commit -m "feat: add column metadata API with Keboola push support"
```

---

### Task 4: Final Integration

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --timeout=60`
Expected: ALL PASS

- [ ] **Step 2: Commit if any fixes needed**

```bash
git add -A
git commit -m "fix: address metadata writer integration issues"
```
