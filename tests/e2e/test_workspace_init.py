"""F.1 — cold-start workspace creation + `agnes init` on first chat.

The cloud-chat manager calls ``WorkdirManager.ensure_user_workdir(user_email)``
the first time ``attach`` runs for a session, which lays down the
bundled workspace template under
``/data/users/<safe_email>/workspace/`` and writes the
``.claude/init-complete`` sentinel. The second session for the same
user must skip the heavy init path (the sentinel + DB row already
exist) and respawn quickly.

We use ``AGNES_E2E_FAKE_AGENT=1`` here so this test doesn't need an
Anthropic key — it exercises the *file-system* side-effects of the
attach path, not the LLM. (The fake-agent runner echoes back so we
still get an ``assistant_message`` to know the runner is alive.)

The test connects to the docker-compose E2E env from Phase E:
without ``AGNES_E2E=1`` the ``docker_e2e_agnes`` fixture skips and
this test cascades.
"""

from __future__ import annotations

import os
import time

import pytest

from tests.e2e._helpers import (
    E2E_USER_EMAIL,
    E2E_USER_PASSWORD,
    assert_container_path_exists,
    bootstrap_admin,
    docker_exec,
    pump_until,
    skip_unless_chat_sessions_possible,
)


try:
    # `websockets.sync.client.connect` is the synchronous client added in
    # websockets 12+. The project requires Python 3.11+, so this import
    # is safe in the docker container; on dev macOS this whole module
    # never runs because docker_e2e_agnes skips first.
    from websockets.sync.client import connect as ws_connect

    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover — only reached on very old envs
    ws_connect = None  # type: ignore[assignment]
    _WS_AVAILABLE = False


# Workspace artifacts asserted by Step 6 in the F.1 plan. Each must
# exist after the very first user_msg → assistant_message round-trip.
_REQUIRED_WORKSPACE_FILES = (
    # _safe_email_dir lowercases + replaces non-[a-z0-9_-.@] with '_'.
    # E2E_USER_EMAIL = "e2e@agnes.local" → "e2e@agnes.local" (all safe).
    "/data/users/e2e@agnes.local/workspace/.claude/init-complete",
    "/data/users/e2e@agnes.local/workspace/.claude/settings.json",
    "/data/users/e2e@agnes.local/workspace/.claude/hooks/pre_tool_use.py",
)


def _force_fake_agent_or_skip() -> None:
    """F.1 requires the fake-agent runner — assert the host has set it.

    The runner mode is forwarded into the container via the
    ``AGNES_RUNNER_FAKE_AGENT`` env in docker-compose.e2e.yml
    (``${AGNES_E2E_FAKE_AGENT:-}``). If the operator launched the
    stack without that, F.1 would burn a real Anthropic call for a
    test that doesn't need one — skip with a clear hint.
    """
    if not os.environ.get("AGNES_E2E_FAKE_AGENT"):
        pytest.skip(
            "F.1 requires the fake-agent runner — re-run with "
            "AGNES_E2E_FAKE_AGENT=1 alongside AGNES_E2E=1"
        )
    skip_unless_chat_sessions_possible()


@pytest.fixture(scope="module")
def admin_client(docker_e2e_agnes: str):
    """Bootstrap an admin user once per module via /auth/bootstrap.

    Module-scoped so F.1's two sub-scenarios (first-attach vs second
    user_msg replay) share the same auth state — the second send must
    hit the "workspace already exists" branch, which requires the user
    row to persist between WS connects.
    """
    return bootstrap_admin(
        docker_e2e_agnes, email=E2E_USER_EMAIL, password=E2E_USER_PASSWORD,
    )


def test_cold_start_creates_workspace_then_warm_respawn_is_fast(
    docker_e2e_agnes: str, admin_client,
) -> None:
    """End-to-end: first user_msg builds the workspace; second is fast.

    Step-by-step (mirrors the plan's F.1 sequence):

      1. POST /api/chat/sessions to create a fresh chat session.
      2. WS-connect, wait for ``ready``.
      3. Send ``user_msg: "ping"``; fake-agent echoes ``echo: ping``
         which proves the subprocess spawned and the workdir was
         prepared.
      4. ``docker exec`` to verify the bundled template artifacts
         landed at the expected paths.
      5. Archive that session, create a SECOND one, time the
         attach → ready → assistant_message round-trip; assert it's
         under 30 s (a generous bound that catches a regression where
         we accidentally re-init on every attach).
    """
    _force_fake_agent_or_skip()
    if not _WS_AVAILABLE:
        pytest.skip("websockets.sync.client unavailable — old python?")

    # -- Phase 1: cold-start session -----------------------------------------
    create = admin_client.create_chat_session(surface="web")
    ws_url = admin_client.ws_url_for(create)

    with ws_connect(ws_url, open_timeout=10) as ws:
        first = pump_until(ws, predicate=lambda f: f.get("type") in ("ready", "runner_ready"))
        assert first, "expected at least one ready frame"

        ws.send('{"type":"user_msg","text":"ping"}')
        frames = pump_until(
            ws, predicate=lambda f: f.get("type") == "assistant_message",
        )
        last = frames[-1]
        # Fake-agent echoes "echo: <text>" — the prefix is the contract.
        assert "echo:" in last.get("content", ""), (
            f"expected 'echo:' in fake-agent reply, got: {last!r}"
        )

    # -- Phase 2: verify the bundled template materialized on disk -----------
    for path in _REQUIRED_WORKSPACE_FILES:
        assert_container_path_exists(path)

    # And the settings.json must wire the PreToolUse hook — grepping inside
    # the container avoids needing to ship a copy of the file to the host.
    grep = docker_exec(
        [
            "grep",
            "-q",
            "PreToolUse",
            "/data/users/e2e@agnes.local/workspace/.claude/settings.json",
        ],
    )
    assert grep.returncode == 0, (
        "expected PreToolUse wired in settings.json; "
        f"stderr: {grep.stderr.decode('utf-8', 'replace')!r}"
    )

    # -- Phase 3: warm respawn — should be fast, no re-init ------------------
    # Archive the first session so the manager won't reuse the live entry.
    admin_client.delete(f"/api/chat/sessions/{create['id']}")

    create2 = admin_client.create_chat_session(surface="web")
    ws_url2 = admin_client.ws_url_for(create2)

    t0 = time.monotonic()
    with ws_connect(ws_url2, open_timeout=10) as ws:
        pump_until(ws, predicate=lambda f: f.get("type") in ("ready", "runner_ready"))
        ws.send('{"type":"user_msg","text":"ping2"}')
        pump_until(ws, predicate=lambda f: f.get("type") == "assistant_message")
    elapsed = time.monotonic() - t0

    # First attach involves zip-extract of the bundled template; second
    # only spawns the runner. The bound is generous (30 s) because CI
    # boxes can be slow; what we care about is that we're nowhere near
    # the cold-start tens-of-seconds — typical respawn is < 2 s.
    assert elapsed < 30.0, (
        f"warm respawn took {elapsed:.1f}s — workdir may be re-initializing on every attach"
    )
