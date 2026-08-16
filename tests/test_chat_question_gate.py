"""QuestionGate — the runner's in-process ``can_use_tool`` gate that turns
the agent's AskUserQuestion tool into a real user round-trip in cloud chat.

Unit tests drive ``QuestionGate.ask`` directly (asyncio.run per project
convention); the final test drives the full stdin round-trip through a
fake-agent runner subprocess (``__question__:`` trigger).
"""

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

from app.chat.runner import UNATTENDED, QuestionGate, _question_outcome

_PROJECT_ROOT = str(Path(__file__).parent.parent)


def _tool_input(question: str = "Which color?", *, multi: bool = False) -> dict:
    return {
        "questions": [
            {
                "question": question,
                "header": "Color",
                "options": [{"label": "Red"}, {"label": "Blue"}],
                "multiSelect": multi,
            }
        ]
    }


def test_answered_roundtrip():
    async def _run():
        emitted: list[dict] = []
        gate = QuestionGate(emitted.append, timeout_seconds=5)
        task = asyncio.create_task(gate.ask(_tool_input()))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        req = emitted[0]
        assert req["type"] == "question_request"
        assert req["questions"][0]["question"] == "Which color?"
        assert gate.awaiting_answer() is True
        assert gate.resolve(req["request_id"], {"Which color?": "Red"}) is True
        outcome, answers = await task
        assert outcome == "answered"
        assert answers == {"Which color?": "Red"}
        resolved = [f for f in emitted if f["type"] == "question_resolved"]
        assert resolved and resolved[0]["decision"] == "answered"
        assert resolved[0]["answers"] == {"Which color?": "Red"}
        assert gate.awaiting_answer() is False

    asyncio.run(_run())


def test_dismissed_and_unattended_and_timeout():
    async def _run():
        # Dismissed (None outcome).
        emitted: list[dict] = []
        gate = QuestionGate(emitted.append, timeout_seconds=5)
        task = asyncio.create_task(gate.ask(_tool_input()))
        while not emitted:
            await asyncio.sleep(0.01)
        gate.resolve(emitted[0]["request_id"], None)
        assert await task == ("dismissed", None)
        assert emitted[-1]["decision"] == "dismissed"

        # Unattended (manager-originated sentinel).
        emitted.clear()
        task = asyncio.create_task(gate.ask(_tool_input()))
        while not emitted:
            await asyncio.sleep(0.01)
        gate.resolve(emitted[0]["request_id"], UNATTENDED)
        assert await task == (UNATTENDED, None)
        assert emitted[-1]["decision"] == UNATTENDED

        # Timeout.
        emitted.clear()
        fast = QuestionGate(emitted.append, timeout_seconds=0.05)
        assert await fast.ask(_tool_input()) == ("timeout", None)
        assert emitted[-1]["decision"] == "timeout"

    asyncio.run(_run())


def test_answer_hardening():
    """Junk answers degrade to a dismissal; usable entries are bounded."""

    async def _run():
        emitted: list[dict] = []
        gate = QuestionGate(emitted.append, timeout_seconds=5)

        # Non-dict → dismissed.
        task = asyncio.create_task(gate.ask(_tool_input()))
        while not emitted:
            await asyncio.sleep(0.01)
        gate.resolve(emitted[0]["request_id"], "not-a-dict")
        assert await task == ("dismissed", None)

        # Non-string entries dropped; empty-after-strip dropped; long values
        # truncated; entry count capped at 8.
        emitted.clear()
        task = asyncio.create_task(gate.ask(_tool_input()))
        while not emitted:
            await asyncio.sleep(0.01)
        raw: dict = {f"q{i}": "a" for i in range(20)}
        raw["Which color?"] = "x" * 10_000
        raw["bad-key"] = 42
        raw["blank"] = "   "
        gate.resolve(emitted[0]["request_id"], raw)
        outcome, answers = await task
        assert outcome == "answered"
        assert answers is not None
        assert len(answers) == 8
        for value in answers.values():
            assert len(value) <= 4000
        assert "bad-key" not in answers
        assert "blank" not in answers

        # All entries junk → dismissed, not answered-with-empty.
        emitted.clear()
        task = asyncio.create_task(gate.ask(_tool_input()))
        while not emitted:
            await asyncio.sleep(0.01)
        gate.resolve(emitted[0]["request_id"], {"k": 1, 2: "v", "s": "  "})
        assert await task == ("dismissed", None)

    asyncio.run(_run())


def test_cancel_all_dismisses_and_clears_pending():
    async def _run():
        emitted: list[dict] = []
        gate = QuestionGate(emitted.append, timeout_seconds=5)
        task = asyncio.create_task(gate.ask(_tool_input()))
        while not emitted:
            await asyncio.sleep(0.01)
        assert gate.awaiting_answer() is True
        gate.cancel_all()
        assert await task == ("dismissed", None)
        assert gate.awaiting_answer() is False
        # A stale answer for the retired id is dropped silently.
        assert gate.resolve(emitted[0]["request_id"], {"q": "a"}) is False

    asyncio.run(_run())


def test_task_cancellation_retires_the_card():
    """A cancelled ask (turn teardown) must emit question_resolved and clear
    _pending — the manager retires a card only when it sees the resolution,
    and awaiting_answer() pins the turn watchdog while a future lingers
    (same contract the ApprovalGate cancellation path guards)."""

    async def _run():
        emitted: list[dict] = []
        gate = QuestionGate(emitted.append, timeout_seconds=30)
        task = asyncio.create_task(gate.ask(_tool_input()))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if gate._pending:
                break
        assert gate._pending, "the gate never registered a pending question"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert gate._pending == {}, "a cancelled question leaked its future"
        assert gate.awaiting_answer() is False
        resolved = [f for f in emitted if f.get("type") == "question_resolved"]
        assert resolved and resolved[-1]["decision"] == "cancelled", emitted

    asyncio.run(_run())


def test_malformed_questions_short_circuit():
    async def _run():
        emitted: list[dict] = []
        gate = QuestionGate(emitted.append, timeout_seconds=5)
        assert await gate.ask({}) == ("dismissed", None)
        assert await gate.ask({"questions": "nope"}) == ("dismissed", None)
        assert emitted == []  # no round-trip for nothing to render

    asyncio.run(_run())


def test_question_outcome_mapping():
    assert _question_outcome({"unattended": True}) == UNATTENDED
    assert _question_outcome({"dismissed": True}) is None
    assert _question_outcome({"answers": {"q": "a"}}) == {"q": "a"}
    # unattended wins even if a forged frame carries both (the WS handler
    # never forwards unattended from a client; belt and suspenders).
    assert _question_outcome({"unattended": True, "answers": {"q": "a"}}) == UNATTENDED
    assert _question_outcome({}) is None


def test_request_ids_are_unique_across_gate_instances():
    """Same invariant as ApprovalGate ids: a respawned sandbox restarts the
    counter, and the client dedups cards by request_id."""

    seen: list[str] = []

    def make_gate() -> QuestionGate:
        return QuestionGate(
            lambda frame: (seen.append(frame["request_id"]) if frame.get("type") == "question_request" else None),
            timeout_seconds=0.05,
        )

    async def drive():
        for _ in range(3):
            gate = make_gate()
            for _ in range(2):
                await gate.ask(_tool_input())

    asyncio.run(drive())
    assert len(seen) == 6, seen
    assert len(set(seen)) == 6, f"request ids collided across gate instances: {seen}"


def test_runner_subprocess_roundtrip(tmp_path):
    """Full stdin round-trip through the fake-agent runner: __question__:
    trigger → question_request frame out → question_answer in →
    question:answered assistant message. Also proves a stale answer id is
    dropped without crashing the frame loop."""

    async def _run():
        env = os.environ.copy()
        env["PYTHONPATH"] = _PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["AGNES_RUNNER_FAKE_AGENT"] = "1"
        env["AGNES_SESSION_ID"] = "chat_ques"
        env["AGNES_APPROVAL_TIMEOUT_SECONDS"] = "10"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.chat.runner",
            "--session-id",
            "chat_ques",
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

        # stale answer for an unknown request: must be silently dropped
        proc.stdin.write(
            (json.dumps({"type": "question_answer", "request_id": "ghost", "answers": {"q": "a"}}) + "\n").encode()
        )
        proc.stdin.write((json.dumps({"type": "user_msg", "text": "__question__:Which color?"}) + "\n").encode())
        await proc.stdin.drain()

        try:
            req = json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
        except TimeoutError:
            proc.kill()
            err = (await proc.stderr.read())[:2000].decode(errors="replace")
            raise AssertionError(f"no question_request frame; runner stderr: {err}") from None
        assert req["type"] == "question_request"
        assert req["questions"][0]["question"] == "Which color?"

        proc.stdin.write(
            (
                json.dumps(
                    {
                        "type": "question_answer",
                        "request_id": req["request_id"],
                        "answers": {"Which color?": "A"},
                    }
                )
                + "\n"
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
        assert "question_resolved" in types
        msg = next(f for f in frames if f["type"] == "assistant_message")
        assert msg["content"] == 'question:answered:{"Which color?": "A"}'

        proc.stdin.close()
        try:
            rc = await asyncio.wait_for(proc.wait(), 10)
        except TimeoutError:
            proc.kill()
            raise AssertionError("runner did not exit after stdin EOF") from None
        assert rc == 0

    asyncio.run(_run())
