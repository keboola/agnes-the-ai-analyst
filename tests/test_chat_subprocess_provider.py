"""SubprocessProvider — unjailed dev mode tests (Task 4.1).

Uses asyncio.run() per the project convention (no pytest-asyncio required).
See test_selective_gzip.py / test_cache_warmup.py for precedent.
"""
import asyncio
import sys
from pathlib import Path

import pytest

from app.chat.subprocess_provider import SubprocessProvider


def test_spawn_runs_echo(tmp_path: Path):
    async def _run():
        prov = SubprocessProvider(nsjail_path=None, require_isolation=False)
        handle = await prov.spawn(
            workdir=tmp_path,
            env={"FOO": "bar"},
            argv=[sys.executable, "-c", "import os, sys; sys.stdout.write(os.environ['FOO']); sys.stdout.flush()"],
        )
        out = await handle.stdout.read(100)
        assert b"bar" in out
        rc = await handle.wait()
        assert rc == 0

    asyncio.run(_run())


def test_require_isolation_refuses_unjailed_on_linux(tmp_path: Path):
    if sys.platform == "darwin":
        pytest.skip("darwin always unjailed in dev")

    async def _run():
        prov = SubprocessProvider(nsjail_path=None, require_isolation=True)
        with pytest.raises(RuntimeError, match="isolation required"):
            await prov.spawn(workdir=tmp_path, env={}, argv=[sys.executable, "-c", "pass"])

    asyncio.run(_run())


def test_kill_sends_sigterm_then_sigkill(tmp_path: Path):
    async def _run():
        prov = SubprocessProvider(nsjail_path=None, require_isolation=False)
        handle = await prov.spawn(
            workdir=tmp_path, env={},
            argv=[sys.executable, "-c", "import time; time.sleep(60)"],
        )
        await handle.kill(grace_sec=0.1)
        rc = await handle.wait()
        assert rc != 0

    asyncio.run(_run())
