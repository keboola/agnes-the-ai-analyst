"""Script management and execution endpoints."""

import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, field_validator
from typing import Optional

import duckdb

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.db import get_system_db
from src.repositories.notifications import ScriptRepository
from src.scheduler import is_valid_schedule, is_table_due

router = APIRouter(prefix="/api/scripts", tags=["scripts"])

SCRIPT_TIMEOUT = int(os.environ.get("SCRIPT_TIMEOUT", "300"))  # 5 min default
SCRIPT_MAX_OUTPUT = int(os.environ.get("SCRIPT_MAX_OUTPUT", "65536"))  # 64KB


class DeployScriptRequest(BaseModel):
    name: str
    source: str
    schedule: Optional[str] = None

    @field_validator("schedule", mode="before")
    @classmethod
    def _validate_schedule(cls, v):
        if v in (None, ""):
            return None
        # Pure-whitespace strings ("   ") fall through to is_valid_schedule
        # and reject — same convention as RegisterTableRequest.sync_schedule.
        # We do NOT silently normalise whitespace to None; surfacing the
        # caller's mistake at register time beats persisting an unusable value.
        if not is_valid_schedule(v):
            raise ValueError(
                f"schedule must be 'every Nm' / 'every Nh' / "
                f"'daily HH:MM[,HH:MM,...]', got {v!r}"
            )
        return v


class RunScriptRequest(BaseModel):
    name: Optional[str] = None
    source: Optional[str] = None


class ScriptResponse(BaseModel):
    id: str
    name: str
    schedule: Optional[str]
    owner: Optional[str]


@router.get("")
async def list_scripts(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List deployed scripts. Admin-only."""
    repo = ScriptRepository(conn)
    scripts = repo.list_all()
    return {"scripts": scripts, "count": len(scripts)}


@router.post("/deploy", status_code=201)
async def deploy_script(
    request: DeployScriptRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Deploy a Python script to be run on the server (optionally on schedule). Admin-only."""
    repo = ScriptRepository(conn)
    script_id = str(uuid.uuid4())
    repo.deploy(
        id=script_id,
        name=request.name,
        owner=user["id"],
        schedule=request.schedule,
        source=request.source,
    )
    return ScriptResponse(
        id=script_id, name=request.name,
        schedule=request.schedule, owner=user["id"],
    )


@router.post("/{script_id}/run")
async def run_deployed_script(
    script_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Run a deployed script by ID. Admin-only."""
    repo = ScriptRepository(conn)
    script = repo.get(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    return _execute_script(script["source"], script["name"])


@router.post("/run")
async def run_adhoc_script(
    request: RunScriptRequest,
    user: dict = Depends(require_admin),
):
    """Run an ad-hoc Python script (not deployed). Admin-only."""
    if not request.source:
        raise HTTPException(status_code=400, detail="Script source required")
    return _execute_script(request.source, request.name or "adhoc")


@router.delete("/{script_id}", status_code=204)
async def undeploy_script(
    script_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = ScriptRepository(conn)
    if not repo.get(script_id):
        raise HTTPException(status_code=404, detail="Script not found")
    repo.undeploy(script_id)


@router.post("/run-due")
async def run_due_scripts(
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Run every deployed script whose ``schedule`` says it is due.

    Iterates ``script_registry``, skips rows without a schedule (those run
    only via explicit POST /{id}/run), evaluates ``is_table_due(schedule,
    last_run)``, and atomically claims each due row via
    ``ScriptRepository.claim_for_run``. Execution is queued as a
    ``BackgroundTask`` so the response returns immediately — the sidecar
    must not block waiting on a long-running script.

    Concurrency: ``claim_for_run`` flips ``last_status`` to ``'running'``
    inside the same UPDATE; a script already in that state is skipped on
    subsequent ticks until the BackgroundTask writes a terminal status via
    ``record_run_result``. There is no max-runtime detection in this PR —
    if a BackgroundTask crashes without writing a terminal status, the
    script stays stuck in ``'running'`` until an operator clears it
    manually (``UPDATE script_registry SET last_status = NULL WHERE id =
    ?``). Documenting this as an accepted v0 limitation; revisit if it
    bites in practice.
    """
    repo = ScriptRepository(conn)
    claimed: list[str] = []
    for script in repo.list_all():
        schedule = script.get("schedule")
        if not schedule:
            continue
        last_run = script.get("last_run")
        last_run_iso = last_run.isoformat() if last_run else None
        if not is_table_due(schedule, last_run_iso):
            continue
        if not repo.claim_for_run(script["id"]):
            # Lost the race / already running — next tick will retry.
            continue
        claimed.append(script["id"])
        background_tasks.add_task(
            _run_claimed_script,
            script_id=script["id"],
            source=script["source"],
            name=script["name"],
        )
    return {"claimed": claimed, "count": len(claimed)}


def _run_claimed_script(script_id: str, source: str, name: str) -> None:
    """Execute a previously-claimed script and write the terminal status.

    Runs in a FastAPI BackgroundTask, so it owns its own DB connection
    (the request-scoped conn is already gone by the time this fires).
    Any exception writes 'failure' and re-raises so the BG handler still
    surfaces the traceback in logs.
    """
    # Fresh connection for the background task — the request-scoped conn
    # was returned to FastAPI by the time this fires.
    bg_conn = get_system_db()
    try:
        bg_repo = ScriptRepository(bg_conn)
        try:
            _execute_script(source, name)
            bg_repo.record_run_result(script_id, status="success")
        except Exception:
            bg_repo.record_run_result(script_id, status="failure")
            raise
    finally:
        bg_conn.close()


def _execute_script(source: str, name: str) -> dict:
    """Execute a Python script in a sandboxed subprocess.

    The blocklist below is defense-in-depth, not a primary trust boundary.
    The role gate on the route (admin-only) is the actual boundary; the
    blocklist catches obvious mistakes, not a hostile admin."""
    # Comprehensive safety checks — block dangerous patterns
    blocked_patterns = [
        # Direct imports of dangerous modules
        "import subprocess", "from subprocess",
        "import shutil", "from shutil",
        "import ctypes", "from ctypes",
        "import importlib", "from importlib",
        "import socket", "from socket",
        "import requests", "from requests",
        "import httpx", "from httpx",
        "import urllib", "from urllib",
        "import http", "from http",
        # Dynamic import bypasses
        "__import__",
        "importlib",
        # Code execution bypasses
        "exec(",
        "eval(",
        "compile(",
        # OS-level access
        "import os", "from os",
        "import sys", "from sys",
        "import signal", "from signal",
        # File access bypasses
        "open(",
        "pathlib",
        # Dangerous builtins
        "globals()",
        "locals()",
        "getattr(",
        "setattr(",
        "delattr(",
        "breakpoint(",
        # Introspection-chain dunders that can pivot to RCE.
        # `__init__`/`__getattribute__` deliberately omitted: substring
        # match would flag every `def __init__(self):`.
        "__subclasses__",
        "__globals__",
        "__class__",
        "__base__",
        "__bases__",
        "__mro__",
        "__dict__",
        "__code__",
        "__builtins__",
    ]
    import ast

    BLOCKED_MODULES = {"os", "sys", "subprocess", "shutil", "ctypes", "importlib", "socket",
                       "requests", "httpx", "urllib", "http", "signal", "pathlib", "builtins"}
    BLOCKED_FUNCTIONS = {"exec", "eval", "compile", "open", "globals", "locals",
                         "getattr", "setattr", "delattr", "breakpoint", "__import__",
                         "vars"}

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise HTTPException(status_code=400, detail=f"Script syntax error: {e}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BLOCKED_MODULES:
                    raise HTTPException(status_code=400, detail=f"Blocked import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in BLOCKED_MODULES:
                raise HTTPException(status_code=400, detail=f"Blocked import: {node.module}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_FUNCTIONS:
                raise HTTPException(status_code=400, detail=f"Blocked function: {node.func.id}")

    source_lower = source.lower()
    for pattern in blocked_patterns:
        if pattern.lower() in source_lower:
            raise HTTPException(
                status_code=400,
                detail=f"Script contains disallowed pattern: {pattern.split('(')[0].strip()}",
            )

    data_dir = os.environ.get("DATA_DIR", "./data")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        script_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            env={
                "PATH": "/usr/bin:/usr/local/bin",
                "DATA_DIR": data_dir,
                "HOME": "/tmp",
                # Deliberately exclude VIRTUAL_ENV and PYTHONPATH
                # to prevent access to installed packages
            },
            cwd="/tmp",  # restrict working directory
        )
        stdout = result.stdout[:SCRIPT_MAX_OUTPUT]
        stderr = result.stderr[:SCRIPT_MAX_OUTPUT]
        return {
            "name": name,
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": len(result.stdout) > SCRIPT_MAX_OUTPUT or len(result.stderr) > SCRIPT_MAX_OUTPUT,
        }
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Script timed out after {SCRIPT_TIMEOUT}s",
            "truncated": False,
        }
    finally:
        os.unlink(script_path)
