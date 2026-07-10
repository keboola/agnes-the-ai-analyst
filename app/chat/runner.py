"""In-subprocess entrypoint. Runs claude-agent-sdk inside the chat sandbox.

Stdin: JSON lines, one per frame. Inbound types: user_msg, cancel.
Stdout: JSON lines. Outbound types: runner_ready, token, tool_call,
        tool_result, assistant_message, error, done.

Env (set by ChatManager via the sandbox provider — under v1 the
E2BProvider passes these through ``AsyncSandbox.create(envs=...)``):
- AGNES_SESSION_ID, AGNES_USER_EMAIL, AGNES_SERVER, AGNES_TOKEN
- AGNES_DAILY_BUDGET_USD, AGNES_PER_TOOL_CALL_SECONDS
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


def _emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


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

    The stdio server authenticates the same way the CLI does — off
    ``AGNES_SERVER`` + ``AGNES_TOKEN`` (+ ``AGNES_SESSION_ID`` for the
    per-session re-mint path), which ChatManager._spawn_runner already seeds
    into this process's env. We forward them explicitly on the MCP server's
    own ``env`` rather than relying on inheritance, because the SDK spawns the
    server through the ``claude`` CLI and env inheritance across that hop is
    not guaranteed. ``PATH`` is forwarded so the ``agnes`` console script
    (installed by ``_install_agnes_cli`` into /usr/local/bin) resolves.

    Returns ``{}`` when ``AGNES_SERVER``/``AGNES_TOKEN`` are absent (e.g. the
    fake-agent test path or a misconfigured spawn) so the agent still runs
    with its built-in tools rather than failing on a broken MCP handshake.
    """
    server = os.environ.get("AGNES_SERVER", "").strip()
    token = os.environ.get("AGNES_TOKEN", "").strip()
    if not server or not token:
        return {}
    env = {
        "AGNES_SERVER": server,
        "AGNES_TOKEN": token,
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
                await queue.put(json.loads(line))
            except json.JSONDecodeError:
                continue

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
    - connect() once with the first user_msg
    - query() for each subsequent user_msg
    - receive_response() (async-iter) to consume each turn's messages
    - interrupt() for cancel frames

    Message type mapping (SDK → outbound JSON frames):
    - AssistantMessage with TextBlock content → token frames + assistant_message at turn end
    - AssistantMessage with ToolUseBlock content → tool_call frame
    - AssistantMessage with ToolResultBlock content → tool_result frame
    - ResultMessage → assistant_message frame (turn end, carries usage/model)
    """
    from claude_agent_sdk import (  # type: ignore[import-untyped]
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    def _emit_tool_result(block) -> None:
        result = block.content
        if isinstance(result, list):
            result = " ".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in result)
        _emit(
            {
                "type": "tool_result",
                "id": block.tool_use_id,
                "tool": block.tool_use_id,
                "result": result,
            }
        )

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
    async with ClaudeSDKClient(
        options=ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(workdir),
            setting_sources=["user", "project", "local"],
            mcp_servers=mcp_servers,
        )
    ) as client:
        # Flag to track whether we've called connect() yet
        connected = False

        while True:
            frame = await queue.get()
            t = frame.get("type")

            if t == "_eof":
                return

            if t == "cancel":
                client.interrupt()
                continue

            if t != "user_msg":
                continue

            text = frame.get("text", "")

            # First message: connect; subsequent messages: query
            if not connected:
                await client.connect(text)
                connected = True
            else:
                await client.query(text)

            # Consume the response for this turn
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

            async for msg in client.receive_response():
                if budget_hit:
                    break
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
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
                            # ``id`` is the per-call ``tool_use_id`` —
                            # echoed back verbatim by ``ToolResultBlock``
                            # so the frontend can pair a tool_call with
                            # its result even when several calls to the
                            # same tool are in flight. ``tool`` carries
                            # the human-readable name for the inline
                            # block header.
                            _emit(
                                {
                                    "type": "tool_call",
                                    "id": block.id,
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
                    # ResultMessage signals turn end; receive_response() stops after it
                    _emit(
                        {
                            "type": "assistant_message",
                            "content": "".join(collected_text),
                            "tokens_in": tokens_in,
                            "tokens_out": tokens_out,
                            "model": model,
                        }
                    )

            # Turn finished (receive_response() drained, or budget gate broke
            # the loop). Emit a `done` frame so the UI hides the Stop button —
            # without it the composer is wedged in the "running" state because
            # the frontend only clears it on done/error/cancelled.
            _emit({"type": "done"})


async def amain() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()

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
        _install_agnes_cli()
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
