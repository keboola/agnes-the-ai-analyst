"""Automated e2e for the Cowork bundle's credential resolution.

Runs the *generated* ``mcp_server.py`` as a subprocess against a stub HTTP
server, with crafted credential files on disk, and drives the MCP stdio
protocol. Guards the credential-loading contract so future edits to the
bundle generator can't silently regress it.

Reproduces the 2026-06-15 field failure: a stale ``.agnes-creds.json``
(expired token) sitting next to a valid ``~/.config/agnes/token.json``
must NOT shadow the valid token — ``_load_creds`` skips expired sources and
falls through to a live one.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.api.cowork_bundle import _bundle_mcp_server_py


def _jwt(exp: int, jti: str, email: str = "user@example.com") -> str:
    """A signature-less JWT the proxy can decode for `exp` (sig is never verified)."""
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    head = b64({"alg": "HS256", "typ": "JWT"})
    body = b64({"email": email, "jti": jti, "typ": "pat", "exp": exp})
    return f"{head}.{body}.sig"


class _Stub(BaseHTTPRequestHandler):
    seen_tokens: list[str] = []
    status: int = 200  # override to exercise typed-error paths

    def log_message(self, *a):  # silence
        pass

    def _auth(self) -> str:
        h = self.headers.get("Authorization", "")
        return h[len("Bearer "):] if h.startswith("Bearer ") else ""

    def do_GET(self):
        type(self).seen_tokens.append(self._auth())
        if self.path == "/api/v2/catalog":
            if _Stub.status != 200:
                body = json.dumps({"detail": "nope"}).encode()
                self.send_response(_Stub.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            payload = json.dumps([{"id": "orders", "name": "orders"}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"{}")


@pytest.fixture
def stub_server():
    _Stub.seen_tokens = []
    _Stub.status = 200
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def _run_mcp(script_path, home, calls):
    """Run the generated mcp_server.py, feed MCP JSON-RPC, return parsed responses."""
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})]
    for i, c in enumerate(calls, start=2):
        lines.append(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": c}))
    proc = subprocess.run(
        [sys.executable, str(script_path)],
        input="\n".join(lines) + "\n",
        capture_output=True, text=True, timeout=30,
        env={"HOME": str(home), "PATH": __import__("os").environ.get("PATH", "")},
    )
    out = []
    for ln in proc.stdout.splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out, proc


def _write_script(tmp_path):
    p = tmp_path / "mcp_server.py"
    p.write_text(_bundle_mcp_server_py())
    return p


def _config_dir(home):
    d = home / ".config" / "agnes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_expired_creds_file_does_not_shadow_valid_token(tmp_path, stub_server):
    """The 2026-06-15 bug: expired .agnes-creds.json must not win over valid token.json."""
    _srv, url = stub_server
    home = tmp_path / "home"
    home.mkdir()
    script = _write_script(home)  # HERE = script dir = home

    now = int(time.time())
    expired = _jwt(now - 86400, jti="expired1")
    valid = _jwt(now + 90 * 86400, jti="valid1")

    # priority-2 file holds an EXPIRED token, right next to the script
    (home / ".agnes-creds.json").write_text(
        json.dumps({"server_url": url, "access_token": expired})
    )
    # priority-3 fallback holds a VALID token
    cfg = _config_dir(home)
    (cfg / "config.yaml").write_text(f"server: {url}\n")
    (cfg / "token.json").write_text(json.dumps({"access_token": valid}))

    responses, proc = _run_mcp(script, home, [{"name": "catalog", "arguments": {}}])

    assert _Stub.seen_tokens, f"server never called; stderr={proc.stderr}"
    assert expired not in _Stub.seen_tokens, "proxy used the EXPIRED token (regression)"
    assert valid in _Stub.seen_tokens, "proxy did not fall through to the valid token"


def test_valid_bundle_token_is_used(tmp_path, stub_server):
    """Happy path: a live agnes-bundle.json token is used."""
    _srv, url = stub_server
    home = tmp_path / "home"
    home.mkdir()
    script = _write_script(home)
    now = int(time.time())
    good = _jwt(now + 86400, jti="bundlegood")
    (home / "agnes-bundle.json").write_text(
        json.dumps({"server_url": url, "access_token": good})
    )
    responses, proc = _run_mcp(script, home, [{"name": "catalog", "arguments": {}}])
    assert good in _Stub.seen_tokens, f"bundle token not used; stderr={proc.stderr}"


def test_creds_reread_per_call(tmp_path, stub_server):
    """#2: a rotated token takes effect on the next call without a restart."""
    import os
    _srv, url = stub_server
    home = tmp_path / "home"
    home.mkdir()
    script = _write_script(home)
    now = int(time.time())
    tok_a = _jwt(now + 86400, jti="tokA")
    tok_b = _jwt(now + 86400, jti="tokB")
    creds = home / ".agnes-creds.json"
    creds.write_text(json.dumps({"server_url": url, "access_token": tok_a}))

    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "")},
    )

    def _send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    try:
        _send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        proc.stdout.readline()  # init response
        _send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "catalog", "arguments": {}}})
        proc.stdout.readline()  # call 1 response
        # rotate the token on disk, mid-session
        creds.write_text(json.dumps({"server_url": url, "access_token": tok_b}))
        _send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "catalog", "arguments": {}}})
        proc.stdout.readline()  # call 2 response
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)

    assert tok_a in _Stub.seen_tokens, "first call should use the original token"
    assert tok_b in _Stub.seen_tokens, "rotated token not picked up without restart (regression #2)"


def test_403_surfaces_egress_hint(tmp_path, stub_server):
    """#6: a 403 is reported as egress/forbidden, not 'token expired'."""
    _srv, url = stub_server
    _Stub.status = 403
    home = tmp_path / "home"
    home.mkdir()
    script = _write_script(home)
    now = int(time.time())
    (home / ".agnes-creds.json").write_text(
        json.dumps({"server_url": url, "access_token": _jwt(now + 86400, jti="t403")})
    )
    responses, proc = _run_mcp(script, home, [{"name": "catalog", "arguments": {}}])
    text = json.dumps(responses)
    assert "403" in text or "egress" in text or "forbidden" in text, (
        f"403 not surfaced with a typed hint; got {text}"
    )


def test_unreachable_surfaces_network_hint(tmp_path):
    """#6: an unreachable server is reported as a network/egress issue."""
    import os
    home = tmp_path / "home"
    home.mkdir()
    script = _write_script(home)
    now = int(time.time())
    # point at a closed port → connection refused
    (home / ".agnes-creds.json").write_text(
        json.dumps({"server_url": "http://127.0.0.1:1", "access_token": _jwt(now + 86400, jti="tnet")})
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "catalog", "arguments": {}}}) + "\n",
        capture_output=True, text=True, timeout=30,
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "")},
    )
    assert "network" in proc.stdout or "reach" in proc.stdout, (
        f"network hint missing; stdout={proc.stdout} stderr={proc.stderr}"
    )
