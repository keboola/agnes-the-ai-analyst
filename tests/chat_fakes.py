"""Shared fake helpers for chat tests.

FakeHandle and FakeWS were originally inline in tests/test_chat_manager.py;
they live here so the pause/resume test suite can reuse them without circular
imports. FakeProvider is new: a stateful in-memory SandboxProvider that
mirrors E2B semantics (pause parks the handle; resume returns the same handle
with its memory intact).
"""

from __future__ import annotations

import asyncio
import json


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class FakeHandle:
    def __init__(self) -> None:
        self.pid = 1234
        self.sandbox_id: str = "fake-sbx"
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()
        self._stdin_buf: list[bytes] = []
        self.killed = False

    @property
    def stdin(self):
        outer = self

        class S:
            def write(self, b: bytes) -> None:
                outer._stdin_buf.append(b)

            async def drain(self) -> None:
                return None

        return S()

    @property
    def stdout(self):
        outer = self

        class _OutReader:
            async def readline(self) -> bytes:
                return await outer._lines.get()

        return _OutReader()

    @property
    def stderr(self):
        return self.stdout

    async def wait(self) -> int:
        # block until killed
        while not self.killed:
            await asyncio.sleep(0.01)
        return 137

    async def kill(self, *, grace_sec: float = 5.0) -> None:
        self.killed = True

    # Test helpers
    def emit(self, payload: dict) -> None:
        self._lines.put_nowait((json.dumps(payload) + "\n").encode())

    def emit_eof(self) -> None:
        self._lines.put_nowait(b"")


class FakeProvider:
    """In-memory SandboxProvider: spawn/pause/resume with state retention.

    pause() parks the handle; resume() returns the SAME handle (mirrors E2B
    semantics where the process and its memory survive). Set
    ``fail_resume=True`` to exercise the resume-failure fallback path.
    """

    def __init__(self) -> None:
        self.spawned: list[FakeHandle] = []
        self.paused: dict[str, FakeHandle] = {}
        self.fail_resume = False
        self.keepalive_calls: list[int] = []
        self.destroyed: list[str] = []

    async def spawn(self, *, workdir, env, argv) -> FakeHandle:
        h = FakeHandle()
        h.sandbox_id = f"fake-sbx-{len(self.spawned)}"
        self.spawned.append(h)
        return h

    async def pause(self, handle) -> None:
        self.paused[handle.sandbox_id] = handle

    async def resume(self, *, sandbox_id, runner_pid, env) -> FakeHandle:
        if self.fail_resume or sandbox_id not in self.paused:
            raise RuntimeError(f"sandbox {sandbox_id} gone")
        return self.paused.pop(sandbox_id)

    async def keepalive(self, handle, *, timeout_seconds) -> None:
        self.keepalive_calls.append(timeout_seconds)

    async def destroy(self, *, sandbox_id) -> None:
        self.paused.pop(sandbox_id, None)
        self.destroyed.append(sandbox_id)
        # Test-fidelity fix: a real destroy() (E2B AsyncSandbox.kill) kills
        # the underlying VM and its process, so ANY handle still bound to
        # this sandbox_id — paused OR actively running elsewhere (the
        # cross-gateway takeover race: gateway B destroys gateway A's still
        # ACTIVE sandbox by id) — must have its wait() unblock with a
        # crash-like exit, exactly as a real destroyed process would.
        # Previously this method only recorded the call and never touched
        # the handle, so a destroy() racing another owner's in-flight
        # wait() was silently swallowed in tests: the "owning" gateway's
        # crash-respawn path never fired, masking the split-brain race this
        # fake exists to exercise. Search ALL spawned handles (not just
        # `self.paused`) since the handle being destroyed here may still be
        # the ACTIVE one another simulated gateway is holding.
        for h in self.spawned:
            if h.sandbox_id == sandbox_id:
                h.killed = True
