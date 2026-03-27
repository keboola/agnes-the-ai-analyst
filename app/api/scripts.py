"""Script management and execution endpoints."""

import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

import duckdb

from app.auth.dependencies import get_current_user, require_role, Role, _get_db
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
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = ScriptRepository(conn)
    scripts = repo.list_all()
    return {"scripts": scripts, "count": len(scripts)}


@router.post("/deploy", status_code=201)
async def deploy_script(
    request: DeployScriptRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Deploy a Python script to be run on the server (optionally on schedule)."""
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
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Run a deployed script by ID."""
    repo = ScriptRepository(conn)
    script = repo.get(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    return _execute_script(script["source"], script["name"])


@router.post("/run")
async def run_adhoc_script(
    request: RunScriptRequest,
    user: dict = Depends(get_current_user),
):
    """Run an ad-hoc Python script (not deployed)."""
    if not request.source:
        raise HTTPException(status_code=400, detail="Script source required")
    return _execute_script(request.source, request.name or "adhoc")


@router.delete("/{script_id}", status_code=204)
async def undeploy_script(
    script_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = ScriptRepository(conn)
    if not repo.get(script_id):
        raise HTTPException(status_code=404, detail="Script not found")
    repo.undeploy(script_id)


def _execute_script(source: str, name: str) -> dict:
    """Execute a Python script in a sandboxed subprocess."""
    # Safety checks
    dangerous_imports = ["subprocess", "shutil", "ctypes", "importlib"]
    for imp in dangerous_imports:
        if f"import {imp}" in source or f"from {imp}" in source:
            raise HTTPException(
                status_code=400,
                detail=f"Script contains disallowed import: {imp}",
            )

    data_dir = os.environ.get("DATA_DIR", "./data")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        script_path = f.name

    try:
        result = subprocess.run(
            ["python", script_path],
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            env={
                "PATH": os.environ.get("PATH", ""),
                "DATA_DIR": data_dir,
                "PYTHONPATH": os.getcwd(),
                "HOME": "/tmp",
            },
            cwd=os.getcwd(),
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
