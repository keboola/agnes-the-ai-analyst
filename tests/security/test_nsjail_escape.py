"""nsjail escape-attempt smoke tests.

All tests skip when nsjail is not installed or the platform is darwin.
These are integration tests that require a Linux host with nsjail in PATH.

Uses asyncio.run() per the project convention (no pytest-asyncio required).
See tests/test_chat_subprocess_provider.py for precedent.
"""
import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from app.chat.subprocess_provider import SubprocessProvider


def _skip_unless_nsjail():
    if shutil.which("nsjail") is None or sys.platform == "darwin":
        pytest.skip("nsjail not installed or darwin")


def test_cannot_read_outside_workdir(tmp_path: Path):
    _skip_unless_nsjail()

    async def _run():
        prov = SubprocessProvider(
            nsjail_path=shutil.which("nsjail"),
            nsjail_config_template=Path("config/nsjail/chat-session.cfg.template"),
            require_isolation=True,
        )
        secret = tmp_path.parent / "host-secret"
        secret.write_text("forbidden")
        handle = await prov.spawn(
            workdir=tmp_path, env={},
            argv=["/usr/bin/python3", "-c",
                  f"open('{secret}').read()"],
        )
        rc = await handle.wait()
        assert rc != 0  # blocked

    asyncio.run(_run())


def test_cannot_curl_external(tmp_path: Path):
    _skip_unless_nsjail()

    async def _run():
        prov = SubprocessProvider(
            nsjail_path=shutil.which("nsjail"),
            nsjail_config_template=Path("config/nsjail/chat-session.cfg.template"),
            require_isolation=True,
        )
        handle = await prov.spawn(
            workdir=tmp_path, env={"PATH": "/usr/bin"},
            argv=["/usr/bin/curl", "--max-time", "2", "https://www.google.com"],
        )
        rc = await handle.wait()
        assert rc != 0  # blocked by iptables OWNER allowlist

    asyncio.run(_run())


def test_fork_bomb_capped(tmp_path: Path):
    _skip_unless_nsjail()

    async def _run():
        prov = SubprocessProvider(
            nsjail_path=shutil.which("nsjail"),
            nsjail_config_template=Path("config/nsjail/chat-session.cfg.template"),
            require_isolation=True,
        )
        handle = await prov.spawn(
            workdir=tmp_path, env={},
            argv=["/bin/sh", "-c", ":(){ :|:& };:"],
        )
        rc = await asyncio.wait_for(handle.wait(), timeout=10)
        assert rc != 0

    asyncio.run(_run())
