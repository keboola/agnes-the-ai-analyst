"""SubprocessProvider — unjailed dev mode tests (Task 4.1) + jailed mode (Task 4.2).

Uses asyncio.run() per the project convention (no pytest-asyncio required).
See test_selective_gzip.py / test_cache_warmup.py for precedent.
"""
import asyncio
import shutil
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


# ---------------------------------------------------------------------------
# Task 4.2 — unit test for _render_nsjail_cfg (platform-agnostic)
# ---------------------------------------------------------------------------

def test_render_nsjail_cfg_substitutes_placeholders(tmp_path: Path):
    """_render_nsjail_cfg must replace all 4 placeholders and write the files."""
    template = Path("config/nsjail/chat-session.cfg.template")
    assert template.exists(), "template file must exist"

    workdir = tmp_path / "session-abc"
    workdir.mkdir()

    prov = SubprocessProvider(
        nsjail_path=None,
        nsjail_config_template=template,
        require_isolation=False,
        host_uid=9999,
    )

    cfg_path = prov._render_nsjail_cfg(workdir)

    # Returns path to the written cfg file
    assert cfg_path == workdir / ".nsjail.cfg"
    assert cfg_path.exists()

    rendered = cfg_path.read_text(encoding="utf-8")

    # All placeholders must be gone
    assert "{{WORKDIR}}" not in rendered
    assert "{{MARKETPLACE_DIR}}" not in rendered
    assert "{{HOST_UID}}" not in rendered
    assert "{{HOST_GID}}" not in rendered

    # Correct values must be present
    assert str(workdir) in rendered
    assert "9999" in rendered  # host_uid override

    # .allowed-egress.txt must be written with the 3 expected lines
    egress_path = workdir / ".allowed-egress.txt"
    assert egress_path.exists()
    egress_lines = egress_path.read_text(encoding="utf-8").splitlines()
    assert "127.0.0.1" in egress_lines
    assert "api.anthropic.com:443" in egress_lines
    assert "api.github.com:443" in egress_lines


# ---------------------------------------------------------------------------
# Task 4.2 — jailed integration tests (skip on darwin / no nsjail)
# ---------------------------------------------------------------------------

def _nsjail_available() -> bool:
    return shutil.which("nsjail") is not None and sys.platform != "darwin"


@pytest.mark.skipif(not _nsjail_available(), reason="nsjail not installed or darwin")
def test_jailed_spawn_runs_python_inside(tmp_path: Path):
    async def _run():
        template = Path("config/nsjail/chat-session.cfg.template")
        assert template.exists()
        prov = SubprocessProvider(
            nsjail_path=shutil.which("nsjail"),
            nsjail_config_template=template,
            require_isolation=True,
        )
        handle = await prov.spawn(
            workdir=tmp_path, env={},
            argv=["/usr/bin/python3", "-c", "print('inside')"],
        )
        out = await handle.stdout.read(200)
        assert b"inside" in out
        rc = await handle.wait()
        assert rc == 0

    asyncio.run(_run())
