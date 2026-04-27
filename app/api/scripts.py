"""Script management and execution endpoints."""

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

import duckdb

from app.auth.dependencies import get_current_user, require_role, _get_db
from src.rbac import Role
from src.repositories.notifications import ScriptRepository

router = APIRouter(prefix="/api/scripts", tags=["scripts"])

SCRIPT_TIMEOUT = int(os.environ.get("SCRIPT_TIMEOUT", "300"))  # 5 min default
SCRIPT_MAX_OUTPUT = int(os.environ.get("SCRIPT_MAX_OUTPUT", "65536"))  # 64KB


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
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List deployed scripts. Admin-only — issue #44.

    `ScriptRepository.list_all()` returns the full row including the
    script source code; non-admin callers had been able to read
    admin-deployed scripts via this endpoint. The whole Script API is
    admin-only now (deploy + run + list + undeploy)."""
    repo = ScriptRepository(conn)
    scripts = repo.list_all()
    return {"scripts": scripts, "count": len(scripts)}


@router.post("/deploy", status_code=201)
async def deploy_script(
    request: DeployScriptRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Deploy a Python script to be run on the server (optionally on schedule).

    Admin-only — issue #44. The previous gate (analyst) created a planted-script
    attack: an analyst could deploy a malicious script and wait for an admin to
    run it. The whole Script API is admin-only; analysts who need scripted
    workflows should ask an admin to deploy on their behalf or use the
    read-only data API."""
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
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Run a deployed script by ID. Admin-only — the AST/string sandbox is
    defense-in-depth, not a primary trust boundary (issue #44)."""
    repo = ScriptRepository(conn)
    script = repo.get(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    return _execute_script(script["source"], script["name"])


@router.post("/run")
async def run_adhoc_script(
    request: RunScriptRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
):
    """Run an ad-hoc Python script (not deployed). Admin-only — see /{script_id}/run."""
    if not request.source:
        raise HTTPException(status_code=400, detail="Script source required")
    return _execute_script(request.source, request.name or "adhoc")


@router.delete("/{script_id}", status_code=204)
async def undeploy_script(
    script_id: str,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = ScriptRepository(conn)
    if not repo.get(script_id):
        raise HTTPException(status_code=404, detail="Script not found")
    repo.undeploy(script_id)


def _execute_script(source: str, name: str) -> dict:
    """Execute a Python script in a sandboxed subprocess.

    The blocklist below is **defense-in-depth**, not a primary trust
    boundary — a determined caller with admin rights can bypass it through
    introspection chains we cannot enumerate. The role gate on the route
    (admin-only, see issue #44) is the actual boundary. The blocklist is
    here to catch obvious mistakes, not to stop a hostile admin."""
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
        # Introspection chains that pivot to RCE — issue #44 PoC uses
        # ().__class__.__base__.__subclasses__() to find subprocess.Popen.
        # `__init__`/`__getattribute__` are deliberately NOT here: substring
        # match would flag every `def __init__(self):`. The chain is broken
        # at the next link (`__subclasses__`, `__globals__`, etc.) anyway.
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
                         # Introspection helper that exposes module dicts and frame
                         # locals — common pivot for sandbox escape (issue #44).
                         # `type(x)` and `dir(x)` are intentionally NOT here:
                         # they're frequent in legitimate admin scripts and the
                         # dunder-pattern blocklist below already catches the
                         # follow-on attribute access (`__class__`, `__mro__`).
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
