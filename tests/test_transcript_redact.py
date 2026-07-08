"""Tests for `cli.lib.transcript_redact` — JWT scrubbing before `agnes push`
uploads a session transcript (or CLAUDE.local.md) to the server.

See issue #753: the setup prompt writes the raw PAT into the bootstrap
session's Claude Code transcript (heredoc), and `agnes push` used to upload
that transcript byte-for-byte. This module strips JWT-shaped tokens from
text before it ever leaves the client.
"""

import json

from cli.lib.transcript_redact import redact_bytes, redact_lines, redact_text

# A syntactically-real (but not secret-bearing) HS256 JWT: header.payload.sig.
_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
_SENTINEL = "[REDACTED-JWT]"


def test_redacts_bare_jwt():
    out = redact_text(f"token: {_JWT}")
    assert _JWT not in out
    assert _SENTINEL in out


def test_redacts_jwt_inside_json_string_value():
    """JWTs typically appear as a JSON string value inside a jsonl line —
    e.g. a `bash -c 'cat > ~/.agnes/token <<EOF\n{token}\nEOF'` heredoc
    captured verbatim in a tool_use transcript entry."""
    line = json.dumps({"type": "tool_use", "input": {"command": f"echo {_JWT}"}})
    out = redact_text(line)
    assert _JWT not in out
    assert _SENTINEL in out
    # Still valid JSON after redaction.
    parsed = json.loads(out)
    assert _JWT not in parsed["input"]["command"]


def test_does_not_touch_git_sha():
    sha = "a" * 40
    text = f"commit {sha} fixed the bug"
    assert redact_text(text) == text


def test_does_not_touch_base64_blob_without_two_dots():
    blob = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" + "A" * 60 + "=="
    text = f"payload: {blob}"
    assert redact_text(text) == text


def test_does_not_touch_normal_prose():
    text = "Please run agnes init --server-url https://example.com --token-file ~/.agnes/token"
    assert redact_text(text) == text


def test_idempotent():
    once = redact_text(f"a {_JWT} b")
    twice = redact_text(once)
    assert once == twice


def test_multiple_jwts_in_one_string():
    text = f"first={_JWT} second={_JWT}"
    out = redact_text(text)
    assert _JWT not in out
    assert out.count(_SENTINEL) == 2


def test_multi_line_text():
    text = f"line one\ntoken: {_JWT}\nline three"
    out = redact_text(text)
    assert _JWT not in out
    assert out.splitlines()[0] == "line one"
    assert out.splitlines()[2] == "line three"


def test_redact_bytes_roundtrip():
    data = f"token={_JWT}\n".encode("utf-8")
    out = redact_bytes(data)
    assert isinstance(out, bytes)
    assert _JWT.encode("utf-8") not in out
    assert _SENTINEL.encode("utf-8") in out


def test_redact_lines_generator():
    lines = [f"a: {_JWT}\n", "plain line\n"]
    out = list(redact_lines(lines))
    assert out[0] != lines[0]
    assert _JWT not in out[0]
    assert out[1] == "plain line\n"


def test_empty_string():
    assert redact_text("") == ""
