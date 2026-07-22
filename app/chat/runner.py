"""In-subprocess entrypoint. Runs claude-agent-sdk inside the chat sandbox.

Stdin: JSON lines, one per frame. Inbound types: user_msg, cancel,
       ticket_push (routed to the in-sandbox relay, never enqueued).
Stdout: JSON lines. Outbound types: runner_ready, token, tool_call,
        tool_result, assistant_message, error, done.

Env (set by ChatManager via the sandbox provider — under v1 the
E2BProvider passes these through ``AsyncSandbox.create(envs=...)``):
- AGNES_SESSION_ID, AGNES_USER_EMAIL, AGNES_SERVER, AGNES_TOKEN
- AGNES_DAILY_BUDGET_USD, AGNES_PER_TOOL_CALL_SECONDS

Before any CLI/MCP subprocess spawn, ``_start_relay`` starts an in-sandbox
loopback relay (``app/chat/relay.py``) and rewrites ``AGNES_SERVER``/
``ANTHROPIC_BASE_URL``/``ANTHROPIC_API_KEY`` in this process's env to point at
it with a dummy key — the relay is the only thing that ever holds a real
credential, fed in-memory ``ticket_push`` frames over stdin.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # names for annotations only — no runtime import (see below)
    from app.chat.relay import Relay

# NOTE: `app.chat.relay` is intentionally NOT imported at module level. This
# file runs as a standalone script (`python3 /work/runner.py`) inside the E2B
# sandbox, where the `app` package does not exist until `_install_agnes_cli()`
# pip-installs the uploaded wheel. A module-level `from app.chat.relay import
# Relay` crashed the runner at interpreter startup with `ModuleNotFoundError:
# No module named 'app'` — before the install ever ran — taking chat down
# end-to-end. `_start_relay()` imports it lazily, after the install. The
# forward-ref annotation below is a string (PEP 563 / `from __future__ import
# annotations`) so it needs no import at load time.

# Module-level in-sandbox relay. Populated by ``_start_relay`` in ``amain()``
# before any CLI/MCP subprocess spawn, and fed fresh tickets pushed by the
# manager over stdin (see ``_dispatch_frame``). Stays ``None`` in fake-agent
# test mode (AGNES_RUNNER_FAKE_AGENT=1), where there is no real CLI/MCP
# subprocess to broker credentials for.
_relay: "Relay | None" = None

# Directory the agnes CLI wheel is staged in by ChatManager at spawn
# (e2b_workspace_sync.upload_agnes_wheel keeps the wheel's PEP 427 filename).
# Module-level so tests can point it at a temp dir.
_SANDBOX_WHEEL_DIR = "/tmp/agnes-cli"
# ``.ready`` sentinel the manager writes after staging the wheel. The runner
# process starts BEFORE that upload completes (provider.spawn launches it),
# so we wait for the sentinel before installing — otherwise we'd glob an empty
# dir and skip the install. Bounded; on timeout we proceed best-effort.
# Module-level so tests can zero the wait.
_WHEEL_WAIT_SECONDS = 60

# Bounded wait for the manager's workspace-upload sentinel (path arrives via
# AGNES_WORKSPACE_SYNC_SENTINEL; empty/unset → no wait, e.g. providers that
# mount the workspace themselves). The wheel sentinel above only guarantees
# the CLI wheel — the workspace tree lands separately (and slower), and the
# agent CLI reads CLAUDE.md/.claude from /work at startup, so the CLI spawn
# must gate on this one. Generous bound: a workspace near the 100 MB cap can
# take a while; on timeout we proceed best-effort (agent on a possibly
# incomplete workspace beats no agent). Module-level so tests can zero it.
_WORKSPACE_WAIT_SECONDS = 180


def _emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _stream_event_delta_text(event: dict) -> str:
    """Extract the user-visible text delta from a raw Anthropic stream event.

    Returns ``""`` for everything that isn't assistant prose — block starts,
    tool-input ``input_json_delta``s, ``thinking_delta``s, message stops —
    so the caller can emit token frames off ``text_delta``s alone.
    """
    if not isinstance(event, dict) or event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta") or {}
    if delta.get("type") != "text_delta":
        return ""
    return delta.get("text", "") or ""


def _install_agnes_cli() -> None:
    """Install the agnes CLI from the spawn-uploaded wheel so the agent's
    ``agnes catalog/query/describe/snapshot`` tool calls resolve on PATH.

    Without this the sandbox has the CLI's *dependencies* (baked into the
    template image) but not the ``agnes`` console script itself, so half the
    cloud-chat data-analysis rails ("Querying Agnes data" in CLAUDE.md) fail
    with "command not found".

    - ``--no-deps``: every runtime dep is already in the template image;
      reinstalling the tree would add seconds to every spawn.
    - NO ``--user``: the console script must land in ``/usr/local/bin`` (the
      e2b base image chmods ``/usr/local`` 777, so the non-root sandbox
      ``user`` can write there). A ``--user`` install lands ``agnes`` in
      ``~/.local/bin``, which is NOT on the PATH the agent's Bash tool runs
      with — Claude Code's Bash tool resets PATH to a system default
      (``/usr/local/bin:/usr/bin:/bin:…``) and does NOT inherit the runner's
      env, so ``~/.local/bin`` would be invisible and ``agnes`` would still be
      "command not found".
    - ``--break-system-packages``: clears the PEP 668 externally-managed guard
      the Debian/Ubuntu base image sets.

    Best-effort and silent on stdout: pip's chatter is routed to stderr so it
    never corrupts the stdout JSON-frame protocol, and a failure here leaves
    ``agnes`` absent but the chat session otherwise functional — so we log to
    stderr rather than emit a user-facing error frame.
    """
    # Wait for the manager to finish staging the wheel (it writes a ``.ready``
    # sentinel last). Without this barrier we race the upload and glob an empty
    # dir. Bounded — a dev image without a wheel still writes the sentinel, so
    # the normal path returns in milliseconds; the timeout only bites if the
    # upload never happens at all.
    ready = Path(_SANDBOX_WHEEL_DIR) / ".ready"
    deadline = time.monotonic() + _WHEEL_WAIT_SECONDS
    while time.monotonic() < deadline and not ready.exists():
        time.sleep(0.5)
    # The wheel keeps its PEP 427 name (pip rejects a renamed wheel), so glob
    # the staging dir rather than assuming a fixed filename.
    wheels = sorted(glob.glob(f"{_SANDBOX_WHEEL_DIR}/*.whl"))
    if not wheels:
        return
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--break-system-packages",
                wheels[-1],
            ],
            # stdin MUST be isolated from the parent's fd 0: the runner's
            # asyncio stdin reader has connect_read_pipe'd fd 0 into
            # non-blocking mode, and a child inheriting that same fd corrupts
            # the reader (user_msg frames then never arrive — the agent hangs
            # with no response). DEVNULL gives pip its own stdin.
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr.fileno(),
            stderr=sys.stderr.fileno(),
            check=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal; agent still runs
        print(f"agnes CLI install failed: {exc}", file=sys.stderr, flush=True)


async def _wait_workspace_ready() -> bool:
    """Wait for the manager's workspace-upload sentinel before the agent CLI
    spawns.

    The runner process starts while ``upload_workspace`` is still pushing the
    tree into ``/work`` — spawning ``claude`` earlier would boot it against an
    empty project (no CLAUDE.md data rails, no ``.claude`` settings/plugins).
    Sentinel path comes from ``AGNES_WORKSPACE_SYNC_SENTINEL``; empty/unset
    means the provider mounts the workspace itself and there is nothing to
    wait for. Bounded and best-effort: on timeout we log to stderr and let the
    agent start anyway.
    """
    sentinel = os.environ.get("AGNES_WORKSPACE_SYNC_SENTINEL", "").strip()
    if not sentinel:
        return True
    path = Path(sentinel)
    deadline = time.monotonic() + _WORKSPACE_WAIT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            return True
        await asyncio.sleep(0.25)
    print(
        f"workspace-ready sentinel {sentinel} never appeared after "
        f"{_WORKSPACE_WAIT_SECONDS}s; starting agent on a possibly-incomplete workspace",
        file=sys.stderr,
        flush=True,
    )
    return False


def _agnes_mcp_servers() -> dict:
    """Build the ``mcp_servers`` config that connects the sandbox agent to the
    Agnes MCP stdio server (``agnes mcp``).

    This is what makes the cloud-chat agent == local Claude Code / Cowork for
    MCP: the same ``agnes mcp`` stdio server that an analyst's local install
    spawns (cli/mcp/server.py) is spawned here as a child of the SDK's
    ``claude`` process. It exposes the built-in cowork tools (catalog, query,
    describe, …) PLUS the RBAC-filtered Universal-MCP *passthrough* tools the
    caller's groups can see (registered dynamically at run() start via
    cli/mcp/_dynamic_passthrough.py against ``/api/mcp/passthrough/tools``).
    Without this, the sandbox agent could only reach Agnes through the
    ``agnes`` CLI's Bash surface and never saw passthrough tools at all.

    The stdio server authenticates off ``AGNES_SERVER`` (+ ``AGNES_SESSION_ID``
    for the per-session re-mint path). ``AGNES_SERVER`` is the in-sandbox
    loopback relay's address by the time this runs (``_start_relay`` rewrites
    it before any subprocess spawn), not the real Agnes server — the relay is
    the only thing in the sandbox that ever holds an authenticating
    credential (a short-lived broker ticket pushed over stdin, see
    ``_dispatch_frame``), attached on the outbound leg. No ``AGNES_TOKEN`` is
    placed in this env (AC-F2b). We forward ``AGNES_SERVER`` explicitly on the
    MCP server's own ``env`` rather than relying on inheritance, because the
    SDK spawns the server through the ``claude`` CLI and env inheritance
    across that hop is not guaranteed. ``PATH`` is forwarded so the ``agnes``
    console script (installed by ``_install_agnes_cli`` into /usr/local/bin)
    resolves.

    Returns ``{}`` when ``AGNES_SERVER`` is absent (e.g. the fake-agent test
    path or a misconfigured spawn) so the agent still runs with its built-in
    tools rather than failing on a broken MCP handshake.
    """
    server = os.environ.get("AGNES_SERVER", "").strip()
    if not server:
        return {}
    # The MCP stdio server must ride the mcp-scoped broker ticket, not the
    # agent process's main-scoped one. `_start_relay` set this process's
    # AGNES_SERVER to the relay's `/agnes-api` path (main scope); rewrite it to
    # `/agnes-mcp` for the MCP subprocess so the relay attaches the mcp ticket
    # (relay._SCOPE_FOR_PREFIX). Without this, the minted+pushed mcp ticket is
    # dead and both surfaces share one scope, defeating the split (§11).
    mcp_server = server.replace("/agnes-api", "/agnes-mcp")
    env = {
        "AGNES_SERVER": mcp_server,
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        # ``agnes mcp`` resolves its config dir via ``expanduser("~/.config/
        # agnes")`` (cli/config.py), which needs HOME. The ``claude`` CLI
        # spawns the stdio server and env inheritance across that hop is not
        # guaranteed, so forward HOME explicitly (default matches the sandbox
        # ``user`` home the manager seeds into the runner process).
        "HOME": os.environ.get("HOME", "/home/user"),
    }
    if session_id := os.environ.get("AGNES_SESSION_ID", "").strip():
        env["AGNES_SESSION_ID"] = session_id
    return {
        "agnes": {
            "type": "stdio",
            "command": "agnes",
            "args": ["mcp"],
            "env": env,
        }
    }


def _bootstrap_marketplace(workdir: str) -> None:
    """Install the user's RBAC-filtered Agnes marketplace plugins (skills)
    into this session's project so the agent can use them.

    Runs the same ``agnes refresh-marketplace --bootstrap`` the analyst
    workspace runs at first init: it clones the per-user marketplace bare repo
    (PAT-gated, from AGNES_SERVER), registers it with the in-sandbox ``claude``
    CLI (``claude plugin marketplace add``), and enables the plugins in the
    project (cwd). Combined with ``setting_sources=["project"]`` on the SDK
    client, the agent then sees the plugin skills (e.g. ``keboola-howto``).

    Without this the sandbox only has Claude Code's built-in skills — the
    synced marketplace is invisible. Best-effort and bounded: a failure (no
    token, network, claude CLI quirk) leaves the agent on built-in skills only
    rather than blocking the session; output is routed to stderr so it never
    corrupts the stdout JSON-frame protocol.
    """
    from shutil import which

    if which("agnes") is None:
        return
    try:
        subprocess.run(
            ["agnes", "refresh-marketplace", "--bootstrap"],
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr.fileno(),
            stderr=sys.stderr.fileno(),
            check=False,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal; agent still runs
        print(f"marketplace bootstrap failed: {exc}", file=sys.stderr, flush=True)


async def _dispatch_frame(frame: dict, queue: "asyncio.Queue[dict]") -> None:
    """Route one parsed inbound stdin frame.

    ``ticket_push`` frames (``{"type": "ticket_push", "main": ..., "mcp":
    ...}``) update the module-level relay's in-memory tickets and are never
    enqueued for the agent loop — the agent must never see a ticket. Every
    other frame type (``user_msg``, ``cancel``, ``_eof``) is queued
    unchanged, exactly as it always has been.
    """
    if frame.get("type") == "ticket_push":
        if _relay is not None:
            _relay.set_tickets(frame.get("main", ""), frame.get("mcp", ""))
        return
    await queue.put(frame)


async def _stdin_lines() -> "asyncio.Queue[dict]":
    queue: asyncio.Queue[dict] = asyncio.Queue()

    async def reader() -> None:
        loop = asyncio.get_running_loop()
        reader_obj = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader_obj)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        while True:
            line = await reader_obj.readline()
            if not line:
                await queue.put({"type": "_eof"})
                return
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            await _dispatch_frame(frame, queue)

    asyncio.create_task(reader())
    return queue


async def _fake_agent_loop(
    queue: "asyncio.Queue[dict]",
    *,
    per_tool_seconds: float = 90.0,
    tool_calls_per_turn: int = 50,
) -> None:
    """Used by tests via AGNES_RUNNER_FAKE_AGENT=1. Echoes user_msg back.

    Special messages:
    - ``__slow_tool__`` — simulates a tool call that exceeds the per-tool
      wall-clock cap. Emits ``tool_call`` then, after timeout, emits a
      synthetic ``tool_result: {timeout: true}``.
    - ``__many_tools__:N`` — fires N tool_call frames to exercise the
      per-turn tool-call budget gate.
    """
    while True:
        frame = await queue.get()
        if frame.get("type") == "_eof":
            return
        if frame.get("type") == "user_msg":
            text = frame.get("text", "")
            if text == "__slow_tool__":
                _emit({"type": "tool_call", "tool": "run_query", "args": {"sql": "..."}})
                try:
                    await asyncio.wait_for(
                        asyncio.sleep(per_tool_seconds + 5),
                        timeout=per_tool_seconds,
                    )
                except asyncio.TimeoutError:
                    _emit(
                        {
                            "type": "tool_result",
                            "tool": "run_query",
                            "result": {"timeout": True},
                        }
                    )
                continue
            if text.startswith("__many_tools__:"):
                try:
                    requested = int(text.split(":", 1)[1])
                except ValueError:
                    requested = 0
                # Tool-call budget gate (B.2.d): cap emitted tool_call frames
                # per turn at tool_calls_per_turn; on overflow emit a
                # confirmation_required and stop until the next user_msg.
                count = 0
                budget_hit = False
                for i in range(requested):
                    if count >= tool_calls_per_turn:
                        _emit(
                            {
                                "type": "confirmation_required",
                                "reason": "tool_call_budget",
                                "budget": tool_calls_per_turn,
                            }
                        )
                        budget_hit = True
                        break
                    _emit({"type": "tool_call", "tool": f"t{i}", "args": {}})
                    count += 1
                if not budget_hit:
                    _emit(
                        {
                            "type": "assistant_message",
                            "content": f"emitted {count} tool calls",
                            "tokens_in": 1,
                            "tokens_out": 1,
                            "model": "fake",
                        }
                    )
                continue
            _emit(
                {
                    "type": "assistant_message",
                    "content": f"echo: {text}",
                    "tokens_in": 1,
                    "tokens_out": 1,
                    "model": "fake",
                }
            )


async def _real_agent_loop(
    queue: "asyncio.Queue[dict]",
    workdir: Path,
    *,
    tool_calls_per_turn: int = 50,
) -> None:
    """Real claude-agent-sdk-backed loop.

    Per-tool wall-clock cap (Phase 12.2): the fake-agent path enforces
    AGNES_PER_TOOL_CALL_SECONDS via asyncio.wait_for in _fake_agent_loop.
    For the real SDK path, tool dispatch is handled inside ClaudeSDKClient
    (agnes receives tool_call/tool_result frames, not raw coroutines), so
    per-tool wrapping is not straightforward at this boundary. A simpler
    wall-clock timeout is applied at the whole-turn level: if
    receive_response() takes longer than per_tool_seconds * max_tools_per_turn,
    the connection is interrupted. Full per-tool granularity requires either
    an SDK API that exposes individual tool dispatch coroutines, or an
    out-of-process watchdog. TODO(Phase 12.2): revisit when claude-agent-sdk
    exposes a per-tool hook or run_tool() coroutine.

    Uses ClaudeSDKClient for persistent-session bidirectional communication:
    - the ``async with`` block connects EAGERLY (``__aenter__`` → ``connect()``
      with an empty stream), so the ``claude`` CLI subprocess boots while the
      user is still typing their first message
    - query() for every user_msg (the previous connect(text)-on-first-message
      pattern spawned a SECOND CLI on top of the one ``__aenter__`` already
      started — a full CLI boot added to first-message latency)
    - each turn's receive_response() drains in a CONCURRENT task
      (_consume_turn) while this loop keeps watching the stdin queue — a
      cancel frame arriving mid-turn interrupts the live turn (with the old
      single-consumer design it sat in the queue until the turn finished on
      its own, so Stop did nothing)
    - interrupt() for cancel frames; user_msg/_eof frames arriving mid-turn
      are buffered and processed after the turn, preserving order

    Message type mapping (SDK → outbound JSON frames):
    - StreamEvent text deltas → token frames as the model produces them
      (include_partial_messages; falls back to whole-TextBlock token frames
      when the SDK predates StreamEvent or no deltas arrive)
    - AssistantMessage with TextBlock content → collected for the turn-end
      assistant_message (token frame only when no deltas streamed this turn)
    - AssistantMessage with ToolUseBlock content → tool_call frame
    - AssistantMessage with ToolResultBlock content → tool_result frame
    - ResultMessage → assistant_message frame (turn end, carries usage/model)
    """
    from claude_agent_sdk import (  # type: ignore[import-untyped]
        ClaudeAgentOptions,
        ClaudeSDKClient,
    )

    try:  # StreamEvent ships in newer claude-agent-sdk releases only
        from claude_agent_sdk import StreamEvent  # type: ignore[attr-defined]
    except ImportError:
        StreamEvent = None

    # ``bypassPermissions`` so the agent can run its tools (Bash → ``agnes
    # catalog``/``query``/…) autonomously. The SDK's default permission mode
    # denies any tool needing approval in this headless context (no human to
    # prompt), so the agent emits a tool_call and then hangs / hallucinates
    # success without ever executing it. The E2B microVM is the isolation
    # boundary here (ephemeral, per-session); egress control is the workspace
    # PreToolUse hook's job and is documented as best-effort/fail-open. The
    # SDK-native in-process gate (``can_use_tool``) needs streaming-input mode
    # — a larger runner refactor tracked separately.
    # Load the workspace's filesystem config (user + project + local) — the
    # same scopes the local `claude` CLI loads by default. The SDK loads NONE
    # of them unless told to (its isolation default), which would make the
    # cloud-chat agent behave differently from a local Agnes install: it would
    # miss the workspace CLAUDE.md (the data rails that tell it to use the
    # `agnes` CLI instead of hunting for local files) and any installed
    # marketplace plugins. Loading them keeps cloud-chat == local. (The
    # marketplace registers in user scope and enables plugins at project scope,
    # so both must load for a bootstrapped plugin to resolve.)
    # ``mcp_servers`` connects the agent to the Agnes MCP stdio server so it
    # sees the RBAC-filtered passthrough tools (crm_* etc.) — the same surface
    # a local Claude Code / Cowork install gets. Empty dict when unconfigured
    # (fake-agent tests) so the agent still runs with built-in tools.
    mcp_servers = _agnes_mcp_servers()
    options_kwargs: dict = dict(
        permission_mode="bypassPermissions",
        cwd=str(workdir),
        setting_sources=["user", "project", "local"],
        mcp_servers=mcp_servers,
    )
    # Token-level streaming (include_partial_messages) when the installed SDK
    # supports it: the UI then renders text as the model produces it instead
    # of one token frame per completed content block (which for a long answer
    # means seconds of dead air followed by the whole paragraph at once).
    partial_streaming = StreamEvent is not None and "include_partial_messages" in getattr(
        ClaudeAgentOptions, "__dataclass_fields__", {}
    )
    if partial_streaming:
        options_kwargs["include_partial_messages"] = True

    async def _interrupt(client) -> None:
        # interrupt() is a coroutine — an un-awaited call never reaches the
        # CLI and the turn keeps running (the historical cancel-does-nothing
        # bug). Best-effort: a cancel racing the turn's natural end must not
        # kill the runner.
        try:
            await client.interrupt()
        except Exception as exc:  # noqa: BLE001
            print(f"interrupt failed: {exc}", file=sys.stderr, flush=True)

    async with ClaudeSDKClient(options=ClaudeAgentOptions(**options_kwargs)) as client:
        # ``__aenter__`` above already connected (empty-stream streaming mode)
        # — the CLI subprocess is booting from this point on, typically
        # finishing before the first user_msg arrives.

        # Frames that arrived while a turn was in flight (queued follow-up
        # user_msg, an _eof) — processed in order before the queue is read
        # again, preserving the pre-concurrency single-consumer semantics.
        pending_frames: list[dict] = []
        # Persistent queue.get() task. NEVER cancelled — cancelling a
        # Queue.get() that has already been handed an item loses the frame;
        # instead the outstanding task is carried across turns and awaited by
        # whichever loop (outer or mid-turn watcher) runs next.
        next_frame_task: asyncio.Task | None = None

        def _frame_task() -> asyncio.Task:
            nonlocal next_frame_task
            if next_frame_task is None:
                next_frame_task = asyncio.create_task(queue.get())
            return next_frame_task

        while True:
            if pending_frames:
                frame = pending_frames.pop(0)
            else:
                frame = await _frame_task()
                next_frame_task = None
            t = frame.get("type")

            if t == "_eof":
                return

            if t == "cancel":
                # Between turns: nothing is running, but the interrupt may
                # still race a just-finished turn — best-effort.
                await _interrupt(client)
                continue

            if t != "user_msg":
                continue

            text = frame.get("text", "")

            await client.query(text)

            # Consume the turn as a concurrent task while this loop keeps
            # watching the stdin queue. A single-consumer design (await
            # queue.get() only at the top of the loop) meant a `cancel`
            # arriving MID-TURN sat in the queue until the turn finished
            # naturally — by which point interrupt() was a no-op and the
            # Stop button did nothing (Devin Review on #975).
            turn_task = asyncio.create_task(_consume_turn(client, tool_calls_per_turn=tool_calls_per_turn))
            interrupted_this_turn = False
            while not turn_task.done():
                ft = _frame_task()
                await asyncio.wait({turn_task, ft}, return_when=asyncio.FIRST_COMPLETED)
                if ft.done():
                    next_frame_task = None
                    mid = ft.result()
                    if mid.get("type") == "cancel":
                        # Interrupt the LIVE turn; receive_response() then
                        # winds down and turn_task completes.
                        interrupted_this_turn = True
                        await _interrupt(client)
                    else:
                        # user_msg / _eof: keep for after the turn, in order.
                        pending_frames.append(mid)
            if interrupted_this_turn and turn_task.exception() is not None:
                # Some SDK/CLI builds surface a user interrupt as an exception
                # out of receive_response() instead of a graceful
                # ResultMessage. That is the OUTCOME THE USER ASKED FOR — eat
                # it so pressing Stop never tears down the whole runner (and
                # with it the session). A turn crashing WITHOUT an interrupt
                # still propagates below: runner exits, manager respawns.
                print(
                    f"turn ended with exception after interrupt (expected): {turn_task.exception()}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                await turn_task  # propagate a crashed turn (outer handler emits error frame)
            # `done` is emitted here, not inside `_consume_turn`, so it never
            # fires ahead of a genuine crash: `await turn_task` above raises
            # before reaching this line, and the manager's `error` frame
            # handler (not `done`) is what runs — preserving the turn buffer
            # so the partial answer already streamed to the user gets saved
            # (Devin Review on #975).
            _emit({"type": "done"})


async def _consume_turn(client, *, tool_calls_per_turn: int = 50) -> None:
    """Drain one turn's ``receive_response()`` stream into outbound frames.

    Runs as its own task so ``_real_agent_loop`` can keep consuming stdin
    frames (cancel!) while the turn is in flight. Does NOT emit the `done`
    frame itself — the caller does that, and only when this coroutine
    returns without raising, so a genuine crash mid-stream propagates as an
    `error` frame instead, leaving the turn buffer intact for partial-save.
    """
    from claude_agent_sdk import (  # type: ignore[import-untyped]
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    try:  # StreamEvent ships in newer claude-agent-sdk releases only
        from claude_agent_sdk import StreamEvent  # type: ignore[attr-defined]
    except ImportError:
        StreamEvent = None

    def _emit_tool_result(block) -> None:
        result = block.content
        if isinstance(result, list):
            result = " ".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in result)
        _emit(
            {
                "type": "tool_result",
                "id": block.tool_use_id,
                # Dedicated pairing key: the manager's frame envelope
                # (frame_seq.stamp_frame) OVERWRITES ``id`` with
                # ``chat_id:seq`` before fan-out, so the UI can never pair
                # tool_call↔tool_result via ``id`` — every tool block was
                # stuck on "running…" forever. ``tool_use_id`` survives the
                # stamp untouched.
                "tool_use_id": block.tool_use_id,
                "tool": block.tool_use_id,
                "result": result,
            }
        )

    collected_text: list[str] = []
    tokens_in = 0
    tokens_out = 0
    model = ""
    # Per-turn tool-call budget: count tool_call emissions; on
    # overflow emit a confirmation_required frame and break the loop
    # so the agent pauses until the next user_msg (which counts as
    # confirmation). Safety net against runaway tool chains.
    tool_calls_this_turn = 0
    budget_hit = False
    # Text streamed via StreamEvent deltas this turn. Non-empty ⇒ the
    # completed TextBlock repeats text the user has already seen, so its
    # whole-block token frame is suppressed (it still feeds collected_text
    # for the turn-end assistant_message). Also the fallback content source
    # should an SDK build stream deltas without a final consolidated
    # TextBlock — otherwise the live UI would show the answer but the
    # persisted assistant_message would be empty.
    streamed_pieces: list[str] = []

    # Idle watchdog: a tool call that never returns (e.g. an in-sandbox
    # `agnes pull` blocked on network) wedged the turn FOREVER — the SDK's
    # per-tool cap was never implemented for the real-agent path (Phase
    # 12.2 TODO), so the user stared at "running…" indefinitely. If the
    # agent stream produces NO message for this long, interrupt the turn
    # and surface an error frame instead. Generous default: silence is
    # normal while a model generates a long block (no partial streaming on
    # older-template SDKs), so this is a wedge-breaker, not a latency cap.
    idle_seconds = float(os.environ.get("AGNES_TURN_IDLE_SECONDS", "300") or "300")
    stream = client.receive_response().__aiter__()
    while True:
        try:
            msg = await asyncio.wait_for(stream.__anext__(), timeout=idle_seconds)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            _emit(
                {
                    "type": "error",
                    "kind": "turn_idle_timeout",
                    "message": (
                        f"no agent activity for {int(idle_seconds)}s; "
                        "interrupting the turn (a tool call is likely stuck)"
                    ),
                }
            )
            try:
                await client.interrupt()
            except Exception as exc:  # noqa: BLE001 — watchdog must not crash the runner
                print(f"idle-watchdog interrupt failed: {exc}", file=sys.stderr, flush=True)
            break
        if budget_hit:
            break
        if StreamEvent is not None and isinstance(msg, StreamEvent):
            # Token-level delta straight off the model stream. Only
            # top-level assistant text — subagent/tool-side streams
            # carry parent_tool_use_id and stay internal.
            if getattr(msg, "parent_tool_use_id", None) is None:
                piece = _stream_event_delta_text(msg.event)
                if piece:
                    _emit({"type": "token", "text": piece})
                    streamed_pieces.append(piece)
            continue
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if not streamed_pieces:
                        _emit({"type": "token", "text": block.text})
                    collected_text.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    if tool_calls_this_turn >= tool_calls_per_turn:
                        _emit(
                            {
                                "type": "confirmation_required",
                                "reason": "tool_call_budget",
                                "budget": tool_calls_per_turn,
                            }
                        )
                        budget_hit = True
                        break
                    # ``tool_use_id`` is the per-call pairing key —
                    # echoed back verbatim by ``ToolResultBlock`` so
                    # the frontend can pair a tool_call with its
                    # result even when several calls to the same tool
                    # are in flight. It rides its own key because the
                    # manager's frame envelope overwrites ``id`` with
                    # ``chat_id:seq`` (see _emit_tool_result). ``tool``
                    # carries the human-readable name for the inline
                    # block header.
                    _emit(
                        {
                            "type": "tool_call",
                            "id": block.id,
                            "tool_use_id": block.id,
                            "tool": block.name,
                            "args": block.input,
                        }
                    )
                    tool_calls_this_turn += 1
                elif isinstance(block, ToolResultBlock):
                    _emit_tool_result(block)
            model = msg.model
            if msg.usage:
                tokens_in += msg.usage.get("input_tokens", 0)
                tokens_out += msg.usage.get("output_tokens", 0)

        elif isinstance(msg, UserMessage):
            # The SDK feeds tool results back as a UserMessage carrying
            # ToolResultBlock(s) — NOT inside the AssistantMessage. Without
            # handling this branch the runner never emits a tool_result
            # frame, so the inline tool block in the UI is stuck on
            # "running…" forever even though the tool finished.
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        _emit_tool_result(block)

        elif isinstance(msg, ResultMessage):
            if msg.usage:
                tokens_in = msg.usage.get("input_tokens", tokens_in)
                tokens_out = msg.usage.get("output_tokens", tokens_out)
            # ResultMessage signals turn end; receive_response() stops after it.
            # Content prefers the consolidated TextBlocks (canonical);
            # falls back to the streamed deltas should an SDK build omit
            # the final block under partial streaming. Blocks are joined
            # with a blank line: each TextBlock is a prose segment
            # bracketing tool calls, and a bare "".join squashed segment
            # boundaries into mid-word runs ("…znovu:Z MCP pull…" in the
            # persisted history). Deltas keep "" — they are sub-block
            # fragments of one segment.
            _emit(
                {
                    "type": "assistant_message",
                    "content": "\n\n".join(t for t in collected_text if t.strip()) or "".join(streamed_pieces),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "model": model,
                }
            )


async def _start_relay() -> int:
    """Start the module-level loopback relay and rewrite this process's env
    so every subsequently-spawned CLI/MCP subprocess (``claude``, ``agnes``,
    ``agnes mcp``) talks to the relay with a dummy key instead of the real
    Anthropic/Agnes endpoints with a real credential.

    Must run before any such subprocess is spawned. Captures the real
    ``AGNES_SERVER`` value to construct the ``Relay`` *before* overwriting it
    below — the relay itself still needs the real server URL to forward
    brokered requests to.
    """
    global _relay
    # Lazy import: the `app` package only exists after _install_agnes_cli()
    # has pip-installed the uploaded wheel, so this MUST run after that step
    # (see amain()). Importing at module scope crashed the runner at startup.
    from app.chat.relay import Relay

    real_server = os.environ.get("AGNES_SERVER", "").strip()
    _relay = Relay(server_url=real_server)
    port = await _relay.start()
    os.environ["AGNES_SERVER"] = f"http://127.0.0.1:{port}/agnes-api"
    os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}/anthropic"
    os.environ["ANTHROPIC_API_KEY"] = "sk-dummy-broker"
    return port


async def amain() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.parse_args()  # validates required --session-id; value read from env

    workdir = Path(os.environ.get("AGNES_WORKDIR", os.getcwd()))

    fake_agent = os.environ.get("AGNES_RUNNER_FAKE_AGENT") == "1"

    # Install the agnes CLI BEFORE handing fd 0 to asyncio (_stdin_lines calls
    # connect_read_pipe, which puts stdin in non-blocking mode). Running the
    # pip subprocess after that wedges the asyncio stdin reader — user_msg
    # frames then never arrive and the agent hangs with no response. Doing it
    # here, before the reader is attached, keeps fd 0 a plain blocking pipe for
    # the duration of the install; the client's first user_msg simply buffers
    # in the OS pipe until _stdin_lines() starts reading. Skipped in fake-agent
    # mode (tests) — there is no wheel to install.
    if not fake_agent:
        # Install the agnes CLI wheel FIRST: it ships the `app` package that
        # _start_relay() imports (`from app.chat.relay import Relay`). Ordering
        # it after _start_relay crashed the relay import with ModuleNotFoundError
        # (the `app` package doesn't exist in the sandbox until this install).
        # Safe to run before the relay: the install is an offline
        # `pip install --no-deps <uploaded wheel>` — it hits no network and
        # needs no broker. The relay only has to be up before the AGENT
        # subprocess spawns (`claude`, `agnes mcp`), which come further below.
        _install_agnes_cli()
        # Now start the in-sandbox loopback relay and repoint this process's env
        # at it, before any CLI/MCP agent subprocess spawn — the real agent
        # loop's `claude` spawn and `_agnes_mcp_servers()`'s `agnes mcp` spawn
        # must see the rewritten AGNES_SERVER / ANTHROPIC_* env.
        await _start_relay()
        # Barrier: the workspace tree must be fully in /work before anything
        # reads or writes it — the marketplace bootstrap writes project-level
        # plugin state a late-finishing workspace extraction would clobber,
        # and the agent CLI (spawned eagerly by _real_agent_loop's `async
        # with`) loads CLAUDE.md/.claude from /work at boot. The wheel install
        # above deliberately does NOT gate on this — it overlaps the upload.
        await _wait_workspace_ready()
        # Opt-in (AGNES_BOOTSTRAP_MARKETPLACE=1): install the user's marketplace
        # plugins into this project so setting_sources surfaces them. After the
        # CLI install (needs the `agnes` binary); before the reader attaches for
        # the same fd-0 reason as the install.
        if os.environ.get("AGNES_BOOTSTRAP_MARKETPLACE") == "1":
            _bootstrap_marketplace(str(workdir))

    _emit({"type": "runner_ready"})
    queue = await _stdin_lines()

    per_tool = float(os.environ.get("AGNES_PER_TOOL_CALL_SECONDS", "90"))
    tool_calls_per_turn = int(os.environ.get("AGNES_TOOL_CALLS_PER_TURN", "50"))
    if fake_agent:
        await _fake_agent_loop(
            queue,
            per_tool_seconds=per_tool,
            tool_calls_per_turn=tool_calls_per_turn,
        )
    else:
        try:
            await _real_agent_loop(
                queue,
                workdir,
                tool_calls_per_turn=tool_calls_per_turn,
            )
        except Exception as exc:
            _emit({"type": "error", "kind": "runner_exception", "message": str(exc)})
            raise


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
