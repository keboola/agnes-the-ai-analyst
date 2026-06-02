"""SandboxProvider Protocol — runtime extension point for sandbox engines."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class SandboxHandle(Protocol):
    pid: int
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
