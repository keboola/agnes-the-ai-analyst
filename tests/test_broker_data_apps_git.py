"""Git traffic from a chat sandbox to a hosted app's repo.

Before this route the authoring flow had no transport at all, and every layer
said so in a different voice:

- the sandbox reaches Agnes only through the in-sandbox relay (`runner.py`
  rewrites `AGNES_SERVER` to it so "the relay is the only thing that ever holds
  a real credential"), and the relay's `data_apps` ticket is confined by
  `_within_data_apps_prefix` to `/api/data-apps*`;
- a repo lives at `/data-apps.git/<slug>` — a different top-level prefix, so
  the broker refused it;
- going direct was refused by the sandbox's own egress hook, whose allowlist
  cannot name a per-deployment host because the hook ships verbatim.

Watched live on a running instance: the agent fetched a git credential, then
failed to clone by name, by hostname and by IP, including one attempt with the
sandbox bypass. `data_app_git_credential` was handing out a URL that could not
be used from where the agent runs.

This route is the transport, and it differs from its `{method, path, body}`
siblings in two ways that the tests below pin: it PROXIES (git speaks binary
bodies, its own content types, and opens with a GET), and it attaches a
CREDENTIAL rather than an identity JWT, because the git surface authenticates
only a `data-app-git:<slug>` PAT in basic auth.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BROKER = Path("app/api/broker.py")
RELAY = Path("app/chat/relay.py")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _route_body() -> str:
    src = _read(BROKER)
    start = src.index("async def data_apps_git_broker(")
    return src[start : src.index("\n# Registered as two distinct routes", start)]


# ── the transport exists at all ─────────────────────────────────────────────


def test_the_relay_carries_the_git_prefix_on_the_data_apps_ticket():
    src = _read(RELAY)
    assert '"/data-apps.git": "data_apps"' in src, "the relay must know the prefix and its scope"


def test_git_is_not_an_envelope_route():
    """The `{method, path, body}` envelope is JSON-only. Git's bodies are
    binary pack streams — routing it through the envelope would corrupt them
    (or silently drop them, since the envelope json-decodes and gives up)."""
    src = _read(RELAY)
    envelope = re.search(r"_ENVELOPE_PREFIXES = frozenset\(\{(.*?)\}\)", src, re.DOTALL)
    assert envelope, "_ENVELOPE_PREFIXES moved — re-point this guard"
    assert "data-apps.git" not in envelope.group(1)


def test_the_transparent_leg_passes_the_method_through():
    """Git's first call is `GET /info/refs?service=git-upload-pack`. The
    transparent branch was hardcoded to POST, which turned that into a 405
    before the repo was ever reached."""
    src = _read(RELAY)
    assert "client.request(method.upper(), url, content=body, headers=headers)" in src
    assert "return await client.post(url, content=body, headers=headers)" not in src


def test_the_broker_route_accepts_the_methods_git_uses():
    """Registered per verb, not as one `methods=["GET","POST"]` route: FastAPI
    derives one operation_id per function+path and warns about the duplicate
    otherwise — the same reason `data_apps_git.py` splits its own pair."""
    src = _read(BROKER)
    for method, op in (("GET", "broker_data_apps_git_get"), ("POST", "broker_data_apps_git_post")):
        assert f'methods=["{method}"]' in src and f'operation_id="{op}"' in src


# ── authorization ───────────────────────────────────────────────────────────


def test_the_route_requires_the_data_apps_scope():
    assert '_require_scope(row, "data_apps")' in _route_body()


def test_the_slug_is_pinned_to_an_app_the_ticket_owner_may_reach():
    """Without this a `data_apps` ticket reaches ANY app's repo — and this
    route can push, so it would be a write to someone else's code."""
    body = _route_body()
    assert "data_apps_repo().get_by_slug" in body
    assert 'app_row.get("owner_user_id") != user["id"]' in body
    assert "is_user_admin" in body
    assert '"forbidden"' in body


def test_a_rejected_slug_is_audited():
    assert "broker_data_apps_git_rejected" in _route_body()


def test_co_sessions_and_scoped_agents_are_refused():
    """A co-session has no single owner to attribute a commit to, and an agent
    session's authority is owner-grants ∩ agent-scope, which says nothing about
    repository access. Both fail closed rather than falling through to the
    owner — the exact mistake `_mint_identity_jwt` documents for its own
    co-session branch."""
    src = _read(BROKER)
    helper = src[src.index("def _ticket_owner_for_git(") : src.index("async def data_apps_git_broker(")]
    assert '"git_not_available_to_co_session"' in helper
    assert '"git_not_available_to_scoped_agent"' in helper
    assert "agent_is_passthrough(agent)" in helper, "a passthrough agent is the ordinary web-chat case"


# ── the credential ──────────────────────────────────────────────────────────


def test_the_credential_is_attached_by_the_broker_not_held_by_the_sandbox():
    """The whole point of the relay is that the sandbox never holds a real
    credential. This route mints one on the server side and puts it in basic
    auth, which is the only thing the git surface authenticates.

    That the credential actually *works* is proved by
    `test_owner_reaches_the_git_surface_through_the_broker` below — the git
    surface accepts nothing else, so a 200 from it is the real assertion.
    This one only pins the shape."""
    body = _route_body()
    assert "mint_git_token" in body
    assert 'base64.b64encode(f"agnes:{token}".encode())' in body
    assert '"Authorization": f"Basic {basic}"' in body


def test_the_per_request_credential_is_revoked_even_on_failure():
    """The happy path is covered behaviourally by
    `test_the_per_request_credential_does_not_outlive_the_call`; what a test
    cannot easily provoke is an upstream that raises, so the ordering is
    pinned here instead."""
    body = _route_body()
    assert "finally:" in body
    assert body.index("finally:") < body.index("access_token_repo().revoke")


def test_the_route_keeps_its_database_work_off_the_event_loop():
    """`async def` handler + synchronous repo calls = the single uvicorn loop
    blocked for every other user while a sandbox clones. The module already
    treats this as a known hazard (`require_broker_ticket` is a plain `def`
    for exactly this reason), and this route does several reads and two
    writes (Devin Review on this PR)."""
    body = _route_body()
    for call in (
        "run_in_threadpool(_ticket_owner_for_git",
        "run_in_threadpool(data_apps_repo().get_by_slug",
        "run_in_threadpool(is_user_admin",
        "run_in_threadpool(mint_git_token",
        "run_in_threadpool(access_token_repo().revoke",
    ):
        assert call in body, f"missing: {call} — that repo call runs inline on the event loop"


def test_the_token_is_minted_for_the_requesting_user():
    """An admin proxying for someone else's app would otherwise have the push
    attributed to the owner in `REMOTE_USER` — the same "nobody to attribute a
    commit to" problem that makes co-sessions a hard refusal."""
    assert "mint_git_token, app_row, user" in _route_body()


def test_the_query_string_survives():
    """`?service=git-upload-pack` is how smart-HTTP negotiates; dropping it
    turns discovery into a dumb-protocol request against a repo that does not
    serve one."""
    assert "request.url.query" in _route_body()


def test_the_minter_is_reusable_and_returns_the_token_id():
    """`_mint_git_credential` returns a URL, which this route cannot use — it
    needs the raw token plus the id to revoke. Both callers now share one
    minter so the scope and TTL cannot drift apart."""
    src = _read(Path("app/api/data_apps.py"))
    assert "def mint_git_token(row: dict, as_user: dict | None = None) -> tuple[str, str]:" in src
    cred = src[src.index("def _mint_git_credential(") : src.index("# Cookie carrying a")]
    assert "mint_git_token(row)" in cred, "the URL builder must delegate, not duplicate the mint"


SKILL = Path("app/initial_workspace_default/.claude/skills/agnes-data-apps-extras/SKILL.md")


def test_the_skill_sends_the_agent_through_the_relay():
    """A transport nobody is told about is not a transport. The skill has to
    name the relay URL AND warn off `data_app_git_credential`'s URL, which is
    the one an agent reaches for and the one that cannot work from a sandbox."""
    body = SKILL.read_text(encoding="utf-8")
    assert "/data-apps.git/<slug>" in body
    assert "127.0.0.1" in body, "the loopback relay is the reachable origin"
    assert "data_app_git_credential" in body
    warn = body[body.index("data_app_git_credential") - 400 : body.index("data_app_git_credential") + 400]
    assert "not" in warn.lower(), "the credential URL has to be warned off, not merely mentioned"


def test_the_skill_orders_clone_before_scaffold():
    """Watched live: the skill said to copy the scaffold "into the app's
    managed repo" *before* it said how to obtain that repo, so the run never
    cloned — it read the clone URL as a directory and committed the session
    workspace instead. Order is the fix, so order is what this pins."""
    body = SKILL.read_text(encoding="utf-8")
    assert body.index("git clone") < body.index("/work/scaffolds/nodejs-dashboard/"), (
        "the clone step must come before the scaffold copy that depends on it"
    )


def test_the_skill_names_the_two_errors_a_missing_push_produces():
    """`parent_has_no_main` and `dev_requires_draft` are one missing push seen
    twice; a run that does not know that retries them as separate bugs."""
    body = SKILL.read_text(encoding="utf-8")
    assert "parent_has_no_main" in body
    assert "dev_requires_draft" in body


# --- Behavioural coverage -------------------------------------------------
#
# The guards above assert on the *text* of broker.py/relay.py, which will keep
# passing if behaviour regresses while the source reads the same (Devin Review
# on this PR). These drive the route itself.


@pytest.fixture
def git_broker_env(e2e_env):
    """A real app repo + owner + non-owner, each with a `data_apps` ticket.

    Drives the real `git http-backend` CGI, so it needs the binary; without
    it the route would 500 for environmental rather than behavioural reasons
    (Devin Review).
    """
    import hashlib
    import shutil
    import uuid

    if shutil.which("git") is None:
        pytest.skip("git binary not available")

    import yaml
    from fastapi.testclient import TestClient

    import app.instance_config as instance_config
    from app.chat.types import Surface
    from app.main import create_app
    from src.data_apps.git_repos import init_app_repo
    from src.db import get_system_db
    from src.repositories import chat_session_repo, ticket_repo
    from src.repositories.data_apps import DataAppsRepository
    from src.repositories.users import UserRepository

    state = e2e_env["data_dir"] / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True}}))
    instance_config._instance_config = None

    conn = get_system_db()
    users = UserRepository(conn)
    users.create(id="gbowner", email="gbowner@test.local", name="Owner")
    users.create(id="gbother", email="gbother@test.local", name="Other")
    DataAppsRepository(conn).create(slug="gbapp", name="GB App", owner_user_id="gbowner")
    conn.close()
    init_app_repo("gbapp")

    owner_sess = chat_session_repo().create_session(user_email="gbowner@test.local", surface=Surface.WEB)
    other_sess = chat_session_repo().create_session(user_email="gbother@test.local", surface=Surface.WEB)
    return {
        "client": TestClient(create_app()),
        "owner_ticket": ticket_repo().mint(owner_sess.id, "data_apps"),
        "other_ticket": ticket_repo().mint(other_sess.id, "data_apps"),
        "main_ticket": ticket_repo().mint(owner_sess.id, "main"),
        "hashlib": hashlib,
        "uuid": uuid,
    }


_REFS = "info/refs?service=git-upload-pack"


def test_owner_reaches_the_git_surface_through_the_broker(git_broker_env):
    """The whole point: a sandbox ticket, no credential of its own, gets the
    packfile advertisement back from the app's real repo."""
    env = git_broker_env
    r = env["client"].get(
        f"/api/broker/data-apps.git/gbapp/{_REFS}",
        headers={"Authorization": f"Bearer {env['owner_ticket']}"},
    )
    assert r.status_code == 200, r.text
    # The query string has to survive the hop — git refuses a bare /info/refs.
    assert b"service=git-upload-pack" in r.content


def test_non_owner_is_refused(git_broker_env):
    env = git_broker_env
    r = env["client"].get(
        f"/api/broker/data-apps.git/gbapp/{_REFS}",
        headers={"Authorization": f"Bearer {env['other_ticket']}"},
    )
    assert r.status_code == 403, r.text


def test_wrong_scope_ticket_cannot_authenticate(git_broker_env):
    env = git_broker_env
    r = env["client"].get(
        f"/api/broker/data-apps.git/gbapp/{_REFS}",
        headers={"Authorization": f"Bearer {env['main_ticket']}"},
    )
    assert r.status_code == 401, r.text


def test_dot_dot_in_the_path_is_refused(git_broker_env):
    """Confinement to the `/data-apps.git/<slug>` prefix, the property the
    sibling route's `_normalize_broker_path` exists to guarantee."""
    env = git_broker_env
    r = env["client"].get(
        "/api/broker/data-apps.git/gbapp/../../api/admin/users",
        headers={"Authorization": f"Bearer {env['owner_ticket']}"},
    )
    assert r.status_code in (400, 404), r.text
    assert r.status_code != 200


def test_unknown_slug_is_404(git_broker_env):
    env = git_broker_env
    r = env["client"].get(
        f"/api/broker/data-apps.git/nosuchapp/{_REFS}",
        headers={"Authorization": f"Bearer {env['owner_ticket']}"},
    )
    assert r.status_code == 404, r.text


def test_the_per_request_credential_does_not_outlive_the_call(git_broker_env):
    """A per-request token that survives the request is a credential leak —
    the same class as the container tokens that piled up unrevoked."""
    from src.db import get_system_db

    env = git_broker_env
    r = env["client"].get(
        f"/api/broker/data-apps.git/gbapp/{_REFS}",
        headers={"Authorization": f"Bearer {env['owner_ticket']}"},
    )
    assert r.status_code == 200, r.text

    conn = get_system_db()
    try:
        live = conn.execute(
            "SELECT COUNT(*) FROM personal_access_tokens WHERE name = 'data-app-git:gbapp' AND revoked_at IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert live == 0, f"{live} per-request git token(s) left live after the call"


def test_git_protocol_and_encoding_headers_reach_the_git_surface():
    """Carrying only Content-Type silently downgraded protocol v2 on the
    brokered path but not on the direct one — the git surface reads
    `git-protocol` off the request — and described a gzipped body as
    uncompressed (Devin Review on this PR)."""
    body = _route_body()
    for name in ("content-type", "content-encoding", "git-protocol"):
        assert f'"{name}"' in body, f"{name} is dropped on the way to the git surface"
