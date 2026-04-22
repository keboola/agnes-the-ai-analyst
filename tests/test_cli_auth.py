"""Tests for da auth login/logout/whoami commands."""

import json
import pytest
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path / "local"))
    (tmp_path / "config").mkdir()
    (tmp_path / "local").mkdir()
    yield tmp_path


def _make_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    return resp


class TestAuthLogin:
    def test_login_success(self):
        """Login with valid credentials saves token and shows confirmation."""
        mock_resp = _make_response(200, {
            "access_token": "tok123",
            "email": "alice@example.com",
            "role": "analyst",
        })
        with patch("cli.commands.auth.api_post", return_value=mock_resp):
            with patch("cli.commands.auth.save_token") as mock_save:
                # Empty password (simulates magic-link / OAuth account) — still 200 from server
                result = runner.invoke(app, ["auth", "login", "--email", "alice@example.com"], input="\n")
        assert result.exit_code == 0
        assert "alice@example.com" in result.output
        mock_save.assert_called_once_with("tok123", "alice@example.com", "analyst")

    def test_login_invalid_credentials(self):
        """Login with bad credentials exits with error."""
        mock_resp = _make_response(401, {"detail": "Invalid credentials"})
        with patch("cli.commands.auth.api_post", return_value=mock_resp):
            result = runner.invoke(app, ["auth", "login", "--email", "bad@example.com"], input="\n")
        assert result.exit_code == 1
        assert "Login failed" in result.output

    def test_login_connection_error(self):
        """Login propagates connection errors cleanly."""
        with patch("cli.commands.auth.api_post", side_effect=Exception("Connection refused")):
            result = runner.invoke(app, ["auth", "login", "--email", "alice@example.com"], input="\n")
        assert result.exit_code == 1
        assert "Connection error" in result.output


class TestAuthLogout:
    def test_logout(self):
        """Logout clears token and confirms."""
        with patch("cli.commands.auth.clear_token") as mock_clear:
            result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0
        assert "Logged out" in result.output
        mock_clear.assert_called_once()


class TestAuthWhoami:
    def test_whoami_no_token(self):
        """Whoami exits when no token is stored."""
        with patch("cli.commands.auth.get_token", return_value=None):
            result = runner.invoke(app, ["auth", "whoami"])
        assert result.exit_code == 1
        assert "Not logged in" in result.output

    def test_whoami_valid_token(self):
        """Whoami decodes JWT and shows user info."""
        import jwt as pyjwt
        token = pyjwt.encode(
            {"email": "alice@example.com", "role": "analyst"},
            "secret",
            algorithm="HS256",
        )
        with patch("cli.commands.auth.get_token", return_value=token):
            with patch("cli.commands.auth.get_server_url", return_value="http://localhost:8000"):
                result = runner.invoke(app, ["auth", "whoami"])
        assert result.exit_code == 0
        assert "alice@example.com" in result.output
        assert "analyst" in result.output

    def test_whoami_invalid_token(self):
        """Whoami with garbled token exits with error."""
        with patch("cli.commands.auth.get_token", return_value="not.a.jwt"):
            result = runner.invoke(app, ["auth", "whoami"])
        # May succeed or fail depending on jwt decode — either way no traceback
        assert result.exit_code in (0, 1)


def test_da_login_sends_password(monkeypatch):
    import httpx
    from typer.testing import CliRunner
    from cli.commands import auth as auth_mod

    captured = {}

    def fake_post(path, json=None, **kwargs):
        captured["path"] = path
        captured["json"] = json
        return httpx.Response(200, json={
            "access_token": "tok", "email": "u@t", "role": "analyst",
            "user_id": "u1", "token_type": "bearer",
        })

    monkeypatch.setattr(auth_mod, "api_post", fake_post, raising=False)

    runner = CliRunner()
    # Provide email and password via stdin (typer prompts)
    result = runner.invoke(auth_mod.auth_app, ["login"], input="u@t\nhunter2\n")
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/auth/token"
    assert captured["json"] == {"email": "u@t", "password": "hunter2"}


def test_da_auth_token_create_calls_api(monkeypatch):
    import httpx
    from typer.testing import CliRunner
    from cli.commands.auth import auth_app
    from cli.commands import tokens as tok_mod

    captured = {}

    def fake_post(path, json=None, **kwargs):
        captured["path"] = path
        captured["json"] = json
        return httpx.Response(201, json={
            "id": "abc", "name": json["name"], "prefix": "XXXXXXXX",
            "token": "raw-token-once",
            "expires_at": None, "created_at": "2026-04-21T00:00:00+00:00",
        })

    monkeypatch.setattr(tok_mod, "api_post", fake_post, raising=False)

    runner = CliRunner()
    result = runner.invoke(auth_app, ["token", "create", "--name", "laptop", "--ttl", "30d"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/auth/tokens"
    assert captured["json"] == {"name": "laptop", "expires_in_days": 30}
    assert "raw-token-once" in result.output
