"""FastAPI route serving the per-user bare repo over git smart-HTTP.

Registered at `/marketplace.git/{path:path}` (GET + POST). Claude Code
registers the URL:

    /plugin marketplace add https://x:<PAT>@host/marketplace.git/

git CLI does not speak Bearer tokens — it only sends HTTP Basic. By
convention (same as GitHub PATs) the username is ignored and the password
field carries the bearer token. We extract it, validate via the shared
`resolve_token_to_user`, resolve the caller's filtered bare repo via
`git_backend.ensure_repo_for_user`, then hand the request off to the real
`git http-backend` CLI binary, run as an OS subprocess speaking the CGI
protocol (the same mechanism nginx+fcgiwrap / Apache mod_cgi use to serve
git smart-HTTP).

This used to be served by dulwich's pure-Python `HTTPGitApplication` bridged
into ASGI via `a2wsgi.WSGIMiddleware`. Even though the WSGI call was offloaded
to a thread pool, it stayed inside the single uvicorn OS process — and
dulwich's smart-HTTP pack generation is CPU-heavy pure-Python work that holds
the GIL for seconds at a time, starving the asyncio event loop (health checks,
every other request) even though it looked "off-thread". Running the real
`git http-backend` binary as a genuine subprocess releases the GIL completely
during pack generation; the parent process only awaits async subprocess I/O.

Repo *building* (`git_backend.ensure_repo_for_user`) is untouched — that part
is fast, ETag-cached, and ends before the DB connection closes. Only the
*serving* step changed.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from app.auth.pat_resolver import resolve_token_to_user
from app.marketplace_server import git_backend
from src.db import get_system_db

logger = logging.getLogger(__name__)

router = APIRouter()

_GIT_HTTP_BACKEND = ("git", "http-backend")


def token_from_basic_auth(auth_header: Optional[str]) -> Optional[str]:
    """Extract the password (= PAT in our scheme) from an HTTP Basic header.

    Username is discarded; git CLI typically sends `x`, `x-access-token`,
    `git`, etc. Returns None for missing / malformed / non-Basic headers.
    """
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(parts[1], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    _, _, password = decoded.partition(":")
    return password or None


def _unauthorized() -> Response:
    return Response(
        content=b"authentication required\n",
        status_code=401,
        media_type="text/plain; charset=utf-8",
        headers={"WWW-Authenticate": 'Basic realm="agnes-marketplace"'},
    )


def _server_error() -> Response:
    return Response(
        content=b"internal server error\n",
        status_code=500,
        media_type="text/plain; charset=utf-8",
    )


def _build_cgi_env(request: Request, path: str, repo_path: Path, remote_user: Optional[str]) -> dict:
    """Build the CGI/1.1 environment `git http-backend` expects.

    URL translation per `man git-http-backend`: the backend concatenates
    `GIT_PROJECT_ROOT` + `PATH_INFO` to find the repo on disk. Our
    `repo_path` already points at the exact bare repo for this caller (not a
    directory of repos), so `GIT_PROJECT_ROOT=repo_path` + `PATH_INFO=/<path>`
    resolves to `<repo_path>/<path>` — e.g. `<repo_path>/info/refs`.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_HTTP_EXPORT_ALL": "1",
            "GIT_PROJECT_ROOT": str(repo_path),
            "PATH_INFO": f"/{path}",
            "REQUEST_METHOD": request.method,
            "QUERY_STRING": request.url.query or "",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "GATEWAY_INTERFACE": "CGI/1.1",
        }
    )
    content_type = request.headers.get("content-type")
    if content_type:
        env["CONTENT_TYPE"] = content_type
    content_length = request.headers.get("content-length")
    if content_length is not None:
        env["CONTENT_LENGTH"] = content_length
    if remote_user:
        env["REMOTE_USER"] = remote_user
    # Modern git (protocol v2) negotiation — forward the client's declared
    # protocol version if present, same as the Apache/nginx recipes in
    # `man git-http-backend` (SetEnvIf Git-Protocol ... GIT_PROTOCOL=$0).
    git_protocol = request.headers.get("git-protocol")
    if git_protocol:
        env["GIT_PROTOCOL"] = git_protocol
    return env


def _parse_cgi_status(header_block: bytes) -> tuple[int, list[tuple[str, str]]]:
    """Split the CGI response preamble into (status_code, header_pairs).

    `git http-backend` emits a `Status: <code> <reason>` header per the CGI
    convention (not a raw HTTP status line). Absence of `Status:` implies 200
    per the CGI spec.
    """
    status_code = 200
    headers: list[tuple[str, str]] = []
    for line in header_block.split(b"\r\n" if b"\r\n" in header_block else b"\n"):
        line = line.strip(b"\r\n")
        if not line:
            continue
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        name_s = name.strip().decode("latin-1")
        value_s = value.strip().decode("latin-1")
        if name_s.lower() == "status":
            try:
                status_code = int(value_s.split(" ", 1)[0])
            except ValueError:
                status_code = 200
            continue
        headers.append((name_s, value_s))
    return status_code, headers


async def _read_cgi_headers(stdout: asyncio.StreamReader) -> bytes:
    """Read stdout up to (and including) the blank line separating CGI
    headers from the body."""
    header_bytes = b""
    while True:
        line = await stdout.readline()
        if not line:
            break
        header_bytes += line
        if line in (b"\r\n", b"\n"):
            break
    return header_bytes


async def _drain_stderr(stderr: asyncio.StreamReader, chunks: list[bytes]) -> None:
    """Read `stderr` to EOF concurrently with stdout, buffering chunks.

    `git http-backend`'s stdout and stderr are two independent OS pipes with
    their own (~64KB on Linux) kernel buffers. If we only read stderr after
    the process exits (or after stdout hits EOF), a child that writes enough
    to stderr to fill its pipe blocks on that `write()` — and since it's
    blocked, it never produces more stdout either, so a sequential
    stdout-then-stderr reader deadlocks forever. Draining both streams
    concurrently (this coroutine running alongside the stdout read loop)
    avoids that.
    """
    while True:
        chunk = await stderr.read(65536)
        if not chunk:
            break
        chunks.append(chunk)


async def _run_git_http_backend(env: dict, body: bytes) -> tuple[int, list[tuple[str, str]], AsyncIterator[bytes]]:
    """Run `git http-backend` as a subprocess, feed it *body* on stdin, and
    return (status_code, headers, body_stream). The GIL is not held by the
    parent process during pack generation — the heavy lifting happens in a
    real OS child process."""
    proc = await asyncio.create_subprocess_exec(
        *_GIT_HTTP_BACKEND,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    proc.stdin.write(body)
    proc.stdin.close()

    # Start draining stderr immediately, concurrently with everything below —
    # see `_drain_stderr` for why this must not happen sequentially after
    # stdout EOF / process exit.
    stderr_chunks: list[bytes] = []
    stderr_task = asyncio.ensure_future(_drain_stderr(proc.stderr, stderr_chunks))

    header_block = await _read_cgi_headers(proc.stdout)
    status_code, headers = _parse_cgi_status(header_block)

    async def body_stream() -> AsyncIterator[bytes]:
        assert proc.stdout is not None
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            await proc.wait()
            await stderr_task
            if proc.returncode not in (0, None):
                stderr = b"".join(stderr_chunks)
                logger.error(
                    "git http-backend exited %s: %s",
                    proc.returncode,
                    stderr.decode("utf-8", errors="replace"),
                )

    return status_code, headers, body_stream()


async def _marketplace_git(path: str, request: Request):
    token = token_from_basic_auth(request.headers.get("authorization"))
    if not token:
        return _unauthorized()

    # resolve_token_to_user / ensure_repo_for_user route through the
    # repository factory and ignore ``conn``; on Postgres pass None so the
    # system DuckDB is never opened (forbidden invariant).
    from src.repositories import use_pg

    conn = None
    try:
        conn = None if use_pg() else get_system_db()
    except Exception:
        logger.exception("get_system_db() failed")
        return _server_error()

    try:
        # Git channel doesn't need the reason — just auth yes/no.
        user, _reason = resolve_token_to_user(conn, token)
        if not user:
            return _unauthorized()

        try:
            repo_path = git_backend.ensure_repo_for_user(conn, user)
        except Exception:
            logger.exception("Failed to build repo for user %r", user.get("email") or user.get("id"))
            return _server_error()
    finally:
        # DB touchpoints (resolve_token_to_user, ensure_repo_for_user) are
        # both done — close before spawning the subprocess, which only reads
        # the pre-built bare repo directory from disk and never touches
        # DuckDB.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    body = await request.body()
    remote_user = user.get("email") or user.get("id")
    env = _build_cgi_env(request, path, repo_path, remote_user)

    try:
        status_code, headers, stream = await _run_git_http_backend(env, body)
    except FileNotFoundError:
        logger.exception("git http-backend binary not found")
        return _server_error()
    except Exception:
        logger.exception("git http-backend failed for user %r", user.get("id"))
        return _server_error()

    return StreamingResponse(stream, status_code=status_code, headers=dict(headers))


# Registered as two distinct routes (not one `methods=["GET", "POST"]` route)
# so each method gets its own `operation_id` — a single multi-method APIRoute
# shares one `unique_id` across all its methods, which trips FastAPI's
# duplicate-operation-id warning during schema generation.
router.add_api_route(
    "/marketplace.git/{path:path}",
    _marketplace_git,
    methods=["GET"],
    operation_id="marketplace_git_get",
)
router.add_api_route(
    "/marketplace.git/{path:path}",
    _marketplace_git,
    methods=["POST"],
    operation_id="marketplace_git_post",
)
