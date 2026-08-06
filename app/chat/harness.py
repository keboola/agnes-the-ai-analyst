"""AgentHarness seam — runtime extension point for agent engines.

Mirrors ``app/chat/provider.py``'s ``SandboxProvider`` pattern: the chat
stack is decoupled from any single agent engine at a narrow, explicit
seam instead of hard-wiring one. Today **Claude Code** (the
``claude-agent-sdk`` CLI inside the sandbox) is the only production
harness — the platform deliberately leans on its ecosystem (skills,
marketplace re-serving, hooks, the full tool set) — but selection and
validation flow through this module so an alternative engine can plug in
behind the same frame protocol without touching the manager or the
runner's stdio contract.

The seam has three parts:

- ``APPROVED_HARNESSES`` — the allowlist an operator-configured
  ``chat.harness`` is validated against at boot
  (``app/main.py::_chat_harness_ok``; unknown values refuse chat rather
  than silently falling back — the explicit-choice-invalid-throws rule).
- ``AGNES_HARNESS`` env — how the manager tells the in-sandbox runner
  which engine to drive. The runner keeps its own id→loop registry
  (``app/chat/runner.py::HARNESSES``; that file runs standalone in the
  sandbox and cannot import this module before the wheel install) and
  degrades to the default with a stderr warning on an unknown id — the
  inherited-choice-invalid-falls-back rule, so a version-skewed sandbox
  never hard-crashes over a new id it doesn't know yet.
- ``AgentHarnessLoop`` — the callable contract a harness implementation
  satisfies: drive one runner session end-to-end, consuming inbound
  frames from ``queue`` and emitting outbound frames on stdout, honoring
  the ApprovalGate.

Adding a second harness = implement the loop in the runner, register it
in ``HARNESSES``, extend ``APPROVED_HARNESSES``, and ship whatever
sandbox tooling the engine needs in the template. Per-scope/per-agent
harness pinning (precedence request > agent profile > instance default)
is deliberately NOT built until a second engine exists — the enum and
env plumbing here are the stable part; speculative routing is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import asyncio

    from app.chat.runner import ApprovalGate

#: Harness ids an operator may configure. Order is cosmetic; membership
#: is the contract.
APPROVED_HARNESSES: tuple[str, ...] = ("claude-code",)

DEFAULT_HARNESS = "claude-code"

#: Env var the manager sets at spawn to select the runner-side loop.
HARNESS_ENV = "AGNES_HARNESS"


@runtime_checkable
class AgentHarnessLoop(Protocol):
    """One full runner session: consume inbound frames, emit outbound."""

    async def __call__(
        self,
        queue: "asyncio.Queue[dict]",
        workdir: Path,
        *,
        tool_calls_per_turn: int = 50,
        gate: "ApprovalGate | None" = None,
    ) -> None: ...
