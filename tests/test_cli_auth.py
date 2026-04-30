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
            "role": "user",
        })
        with patch("cli.commands.auth.api_post", return_value=mock_resp):
            with patch("cli.commands.auth.save_token") as mock_save:
                # Empty password (simulates magic-link / OAuth account) — still 200 from server
                result = runner.invoke(app, ["auth", "login", "--email", "alice@example.com"], input="\n")
        assert result.exit_code == 0
        assert "alice@example.com" in result.output
        mock_save.assert_called_once_with("tok123", "alice@example.com")

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


class TestAuthImportToken:
    def _make_jwt(self, email="alice@example.com", typ="pat"):
        import jwt as pyjwt
        return pyjwt.encode(
            {"email": email, "typ": typ, "sub": "u-1"},
            "unused",
            algorithm="HS256",
        )

    def _mock_verify(self, status_code=200, json_data=None):
        """Build a patcher for cli.commands.auth.httpx.Client that returns a canned response."""
        resp = _make_response(status_code, json_data or {})
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False
        mock_client.get.return_value = resp
        return patch("cli.commands.auth.httpx.Client", return_value=mock_client)

    def test_import_token_success_writes_canonical_format(self, tmp_path, monkeypatch):
        """Valid JWT + 200 from server -> canonical token.json on disk."""
        monkeypatch.setenv("DA_SERVER", "http://example.test")
        token = self._make_jwt(email="bob@example.com")

        with self._mock_verify(200):
            result = runner.invoke(app, ["auth", "import-token", "--token", token])

        assert result.exit_code == 0, result.output
        assert "bob@example.com" in result.output

        token_file = tmp_path / "config" / "token.json"
        assert token_file.exists()
        data = json.loads(token_file.read_text())
        # v19: token.json no longer carries a role label (auth derives admin
        # from group memberships server-side).
        assert data == {"access_token": token, "email": "bob@example.com"}

    def test_import_token_401_does_not_overwrite_existing(self, tmp_path, monkeypatch):
        """A 401 response aborts import and leaves the prior token file untouched."""
        monkeypatch.setenv("DA_SERVER", "http://example.test")
        existing = {"access_token": "keep-me", "email": "old@example.com"}
        token_file = tmp_path / "config" / "token.json"
        token_file.write_text(json.dumps(existing))

        token = self._make_jwt()
        with self._mock_verify(401, {"detail": "Token revoked"}):
            result = runner.invoke(app, ["auth", "import-token", "--token", token])

        assert result.exit_code == 1
        assert "Token rejected by server" in result.output
        assert "Token revoked" in result.output
        # Existing file must be intact.
        assert json.loads(token_file.read_text()) == existing

    def test_import_token_with_server_flag_persists_server_to_config_yaml(
        self, tmp_path, monkeypatch
    ):
        """Passing --server should write `server: URL` to ~/.config/da/config.yaml
        so the user never has to configure the server in a separate step."""
        # No DA_SERVER env var — rely entirely on the --server flag for persistence.
        monkeypatch.delenv("DA_SERVER", raising=False)
        token = self._make_jwt(email="dave@example.com")

        with self._mock_verify(200):
            result = runner.invoke(
                app,
                [
                    "auth", "import-token",
                    "--token", token,
                    "--server", "https://agnes.example.com",
                ],
            )
        assert result.exit_code == 0, result.output

        config_file = tmp_path / "config" / "config.yaml"
        assert config_file.exists(), "config.yaml must be written when --server is passed"
        import yaml
        cfg = yaml.safe_load(config_file.read_text())
        assert cfg.get("server") == "https://agnes.example.com"

    def test_import_token_claim_fallback_via_cli_email_override(self, tmp_path, monkeypatch):
        """Missing email claim -> refuse without --email, accept with it.
        v19 dropped the --role flag (token.json no longer carries role)."""
        import jwt as pyjwt
        monkeypatch.setenv("DA_SERVER", "http://example.test")
        # JWT without email claim — simulates a malformed or minimal token.
        token = pyjwt.encode({"sub": "u-1", "typ": "pat"}, "unused", algorithm="HS256")

        with self._mock_verify(200):
            fail_result = runner.invoke(app, ["auth", "import-token", "--token", token])
        assert fail_result.exit_code == 1
        assert "missing" in fail_result.output.lower()

        with self._mock_verify(200):
            ok_result = runner.invoke(
                app,
                [
                    "auth", "import-token",
                    "--token", token,
                    "--email", "carol@example.com",
                ],
            )
        assert ok_result.exit_code == 0, ok_result.output
        token_file = tmp_path / "config" / "token.json"
        data = json.loads(token_file.read_text())
        assert data == {"access_token": token, "email": "carol@example.com"}


class TestAuthWhoami:
    def test_whoami_no_token(self):
        """Whoami exits when no token is stored."""
        with patch("cli.commands.auth.get_token", return_value=None):
            result = runner.invoke(app, ["auth", "whoami"])
        assert result.exit_code == 1
        assert "Not logged in" in result.output

    def test_whoami_valid_token(self):
        """Whoami decodes JWT and shows user info. v19: no role claim."""
        import jwt as pyjwt
        token = pyjwt.encode(
            {"email": "alice@example.com"},
            "secret",
            algorithm="HS256",
        )
        with patch("cli.commands.auth.get_token", return_value=token):
            with patch("cli.commands.auth.get_server_url", return_value="http://localhost:8000"):
                result = runner.invoke(app, ["auth", "whoami"])
        assert result.exit_code == 0
        assert "alice@example.com" in result.output

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
