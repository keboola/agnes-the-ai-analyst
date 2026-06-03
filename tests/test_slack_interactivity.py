"""Tests for Slack Block Kit interactivity (Phase 3)."""
import asyncio


def test_schedule_keeps_strong_ref_until_done():
    from services.slack_bot import events as ev

    ran = []
    async def work():
        ran.append(True)

    async def _drive():
        ev._schedule(work())
        # Give the scheduled task a turn to run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_drive())
    assert ran == [True]


def test_run_logged_swallows_exceptions():
    from services.slack_bot import events as ev

    async def boom():
        raise ValueError("kaboom")

    # Must NOT raise — _run_logged is the only recovery path post-ack.
    asyncio.run(ev._run_logged(boom()))


class _FakeResp:
    def __init__(self, data): self._data = data
    def json(self): return self._data


class _FakeClient:
    """Captures (url, json) of each post; returns canned ts for postMessage."""
    def __init__(self, *a, **k): self.calls = []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, headers=None, json=None):
        self.calls.append((url, json))
        return _FakeResp({"ok": True, "ts": "9.9"})


def _patch_client(monkeypatch):
    captured = {}
    def factory(*a, **k):
        captured["client"] = _FakeClient()
        return captured["client"]
    import services.slack_bot.sender as snd
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(snd.httpx, "AsyncClient", factory)
    return captured


def test_send_ephemeral_posts_to_response_url(monkeypatch):
    import services.slack_bot.sender as snd
    cap = _patch_client(monkeypatch)
    asyncio.run(snd.send_ephemeral("https://hooks.example/r", "nope"))
    url, body = cap["client"].calls[0]
    assert url == "https://hooks.example/r"
    assert body["response_type"] == "ephemeral"
    assert body["text"] == "nope"


def test_is_channel_allowlisted_default_deny_and_grant(tmp_path):
    import duckdb
    from services.slack_bot.binding import is_channel_allowlisted
    conn = duckdb.connect()
    # Real schema: resource_grants uses group_id FK to user_groups.id
    conn.execute(
        "CREATE TABLE user_groups ("
        " id VARCHAR PRIMARY KEY, name VARCHAR, is_system BOOLEAN)"
    )
    conn.execute(
        "CREATE TABLE resource_grants ("
        " id VARCHAR, group_id VARCHAR, resource_type VARCHAR, resource_id VARCHAR)"
    )
    conn.execute("INSERT INTO user_groups VALUES ('g1', 'Everyone', TRUE)")
    # default-deny
    assert is_channel_allowlisted(conn, "C1") is False
    # Everyone grant flips it on
    conn.execute(
        "INSERT INTO resource_grants VALUES ('r1', 'g1', 'slack_channel', 'C1')"
    )
    assert is_channel_allowlisted(conn, "C1") is True
    # other channels stay denied
    assert is_channel_allowlisted(conn, "C2") is False


def test_soft_archive_dm_kills_and_archives_existing(monkeypatch):
    from types import SimpleNamespace
    from services.slack_bot import commands as cmd

    killed, archived = [], []
    async def kill(sid, reason=None): killed.append(sid)
    mgr = SimpleNamespace(kill=kill)
    repo = SimpleNamespace(
        get_slack_dm_session=lambda channel: SimpleNamespace(id="s1"),
        archive_session=lambda sid: archived.append(sid),
    )
    app = SimpleNamespace(state=SimpleNamespace(chat_manager=mgr, chat_repo=repo))
    # The existing _soft_archive_dm(app, slack_user_id) resolves the IM channel via open_im.
    # We patch open_im so it returns a channel id.
    import services.slack_bot.commands as cmd_mod
    async def fake_open_im(uid): return "D1"
    monkeypatch.setattr(cmd_mod, "open_im", fake_open_im)
    asyncio.run(cmd._soft_archive_dm(app, "U1"))
    assert killed == ["s1"]
    assert archived == ["s1"]


def test_soft_archive_dm_noop_when_no_session(monkeypatch):
    from types import SimpleNamespace
    from services.slack_bot import commands as cmd

    killed, archived = [], []
    async def kill(sid, reason=None): killed.append(sid)
    mgr = SimpleNamespace(kill=kill)
    repo = SimpleNamespace(
        get_slack_dm_session=lambda channel: None,
        archive_session=lambda sid: archived.append(sid),
    )
    app = SimpleNamespace(state=SimpleNamespace(chat_manager=mgr, chat_repo=repo))
    import services.slack_bot.commands as cmd_mod
    async def fake_open_im(uid): return "D1"
    monkeypatch.setattr(cmd_mod, "open_im", fake_open_im)
    asyncio.run(cmd._soft_archive_dm(app, "U1"))
    assert killed == [] and archived == []
