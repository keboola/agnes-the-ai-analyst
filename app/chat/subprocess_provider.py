"""Default SandboxProvider — asyncio.subprocess + nsjail (Linux) / unjailed (Darwin dev)."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_ENV_ALLOWLIST = {
    "AGNES_TOKEN", "AGNES_API", "AGNES_WORKDIR", "AGNES_SESSION_ID",
    "AGNES_USER_EMAIL", "AGNES_DAILY_BUDGET_USD", "AGNES_PER_TOOL_CALL_SECONDS",
    "PATH", "HOME", "TERM", "LANG", "PYTHONUNBUFFERED",
}


def _scrub_env(env: dict[str, str]) -> dict[str, str]:
    # Filters a dict to only allowlisted keys.
    # Called with dict(os.environ) to strip host secrets (tokens, credentials,
    # SA keys) from the subprocess environment.  The *caller-supplied* env dict
    # (from ChatManager._spawn_runner) is layered on top via .update() after
    # this call and is fully trusted — it carries only Agnes session vars that
    # the caller constructed explicitly.  Do NOT pass os.environ as the caller
    # env; if you do, host secrets will bypass the scrub.
    return {k: v for k, v in env.items() if k in _ENV_ALLOWLIST}


@dataclass
class SubprocessHandle:
    pid: int
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    _proc: asyncio.subprocess.Process

    async def wait(self) -> int:
        return await self._proc.wait()

    async def kill(self, *, grace_sec: float = 5.0) -> None:
        if self._proc.returncode is not None:
            return
        try:
            self._proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=grace_sec)
        except asyncio.TimeoutError:
            try:
                self._proc.kill()
            except ProcessLookupError:
                return


class SubprocessProvider:
    def __init__(
        self,
        *,
        nsjail_path: Optional[str] = None,
        nsjail_config_template: Optional[Path] = None,
        require_isolation: bool = True,
        host_uid: Optional[int] = None,
    ) -> None:
        self._nsjail_path = nsjail_path
        self._nsjail_config_template = nsjail_config_template
        self._require_isolation = require_isolation
        self._host_uid = host_uid

    async def spawn(
        self, *, workdir: Path, env: dict[str, str], argv: list[str],
    ) -> SubprocessHandle:
        # Start from a scrubbed slice of the host environment, then layer the
        # caller-supplied env on top.  The allowlist prevents host secrets
        # (tokens, credentials) from leaking into the sandbox; the caller dict
        # is fully trusted — it carries Agnes-side session vars.
        scrubbed = _scrub_env(dict(os.environ))
        scrubbed.update(env)
        scrubbed.setdefault("AGNES_WORKDIR", str(workdir))

        if self._is_jailed():
            command = self._wrap_nsjail(workdir, argv)
        else:
            if self._require_isolation and sys.platform != "darwin":
                raise RuntimeError("isolation required: nsjail unavailable")
            if sys.platform != "darwin":
                logger.warning("unjailed subprocess provider — DEV ONLY")
            command = argv

        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir),
            env=scrubbed,
        )
        assert proc.stdin and proc.stdout and proc.stderr
        return SubprocessHandle(
            pid=proc.pid, stdin=proc.stdin, stdout=proc.stdout,
            stderr=proc.stderr, _proc=proc,
        )

    def _is_jailed(self) -> bool:
        return bool(
            self._nsjail_path
            and self._nsjail_config_template
            and self._nsjail_config_template.exists()
            and sys.platform != "darwin"
        )

    def _wrap_nsjail(self, workdir: Path, argv: list[str]) -> list[str]:
        # Render the nsjail config template with per-session paths.
        # Real implementation: see Task 4.2.
        rendered = self._render_nsjail_cfg(workdir)
        return [self._nsjail_path, "--config", str(rendered), "--", *argv]

    def _render_nsjail_cfg(self, workdir: Path) -> Path:
        # Stub — implemented in Task 4.2.
        raise NotImplementedError("nsjail rendering — Task 4.2")
