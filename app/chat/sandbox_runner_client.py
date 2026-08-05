"""Gateway-side client for the apps-runner sidecar's ``/sandboxes/*`` API.

The chat gateway never touches ``/var/run/docker.sock`` — every Docker
operation a chat sandbox needs rides this HTTP client to the sidecar (see
``services/apps_runner/sandbox_api.py`` for the server half and the
socket-confinement rationale).

Deliberately NOT a reuse of ``src/data_apps/runner_client.py``: that client is
synchronous (data apps are driven from request handlers), while the chat
provider lives on the FastAPI event loop and needs a long-lived streaming
attach. Both clients read the same ``APPS_RUNNER_URL`` / ``APPS_RUNNER_TOKEN``
env pair — one sidecar, one credential — and chat must not import from
``src.data_apps``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Ordinary control calls (up / pause / files / …). Generous because `up` pulls
#: nothing but does create + start a container.
DEFAULT_TIMEOUT_SECONDS = 60.0

#: Connect timeout for the attach stream. Its *read* timeout is deliberately
#: infinite: a quiet chat session emits nothing for minutes at a time and a read
#: deadline would tear the attach down mid-session.
STREAM_CONNECT_TIMEOUT_SECONDS = 15.0


class SandboxRunnerUnavailable(RuntimeError):
    """The sidecar could not be reached at all (transport-level failure)."""


class SandboxRunnerError(RuntimeError):
    """The sidecar answered with an error status (4xx/5xx)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"sandbox runner {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class SandboxAttachStream:
    """A live attach to one sandbox: an async iterator of
    ``(stream_name, payload_bytes)`` pairs, closable on pause/kill.

    Owns its own ``httpx.AsyncClient`` because the connection outlives any
    single request — closing the stream closes the client.
    """

    def __init__(self, client: httpx.AsyncClient, ctx: Any, response: httpx.Response) -> None:
        self._client = client
        self._ctx = ctx
        self._response = response
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[tuple[str, bytes]]:
        async for line in self._response.aiter_lines():
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                # A truncated frame is not worth killing the session over.
                logger.debug("sandbox stream: dropping non-JSON line")
                continue
            data = frame.get("data") or ""
            try:
                payload = base64.b64decode(data)
            except (ValueError, TypeError):
                continue
            yield str(frame.get("stream") or "stdout"), payload

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._ctx.__aexit__(None, None, None)
        except Exception:
            logger.debug("sandbox stream close failed", exc_info=True)
        try:
            await self._client.aclose()
        except Exception:
            logger.debug("sandbox stream client close failed", exc_info=True)


class SandboxRunnerClient:
    """Async client for the sidecar's ``/sandboxes/*`` routes."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = (base_url or os.environ.get("APPS_RUNNER_URL", "http://apps-runner:8600")).rstrip("/")
        self.token = token if token is not None else os.environ.get("APPS_RUNNER_TOKEN", "")
        self._transport = transport
        self._timeout = timeout

    # --- plumbing ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"X-Runner-Token": self.token}

    async def _request(self, method: str, path: str, **kw: Any) -> dict:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
                r = await client.request(method, f"{self.base_url}{path}", headers=self._headers(), **kw)
        except httpx.TransportError as exc:
            raise SandboxRunnerUnavailable(str(exc)) from exc
        if r.status_code >= 400:
            raise SandboxRunnerError(r.status_code, _detail(r))
        return r.json()

    # --- lifecycle --------------------------------------------------------

    async def up(self, name: str, spec: dict) -> dict:
        return await self._request("POST", f"/sandboxes/{name}/up", json={"spec": spec})

    async def pause(self, name: str) -> dict:
        return await self._request("POST", f"/sandboxes/{name}/pause")

    async def resume(self, name: str) -> dict:
        return await self._request("POST", f"/sandboxes/{name}/resume")

    async def rm(self, name: str, *, grace_sec: float = 0.0) -> dict:
        """Remove the sandbox. ``grace_sec > 0`` asks the daemon for a
        SIGTERM-then-SIGKILL stop first (session teardown); the paused-TTL
        reaper passes 0 and force-removes immediately."""
        return await self._request("POST", f"/sandboxes/{name}/rm", json={"grace_sec": grace_sec})

    async def status(self, name: str) -> dict:
        return await self._request("GET", f"/sandboxes/{name}/status")

    async def list_sandboxes(self) -> list[dict]:
        return list((await self._request("GET", "/sandboxes")).get("sandboxes") or [])

    async def probe(self, image: str = "") -> dict:
        return await self._request("GET", "/sandboxes/probe", params={"image": image})

    # --- files ------------------------------------------------------------

    async def write_file(self, name: str, path: str, data: bytes | str) -> dict:
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        return await self._request(
            "POST",
            f"/sandboxes/{name}/files",
            json={"path": path, "content_b64": base64.b64encode(raw).decode()},
        )

    async def read_file(self, name: str, path: str) -> bytes:
        """One file's raw bytes. Not ``_request``: the sidecar streams the
        body as ``application/octet-stream`` (kept out of *its* memory — it
        runs under a small cgroup limit), so this reads ``content`` off a
        plain GET instead of decoding a base64 JSON envelope."""
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
                r = await client.get(
                    f"{self.base_url}/sandboxes/{name}/files",
                    headers=self._headers(),
                    params={"path": path, "op": "read"},
                )
        except httpx.TransportError as exc:
            raise SandboxRunnerUnavailable(str(exc)) from exc
        if r.status_code >= 400:
            raise SandboxRunnerError(r.status_code, _detail(r))
        return r.content

    async def list_files(self, name: str, path: str) -> list[dict]:
        body = await self._request("GET", f"/sandboxes/{name}/files", params={"path": path, "op": "list"})
        return list(body.get("entries") or [])

    # --- streams ----------------------------------------------------------

    async def send_stdin(self, name: str, data: bytes) -> dict:
        return await self._request(
            "POST",
            f"/sandboxes/{name}/stdin",
            json={"data_b64": base64.b64encode(bytes(data)).decode()},
        )

    async def open_stream(self, name: str, *, replay: bool = False) -> SandboxAttachStream:
        """Attach to the sandbox and return the live NDJSON frame stream.

        ``replay=True`` asks Docker to re-send what the container already wrote
        — correct for the first attach after create (nothing has consumed that
        output yet), wrong for a post-resume reattach.
        """
        client = httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(
                connect=STREAM_CONNECT_TIMEOUT_SECONDS,
                read=None,
                write=self._timeout,
                pool=STREAM_CONNECT_TIMEOUT_SECONDS,
            ),
        )
        ctx = client.stream(
            "GET",
            f"{self.base_url}/sandboxes/{name}/stream",
            headers=self._headers(),
            params={"replay": "true" if replay else "false"},
        )
        try:
            response = await ctx.__aenter__()
        except httpx.TransportError as exc:
            await client.aclose()
            raise SandboxRunnerUnavailable(str(exc)) from exc
        except Exception:
            await client.aclose()
            raise
        if response.status_code >= 400:
            await response.aread()
            detail = _detail(response)
            status = response.status_code
            await ctx.__aexit__(None, None, None)
            await client.aclose()
            raise SandboxRunnerError(status, detail)
        return SandboxAttachStream(client, ctx, response)


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text))
    except Exception:  # noqa: BLE001 — non-JSON error body
        return response.text
