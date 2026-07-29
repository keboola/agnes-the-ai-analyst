"""Tests for `cli.client.api_post_sse` — the AG-UI SSE reader `agnes chat`
(`cli/commands/chat.py`) builds on.

Uses ``httpx.MockTransport`` to fake the wire response body byte-for-byte
rather than mocking ``api_post_sse`` itself (that's what
``tests/test_cli_chat.py`` does for the CLI-command layer) — this file is
the one place the actual SSE line-parsing gets exercised.
"""

from __future__ import annotations

import httpx
import pytest

import cli.client as client_mod
from cli.client import AgnesTransportError, ApiSseError, api_post_sse


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


def _install_transport(monkeypatch, handler) -> None:
    """Patch `cli.client.get_client` so `api_post_sse` talks to a
    `httpx.MockTransport` instead of a real socket. `api_post_sse` always
    calls `get_client(timeout=...)` with a keyword arg — the fake accepts
    and ignores it."""

    def fake_get_client(timeout=30.0):
        return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")

    monkeypatch.setattr(client_mod, "get_client", fake_get_client)


def _sse_response(body: bytes, status_code: int = 200, **kwargs) -> "callable":
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body, **kwargs)

    return handler


class TestDataParsing:
    def test_data_prefix_with_leading_space_is_stripped(self, monkeypatch):
        body = b'data: {"type": "RUN_STARTED"}\n\n'
        _install_transport(monkeypatch, _sse_response(body))

        events = list(api_post_sse("/x"))

        assert events == [{"type": "RUN_STARTED"}]

    def test_data_prefix_without_space_also_parses(self, monkeypatch):
        body = b'data:{"type": "RUN_STARTED"}\n\n'
        _install_transport(monkeypatch, _sse_response(body))

        events = list(api_post_sse("/x"))

        assert events == [{"type": "RUN_STARTED"}]

    def test_blank_lines_dispatch_records_and_multiple_records_are_yielded(self, monkeypatch):
        body = b'data: {"type": "A"}\n\ndata: {"type": "B"}\n\n'
        _install_transport(monkeypatch, _sse_response(body))

        events = list(api_post_sse("/x"))

        assert events == [{"type": "A"}, {"type": "B"}]

    def test_event_and_id_lines_are_skipped_not_yielded_as_events(self, monkeypatch):
        body = b'id: sess-1:1\nevent: RUN_STARTED\ndata: {"type": "RUN_STARTED"}\n\n'
        _install_transport(monkeypatch, _sse_response(body))

        events = list(api_post_sse("/x"))

        # Only the parsed `data:` payload comes out — the id:/event: lines
        # are metadata, not separate events.
        assert events == [{"type": "RUN_STARTED"}]

    def test_multiline_data_record_is_joined_with_newline_before_parsing(self, monkeypatch):
        # Per the SSE spec, consecutive `data:` lines within one record
        # join with "\n" before the payload is parsed. The server's own
        # producer (`app.api.agent_sse.sse_bytes`) never emits more than
        # one `data:` line per record today, but a spec-correct client
        # must not assume that forever.
        body = b'data: {"type": "TEXT_MESSAGE_CONTENT",\ndata: "delta": "hi"}\n\n'
        _install_transport(monkeypatch, _sse_response(body))

        events = list(api_post_sse("/x"))

        assert events == [{"type": "TEXT_MESSAGE_CONTENT", "delta": "hi"}]


class TestMalformedPayloads:
    def test_non_json_data_payload_is_skipped_not_raised(self, monkeypatch):
        body = b"data: not-json-at-all\n\n" + b'data: {"type": "RUN_FINISHED"}\n\n'
        _install_transport(monkeypatch, _sse_response(body))

        events = list(api_post_sse("/x"))

        # The malformed record is dropped; the well-formed one after it
        # still comes through — one bad record must not poison the stream.
        assert events == [{"type": "RUN_FINISHED"}]

    def test_malformed_payload_count_is_reported_to_stderr_at_stream_end(self, monkeypatch, capsys):
        body = b"data: not-json-1\n\n" + b"data: not-json-2\n\n" + b'data: {"type": "RUN_FINISHED"}\n\n'
        _install_transport(monkeypatch, _sse_response(body))

        list(api_post_sse("/x"))

        captured = capsys.readouterr()
        assert "2" in captured.err
        assert "malformed" in captured.err.lower()


class TestHttpErrorBranch:
    def test_status_code_over_400_raises_api_sse_error_with_parsed_json_body(self, monkeypatch):
        handler = _sse_response(b'{"detail": {"code": "session_not_found"}}', status_code=404)
        _install_transport(monkeypatch, handler)

        with pytest.raises(ApiSseError) as exc_info:
            list(api_post_sse("/x"))

        assert exc_info.value.status_code == 404
        assert exc_info.value.body == {"detail": {"code": "session_not_found"}}

    def test_status_code_over_400_falls_back_to_raw_text_when_not_json(self, monkeypatch):
        handler = _sse_response(b"internal server error, not json", status_code=500)
        _install_transport(monkeypatch, handler)

        with pytest.raises(ApiSseError) as exc_info:
            list(api_post_sse("/x"))

        assert exc_info.value.status_code == 500
        assert exc_info.value.body == "internal server error, not json"

    def test_no_events_are_yielded_before_the_error_status_is_raised(self, monkeypatch):
        # Even if a >=400 response somehow carried an SSE-shaped body, the
        # status check happens before any line is parsed — no partial
        # events should leak out ahead of the raised error.
        body = b'data: {"type": "RUN_STARTED"}\n\n'
        handler = _sse_response(body, status_code=409)
        _install_transport(monkeypatch, handler)

        gen = api_post_sse("/x")
        with pytest.raises(ApiSseError):
            next(gen)


class TestTransportFailure:
    def test_httpx_transport_failure_is_translated_to_agnes_transport_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _install_transport(monkeypatch, handler)

        with pytest.raises(AgnesTransportError):
            list(api_post_sse("/x"))

    def test_read_timeout_mid_stream_is_translated_too(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        _install_transport(monkeypatch, handler)

        with pytest.raises(AgnesTransportError):
            list(api_post_sse("/x"))
