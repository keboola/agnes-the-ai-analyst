"""SandboxProvider Protocol — runtime extension point for sandbox engines."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class SandboxHandle(Protocol):
    pid: int
    sandbox_id: str  # provider-scoped id used for pause/resume
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader

    async def wait(self) -> int: ...
    async def kill(self, *, grace_sec: float = 5.0) -> None: ...


@runtime_checkable
class SandboxProvider(Protocol):
    async def spawn(
        self,
        *,
        workdir: Path,
        env: dict[str, str],
        argv: list[str],
    ) -> SandboxHandle: ...

    async def pause(self, handle: SandboxHandle) -> None:
        """Snapshot the sandbox (memory + fs + running processes) and detach."""
        ...

    async def resume(
        self,
        *,
        sandbox_id: str,
        runner_pid: int,
        env: dict[str, str],
    ) -> SandboxHandle:
        """Reconnect a paused sandbox and reattach to the still-running runner."""
        ...

    async def keepalive(self, handle: SandboxHandle, *, timeout_seconds: int) -> None:
        """Extend the sandbox's external timeout. No-op for local providers."""
        ...

    async def destroy(self, *, sandbox_id: str) -> None:
        """Delete a paused sandbox without resuming it. Used by the paused-TTL reaper."""
        ...
