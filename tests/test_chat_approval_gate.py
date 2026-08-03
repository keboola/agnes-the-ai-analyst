"""ApprovalGate — the runner's in-process PreToolUse gate that makes the
workspace hook's ``ask`` verdicts real in cloud chat.

Unit tests drive ``ApprovalGate.check`` directly (asyncio.run per project
convention); the final test drives the full stdin round-trip through a
fake-agent runner subprocess (``__approval__:`` trigger).
"""

import asyncio
import json
import os
import sys

import pytest
from pathlib import Path

from app.chat.runner import ApprovalGate

_PROJECT_ROOT = str(Path(__file__).parent.parent)

# Verdict fixture hook: deny for "denyme", ask for "askme", allow otherwise.
_HOOK_SRC = """\
import json, sys
p = json.loads(sys.stdin.read() or "{}")
cmd = (p.get("tool_input") or {}).get("command", "")
if "denyme" in cmd:
    print(json.dumps({"permissionDecision": "deny", "permissionDecisionReason": "nope"}))
elif "askme" in cmd:
    print(json.dumps({"permissionDecision": "ask", "permissionDecisionReason": "needs approval"}))
else:
    print(json.dumps({"permissionDecision": "allow"}))
"""


def _write_hook(tmp_path: Path, src: str = _HOOK_SRC) -> Path:
    hook = tmp_path / ".claude" / "hooks" / "pre_tool_use.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(src)
    return hook


def _bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def _decision_of(out: dict) -> str | None:
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision")


def test_file_hook_deny_passes_through(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path))
        out = await gate.check(_bash("denyme"), None, {})
        assert _decision_of(out) == "deny"
        assert "nope" in out["hookSpecificOutput"]["permissionDecisionReason"]
        assert emitted == []  # no approval round-trip for a deny

    asyncio.run(_run())


def test_file_hook_allow_yields_no_opinion(tmp_path):
    async def _run():
        gate = ApprovalGate(lambda f: None, _write_hook(tmp_path))
        assert await gate.check(_bash("ls"), None, {}) == {}

    asyncio.run(_run())


def test_missing_and_broken_hooks_yield_no_opinion(tmp_path):
    async def _run():
        gate = ApprovalGate(lambda f: None, tmp_path / "nope.py")
        assert await gate.check(_bash("askme"), None, {}) == {}
        broken = _write_hook(tmp_path, "print('this is not json')")
        gate2 = ApprovalGate(lambda f: None, broken)
        assert await gate2.check(_bash("askme"), None, {}) == {}

    asyncio.run(_run())


def test_ask_allow_roundtrip(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_bash("askme now"), None, {}))
        # wait for the request frame
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        req = emitted[0]
        assert req["type"] == "approval_request"
        assert req["reason"] == "needs approval"
        assert req["command"] == "askme now"
        assert gate.resolve(req["request_id"], "allow") is True
        out = await task
        assert _decision_of(out) == "allow"
        resolved = [f for f in emitted if f["type"] == "approval_resolved"]
        assert resolved and resolved[0]["decision"] == "allow"

    asyncio.run(_run())


def test_allow_session_dedupes_same_command_only(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_bash("askme now"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        gate.resolve(emitted[0]["request_id"], "allow_session")
        assert _decision_of(await task) == "allow"
        # IDENTICAL command: allowed immediately, no second request frame
        out2 = await gate.check(_bash("askme now"), None, {})
        assert _decision_of(out2) == "allow"
        assert len([f for f in emitted if f["type"] == "approval_request"]) == 1

    asyncio.run(_run())


def test_allow_session_does_not_leak_across_commands(tmp_path):
    """Dedup is command-keyed, not reason-keyed: approving one command of a
    family must NOT pre-approve a different command sharing the hook's
    reason string (review finding on #1145)."""

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_bash("askme grant"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        gate.resolve(emitted[0]["request_id"], "allow_session")
        assert _decision_of(await task) == "allow"
        # DIFFERENT command, SAME hook reason ("needs approval"): a fresh
        # request must be raised, not silently pre-approved
        task2 = asyncio.create_task(gate.check(_bash("askme delete"), None, {}))
        for _ in range(100):
            if len([f for f in emitted if f["type"] == "approval_request"]) == 2:
                break
            await asyncio.sleep(0.01)
        reqs = [f for f in emitted if f["type"] == "approval_request"]
        assert len(reqs) == 2
        assert gate.awaiting_approval() is True
        gate.resolve(reqs[1]["request_id"], "deny")
        assert _decision_of(await task2) == "deny"

    asyncio.run(_run())


def test_awaiting_approval_reflects_pending(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        assert gate.awaiting_approval() is False
        task = asyncio.create_task(gate.check(_bash("askme"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        assert gate.awaiting_approval() is True  # suspended on the human
        gate.resolve(emitted[0]["request_id"], "allow")
        await task
        assert gate.awaiting_approval() is False

    asyncio.run(_run())


def test_user_deny_denies_tool(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_bash("askme"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        gate.resolve(emitted[0]["request_id"], "deny")
        out = await task
        assert _decision_of(out) == "deny"
        assert "denied" in out["hookSpecificOutput"]["permissionDecisionReason"].lower()

    asyncio.run(_run())


def test_timeout_denies(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=0.05)
        out = await gate.check(_bash("askme"), None, {})
        assert _decision_of(out) == "deny"
        assert "timed out" in out["hookSpecificOutput"]["permissionDecisionReason"].lower()
        resolved = [f for f in emitted if f["type"] == "approval_resolved"]
        assert resolved and resolved[0]["decision"] == "timeout"

    asyncio.run(_run())


def test_disabled_gate_denies_ask_without_prompting(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), enabled=False)
        out = await gate.check(_bash("askme"), None, {})
        assert _decision_of(out) == "deny"
        assert emitted == []

    asyncio.run(_run())


def test_cancel_all_denies_pending(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_bash("askme"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        gate.cancel_all()
        out = await task
        assert _decision_of(out) == "deny"

    asyncio.run(_run())


def test_invalid_decision_hardens_to_deny(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_bash("askme"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        gate.resolve(emitted[0]["request_id"], "totally-bogus")
        assert _decision_of(await task) == "deny"

    asyncio.run(_run())


def test_runner_subprocess_roundtrip(tmp_path):
    """Full stdin round-trip through the fake-agent runner: __approval__:
    trigger → approval_request frame out → approval_decision in →
    gate:allow assistant message. Also proves a stale decision id is
    dropped without crashing the frame loop."""

    async def _run():
        _write_hook(tmp_path)
        env = os.environ.copy()
        env["PYTHONPATH"] = _PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["AGNES_RUNNER_FAKE_AGENT"] = "1"
        env["AGNES_SESSION_ID"] = "chat_appr"
        env["AGNES_APPROVAL_TIMEOUT_SECONDS"] = "10"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.chat.runner",
            "--session-id",
            "chat_appr",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(tmp_path),
        )
        assert proc.stdin and proc.stdout

        try:
            line = await asyncio.wait_for(proc.stdout.readline(), 10)
        except TimeoutError:
            proc.kill()
            err = (await proc.stderr.read())[:2000].decode(errors="replace")
            raise AssertionError(f"no runner_ready frame; runner stderr: {err}") from None
        assert json.loads(line) == {"type": "runner_ready"}

        # stale decision for an unknown request: must be silently dropped
        proc.stdin.write(
            (json.dumps({"type": "approval_decision", "request_id": "ghost", "decision": "allow"}) + "\n").encode()
        )
        proc.stdin.write((json.dumps({"type": "user_msg", "text": "__approval__:askme now"}) + "\n").encode())
        await proc.stdin.drain()

        try:
            req = json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
        except TimeoutError:
            proc.kill()
            err = (await proc.stderr.read())[:2000].decode(errors="replace")
            raise AssertionError(f"no approval_request frame; runner stderr: {err}") from None
        assert req["type"] == "approval_request"
        assert req["reason"] == "needs approval"

        proc.stdin.write(
            (
                json.dumps({"type": "approval_decision", "request_id": req["request_id"], "decision": "allow"}) + "\n"
            ).encode()
        )
        await proc.stdin.drain()

        frames = []
        for i in range(3):
            try:
                frames.append(json.loads(await asyncio.wait_for(proc.stdout.readline(), 10)))
            except TimeoutError:
                proc.kill()
                raise AssertionError(f"missing frame {i + 1}/3; got so far: {frames}") from None
        types = [f["type"] for f in frames]
        assert "approval_resolved" in types
        msg = next(f for f in frames if f["type"] == "assistant_message")
        assert msg["content"] == "gate:allow"

        proc.stdin.close()
        try:
            rc = await asyncio.wait_for(proc.wait(), 10)
        except TimeoutError:
            proc.kill()
            raise AssertionError("runner did not exit after stdin EOF") from None
        assert rc == 0

    asyncio.run(_run())


def test_cancelled_anext_closes_a_plain_async_generator():
    """Why the turn loop keeps a shielded in-flight ``__anext__``.

    ``asyncio.wait_for`` cancels its argument on timeout, and a cancellation
    delivered while an async generator is suspended at an ``await`` closes
    it — the next ``__anext__`` then raises ``StopAsyncIteration``. The turn
    loop would read that as "stream finished" and end silently, which is
    exactly the path an approval longer than one poll slice takes. This
    pins the language behavior the shield exists for, so the mitigation is
    not quietly removed later.
    """
    import asyncio

    async def gen():
        yield "a"
        await asyncio.sleep(5)
        yield "b"

    async def drive():
        it = gen().__aiter__()
        assert await asyncio.wait_for(it.__anext__(), timeout=1) == "a"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(it.__anext__(), timeout=0.05)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(it.__anext__(), timeout=1)

    asyncio.run(drive())


def test_shielded_pending_anext_survives_repeated_timeouts():
    """The pattern the turn loop uses: the generator stays alive across polls."""
    import asyncio

    async def gen():
        yield "a"
        await asyncio.sleep(0.4)
        yield "b"

    async def drive():
        it = gen().__aiter__()
        pending = None
        got = []
        for _ in range(40):
            if pending is None:
                pending = asyncio.ensure_future(it.__anext__())
            try:
                got.append(await asyncio.wait_for(asyncio.shield(pending), timeout=0.05))
                pending = None
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                continue
        assert got == ["a", "b"], got

    asyncio.run(drive())
