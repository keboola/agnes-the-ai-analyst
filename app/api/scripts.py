"""Script management and execution endpoints."""

import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

import duckdb

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories.audit import AuditRepository
from src.repositories.notifications import ScriptRepository

router = APIRouter(prefix="/api/scripts", tags=["scripts"])

SCRIPT_TIMEOUT = int(os.environ.get("SCRIPT_TIMEOUT", "300"))  # 5 min default
SCRIPT_MAX_OUTPUT = int(os.environ.get("SCRIPT_MAX_OUTPUT", "65536"))  # 64KB


# ---------------------------------------------------------------------------
# Audit helper — same shape as app/api/users.py::_audit / marketplaces.py
# ---------------------------------------------------------------------------
# Server-side Python execution is the highest-blast-radius action in the
# product (admin-only sandboxed subprocess). Every deploy / run / delete
# writes a row to ``audit_log`` so an operator can answer "who ran what,
# when, and against which script ID" without diffing logs.


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    target_id: str,
    params: Optional[dict] = None,
) -> None:
    try:
        safe_params = None
        if params:
            safe_params = {}
            for k, v in params.items():
                if isinstance(v, datetime):
                    safe_params[k] = v.isoformat()
                else:
                    safe_params[k] = v
        AuditRepository(conn).log(
            user_id=actor_id,
            action=action,
            resource=f"script:{target_id}",
            params=safe_params,
        )
    except Exception:
        # Audit must not break the user-facing operation. The caller still
        # observes the success/failure of the actual mutation.
        pass


class DeployScriptRequest(BaseModel):
    name: str
    source: str
    schedule: Optional[str] = None


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
    _audit(
        conn, user["id"], "script.deploy", script_id,
        {"name": request.name, "schedule": request.schedule,
         "source_bytes": len(request.source or "")},
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
    result = _execute_script(script["source"], script["name"])
    _audit(
        conn, user["id"], "script.run", script_id,
        {"name": script["name"], "exit_code": result.get("exit_code"),
         "truncated": result.get("truncated", False)},
    )
    return result


@router.post("/run")
async def run_adhoc_script(
    request: RunScriptRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Run an ad-hoc Python script (not deployed). Admin-only."""
    if not request.source:
        raise HTTPException(status_code=400, detail="Script source required")
    name = request.name or "adhoc"
    result = _execute_script(request.source, name)
    _audit(
        conn, user["id"], "script.run_adhoc", "adhoc",
        {"name": name, "source_bytes": len(request.source),
         "exit_code": result.get("exit_code")},
    )
    return result


@router.delete("/{script_id}", status_code=204)
async def undeploy_script(
    script_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = ScriptRepository(conn)
    script = repo.get(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    repo.undeploy(script_id)
    _audit(
        conn, user["id"], "script.delete", script_id,
        {"name": script.get("name")},
    )


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
