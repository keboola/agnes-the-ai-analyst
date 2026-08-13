"""`agnes attachment get` — client-side behavior.

The server route is covered by tests/test_attachment_download.py; this file
covers what only the CLI does: deriving the output name from
Content-Disposition (including Starlette's RFC 5987 `filename*=utf-8''`
extended form — the ONLY form it emits for names with spaces or non-ASCII),
basename hardening of the untrusted header, the `-o` path, and how the
route's error taxonomy renders on stderr.

`api_get` is monkeypatched with canned httpx.Response objects — no server.
"""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from cli.commands.attachment import _server_filename, attachment_app

runner = CliRunner()


def _resp(status: int, content: bytes = b"", headers: dict | None = None, json_detail=None):
    if json_detail is not None:
        return httpx.Response(status, json={"detail": json_detail}, request=httpx.Request("GET", "http://t"))
    return httpx.Response(status, content=content, headers=headers or {}, request=httpx.Request("GET", "http://t"))


@pytest.fixture
def fake_api(monkeypatch):
    """Patch api_get in the command module; returns a setter for the response."""
    holder = {}

    def _api_get(path, **kwargs):
        holder["path"] = path
        return holder["resp"]

    monkeypatch.setattr("cli.commands.attachment.api_get", _api_get)
    return holder


class TestServerFilename:
    def test_extended_utf8_form_is_decoded(self):
        # What Starlette actually emits for a name with a space.
        cd = "attachment; filename*=utf-8''56340_bug%20report.pdf"
        assert _server_filename(cd) == "56340_bug report.pdf"

    def test_extended_form_with_non_ascii(self):
        cd = "attachment; filename*=utf-8''p%C5%99%C3%ADloha.png"
        assert _server_filename(cd) == "příloha.png"

    def test_plain_quoted_form(self):
        assert _server_filename('attachment; filename="plain.pdf"') == "plain.pdf"

    def test_plain_unquoted_form(self):
        assert _server_filename("attachment; filename=plain.pdf") == "plain.pdf"

    def test_crafted_path_is_reduced_to_basename(self):
        assert _server_filename('attachment; filename="../../etc/passwd"') == "passwd"
        assert _server_filename("attachment; filename*=utf-8''..%2F..%2Fevil.sh") == "evil.sh"

    def test_absent_header_is_empty(self):
        assert _server_filename("") == ""
        assert _server_filename("attachment") == ""


class TestGetCommand:
    def test_writes_decoded_extended_filename(self, fake_api, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        fake_api["resp"] = _resp(200, b"bytes!", {"content-disposition": "attachment; filename*=utf-8''a%20b.pdf"})
        result = runner.invoke(attachment_app, ["jira", "101"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "a b.pdf").read_bytes() == b"bytes!"
        assert "Wrote 6 bytes" in result.output
        assert fake_api["path"] == "/api/attachments/jira/101/download"

    def test_falls_back_to_source_and_id_without_header(self, fake_api, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        fake_api["resp"] = _resp(200, b"x")
        result = runner.invoke(attachment_app, ["jira", "101"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "jira_101").read_bytes() == b"x"

    def test_output_option_wins_and_creates_parents(self, fake_api, tmp_path):
        fake_api["resp"] = _resp(200, b"x", {"content-disposition": 'attachment; filename="ignored.png"'})
        target = tmp_path / "deep" / "dir" / "mine.png"
        result = runner.invoke(attachment_app, ["jira", "101", "-o", str(target)])
        assert result.exit_code == 0, result.output
        assert target.read_bytes() == b"x"

    def test_not_stored_404_renders_code_and_hint(self, fake_api):
        fake_api["resp"] = _resp(404, json_detail={"code": "attachment_not_stored", "hint": "fetch it upstream"})
        result = runner.invoke(attachment_app, ["jira", "102"])
        assert result.exit_code == 1
        err = result.output + (result.stderr or "")
        assert "attachment_not_stored" in err
        assert "fetch it upstream" in err

    def test_403_shows_server_detail_not_login_hint(self, fake_api):
        fake_api["resp"] = _resp(403, json_detail="Table 'jira_attachments' is not in your stack.")
        result = runner.invoke(attachment_app, ["jira", "101"])
        assert result.exit_code == 1
        err = result.output + (result.stderr or "")
        assert "jira_attachments" in err
        assert "authentication required" not in err

    def test_401_shows_login_hint(self, fake_api):
        fake_api["resp"] = _resp(401)
        result = runner.invoke(attachment_app, ["jira", "101"])
        assert result.exit_code == 1
        err = result.output + (result.stderr or "")
        assert "agnes auth login" in err
