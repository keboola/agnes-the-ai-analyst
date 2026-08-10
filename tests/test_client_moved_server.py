"""`cli/client.py` turns an API redirect into an actionable error.

A deployment that changes hostname typically leaves the old name answering
`308 Permanent Redirect` for a while. httpx does not follow redirects by
default, so every `api_*` helper handed its caller a bare 3xx response and
the shared renderer printed `HTTP 308:` with an empty body — no destination,
no remedy, on every single command.

Following the redirect is NOT the fix: httpx strips `Authorization` on a
cross-origin hop (`httpx._client.Client._redirect_headers`), so the retry
would arrive unauthenticated and the user would get `401 Not authenticated`
instead — a worse message for the same underlying cause. The CLI stops and
names the new address instead.
"""

from unittest.mock import patch

import httpx
import pytest


def _redirect_response(status: int, location: str, requested: str) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"Location": location} if location else {},
        request=httpx.Request("GET", requested),
    )


class TestMovedServerHook:
    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_cross_origin_redirect_exits_2_and_names_the_new_address(self, status, capsys):
        from cli.client import _check_moved_server

        resp = _redirect_response(
            status,
            "https://agnes.new.example/api/v1/agents",
            "https://agnes.old.example/api/v1/agents",
        )
        with patch("cli.client.get_server_url", return_value="https://agnes.old.example"):
            with pytest.raises(SystemExit) as exc:
                _check_moved_server(resp)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        # The destination is the one thing the old message lacked.
        assert "https://agnes.new.example" in err
        assert str(status) in err
        # And a remedy the user can actually run.
        assert "AGNES_SERVER" in err

    def test_success_response_is_untouched(self):
        from cli.client import _check_moved_server

        resp = httpx.Response(status_code=200, request=httpx.Request("GET", "https://x/api"))
        _check_moved_server(resp)  # must not raise

    def test_error_response_is_left_to_the_normal_renderer(self):
        from cli.client import _check_moved_server

        resp = httpx.Response(status_code=403, request=httpx.Request("GET", "https://x/api"))
        _check_moved_server(resp)  # must not raise

    def test_same_origin_redirect_does_not_claim_the_server_moved(self, capsys):
        """A relative/in-app redirect is not a hostname change.

        Still a hard stop (the helper cannot transparently retry), but the
        message must not tell the user to re-point their config at the very
        host they are already using.
        """
        from cli.client import _check_moved_server

        resp = _redirect_response(307, "/api/v1/agents/", "https://agnes.example/api/v1/agents")
        with patch("cli.client.get_server_url", return_value="https://agnes.example"):
            with pytest.raises(SystemExit) as exc:
                _check_moved_server(resp)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "AGNES_SERVER" not in err

    def test_protocol_relative_location_is_treated_as_a_move(self, capsys):
        """`Location: //host/path` is absolute, despite the leading slash.

        Classifying it by `startswith("/")` files a cross-host move under
        "same origin" and tells the user nothing about the new host.
        """
        from cli.client import _check_moved_server

        resp = _redirect_response(308, "//new.example/api/v1/agents", "https://old.example/api/v1/agents")
        with patch("cli.client.get_server_url", return_value="https://old.example"):
            with pytest.raises(SystemExit):
                _check_moved_server(resp)
        err = capsys.readouterr().err
        assert "AGNES_SERVER" in err
        # A scheme-less base would be pasted into the config as `//new.example`.
        assert "AGNES_SERVER=https://new.example" in err

    def test_hint_keeps_a_non_default_port(self, capsys):
        from cli.client import _check_moved_server

        resp = _redirect_response(
            308, "https://new.example:8443/api/v1/agents?x=1", "https://old.example/api/v1/agents"
        )
        with patch("cli.client.get_server_url", return_value="https://old.example"):
            with pytest.raises(SystemExit):
                _check_moved_server(resp)
        err = capsys.readouterr().err
        assert "AGNES_SERVER=https://new.example:8443" in err
        assert "x=1" not in err.split("AGNES_SERVER=")[1].split()[0]

    def test_redirect_without_location_still_reports_the_status(self, capsys):
        from cli.client import _check_moved_server

        resp = _redirect_response(308, "", "https://agnes.example/api/v1/agents")
        with patch("cli.client.get_server_url", return_value="https://agnes.example"):
            with pytest.raises(SystemExit) as exc:
                _check_moved_server(resp)
        assert exc.value.code == 2
        assert "308" in capsys.readouterr().err


class TestHookIsWired:
    def test_get_client_registers_the_moved_server_hook(self):
        """A hook nobody calls protects nobody — pin the registration."""
        from cli.client import _check_moved_server, get_client

        with patch("cli.client.get_token", return_value="t"):
            with patch("cli.client.get_server_url", return_value="https://agnes.example"):
                client = get_client()
        try:
            assert _check_moved_server in client.event_hooks["response"]
        finally:
            client.close()

    def test_client_still_does_not_follow_redirects(self):
        """Following would strip Authorization cross-origin (see module docstring)."""
        from cli.client import get_client

        with patch("cli.client.get_token", return_value="t"):
            with patch("cli.client.get_server_url", return_value="https://agnes.example"):
                client = get_client()
        try:
            assert client.follow_redirects is False
        finally:
            client.close()
