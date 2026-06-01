"""Runner JSON-line protocol tests (Task 6.1).

Uses asyncio.run() per the project convention (no pytest-asyncio required).
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

# Project root as seen from this worktree — needed so the subprocess can
# import app.chat.runner when the editable install points at a different
# checkout (e.g. running tests from a git worktree).
_PROJECT_ROOT = str(Path(__file__).parent.parent)


def test_runner_emits_ready_then_echoes_with_fake_agent(tmp_path: Path):
    async def _run():
        env = os.environ.copy()
        # Ensure the worktree's app package is importable inside the subprocess.
        env["PYTHONPATH"] = _PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["AGNES_RUNNER_FAKE_AGENT"] = "1"  # turns off real SDK call
        env["AGNES_SESSION_ID"] = "chat_test"
        env["AGNES_USER_EMAIL"] = "u@x"
        env["AGNES_API"] = "http://127.0.0.1:8000"
        env["AGNES_TOKEN"] = "fake"
        env["AGNES_DAILY_BUDGET_USD"] = "20"
        env["AGNES_PER_TOOL_CALL_SECONDS"] = "90"

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.chat.runner", "--session-id", "chat_test",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=str(tmp_path),
        )
        assert proc.stdin and proc.stdout

        line = await proc.stdout.readline()
        frame = json.loads(line)
        assert frame == {"type": "runner_ready"}

        proc.stdin.write((json.dumps({"type": "user_msg", "text": "hi"}) + "\n").encode())
        await proc.stdin.drain()

        # Fake-agent mode echoes back as assistant_message
        line = await proc.stdout.readline()
        frame = json.loads(line)
        assert frame["type"] == "assistant_message"
        assert "hi" in frame["content"]

        proc.stdin.close()
        rc = await proc.wait()
        assert rc == 0

    asyncio.run(_run())


def test_tool_call_budget_emits_confirmation_required(tmp_path: Path):
    """When the per-turn tool-call budget is exhausted, the runner emits a
    `confirmation_required` frame instead of continuing.

    Tested via fake-agent: feeding `__many_tools__:N` makes the fake agent
    fire N tool_call frames in a row; the budget gate stops at the configured
    cap.
    """
    async def _run():
        env = os.environ.copy()
        env["PYTHONPATH"] = _PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["AGNES_RUNNER_FAKE_AGENT"] = "1"
        env["AGNES_PER_TOOL_CALL_SECONDS"] = "5"
        env["AGNES_TOOL_CALLS_PER_TURN"] = "2"
        env["AGNES_SESSION_ID"] = "s"
        env["AGNES_USER_EMAIL"] = "u@x"
        env["AGNES_API"] = "http://127.0.0.1:8000"
        env["AGNES_TOKEN"] = "fake"

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.chat.runner", "--session-id", "s",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=str(tmp_path),
        )
        assert proc.stdin and proc.stdout

        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
        assert json.loads(line) == {"type": "runner_ready"}

        proc.stdin.write(
            (json.dumps({"type": "user_msg", "text": "__many_tools__:5"}) + "\n").encode()
        )
        await proc.stdin.drain()

        tool_calls_seen = 0
        saw_budget = False
        for _ in range(20):
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
            frame = json.loads(line)
            if frame.get("type") == "tool_call":
                tool_calls_seen += 1
            if frame.get("type") == "confirmation_required":
                saw_budget = True
                assert frame.get("reason") == "tool_call_budget"
                break

        assert saw_budget, (
            f"expected confirmation_required after tool budget; saw {tool_calls_seen} calls"
        )
        # Budget capped emission at 2.
        assert tool_calls_seen == 2

        proc.stdin.close()
        await proc.wait()

    asyncio.run(_run())


def test_install_agnes_cli_noop_without_wheel(tmp_path: Path, monkeypatch):
    """Empty staging dir → install is a silent no-op (no pip invocation)."""
    from app.chat import runner

    monkeypatch.setattr(runner, "_SANDBOX_WHEEL_DIR", str(tmp_path / "empty"))
    called = []
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: called.append(a))

    runner._install_agnes_cli()
    assert called == []


def test_install_agnes_cli_invokes_pip_no_deps_user(tmp_path: Path, monkeypatch):
    """With a wheel staged, pip is invoked --no-deps --user --break-system-packages
    against the PEP 427 wheel filename (not a renamed agnes.whl)."""
    from app.chat import runner

    staging = tmp_path / "agnes-cli"
    staging.mkdir()
    wheel = staging / "agnes_the_ai_analyst-0.55.25-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(runner, "_SANDBOX_WHEEL_DIR", str(staging))

    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(runner.subprocess, "run", _fake_run)

    runner._install_agnes_cli()

    argv = captured["argv"]
    assert argv[:4] == [sys.executable, "-m", "pip", "install"]
    assert "--no-deps" in argv
    assert "--user" in argv
    assert "--break-system-packages" in argv
    assert str(wheel) == argv[-1]
    assert argv[-1].endswith("agnes_the_ai_analyst-0.55.25-py3-none-any.whl")


def test_install_agnes_cli_swallows_pip_failure(tmp_path: Path, monkeypatch, capsys):
    """A pip failure is non-fatal — logged to stderr, no exception raised."""
    from app.chat import runner

    staging = tmp_path / "agnes-cli"
    staging.mkdir()
    (staging / "agnes_the_ai_analyst-0.55.25-py3-none-any.whl").write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(runner, "_SANDBOX_WHEEL_DIR", str(staging))

    def _boom(*a, **k):
        raise RuntimeError("pip exploded")

    monkeypatch.setattr(runner.subprocess, "run", _boom)

    runner._install_agnes_cli()  # must not raise
    assert "agnes CLI install failed" in capsys.readouterr().err


def test_per_tool_call_timeout_emits_synthetic_result(tmp_path: Path):
    """__slow_tool__ triggers a tool_call followed by tool_result: {timeout: true}."""
    async def _run():
        env = os.environ.copy()
        env["PYTHONPATH"] = _PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["AGNES_RUNNER_FAKE_AGENT"] = "1"
        env["AGNES_PER_TOOL_CALL_SECONDS"] = "0.5"  # very short cap for test speed
        env["AGNES_SESSION_ID"] = "s"
        env["AGNES_USER_EMAIL"] = "u@x"
        env["AGNES_API"] = "http://127.0.0.1:8000"
        env["AGNES_TOKEN"] = "fake"

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.chat.runner", "--session-id", "s",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=str(tmp_path),
        )
        assert proc.stdin and proc.stdout

        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
        assert json.loads(line) == {"type": "runner_ready"}

        proc.stdin.write((json.dumps({"type": "user_msg", "text": "__slow_tool__"}) + "\n").encode())
        await proc.stdin.drain()

        saw_call = False
        saw_timeout = False
        for _ in range(10):
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
            frame = json.loads(line)
            if frame.get("type") == "tool_call":
                saw_call = True
            if frame.get("type") == "tool_result" and frame.get("result", {}).get("timeout"):
                saw_timeout = True
                break

        assert saw_call, "expected tool_call frame"
        assert saw_timeout, "expected tool_result with timeout=true"

        proc.stdin.close()
        await proc.wait()

    asyncio.run(_run())
