"""F.9 — sub-agent dispatch via the claude-agent-sdk `Task` tool.

This exercises the marketplace-plugin path: a user asks the agent to
"spawn the X agent" and the runner emits a ``Task`` tool_call frame
rather than ``Bash``. We assert two things:

  1. A ``Task`` tool_call frame arrived on the WS (the model decided
     to delegate rather than answer directly or shell out).
  2. The audit_log row written by the chat manager's pump loop
     records the dispatch (``action='chat.tool_call'`` with
     ``details->tool = 'Task'``).

Prerequisite: the per-user workspace must ship at least one
``.claude/agents/agnes-*.md`` file. The bundled default workspace
under ``app/initial_workspace_default/`` does not currently include
agents — that's tracked in the cloud-chat plan as a follow-up. If the
container hasn't been hydrated with agents, the test skips with a
hint pointing at where they need to land.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e._helpers import (
    E2E_USER_EMAIL,
    E2E_USER_PASSWORD,
    bootstrap_admin,
    docker_exec,
    pump_until,
)


pytestmark = pytest.mark.real_llm


try:
    from websockets.sync.client import connect as ws_connect

    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover
    ws_connect = None  # type: ignore[assignment]
    _WS_AVAILABLE = False


_USER_AGENTS_DIR = "/data/users/e2e@agnes.local/workspace/.claude/agents"


def _discover_one_agent_name() -> str | None:
    """Return the slug of an installed ``.claude/agents/agnes-*.md`` file.

    The agent dispatch test needs a real subagent to address by name —
    bundling a stub here would be out of scope (it would mean
    modifying ``app/initial_workspace_default/``, which other waves
    own). Instead we discover what's already installed in the test
    container and skip if there's nothing.
    """
    proc = docker_exec(
        [
            "sh", "-c",
            f"ls {_USER_AGENTS_DIR}/agnes-*.md 2>/dev/null | head -n 1 || true",
        ],
        timeout=10.0,
    )
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", "replace").strip()
    if not out:
        return None
    # `/data/.../agents/agnes-reviewer-architecture.md` → slug
    fname = out.rsplit("/", 1)[-1]
    if fname.endswith(".md"):
        fname = fname[:-3]
    return fname


@pytest.fixture(scope="module")
def admin_client(docker_e2e_agnes: str):
    return bootstrap_admin(
        docker_e2e_agnes, email=E2E_USER_EMAIL, password=E2E_USER_PASSWORD,
    )


@pytest.fixture(scope="module")
def installed_agent_slug(docker_e2e_agnes: str, admin_client) -> str:
    """Trigger workdir init (any session does this), then probe for agents.

    The user's ``.claude/agents/`` dir doesn't exist until the workdir
    has been initialized. We do a no-op chat session here to ensure
    the bundled template was unpacked, then look for an agent slug.
    Skips the whole module if none are installed.
    """
    # Spin up a throw-away session to force workdir init.
    sess = admin_client.create_chat_session(surface="web")
    ws_url = admin_client.ws_url_for(sess)
    if not _WS_AVAILABLE:
        pytest.skip("websockets.sync.client unavailable — old python?")
    with ws_connect(ws_url, open_timeout=15) as ws:
        pump_until(
            ws, predicate=lambda f: f.get("type") in ("ready", "runner_ready"),
        )
    admin_client.delete(f"/api/chat/sessions/{sess['id']}")

    slug = _discover_one_agent_name()
    if not slug:
        pytest.skip(
            "F.9 needs at least one `.claude/agents/agnes-*.md` in the "
            "bundled workspace template. None found under "
            f"{_USER_AGENTS_DIR} — extend "
            "app/initial_workspace_default/ before running F.9."
        )
    return slug


def test_f9_task_tool_dispatch_to_named_subagent(
    docker_e2e_agnes: str, admin_client, installed_agent_slug: str,
) -> None:
    """End-to-end: prompt names an agent → runner emits a Task tool_call."""
    if not _WS_AVAILABLE:
        pytest.skip("websockets.sync.client unavailable — old python?")

    session = admin_client.create_chat_session(surface="web")
    ws_url = admin_client.ws_url_for(session)

    saw_task_call = False
    final = ""

    with ws_connect(ws_url, open_timeout=15) as ws:
        pump_until(ws, predicate=lambda f: f.get("type") in ("ready", "runner_ready"))
        prompt = (
            f"Use the Task tool to dispatch a subagent named "
            f"'{installed_agent_slug}'. Ask it to summarise its role in one "
            "sentence and return that summary as the final answer."
        )
        ws.send(json.dumps({"type": "user_msg", "text": prompt}))
        for _ in range(400):
            try:
                raw = ws.recv(timeout=120.0)
            except TimeoutError:
                break
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = frame.get("type")
            if t == "tool_call" and (frame.get("tool") or "").lower() == "task":
                saw_task_call = True
            elif t == "assistant_message":
                content = (frame.get("content") or "").strip()
                if content:
                    final = content
                    break

    assert saw_task_call, (
        "expected at least one `Task` tool_call frame after asking the "
        f"agent to dispatch the {installed_agent_slug!r} subagent. The model "
        "may have answered directly without delegating — check the chat "
        "transcript in the audit log."
    )
    assert final, "no assistant_message received after the Task dispatch turn"

    # Audit row: the manager pump writes ``action='chat.tool_call'`` with
    # ``details.tool = 'Task'`` for each Task frame it sees.
    snippet = (
        "import duckdb, json;"
        "c=duckdb.connect('/data/state/system.duckdb', read_only=True);"
        "rows=c.execute("
        "  \"SELECT details FROM audit_log WHERE action='chat.tool_call' \""
        f"  \"AND details LIKE '%{session['id']}%' AND details LIKE '%\\\"Task\\\"%'\""
        ").fetchall();"
        "print(len(rows))"
    )
    proc = docker_exec(["/opt/venv/bin/python", "-c", snippet], timeout=30.0)
    assert proc.returncode == 0, (
        f"audit query failed: {proc.stderr.decode('utf-8', 'replace')!r}"
    )
    count = int(proc.stdout.decode("utf-8", "replace").strip() or "0")
    assert count > 0, (
        "expected at least one Task-tool audit_log row for session "
        f"{session['id']}; got {count}"
    )
