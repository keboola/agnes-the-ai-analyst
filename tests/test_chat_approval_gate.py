"""ApprovalGate — the runner's in-process PreToolUse gate that makes the
workspace hook's ``ask`` verdicts real in cloud chat.

Unit tests drive ``ApprovalGate.check`` directly (asyncio.run per project
convention); the final test drives the full stdin round-trip through a
fake-agent runner subprocess (``__approval__:`` trigger).
"""

import asyncio
import json
import os
import pathlib
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


def test_unattended_denies_with_an_actionable_message(tmp_path):
    """The manager resolves a request nobody can answer with `unattended`
    (agent-API one-shot). It denies like a timeout but says WHY and what to
    do instead — the runner's message is the only thing the calling agent
    sees."""

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=30)
        task = asyncio.create_task(gate.check(_bash("askme"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        assert gate.resolve(emitted[0]["request_id"], "unattended") is True
        out = await task
        assert _decision_of(out) == "deny"
        why = out["hookSpecificOutput"]["permissionDecisionReason"].lower()
        assert "agent api" in why and "run the command themselves" in why
        resolved = [f for f in emitted if f["type"] == "approval_resolved"]
        assert resolved and resolved[0]["decision"] == "unattended"

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


def test_file_hook_accepts_the_nested_verdict_shape(tmp_path, monkeypatch):
    """Claude Code allows the verdict nested under hookSpecificOutput.

    The bundled hook emits the flat shape, but an operator override written
    against the nested spec shape must not read as "no opinion" — its
    ask/deny rules would be silently inert.
    """
    import app.chat.runner as runner

    hook = tmp_path / "hook.py"
    hook.write_text(
        "import json,sys\n"
        'print(json.dumps({"hookSpecificOutput": {"permissionDecision": "deny",'
        ' "permissionDecisionReason": "nope"}}))\n'
    )
    gate = runner.ApprovalGate.__new__(runner.ApprovalGate)
    gate._hook_path = hook
    out = gate.run_file_hook({"tool_name": "Bash", "tool_input": {"command": "x"}})
    assert out.get("permissionDecision") == "deny"
    assert out.get("permissionDecisionReason") == "nope"


def test_file_hook_flat_shape_still_wins(tmp_path):
    import app.chat.runner as runner

    hook = tmp_path / "hook.py"
    hook.write_text("import json\nprint(json.dumps({'permissionDecision': 'allow'}))\n")
    gate = runner.ApprovalGate.__new__(runner.ApprovalGate)
    gate._hook_path = hook
    assert gate.run_file_hook({"tool_name": "Bash"}).get("permissionDecision") == "allow"


def test_gate_disables_itself_when_hookmatcher_takes_no_timeout():
    """Without a matcher timeout the gate cannot guarantee it blocks.

    The gate's own `asyncio.wait_for` bounds only the gate's wait, not the
    CLI's. If the CLI timed the PreToolUse hook out and treated it as
    non-blocking, the tool would run while a human was still being asked and
    their decision would land after the fact — the barrier silently becoming
    a delay. Denying is the only honest option, so the fallback must fail
    closed rather than arm an unenforceable gate.
    """
    import app.chat.runner as runner

    gate = runner.ApprovalGate.__new__(runner.ApprovalGate)
    gate._enabled = True
    gate._disabled_reason = ""
    gate._session_approved = set()
    gate.disable_unsupported("HookMatcher takes no timeout")
    assert gate._enabled is False
    # The recorded reason must reach the agent, so it cannot relay the
    # wrong explanation (e.g. "this session was not started in web chat").
    assert "timeout" in gate._disabled_reason


def test_installed_sdk_hookmatcher_supports_timeout():
    """The fallback above must stay unreachable on a supported SDK.

    If a future pin lands on a build without `timeout`, approvals would
    silently switch to deny-everything — better to fail here.
    """
    import dataclasses

    from claude_agent_sdk import HookMatcher

    assert "timeout" in [f.name for f in dataclasses.fields(HookMatcher)], (
        "installed claude-agent-sdk HookMatcher has no timeout field; the approval gate "
        "will refuse to arm (see ApprovalGate.disable_unsupported)"
    )


def test_disabled_gate_is_still_registered_so_it_can_deny(tmp_path):
    """A disabled gate must still be wired into the SDK, or it denies nothing.

    Setting `_enabled = False` only produces a deny if the hook is actually
    called. Skipping registration on an SDK whose HookMatcher takes no
    timeout left nothing to deny — ask-flagged commands ran unasked, the
    exact behavior this feature removes. A disabled gate answers instantly,
    so there is no wait for a CLI-side hook timeout to cut short.
    """
    import app.chat.runner as runner

    import asyncio

    hook = tmp_path / "hook.py"
    hook.write_text("import json\nprint(json.dumps({'permissionDecision': 'ask', 'permissionDecisionReason': 'risky'}))\n")

    gate = runner.ApprovalGate.__new__(runner.ApprovalGate)
    gate._enabled = False
    gate._disabled_reason = "SDK too old"
    gate._session_approved = set()
    gate._hook_path = hook

    res = asyncio.run(gate.check({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, None, None))
    hso = res.get("hookSpecificOutput", res)
    assert hso.get("permissionDecision") == "deny", res
    assert "SDK too old" in (hso.get("permissionDecisionReason") or ""), res


def test_cancelled_approval_does_not_leak_into_pending(tmp_path):
    """A cancelled wait must not pin awaiting_approval() to True forever.

    The turn watchdog reads awaiting_approval() as "a human is deciding" and
    skips the stuck-tool abort. A future left in _pending by a cancellation
    (Stop, turn teardown) therefore disables the watchdog for the rest of
    the session, and a genuinely stuck tool hangs it for good.
    """
    import asyncio
    import contextlib

    import app.chat.runner as runner

    hook = tmp_path / "hook.py"
    hook.write_text("import json\nprint(json.dumps({'permissionDecision': 'ask', 'permissionDecisionReason': 'r'}))\n")

    gate = runner.ApprovalGate.__new__(runner.ApprovalGate)
    gate._enabled = True
    gate._disabled_reason = ""
    gate._session_approved = set()
    gate._pending = {}
    gate._counter = 0
    gate._hook_path = hook
    gate.timeout_seconds = 30
    emitted: list[dict] = []
    gate._emit = emitted.append

    async def drive():
        task = asyncio.create_task(
            gate.check({"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}, None, None)
        )
        # let it register the pending future
        for _ in range(50):
            await asyncio.sleep(0.01)
            if gate._pending:
                break
        assert gate._pending, "the gate never registered a pending approval"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert gate._pending == {}, "a cancelled approval leaked its future"
        assert gate.awaiting_approval() is False
        # …and the card must be retired, or the manager keeps replaying it.
        resolved = [f for f in emitted if f.get("type") == "approval_resolved"]
        assert resolved and resolved[-1]["decision"] == "cancelled", emitted

    asyncio.run(drive())


def test_request_ids_are_unique_across_gate_instances(tmp_path):
    """A respawned sandbox must not mint ids the chat window already knows.

    The old scheme was pid+counter; a fresh sandbox restarts the counter at
    zero and can be handed the same pid, so a new prompt could reuse an id.
    The client dedups cards by request_id, so that prompt was never drawn
    and the command hung until the approval window expired.
    """
    import asyncio

    import app.chat.runner as runner

    hook = tmp_path / "hook.py"
    hook.write_text("import json\nprint(json.dumps({'permissionDecision': 'ask', 'permissionDecisionReason': 'r'}))\n")

    seen: list[str] = []

    def make_gate():
        g = runner.ApprovalGate.__new__(runner.ApprovalGate)
        g._enabled = True
        g._disabled_reason = ""
        g._session_approved = set()
        g._pending = {}
        g._counter = 0          # a fresh sandbox restarts it at zero
        g._hook_path = hook
        g.timeout_seconds = 0.05  # resolve fast; we only want the id
        g._emit = lambda frame: (
            seen.append(frame["request_id"]) if frame.get("type") == "approval_request" else None
        )
        return g

    async def drive():
        for _ in range(3):      # three "sandbox lifetimes"
            gate = make_gate()
            for _ in range(2):
                await gate.check({"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}, None, None)

    asyncio.run(drive())
    assert len(seen) == 6, seen
    assert len(set(seen)) == 6, f"request ids collided across gate instances: {seen}"
