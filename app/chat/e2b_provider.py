"""E2B-backed SandboxProvider.

Spawns each chat session inside an ephemeral E2B microVM. The E2B SDK
exposes a *callback-driven* command API — ``commands.run(cmd,
background=True, on_stdout=..., on_stderr=...)`` — and a *string-based*
stdin pipe — ``commands.send_stdin(pid, data)``. The Agnes
``SandboxProvider`` Protocol (provider.py) however expects asyncio
``StreamReader``/``StreamWriter`` objects, mirroring
``asyncio.create_subprocess_exec``'s interface that the original
subprocess provider produced. This module adapts between the two shapes
so the rest of the chat stack (ChatManager._pump_subprocess_to_ws,
runner stdin marshalling) doesn't need to know which provider it's
talking to.

Reality findings against e2b==1.11.1 that drove the design:

- Top-level export is ``e2b.AsyncSandbox`` (sync ``Sandbox`` also exists;
  we use async to stay inside the FastAPI event loop).
- ``AsyncSandbox.create(template, api_key, envs, timeout,
  allow_internet_access)`` — no ``Sandbox(...)`` constructor as the
  draft plan assumed.
- ``commands.run(cmd: str, background: bool, ...)`` — ``cmd`` is one
  shell-string, not an argv list. We join with ``shlex.quote``.
- ``commands.send_stdin(pid, data: str)`` — data is *str*, not bytes.
- ``AsyncCommandHandle`` exposes ``.pid``, ``.kill()``, ``.wait()``
  (where wait returns a CommandResult with ``.exit_code``), but no
  ``.stdout`` StreamReader — output flows through the callbacks
  registered on ``run()``. Hence the queue→reader adapter below.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Imported as a module-level name so unit tests can ``patch("app.chat.
# e2b_provider.AsyncSandbox")``.
from e2b import AsyncSandbox

logger = logging.getLogger(__name__)


# Default workdir inside the sandbox; matches the path Dockerfile creates
# (``RUN mkdir -p /work``) and the path e2b_workspace_sync.upload uses.
SANDBOX_WORKDIR = "/work"


def _coerce_to_bytes(data) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8", errors="replace")
    return str(data).encode("utf-8", errors="replace")


class _StreamReaderAdapter:
    """asyncio.StreamReader-like wrapper around an asyncio.Queue of bytes.

    Only the methods the chat stack actually uses are implemented
    (``readline`` and ``read``). Both honour an EOF sentinel pushed by
    the owning E2BSandboxHandle when the underlying command exits.
    """

    _EOF = object()

    def __init__(self) -> None:
        self._buf = bytearray()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._eof = False

    def feed(self, chunk: bytes) -> None:
        # Called from the E2B SDK callback context. Non-blocking; the
        # queue is unbounded so a slow consumer never blocks the SDK.
        self._queue.put_nowait(chunk)

    def feed_eof(self) -> None:
        self._queue.put_nowait(self._EOF)

    async def _pump(self) -> bool:
        """Consume one queue item; True if got data, False on EOF."""
        item = await self._queue.get()
        if item is self._EOF:
            self._eof = True
            return False
        self._buf.extend(item)
        return True

    async def readline(self) -> bytes:
        while True:
            # Find newline in current buffer.
            idx = self._buf.find(b"\n")
            if idx != -1:
                line = bytes(self._buf[: idx + 1])
                del self._buf[: idx + 1]
                return line
            if self._eof:
                if self._buf:
                    line = bytes(self._buf)
                    self._buf.clear()
                    return line
                return b""
            ok = await self._pump()
            if not ok and not self._buf:
                return b""

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            # Drain until EOF
            while not self._eof:
                await self._pump()
            data = bytes(self._buf)
            self._buf.clear()
            return data
        while len(self._buf) < n and not self._eof:
            await self._pump()
        take = min(n, len(self._buf))
        data = bytes(self._buf[:take])
        del self._buf[:take]
        return data


class _StreamWriterAdapter:
    """asyncio.StreamWriter-like adapter that pushes into
    ``sandbox.commands.send_stdin(pid, str)``.

    Writes are buffered locally; ``drain()`` flushes via ``send_stdin``.
    """

    def __init__(self, sandbox, pid: int) -> None:
        self._sandbox = sandbox
        self._pid = pid
        self._buf = bytearray()
        self._closed = False

    def write(self, data) -> None:
        if self._closed:
            raise RuntimeError("stdin closed")
        self._buf.extend(_coerce_to_bytes(data))

    async def drain(self) -> None:
        if not self._buf:
            return
        # send_stdin takes str on the SDK side.
        text = bytes(self._buf).decode("utf-8", errors="replace")
        self._buf.clear()
        await self._sandbox.commands.send_stdin(self._pid, text)

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        return None


@dataclass
class E2BSandboxHandle:
    """SandboxHandle adapter wrapping a single E2B background command."""

    pid: int
    stdin: _StreamWriterAdapter
    stdout: _StreamReaderAdapter
    stderr: _StreamReaderAdapter
    _sandbox: Any  # e2b.AsyncSandbox
    _cmd_handle: Any  # e2b.AsyncCommandHandle

    async def wait(self) -> int:
        try:
            result = await self._cmd_handle.wait()
        except Exception:
            logger.exception("e2b command wait() failed; treating as crash exit")
            return -1
        exit_code = getattr(result, "exit_code", None)
        if exit_code is None:
            return 0
        return int(exit_code)

    async def kill(self, *, grace_sec: float = 5.0) -> None:
        # 1) Ask E2B to kill the in-sandbox process. The command handle's
        #    kill() returns bool but we don't gate on it — we always
        #    follow up by tearing down the whole sandbox so a single
        #    runner crash never leaves a billable VM lingering.
        try:
            await asyncio.wait_for(self._cmd_handle.kill(), timeout=grace_sec)
        except (asyncio.TimeoutError, Exception):
            logger.warning("e2b command handle kill timed out; tearing down sandbox")
        # 2) Mark stdout/stderr EOF so any consumer awaiting readline()
        #    unblocks.
        try:
            self.stdout.feed_eof()
            self.stderr.feed_eof()
        except Exception:
            pass
        # 3) Kill the whole sandbox. v1 spawns a fresh sandbox per
        #    session — there's no reuse path that would want to keep it
        #    alive after the runner exits.
        try:
            await self._sandbox.kill()
        except Exception:
            logger.exception("sandbox.kill() failed")


class E2BProvider:
    """SandboxProvider implementation backed by E2B microVMs.

    Constructor params:

    api_key:
        E2B account API key. Empty → spawn() raises (mirrors the
        ANTHROPIC_API_KEY gate in app/main.py).
    template_id:
        The E2B template tag to use. ``agnes-chat`` (Q2 — single mutable
        ``:latest`` tag) is the default but operators set it via
        ``chat.e2b_template_id``.
    sandbox_timeout_seconds:
        Hard ceiling on each sandbox's lifetime. Defaults to 30 min;
        the in-process idle reaper kills sessions earlier when activity
        is quiet.
    upload_runner:
        When True the provider uploads ``app/chat/runner.py`` to
        ``/work/runner.py`` after sandbox create. Defaults True — the
        runner module isn't baked into the template per Q2 trade-off.
    """

    syncs_workspace: bool = False  # the workspace-sync layer is the caller's job

    def __init__(
        self,
        *,
        api_key: str,
        template_id: str,
        sandbox_timeout_seconds: int = 30 * 60,
        upload_runner: bool = True,
    ) -> None:
        self._api_key = api_key
        self._template_id = template_id
        self._timeout = sandbox_timeout_seconds
        self._upload_runner = upload_runner

    async def spawn(
        self,
        *,
        workdir: Path,
        env: dict[str, str],
        argv: list[str],
    ) -> E2BSandboxHandle:
        if not self._api_key:
            raise RuntimeError(
                "E2B_API_KEY missing — refusing to spawn chat sandbox",
            )
        if not self._template_id:
            raise RuntimeError(
                "chat.e2b_template_id missing — refusing to spawn chat sandbox",
            )

        # Per Q4: allow_internet_access=True. Egress allowlist lives only
        # in the PreToolUse hook bundled with the workspace template.
        sandbox = await AsyncSandbox.create(
            template=self._template_id,
            api_key=self._api_key,
            envs=dict(env),
            timeout=self._timeout,
            allow_internet_access=True,
        )

        # Provider-side runner upload. The runner module isn't baked into
        # the template (Q2 — keeps template builds out of the runner-iter
        # loop), so we ship it as text alongside sandbox spawn.
        if self._upload_runner:
            try:
                runner_src = (
                    Path(__file__)
                    .with_name("runner.py")
                    .read_text(
                        encoding="utf-8",
                    )
                )
                await sandbox.files.write(f"{SANDBOX_WORKDIR}/runner.py", runner_src)
            except Exception:
                logger.exception(
                    "failed to upload runner.py into E2B sandbox; "
                    "the in-sandbox process will fall back to whatever is at the argv path",
                )

        # Adapters for the SDK's callback-driven output.
        stdout_adapter = _StreamReaderAdapter()
        stderr_adapter = _StreamReaderAdapter()

        def _on_stdout(chunk) -> None:
            stdout_adapter.feed(_coerce_to_bytes(chunk))

        def _on_stderr(chunk) -> None:
            stderr_adapter.feed(_coerce_to_bytes(chunk))

        # Join argv into a single shell command. The SDK only accepts a
        # str (it runs the command through ``/bin/sh -c`` inside the
        # sandbox), so we shell-quote each element so paths with spaces
        # survive untouched.
        cmd = " ".join(shlex.quote(a) for a in argv)

        # ``timeout=0`` disables the SDK's per-command lifetime cap (default
        # 60 s). The runner runs until we explicitly kill it (idle reaper,
        # WS disconnect, max_session_seconds) or the sandbox itself is
        # torn down. Without this, a long-lived chat session would crash
        # after 60 s with ``TimeoutException: context deadline exceeded``
        # and the manager would respawn-loop until rate-limited.
        cmd_handle = await sandbox.commands.run(
            cmd,
            background=True,
            # ``stdin=True`` keeps the process's stdin OPEN so the runner can
            # receive user messages via ``commands.send_stdin(pid, ...)``. E2B
            # SDK 2.x gates an interactive stdin behind this flag — without it
            # the process gets EOF on stdin and exits immediately, and every
            # ``send_stdin`` then fails with "stdin not enabled or closed",
            # so no chat message (web OR Slack) ever reaches the agent.
            stdin=True,
            cwd=SANDBOX_WORKDIR,
            user="user",
            envs=dict(env),
            on_stdout=_on_stdout,
            on_stderr=_on_stderr,
            timeout=0,
        )

        stdin_adapter = _StreamWriterAdapter(sandbox, cmd_handle.pid)

        return E2BSandboxHandle(
            pid=cmd_handle.pid,
            stdin=stdin_adapter,
            stdout=stdout_adapter,
            stderr=stderr_adapter,
            _sandbox=sandbox,
            _cmd_handle=cmd_handle,
        )
