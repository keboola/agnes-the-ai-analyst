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


def test_value_codec_roundtrip():
    from services.slack_bot import blocks
    v = blocks.encode_value({"chat_id": "sess-1", "owner": "a@example.com"})
    assert isinstance(v, str)
    assert blocks.decode_value(v) == {"chat_id": "sess-1", "owner": "a@example.com"}


def test_decode_value_rejects_garbage():
    from services.slack_bot import blocks
    assert blocks.decode_value("not-json") == {}
    assert blocks.decode_value("") == {}


def test_stop_button_blocks_shape():
    from services.slack_bot import blocks
    bs = blocks.stop_button_blocks(text="working...", chat_id="sess-1", owner="a@example.com")
    section = next(b for b in bs if b["type"] == "section")
    assert section["text"]["text"] == "working..."
    actions = next(b for b in bs if b["type"] == "actions")
    btn = actions["elements"][0]
    assert btn["action_id"] == blocks.ACTION_STOP
    assert blocks.decode_value(btn["value"]) == {"chat_id": "sess-1", "owner": "a@example.com"}


def test_continue_on_web_block_is_link_only():
    from services.slack_bot import blocks
    block = blocks.continue_on_web_block(web_base="https://host.example", chat_id="sess-1")
    btn = block["elements"][0]
    assert btn["url"] == "https://host.example/chat?session=sess-1"
    # Pure link button: no action_id callback (Slack never POSTs link clicks).
    assert "action_id" not in btn


def test_continue_on_web_block_none_when_no_web_base():
    from services.slack_bot import blocks
    # No public_url configured → no deep-link button rather than a broken URL.
    assert blocks.continue_on_web_block(web_base="", chat_id="sess-1") is None


def test_share_to_channel_blocks_carry_token():
    from services.slack_bot import blocks
    bs = blocks.share_to_channel_blocks(channel_id="C123", token="tok-abc")
    actions = next(b for b in bs if b["type"] == "actions")
    btn = actions["elements"][0]
    assert btn["action_id"] == blocks.ACTION_SHARE_CHANNEL
    assert blocks.decode_value(btn["value"]) == {"channel_id": "C123", "token": "tok-abc"}


def test_new_session_block_carries_owner_and_channel():
    from services.slack_bot import blocks
    block = blocks.new_session_block(channel_id="D1", owner="a@example.com")
    btn = block["elements"][0]
    assert btn["action_id"] == blocks.ACTION_NEW_SESSION
    assert blocks.decode_value(btn["value"]) == {"channel_id": "D1", "owner": "a@example.com"}


def test_post_thread_reply_with_blocks_returns_ts(monkeypatch):
    import services.slack_bot.sender as snd
    cap = _patch_client(monkeypatch)
    ts = asyncio.run(snd.post_thread_reply_with_blocks("C1", "1.1", "hi", [{"type": "x"}]))
    assert ts == "9.9"
    url, body = cap["client"].calls[0]
    assert url.endswith("/chat.postMessage")
    assert body["channel"] == "C1" and body["thread_ts"] == "1.1"
    assert body["blocks"] == [{"type": "x"}] and body["text"] == "hi"


def test_update_message_calls_chat_update(monkeypatch):
    import services.slack_bot.sender as snd
    cap = _patch_client(monkeypatch)
    asyncio.run(snd.update_message("C1", "9.9", "final", []))
    url, body = cap["client"].calls[0]
    assert url.endswith("/chat.update")
    assert body == {"channel": "C1", "ts": "9.9", "text": "final", "blocks": []}


def test_post_channel_message_omits_thread_ts(monkeypatch):
    import services.slack_bot.sender as snd
    cap = _patch_client(monkeypatch)
    asyncio.run(snd.post_channel_message("C1", "public answer"))
    url, body = cap["client"].calls[0]
    assert url.endswith("/chat.postMessage")
    assert body == {"channel": "C1", "text": "public answer"}


def test_respond_via_response_url_posts_body(monkeypatch):
    import services.slack_bot.sender as snd
    cap = _patch_client(monkeypatch)
    asyncio.run(snd.respond_via_response_url("https://hooks.example/r", {"delete_original": True}))
    url, body = cap["client"].calls[0]
    assert url == "https://hooks.example/r"
    assert body == {"delete_original": True}


def _block_actions_payload(action_id, value, *, user="U1", channel="C1", response_url="https://r"):
    return {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": channel},
        "response_url": response_url,
        "actions": [{"action_id": action_id, "value": value}],
    }


def test_parse_interaction_extracts_first_action():
    from services.slack_bot import interactivity as inter, blocks
    payload = _block_actions_payload(blocks.ACTION_STOP, blocks.encode_value({"chat_id": "s1"}))
    it = inter.parse_interaction(payload)
    assert it.action_id == blocks.ACTION_STOP
    assert it.slack_user_id == "U1"
    assert it.channel_id == "C1"
    assert it.response_url == "https://r"
    assert it.value == {"chat_id": "s1"}


def test_parse_interaction_no_actions_yields_empty_action_id():
    from services.slack_bot import interactivity as inter
    it = inter.parse_interaction({"type": "block_actions", "user": {"id": "U1"}, "actions": []})
    assert it.action_id == ""
    assert it.value == {}


def test_dispatch_routes_on_action_id(monkeypatch):
    from services.slack_bot import interactivity as inter, blocks
    seen = []
    async def fake_stop(app, it): seen.append(("stop", it.action_id))
    monkeypatch.setattr(inter, "_on_stop", fake_stop)
    it = inter.parse_interaction(_block_actions_payload(blocks.ACTION_STOP, blocks.encode_value({})))
    asyncio.run(inter.dispatch_interaction(object(), it))
    assert seen == [("stop", blocks.ACTION_STOP)]


def test_dispatch_unknown_action_is_noop():
    from services.slack_bot import interactivity as inter
    it = inter.parse_interaction(_block_actions_payload("agnes_unknown", "{}"))
    asyncio.run(inter.dispatch_interaction(object(), it))  # no raise
