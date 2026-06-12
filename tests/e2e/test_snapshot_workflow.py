"""F.7 + F.8 — snapshot create / estimate / cross-session reuse.

These exercise the persistent ``workspace/snapshots/`` shared dir that
the cloud-chat spec calls out as a first-class user asset (it's even
on the destructive-path refuse list in the bundled PreToolUse hook).

F.7 — estimate, then create:
  1. Prompt the agent for a scan-size estimate.
  2. Prompt for an actual snapshot creation, name it ``region_a_recent``.
  3. Verify the on-disk artifact under
     ``/data/users/<user>/workspace/snapshots/`` (the durable location
     symlinked into every session_dir by ``prepare_session_dir``).

F.8 — cross-session reuse:
  1. Archive the F.7 session.
  2. Open a NEW chat session for the SAME user.
  3. Ask "what snapshots do I have?"; expect ``agnes snapshot list``;
     expect ``region_a_recent`` in the reply.

Both depend on the real_llm + docker stack — no fake-agent
substitute, because the test asserts the LLM picks the right CLI
sub-command.

NOTE on the snapshot location:

The exact filesystem layout depends on whether ``AGNES_LOCAL_DIR`` was
set inside the runner env. The plan documents
``/data/users/<user>/workspace/snapshots/<name>.duckdb``; the
production CLI default is ``<cwd>/user/snapshots/<name>.duckdb``. To
keep the test robust to that wiring choice, the assertion walks both
locations under the workspace tree and accepts the first match.
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
    skip_unless_chat_sessions_possible,
)


pytestmark = pytest.mark.real_llm


try:
    from websockets.sync.client import connect as ws_connect

    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover
    ws_connect = None  # type: ignore[assignment]
    _WS_AVAILABLE = False


_SNAPSHOT_NAME = "region_a_recent"
_USER_ROOT = "/data/users/e2e@agnes.local"


def _find_snapshot_artifact(name: str) -> str | None:
    """Search the per-user workspace tree for ``<name>.duckdb``.

    Returns the absolute container path on first match, or None.
    Tries the spec's canonical location first, then the CLI default.
    """
    candidates = (
        f"{_USER_ROOT}/workspace/snapshots/{name}.duckdb",
        f"{_USER_ROOT}/workspace/user/snapshots/{name}.duckdb",
    )
    for path in candidates:
        proc = docker_exec(["test", "-f", path], timeout=10.0)
        if proc.returncode == 0:
            return path
    # Last resort: do a recursive find under the user root in case the
    # CLI grew a new layout. Captures the path so the assertion error
    # can point the operator at the actual location.
    find = docker_exec(
        ["find", _USER_ROOT, "-name", f"{name}.duckdb", "-print"],
        timeout=20.0,
    )
    out = find.stdout.decode("utf-8", "replace").strip()
    return out.splitlines()[0] if out else None


@pytest.fixture(scope="module")
def admin_client(docker_e2e_agnes: str):
    return bootstrap_admin(
        docker_e2e_agnes, email=E2E_USER_EMAIL, password=E2E_USER_PASSWORD,
    )


def _pump_one_turn(ws, *, max_frames: int = 400, timeout: float = 120.0):
    """Drive one prompt → assistant_message turn.

    Returns ``(assistant_text, bash_commands)`` — the final assistant
    reply text plus every ``Bash`` tool_call's ``command`` arg seen
    during the turn. Tests grep over the commands list to assert the
    LLM picked the right `agnes` sub-command.
    """
    bash_cmds: list[str] = []
    final = ""
    for _ in range(max_frames):
        try:
            raw = ws.recv(timeout=timeout)
        except TimeoutError:
            break
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = frame.get("type")
        if t == "tool_call" and (frame.get("tool") or "").lower() == "bash":
            cmd = (frame.get("args") or {}).get("command") or ""
            if cmd:
                bash_cmds.append(cmd)
        elif t == "assistant_message":
            content = (frame.get("content") or "").strip()
            if content:
                final = content
                break
    if not final:
        raise AssertionError(
            f"no assistant_message after {max_frames} frames; "
            f"bash commands seen: {bash_cmds!r}"
        )
    return final, bash_cmds


def test_f7_snapshot_estimate_then_create_writes_artifact(
    docker_e2e_agnes: str, admin_client,
) -> None:
    """End-to-end F.7: estimate first, then materialize a named snapshot."""
    if not _WS_AVAILABLE:
        pytest.skip("websockets.sync.client unavailable — old python?")

    skip_unless_chat_sessions_possible()
    session = admin_client.create_chat_session(surface="web")
    ws_url = admin_client.ws_url_for(session)

    with ws_connect(ws_url, open_timeout=15) as ws:
        pump_until(ws, predicate=lambda f: f.get("type") in ("ready", "runner_ready"))

        # Turn 1 — estimate only
        ws.send(json.dumps({
            "type": "user_msg",
            "text": (
                "Estimate the scan size for a snapshot of the sales table "
                "filtered to region='A'."
            ),
        }))
        estimate_reply, estimate_cmds = _pump_one_turn(ws)
        assert any("--estimate" in c for c in estimate_cmds), (
            f"expected `--estimate`; got cmds: {estimate_cmds!r}"
        )

        # Turn 2 — create the snapshot
        ws.send(json.dumps({
            "type": "user_msg",
            "text": (
                f"Now actually create that snapshot of sales filtered to "
                f"region='A', and name it '{_SNAPSHOT_NAME}'."
            ),
        }))
        create_reply, create_cmds = _pump_one_turn(ws)

    assert any(
        "agnes snapshot create" in c and _SNAPSHOT_NAME in c
        for c in create_cmds
    ), (
        f"expected `agnes snapshot create ... --as {_SNAPSHOT_NAME}`; "
        f"got: {create_cmds!r}"
    )

    artifact = _find_snapshot_artifact(_SNAPSHOT_NAME)
    assert artifact, (
        f"expected snapshot artifact {_SNAPSHOT_NAME}.duckdb under "
        f"{_USER_ROOT}; the LLM may have run the CLI but the file isn't "
        "where we looked. assistant reply was: " + create_reply
    )


def test_f8_snapshot_persists_across_chat_sessions(
    docker_e2e_agnes: str, admin_client,
) -> None:
    """End-to-end F.8: snapshot from a previous session is discoverable.

    This is the per-user persistent workspace claim from the spec —
    the snapshot lives under ``workspace/snapshots/``, which is shared
    across all of a user's sessions via the symlinks set up by
    ``prepare_session_dir``.
    """
    if not _WS_AVAILABLE:
        pytest.skip("websockets.sync.client unavailable — old python?")

    # F.7 must have created the artifact for F.8 to mean anything.
    # We don't re-run F.7 here (pytest would re-execute it in order
    # anyway), but we do double-check the file landed.
    if not _find_snapshot_artifact(_SNAPSHOT_NAME):
        pytest.skip(
            f"prerequisite: F.7 must have created the {_SNAPSHOT_NAME!r} "
            "snapshot before F.8 can verify cross-session visibility"
        )

    # Archive any prior live session, then open a fresh one so the
    # runner subprocess starts clean.
    for sess in admin_client.get("/api/chat/sessions")[1]:
        admin_client.delete(f"/api/chat/sessions/{sess['id']}")

    skip_unless_chat_sessions_possible()
    session = admin_client.create_chat_session(surface="web")
    ws_url = admin_client.ws_url_for(session)

    with ws_connect(ws_url, open_timeout=15) as ws:
        pump_until(ws, predicate=lambda f: f.get("type") in ("ready", "runner_ready"))
        ws.send(json.dumps({
            "type": "user_msg",
            "text": "What snapshots do I currently have available?",
        }))
        reply, cmds = _pump_one_turn(ws)

    assert any("agnes snapshot list" in c for c in cmds), (
        f"expected `agnes snapshot list`; got cmds: {cmds!r}"
    )
    assert _SNAPSHOT_NAME in reply, (
        f"expected the previously-created snapshot {_SNAPSHOT_NAME!r} in the "
        f"reply; got: {reply!r}"
    )
