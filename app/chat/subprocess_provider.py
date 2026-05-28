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


# Env vars passed through `_scrub_env` from the host into the sandbox.
# ANTHROPIC_API_KEY is the LLM-side credential the runner uses to talk to
# Anthropic; without it on this list the real-agent path silently fails on
# its first API call. All other entries are Agnes-side session vars or
# benign locale/PATH defaults; host secrets (DB creds, cloud SA keys) are
# deliberately excluded so they never leak into a sandboxed subprocess.
_ENV_ALLOWLIST = {
    "AGNES_TOKEN", "AGNES_API", "AGNES_WORKDIR", "AGNES_SESSION_ID",
    "AGNES_USER_EMAIL", "AGNES_DAILY_BUDGET_USD", "AGNES_PER_TOOL_CALL_SECONDS",
    "AGNES_TOOL_CALLS_PER_TURN",
    "ANTHROPIC_API_KEY",
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
        assert self._nsjail_config_template is not None
        marketplace_dir = os.environ.get("AGNES_MARKETPLACES_DIR", "/data/marketplaces")
        host_uid = self._host_uid if self._host_uid is not None else os.getuid()
        host_gid = os.getgid()
        template = self._nsjail_config_template.read_text(encoding="utf-8")
        rendered_text = (
            template
            .replace("{{WORKDIR}}", str(workdir))
            .replace("{{MARKETPLACE_DIR}}", marketplace_dir)
            .replace("{{HOST_UID}}", str(host_uid))
            .replace("{{HOST_GID}}", str(host_gid))
        )
        out_path = workdir / ".nsjail.cfg"
        out_path.write_text(rendered_text, encoding="utf-8")
        # Write allowed-egress list for the runner's startup log.
        (workdir / ".allowed-egress.txt").write_text(
            "127.0.0.1\napi.anthropic.com:443\napi.github.com:443\n",
            encoding="utf-8",
        )
        return out_path
