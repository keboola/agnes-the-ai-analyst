

class TestChunkedRequestBodies:
    """Devin Review on #1252: git pushes arrive chunked.

    `Content-Length` alone stopped being enough once git rode this relay — it
    switches to `Transfer-Encoding: chunked` past a small threshold, and a
    chunked request carries no length, so the body was forwarded EMPTY. Small
    pushes worked (they are sized), which made it look like a size limit
    rather than a missing code path.
    """

    @staticmethod
    def _read(payload: bytes, headers: dict) -> bytes:
        """`StreamReader` must be constructed inside the loop that drives it."""
        import asyncio

        from app.chat.relay import _read_request_body

        async def go():
            reader = asyncio.StreamReader()
            reader.feed_data(payload)
            reader.feed_eof()
            return await _read_request_body(reader, headers)

        return asyncio.run(go())

    def test_a_chunked_body_is_reassembled(self):
        raw = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        assert self._read(raw, {"transfer-encoding": "chunked"}) == b"hello world"

    def test_chunk_extensions_and_trailers_are_tolerated(self):
        raw = b"5;ext=1\r\nhello\r\n0\r\nX-Checksum: abc\r\n\r\n"
        assert self._read(raw, {"transfer-encoding": "chunked"}) == b"hello"

    def test_a_sized_body_still_works(self):
        assert self._read(b"hello", {"content-length": "5"}) == b"hello"

    def test_no_body_is_empty(self):
        assert self._read(b"", {}) == b""


def test_the_relay_origin_the_data_apps_skill_uses_is_actually_set():
    """Devin Review on #1252: `$AGNES_SERVER_BASE` lived only in prose.

    The skill tells the in-sandbox assistant to clone from
    `$AGNES_SERVER_BASE/data-apps.git/<slug>`; nothing set it, so the command
    expanded to an empty base. `data-apps.git` is served off the relay ROOT,
    not under `/agnes-api`, so `AGNES_SERVER` cannot stand in for it.
    """
    import pathlib
    import re

    runner = (pathlib.Path(__file__).resolve().parents[1] / "app" / "chat" / "runner.py").read_text(
        encoding="utf-8"
    )
    assert 'os.environ["AGNES_SERVER_BASE"]' in runner, "the skill's clone command has no base to expand"
    # It must be the origin, NOT the /agnes-api base.
    m = re.search(r'os\.environ\["AGNES_SERVER_BASE"\] = f"([^"]+)"', runner)
    assert m and not m.group(1).endswith("/agnes-api"), m.group(1) if m else "not found"

    skill = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app"
        / "initial_workspace_default"
        / ".claude"
        / "skills"
        / "agnes-data-apps-extras"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "$AGNES_SERVER_BASE/data-apps.git/" in skill, "the skill stopped using the variable — re-point this"


def test_the_skill_re_points_the_remote_before_pushing():
    """Devin Review on #1252: the relay port is per-runner-process.

    `_start_relay` binds with `port_hint=0`, so a URL recorded in
    `.git/config` by a clone in one turn is dead after a pause/resume — the
    clone worked and the next push fails to connect, which reads as a broken
    relay rather than a stale remote.
    """
    import pathlib

    skill = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app"
        / "initial_workspace_default"
        / ".claude"
        / "skills"
        / "agnes-data-apps-extras"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "remote set-url origin" in skill, "nothing tells the agent to refresh a stale remote"
    assert "port changes every time the runner starts" in skill, "the reason is not stated"
    # The instruction must come with the clone, not further down the document.
    assert skill.index("remote set-url origin") < skill.index("## 1. Scaffold-first")
