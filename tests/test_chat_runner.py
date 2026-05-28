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
