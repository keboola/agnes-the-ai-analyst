"""E2B smoke test — real sandbox spawn, opt-in via AGNES_E2E_E2B=1.

Burns a real E2B sandbox minute per run. Operator opt-in via
``AGNES_E2E_E2B=1`` + a real ``E2B_API_KEY`` + a built template id in
``E2B_TEMPLATE_ID`` (defaults to ``agnes-chat``). Skips otherwise.

This is the canary that the E2B SDK + our template + our provider all
work end-to-end. The unit tests in ``tests/test_chat_e2b_provider.py``
exercise the provider against a mocked SDK; this one exercises the
real SDK and asserts the same protocol.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest


def _skip_unless_e2b_smoke():
    if not os.environ.get("AGNES_E2E_E2B"):
        pytest.skip("AGNES_E2E_E2B=1 not set — skipping real E2B smoke")
    if not os.environ.get("E2B_API_KEY"):
        pytest.skip("E2B_API_KEY not set — required for real-sandbox smoke")


def test_e2b_spawn_runs_echo_command(tmp_path: Path) -> None:
    """Spawn a real sandbox, run /bin/echo, read stdout back via the handle."""
    _skip_unless_e2b_smoke()

    from app.chat.e2b_provider import E2BProvider

    template = os.environ.get("E2B_TEMPLATE_ID", "agnes-chat")
    api_key = os.environ["E2B_API_KEY"]

    async def _run():
        prov = E2BProvider(
            api_key=api_key,
            template_id=template,
            sandbox_timeout_seconds=120,
            upload_runner=False,  # echo only — no runner needed
        )
        handle = await prov.spawn(
            workdir=tmp_path,
            env={"AGNES_E2E_PROBE": "hello"},
            argv=["/bin/echo", "smoke", "ok"],
        )
        try:
            line = await asyncio.wait_for(handle.stdout.readline(), timeout=30)
            assert b"smoke" in line and b"ok" in line, (
                f"unexpected stdout from echo: {line!r}"
            )
            rc = await asyncio.wait_for(handle.wait(), timeout=30)
            assert rc == 0, f"echo exited rc={rc}"
        finally:
            await handle.kill(grace_sec=2.0)

    asyncio.run(_run())


def test_e2b_files_write_then_read(tmp_path: Path) -> None:
    """Upload a small workspace; read one of the files back via files.read."""
    _skip_unless_e2b_smoke()

    from e2b import AsyncSandbox

    from app.chat.e2b_workspace_sync import upload_workspace
    from app.chat.e2b_provider import SANDBOX_WORKDIR

    template = os.environ.get("E2B_TEMPLATE_ID", "agnes-chat")
    api_key = os.environ["E2B_API_KEY"]

    async def _run():
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "marker.txt").write_text("smoke-content")

        sandbox = await AsyncSandbox.create(
            template=template,
            api_key=api_key,
            timeout=120,
            allow_internet_access=True,
        )
        try:
            sent = await upload_workspace(sandbox, ws, max_bytes=1024 * 1024)
            assert sent > 0
            data = await sandbox.files.read(f"{SANDBOX_WORKDIR}/marker.txt")
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            assert "smoke-content" in data
        finally:
            await sandbox.kill()

    asyncio.run(_run())
