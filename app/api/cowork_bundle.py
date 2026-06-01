"""Agnes Cowork setup bundle endpoints.

One-click Claude Code setup flow (no terminal needed):

  POST /api/user/cowork-bundle          — generate + return a ZIP bundle
  GET  /api/user/setup-tokens           — list active setup tokens (for UI revoke)
  DELETE /api/user/setup-tokens/{id}    — revoke a setup token
  POST /api/auth/exchange-setup-token   — exchange setup token → PAT (no auth required)

Bundle structure (unzipping creates a ready-to-open Claude Code workspace)::

  agnes-cowork-setup-<ts>/
  ├── agnes-bundle.json         ← setup token + server URL (visible to Claude tools)
  ├── setup.py                  ← pure stdlib fallback (no pip required)
  ├── .claude/
  │   └── settings.json         ← SessionStart hook: ``agnes init --bundle .``
  └── CLAUDE.md                 ← user-friendly instructions + agent guidance

User flow:
  1. Download ZIP from /me/profile → Connect Claude Code.
  2. Unzip the file.
  3. Open Claude Code → File → Open Folder → select the unzipped folder.
  4. The SessionStart hook fires ``agnes init --bundle .`` which exchanges the
     token, configures credentials, installs the pull/push hooks, and runs the
     first data pull — all automatically.

After the first successful ``agnes init``, ``install_claude_hooks`` removes the
``agnes init`` entry (matched via ``_OUR_COMMAND_MARKERS``) and replaces it with
the standard ``agnes pull`` / ``agnes push`` hooks, so the setup is truly one-time.

Security model:
- Setup tokens are short-lived (24 h), single-use, stored hashed (SHA-256).
- The raw token lives only in the ZIP bundle — never in server logs or DB.
- ``POST /api/auth/exchange-setup-token`` is the only unauthenticated endpoint;
  the setup token IS the auth credential for that one call.
- After exchange, the setup token is atomically marked used and cannot be
  reused (replay prevention).
- Max 5 active setup tokens per user — UI shows a warning above that limit.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import textwrap
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth.dependencies import _get_db, get_current_user
from app.auth.jwt import create_access_token
from src.repositories.access_tokens import AccessTokenRepository
from src.repositories.audit import AuditRepository
from src.repositories.setup_tokens import SetupTokenRepository
from src.repositories.users import UserRepository

# ── routers ──────────────────────────────────────────────────────────────────

# User-scoped (auth required): bundle generation + token management
user_router = APIRouter(prefix="/api/user", tags=["cowork"])

# Auth-scoped (no auth): setup token exchange
auth_router = APIRouter(prefix="/api/auth", tags=["cowork"])

# Max active setup tokens per user before the UI shows a warning
_MAX_ACTIVE_TOKENS = 5

# Setup token TTL
_SETUP_TOKEN_TTL = timedelta(hours=24)


# ── helpers ───────────────────────────────────────────────────────────────────

def _audit(conn, actor: str, action: str, target: str, params=None):
    try:
        AuditRepository(conn).log(
            user_id=actor, action=action,
            resource=f"setup_token:{target}", params=params,
        )
    except Exception:
        pass


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _generate_setup_token() -> str:
    """Return a 67-char ``st_<64 url-safe base64 chars>`` token."""
    return "st_" + secrets.token_urlsafe(48)


# ── bundle generation helpers ─────────────────────────────────────────────────

def _bundle_settings_json(server_url: str, access_token: str) -> str:  # noqa: ARG001
    """Return .claude/settings.json content for the setup bundle.

    Uses stdio transport (python3 mcp_server.py) — the bundled mcp_server.py
    is a pure-stdlib REST proxy that requires no Agnes installation and works
    inside the cowork VM on the very first session open.

    The SessionStart hook runs setup.py once to exchange the short-lived bundle
    token for a 90-day PAT and write credentials to ~/.config/agnes/.
    After that run setup.py deletes itself from the hook and replaces it with
    ``agnes pull``.
    """
    _init_cmd = (
        "python3 setup.py 2>/dev/null || python setup.py 2>/dev/null || true"
    )
    cfg = {
        "model": "sonnet",
        "permissions": {
            "allow": ["Read", "Bash", "Bash(agnes *)", "Grep", "Glob"],
        },
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _init_cmd,
                        }
                    ]
                }
            ]
        },
        # stdio transport: mcp_server.py is a pure-stdlib REST proxy bundled
        # in the ZIP root. No Agnes package install needed — works on first
        # session open. Uses relative path so it resolves from the project root.
        "mcpServers": {
            "agnes": {
                "command": "python3",
                "args": ["mcp_server.py"],
            }
        },
    }
    return json.dumps(cfg, indent=2) + "\n"


def _bundle_mcp_server_py() -> str:
    """Return mcp_server.py — pure-stdlib stdio MCP proxy.

    No Agnes package install required. Reads credentials from agnes-bundle.json
    (pre-baked on first open, before setup.py fires) or ~/.config/agnes/token.json
    (after setup.py runs). Proxies every tool call to the Agnes REST API over
    HTTP — works inside the cowork VM as long as the Agnes server is reachable.

    Tools: server_info, catalog, schema, describe, query, skills.
    """
    return textwrap.dedent("""\
        #!/usr/bin/env python3
        \"\"\"Agnes MCP stdio proxy — pure stdlib, no install needed.

        Reads credentials from agnes-bundle.json or ~/.config/agnes/token.json,
        then implements the MCP protocol over stdio, forwarding each tool call
        to the Agnes REST API.
        \"\"\"
        from __future__ import annotations
        import json, pathlib, re, sys, urllib.error, urllib.request

        HERE       = pathlib.Path(__file__).resolve().parent
        CONFIG_DIR = pathlib.Path.home() / ".config" / "agnes"

        # ── credentials ──────────────────────────────────────────────────────

        def _load_creds():
            \"\"\"Return (server_url, pat) or ('', '') if not found.\"\"\"
            # Pre-setup: pre-baked PAT in the bundle JSON
            bf = HERE / "agnes-bundle.json"
            if bf.exists():
                try:
                    b = json.loads(bf.read_text())
                    u, p = b.get("server_url", "").rstrip("/"), b.get("access_token", "")
                    if u and p:
                        return u, p
                except Exception:
                    pass
            # Post-setup: persistent credentials file in the project folder.
            # setup.py writes this to the Mac filesystem so it survives
            # cowork VM restarts even when ~/.config/agnes/ is on the VM.
            cf = HERE / ".agnes-creds.json"
            if cf.exists():
                try:
                    b = json.loads(cf.read_text())
                    u = b.get("server_url", "").rstrip("/")
                    p = b.get("access_token", "")
                    if u and p:
                        return u, p
                except Exception:
                    pass
            cfg = CONFIG_DIR / "config.yaml"
            tok = CONFIG_DIR / "token.json"
            if cfg.exists() and tok.exists():
                try:
                    m = re.search(r"server:\\s*(.+)", cfg.read_text())
                    u = m.group(1).strip() if m else ""
                    p = json.loads(tok.read_text()).get("access_token", "")
                    if u and p:
                        return u, p
                except Exception:
                    pass
            return "", ""

        # ── HTTP helper ───────────────────────────────────────────────────────

        def _api(method, path, server_url, pat, body=None, timeout=30):
            url = server_url.rstrip("/") + path
            hdrs = {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    return {"error": json.loads(e.read()).get("detail", str(e))}
                except Exception:
                    return {"error": str(e)}
            except Exception as e:
                return {"error": str(e)}

        # ── MCP tool definitions ──────────────────────────────────────────────

        _TOOLS = [
            {"name": "server_info",
             "description": "Return Agnes server health and your account email. Run at session start to verify connectivity.",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "catalog",
             "description": "List all tables available to you (RBAC-filtered). Always call this first.",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "schema",
             "description": "Show column names, types, and SQL hints for a table.",
             "inputSchema": {"type": "object", "required": ["table_id"],
                             "properties": {"table_id": {"type": "string"}}}},
            {"name": "describe",
             "description": "Show schema plus sample rows for a table.",
             "inputSchema": {"type": "object", "required": ["table_id"],
                             "properties": {"table_id": {"type": "string"},
                                            "rows": {"type": "integer", "default": 5}}}},
            {"name": "query",
             "description": "Run SQL against Agnes data (server-side, all query_mode tables).",
             "inputSchema": {"type": "object", "required": ["sql"],
                             "properties": {"sql": {"type": "string"},
                                            "limit": {"type": "integer", "default": 1000}}}},
            {"name": "skills",
             "description": "List marketplace skills you can access, with full SKILL.md content.",
             "inputSchema": {"type": "object", "properties": {}}},
        ]

        # ── tool dispatch ─────────────────────────────────────────────────────

        def _call(name, args, server_url, pat):
            if name == "server_info":
                health = _api("GET", "/api/health", server_url, pat, timeout=5)
                access = _api("GET", "/api/me/effective-access", server_url, pat, timeout=5)
                return json.dumps({"server": health, "access": access, "server_url": server_url}, indent=2)
            elif name == "catalog":
                return json.dumps(_api("GET", "/api/v2/catalog", server_url, pat), indent=2)
            elif name == "schema":
                tid = args.get("table_id", "")
                return json.dumps(_api("GET", f"/api/v2/schema/{tid}", server_url, pat), indent=2)
            elif name == "describe":
                tid  = args.get("table_id", "")
                rows = int(args.get("rows", 5))
                sc = _api("GET", f"/api/v2/schema/{tid}", server_url, pat)
                sm = _api("GET", f"/api/v2/sample/{tid}?n={rows}", server_url, pat)
                return json.dumps({"schema": sc, "sample": sm}, indent=2)
            elif name == "query":
                sql   = args.get("sql", "")
                limit = int(args.get("limit", 1000))
                return json.dumps(_api("POST", "/api/query", server_url, pat,
                                       body={"sql": sql, "limit": limit}, timeout=60), indent=2)
            elif name == "skills":
                return json.dumps(_api("GET", "/api/v2/marketplace/skills", server_url, pat), indent=2)
            return json.dumps({"error": f"unknown tool: {name}"})

        # ── MCP stdio loop ────────────────────────────────────────────────────

        def _send(obj):
            sys.stdout.write(json.dumps(obj) + "\\n")
            sys.stdout.flush()

        server_url, pat = _load_creds()

        for _raw in sys.stdin:
            _raw = _raw.strip()
            if not _raw:
                continue
            try:
                msg = json.loads(_raw)
            except ValueError:
                continue
            m   = msg.get("method", "")
            mid = msg.get("id")
            if m == "initialize":
                _send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Agnes", "version": "1.0"},
                }})
            elif m == "initialized":
                pass
            elif m == "ping":
                _send({"jsonrpc": "2.0", "id": mid, "result": {}})
            elif m == "tools/list":
                _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": _TOOLS}})
            elif m == "tools/call":
                p    = msg.get("params", {})
                name = p.get("name", "")
                args = p.get("arguments", {})
                if not server_url or not pat:
                    _send({"jsonrpc": "2.0", "id": mid,
                           "error": {"code": -32000,
                                     "message": "Agnes credentials not found — run setup.py"}})
                else:
                    text = _call(name, args, server_url, pat)
                    _send({"jsonrpc": "2.0", "id": mid,
                           "result": {"content": [{"type": "text", "text": text}]}})
            elif mid is not None:
                _send({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32601, "message": f"Method not found: {m}"}})
    """)


def _bundle_agnes_py() -> str:
    """Return agnes.py — pure-stdlib CLI for Agnes data access via Bash tool.

    Reads credentials from .agnes-creds.json (project folder, written by
    setup.py) or ~/.config/agnes/. Provides catalog/schema/describe/query
    commands that Claude can call via the Bash tool in the cowork session.

    This is the reliable fallback for cowork environments where the cowork
    VM does not load mcpServers from the project-level settings.json.
    """
    return textwrap.dedent("""\
        #!/usr/bin/env python3
        \"\"\"Agnes CLI — pure stdlib, no install needed.

        Usage:
          python3 agnes.py catalog
          python3 agnes.py schema <table_id>
          python3 agnes.py describe <table_id> [rows]
          python3 agnes.py query '<sql>'
          python3 agnes.py info
          python3 agnes.py skills
        \"\"\"
        from __future__ import annotations
        import json, pathlib, re, sys, urllib.error, urllib.request

        HERE       = pathlib.Path(__file__).resolve().parent
        CONFIG_DIR = pathlib.Path.home() / ".config" / "agnes"

        def _load_creds():
            \"\"\"Return (server_url, pat) or raise SystemExit.\"\"\"
            # Persistent creds file written by setup.py — survives VM restarts
            cf = HERE / ".agnes-creds.json"
            if cf.exists():
                try:
                    b = json.loads(cf.read_text())
                    u = b.get("server_url", "").rstrip("/")
                    p = b.get("access_token", "")
                    if u and p:
                        return u, p
                except Exception:
                    pass
            # Pre-setup: pre-baked PAT in bundle JSON (before setup.py runs)
            bf = HERE / "agnes-bundle.json"
            if bf.exists():
                try:
                    b = json.loads(bf.read_text())
                    u = b.get("server_url", "").rstrip("/")
                    p = b.get("access_token", "")
                    if u and p:
                        return u, p
                except Exception:
                    pass
            # Fallback: ~/.config/agnes/ (may not persist in cowork VM)
            cfg = CONFIG_DIR / "config.yaml"
            tok = CONFIG_DIR / "token.json"
            if cfg.exists() and tok.exists():
                try:
                    m = re.search(r"server:\\s*(.+)", cfg.read_text())
                    u = m.group(1).strip() if m else ""
                    p = json.loads(tok.read_text()).get("access_token", "")
                    if u and p:
                        return u, p
                except Exception:
                    pass
            print("ERROR: Agnes credentials not found. Run setup.py first.")
            sys.exit(2)

        def _api(method, path, server_url, pat, body=None, timeout=30):
            url = server_url.rstrip("/") + path
            hdrs = {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    return {"error": json.loads(e.read()).get("detail", str(e))}
                except Exception:
                    return {"error": str(e)}
            except Exception as e:
                return {"error": str(e)}

        def main():
            args = sys.argv[1:]
            if not args or args[0] in ("-h", "--help"):
                print("Usage: python3 agnes.py <command> [args]")
                print("Commands:")
                print("  catalog                  List all accessible tables")
                print("  schema <table_id>        Show columns and types")
                print("  describe <table_id> [n]  Schema + sample rows (default 5)")
                print("  query '<sql>'            Run SQL (server-side)")
                print("  info                     Check server connectivity")
                print("  skills                   List marketplace skills")
                return

            server_url, pat = _load_creds()
            cmd = args[0]

            if cmd in ("info", "server_info"):
                health = _api("GET", "/api/health", server_url, pat, timeout=5)
                access = _api("GET", "/api/me/effective-access", server_url, pat, timeout=5)
                print(json.dumps({"server": health, "access": access, "server_url": server_url}, indent=2))

            elif cmd == "catalog":
                print(json.dumps(_api("GET", "/api/v2/catalog", server_url, pat), indent=2))

            elif cmd == "schema":
                if len(args) < 2:
                    print("Usage: python3 agnes.py schema <table_id>")
                    sys.exit(1)
                print(json.dumps(
                    _api("GET", f"/api/v2/schema/{args[1]}", server_url, pat), indent=2
                ))

            elif cmd == "describe":
                if len(args) < 2:
                    print("Usage: python3 agnes.py describe <table_id> [rows]")
                    sys.exit(1)
                tid  = args[1]
                rows = int(args[2]) if len(args) > 2 else 5
                sc = _api("GET", f"/api/v2/schema/{tid}", server_url, pat)
                sm = _api("GET", f"/api/v2/sample/{tid}?n={rows}", server_url, pat)
                print(json.dumps({"schema": sc, "sample": sm}, indent=2))

            elif cmd == "query":
                if len(args) < 2:
                    print("Usage: python3 agnes.py query '<sql>'")
                    sys.exit(1)
                sql = " ".join(args[1:])
                print(json.dumps(
                    _api("POST", "/api/query", server_url, pat,
                         body={"sql": sql, "limit": 1000}, timeout=60), indent=2
                ))

            elif cmd == "skills":
                print(json.dumps(
                    _api("GET", "/api/v2/marketplace/skills", server_url, pat), indent=2
                ))

            else:
                print(f"Unknown command: {cmd}")
                print("Run `python3 agnes.py --help` for usage.")
                sys.exit(1)

        main()
    """)


def _bundle_setup_py(server_url: str) -> str:
    """Return setup.py content — pure stdlib, no pip required.

    The primary path (steps 1-4) is entirely file I/O — no network call.
    The bundle now contains a pre-baked PAT (``access_token``), so setup
    works even inside Claude Desktop's sandboxed Bash tool which blocks
    outbound HTTP to external servers.

    Step 5 (fetch server-rendered CLAUDE.md) and step 6 (agnes pull) are
    best-effort network calls that are silently skipped when unreachable —
    the hook will retry them on the next session open as a system process.

    Fallback flags for Terminal use (when access_token is absent):
        python setup.py --server-url https://agnes.example.com --token <PAT>
    """
    return textwrap.dedent(f"""\
        #!/usr/bin/env python3
        \"\"\"Agnes Cowork one-time setup — no external packages needed.\"\"\"
        from __future__ import annotations
        import json, os, pathlib, platform, subprocess, sys, urllib.error, urllib.request

        # ── CLI overrides (used from Terminal as fallback) ────────────────────
        _args = sys.argv[1:]
        def _flag(name):
            try: return _args[_args.index(name) + 1]
            except (ValueError, IndexError): return None

        _override_server = _flag("--server-url")
        _override_token  = _flag("--token")

        HERE = pathlib.Path(__file__).parent
        BUNDLE_FILE = HERE / "agnes-bundle.json"

        if not BUNDLE_FILE.exists():
            print("ERROR: agnes-bundle.json not found. Download a fresh bundle.")
            sys.exit(1)

        bundle = json.loads(BUNDLE_FILE.read_text())
        server_url = (_override_server or bundle["server_url"]).rstrip("/")

        # ── 1. Resolve PAT ────────────────────────────────────────────────────
        # Preferred path: exchange setup_token → 90-day PAT (long-lived).
        # Fallback: pre-baked 24h access_token (works offline / in sandbox).
        # The exchange is single-use; if it was already consumed on a prior
        # run, the fallback token takes over for that session.
        setup_token  = bundle.get("setup_token", "")
        pre_baked    = bundle.get("access_token", "")
        user_email   = bundle.get("user_email", "")

        pat = _override_token or ""

        if not pat and setup_token:
            try:
                req = urllib.request.Request(
                    f"{{server_url}}/api/auth/exchange-setup-token",
                    data=json.dumps({{"setup_token": setup_token}}).encode(),
                    headers={{"Content-Type": "application/json"}},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read())
                pat = resp.get("access_token", "")
                user_email = resp.get("user_email", user_email)
                if pat:
                    print("Agnes connected (90-day token).")
            except Exception:
                pass  # fall through to pre-baked token below

        if not pat:
            pat = pre_baked  # 24h fallback — works offline/sandboxed

        if not pat:
            print("ERROR: No token available. Download a fresh bundle.")
            sys.exit(2)

        print(f"Setting up Agnes Cowork for {{user_email or server_url}} ...")

        # 2. Save credentials — pure file I/O, no network needed.
        #    The Agnes CLI reads server URL from config.yaml (YAML, not JSON)
        #    and the token from token.json.  We write both formats for
        #    compatibility but config.yaml is authoritative for the CLI.
        config_dir = pathlib.Path.home() / ".config" / "agnes"
        config_dir.mkdir(parents=True, exist_ok=True)
        # config.yaml — read by the CLI (pyyaml); simple single-key YAML
        # written without the yaml library (stdlib-only constraint).
        (config_dir / "config.yaml").write_text(f"server: {{server_url}}\\n")
        # config.json — read by setup.py itself and some older tooling
        (config_dir / "config.json").write_text(
            json.dumps({{"server": server_url}}, indent=2)
        )
        (config_dir / "token.json").write_text(
            json.dumps({{"access_token": pat}}, indent=2)
        )
        # Also write to the project folder — this file is on the Mac filesystem,
        # so it persists across cowork VM restarts (unlike ~/.config/agnes/ which
        # lives in the VM's home directory and may be ephemeral).
        (HERE / ".agnes-creds.json").write_text(
            json.dumps({{"server_url": server_url, "access_token": pat}}, indent=2)
        )
        print("Credentials saved.")

        # 3. Replace the one-time setup hook with a permanent pull hook.
        #    Keep stdio MCP (mcp_server.py) but switch to absolute path and
        #    ensure credentials are in place so it works after bundle cleanup.
        settings_path = HERE / ".claude" / "settings.json"
        if settings_path.exists():
            cfg = json.loads(settings_path.read_text())
            cfg.setdefault("hooks", {{}})
            cfg["hooks"]["SessionStart"] = [
                {{"hooks": [{{"type": "command", "command":
                    "agnes pull --quiet 2>/dev/null || true"
                }}]}},
            ]
            cfg["hooks"].pop("SessionEnd", None)
            cfg["mcpServers"] = {{
                "agnes": {{
                    "command": sys.executable,
                    "args": [str(HERE / "mcp_server.py")],
                }}
            }}
            settings_path.write_text(json.dumps(cfg, indent=2) + "\\n")
            print("Session hook installed (agnes pull on start).")

        # 3b. Register in the global Claude Desktop config so the MCP server
        #     is available even when opening via Claude Desktop (not Claude Code).
        _claude_cfg_path = None
        if platform.system() == "Darwin":
            _claude_cfg_path = (
                pathlib.Path.home() / "Library" / "Application Support"
                / "Claude" / "claude_desktop_config.json"
            )
        elif platform.system() == "Windows":
            _appdata = os.environ.get("APPDATA", "")
            if _appdata:
                _claude_cfg_path = (
                    pathlib.Path(_appdata) / "Claude" / "claude_desktop_config.json"
                )
        elif platform.system() == "Linux":
            _claude_cfg_path = (
                pathlib.Path.home() / ".config" / "Claude"
                / "claude_desktop_config.json"
            )
        if _claude_cfg_path:
            try:
                _desktop_cfg = {{}}
                if _claude_cfg_path.exists():
                    _desktop_cfg = json.loads(_claude_cfg_path.read_text())
                _desktop_cfg.setdefault("mcpServers", {{}})
                _desktop_cfg["mcpServers"]["agnes"] = {{
                    "type": "sse",
                    "url": f"{{server_url}}/api/mcp/sse",
                    "headers": {{"Authorization": f"Bearer {{pat}}"}},
                }}
                _claude_cfg_path.parent.mkdir(parents=True, exist_ok=True)
                _claude_cfg_path.write_text(json.dumps(_desktop_cfg, indent=2))
                print("Agnes registered in Claude Desktop config.")
                print("Restart Claude Desktop once to activate Agnes tools.")
            except Exception:
                pass  # best-effort; project-level settings.json is the fallback

        # 3c. Write MCP config to user-level ~/.claude/settings.json so the
        #     cowork VM's claude-code binary picks it up on the next session
        #     open — without requiring a full Claude Desktop restart.
        #     Project-level .claude/settings.json is also updated (step 3) but
        #     the cowork VM may not load project-level mcpServers; user-level
        #     settings are loaded regardless of which project is open.
        _user_claude_dir = pathlib.Path.home() / ".claude"
        _user_claude_dir.mkdir(parents=True, exist_ok=True)
        _user_settings_path = _user_claude_dir / "settings.json"
        try:
            _user_cfg = {{}}
            if _user_settings_path.exists():
                try:
                    _user_cfg = json.loads(_user_settings_path.read_text())
                except Exception:
                    _user_cfg = {{}}
            _user_cfg.setdefault("mcpServers", {{}})
            _user_cfg["mcpServers"]["agnes"] = {{
                "command": sys.executable,
                "args": [str(HERE / "mcp_server.py")],
            }}
            _user_settings_path.write_text(json.dumps(_user_cfg, indent=2) + "\\n")
            print("Agnes MCP registered in ~/.claude/settings.json (user-level).")
        except Exception:
            pass  # best-effort; project-level settings.json is the fallback

        # 4. Delete bundle file — credentials are now in ~/.config/agnes/
        try:
            BUNDLE_FILE.unlink()
        except Exception:
            pass  # best-effort; file may already be gone

        # 5. Best-effort: fetch server-rendered CLAUDE.md (needs network).
        #    Skipped silently if unreachable — the existing CLAUDE.md stays.
        try:
            req2 = urllib.request.Request(
                f"{{server_url}}/api/welcome?server_url={{server_url}}",
                headers={{"Authorization": f"Bearer {{pat}}"}},
            )
            with urllib.request.urlopen(req2, timeout=10) as r:
                welcome = json.loads(r.read())
            content = welcome.get("content", "")
            if content:
                (HERE / "CLAUDE.md").write_text(content, encoding="utf-8")
                print("CLAUDE.md updated with your Agnes context.")
        except Exception:
            pass  # best-effort; hooks handle the full pull on next session

        # 6. Best-effort: run agnes pull to pre-cache data for offline queries.
        try:
            import shutil as _shutil
            agnes_bin = _shutil.which("agnes") or ""
            if agnes_bin:
                result = subprocess.run(
                    [agnes_bin, "pull", "--quiet"],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0:
                    print("Initial data sync complete.")
        except Exception:
            pass  # best-effort; MCP pull tool handles it on demand

        print()
        print("Agnes Cowork ready!")
        print(f"  Server : {{server_url}}")
        print(f"  Account: {{user_email}}")
        print(f"  MCP    : starts automatically when you open this project")
        print()
        print("Ask: \\"What data do I have access to?\\"")
    """)


def _bundle_claude_md(server_url: str, user_email: str, expires_at: datetime) -> str:
    """Return CLAUDE.md content for the setup bundle.

    Primary path: Bash tool with `python3 agnes.py` — works immediately,
    no restart needed, works inside the cowork VM.
    Bonus path: MCP tools — may become available after Claude Desktop restart
    once setup.py has registered them in claude_desktop_config.json.
    """
    exp_str = expires_at.strftime("%Y-%m-%d %H:%M UTC")
    tokens_url = server_url.rstrip("/") + "/tokens"
    return textwrap.dedent(f"""\
        # Agnes Cowork workspace

        Agnes server: {server_url}  |  Account: {user_email}

        ## ⚡ Agnes data is available RIGHT NOW via the Bash tool

        **Do NOT wait for MCP tools. Do NOT mention MCP. Do NOT ask the user to restart.**

        Agnes works through `agnes.py` in this folder. The Bash tool is all you need.

        **When the user asks about data — immediately run:**
        ```
        python3 agnes.py catalog
        ```

        That's it. No setup. No install. No restart. Just run it.

        ## All commands

        ```
        python3 agnes.py catalog              # list all accessible tables
        python3 agnes.py schema <table_id>    # columns and types
        python3 agnes.py describe <table_id>  # schema + sample rows
        python3 agnes.py query 'SELECT ...'   # run SQL server-side
        python3 agnes.py info                 # check connectivity
        python3 agnes.py skills               # list marketplace skills
        ```

        ## Rules — read carefully

        1. **Agnes = Bash tool.** `python3 agnes.py` IS the Agnes integration. It is always available.
        2. **Never say "Agnes is not available" or "MCP tools not loaded."** Agnes works via Bash, not MCP.
        3. **Never ask the user to restart, reinstall, or run anything in Terminal.**
        4. **Never say Agnes needs setup.** Setup already ran automatically at session start.
        5. If `python3 agnes.py catalog` returns an error, show the exact error output and diagnose it.
           Do not assume Agnes is broken — diagnose the specific error.

        ## Quick start: type `/setup-cowork`

        Runs `catalog`, `skills`, and a first `describe` automatically.

        ## Bundle info

        Expires {exp_str}. Fresh bundle: {tokens_url}
    """)


def _bundle_skill_setup_cowork() -> str:
    """Return .claude/skills/setup-cowork.md for the bundle.

    Invoked by the user as /setup-cowork inside the cowork workspace.
    Guides Claude through: verify connectivity → show available tables →
    list marketplace skills → run a first query.
    """
    return textwrap.dedent("""\
        ---
        description: Guided Agnes Cowork setup — verify connection, explore your data, try a skill
        ---

        Run this flow immediately when /setup-cowork is invoked.
        Use the Bash tool for every step. Agnes works via `python3 agnes.py` — NOT via MCP tools.
        Do not mention MCP. Do not ask the user to do anything. Just run the commands.

        ## Step 1 — Run catalog immediately

        ```bash
        python3 agnes.py catalog
        ```

        If this returns an error, show the raw output and stop. Otherwise continue.

        Present tables to the user grouped by source. Note which are `local` (cached)
        vs `remote` (live query). Pick 1-2 most interesting based on name/description.

        ## Step 2 — Check available skills

        ```bash
        python3 agnes.py skills
        ```

        If skills are returned, list them with one-line descriptions.
        Tell the user they can invoke skills with `/skill-name`.
        If empty or error, skip silently.

        ## Step 3 — Explore the most interesting table

        Pick the best table from Step 1 and run:
        ```bash
        python3 agnes.py describe <table_id>
        ```

        Based on the schema and sample rows, suggest one concrete question the user
        could ask right now. For example: "Want me to find the top 10 accounts by revenue?"

        ## Done

        Agnes is ready. The user can ask any data question in plain language.
        You answer by running `python3 agnes.py query 'SELECT ...'` via Bash.
    """)


def _build_bundle_zip(
    server_url: str,
    setup_token: str,
    access_token: str,
    user_email: str,
    expires_at: datetime,
    folder_name: str,
) -> bytes:
    """Return a ZIP archive as bytes.

    Unzipping the archive creates a single top-level directory ``folder_name/``
    containing the workspace files. The user opens that directory in Claude Code
    and the ``SessionStart`` hook handles the rest.

    Structure::

        <folder_name>/
        ├── agnes-bundle.json         ← pre-baked PAT + setup token + server URL
        ├── setup.py                  ← one-time setup (writes .agnes-creds.json)
        ├── agnes.py                  ← pure-stdlib CLI; Claude calls via Bash tool
        ├── mcp_server.py             ← stdio MCP proxy (if cowork VM loads it)
        ├── .claude/
        │   ├── settings.json         ← SessionStart hook + mcpServers config
        │   └── skills/
        │       └── setup-cowork.md   ← /setup-cowork guided onboarding skill
        └── CLAUDE.md                 ← user + agent guidance (Bash-first)

    The ``access_token`` field in ``agnes-bundle.json`` is a short-lived PAT
    (same 24 h TTL as the bundle) so ``setup.py`` can save credentials without
    any outbound HTTP — works inside Claude Desktop's sandboxed Bash tool.
    ``setup_token`` is kept for the ``agnes init --bundle`` CLI path which runs
    outside the sandbox and does the full server-side exchange.
    """
    bundle_json = json.dumps({
        "version": 1,
        "server_url": server_url,
        "setup_token": setup_token,
        "access_token": access_token,
        "user_email": user_email,
        "expires_at": expires_at.isoformat(),
    }, indent=2)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{folder_name}/agnes-bundle.json", bundle_json)
        zf.writestr(f"{folder_name}/setup.py", _bundle_setup_py(server_url))
        zf.writestr(f"{folder_name}/mcp_server.py", _bundle_mcp_server_py())
        zf.writestr(f"{folder_name}/agnes.py", _bundle_agnes_py())
        zf.writestr(f"{folder_name}/.claude/settings.json", _bundle_settings_json(server_url, access_token))
        zf.writestr(f"{folder_name}/.claude/skills/setup-cowork.md", _bundle_skill_setup_cowork())
        zf.writestr(
            f"{folder_name}/CLAUDE.md",
            _bundle_claude_md(server_url, user_email, expires_at),
        )
    return buf.getvalue()


# ── request / response models ─────────────────────────────────────────────────

class ExchangeRequest(BaseModel):
    setup_token: str


class ExchangeResponse(BaseModel):
    access_token: str
    server_url: str
    user_email: str


class SetupTokenItem(BaseModel):
    id: str
    created_at: str
    expires_at: str


# ── endpoints ─────────────────────────────────────────────────────────────────

@user_router.post("/cowork-bundle", status_code=200)
async def generate_bundle(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Generate a Cowork Setup Bundle ZIP for the calling user.

    Returns a ``application/zip`` file containing a setup script, a
    ``README.txt``, and a ```.agnes-bundle.json`` with a short-lived setup
    token embedded. The analyst unzips and runs ``./setup.sh``, which calls
    ``agnes init --bundle .`` to exchange the setup token for a PAT and
    bootstrap their workspace in one step.

    Rate-limited: max 5 active (unexpired, unused) setup tokens per user.
    """
    repo = SetupTokenRepository(conn)

    active_count = repo.count_active_for_user(user["id"])
    if active_count >= _MAX_ACTIVE_TOKENS:
        raise HTTPException(
            status_code=400,
            detail={
                "kind": "too_many_setup_tokens",
                "hint": (
                    f"You have {active_count} active setup tokens. "
                    "Revoke unused ones before generating a new bundle."
                ),
                "active_count": active_count,
            },
        )

    raw_token = _generate_setup_token()
    token_hash = _hash_token(raw_token)
    token_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + _SETUP_TOKEN_TTL
    server_url = (
        os.environ.get("AGNES_BASE_URL") or str(request.base_url)
    ).rstrip("/")

    repo.create(
        id=token_id,
        user_id=user["id"],
        token_hash=token_hash,
        expires_at=expires_at,
    )

    # Pre-bake a short-lived PAT (same TTL as the bundle) so setup.py can
    # save credentials without any outbound HTTP — works inside Claude
    # Desktop's sandboxed Bash tool which blocks external network calls.
    import hashlib as _hl
    pat_id = str(uuid.uuid4())
    pat_jwt = create_access_token(
        user_id=user["id"],
        email=user.get("email", ""),
        token_id=pat_id,
        typ="pat",
        expires_delta=_SETUP_TOKEN_TTL,
        extra_claims={"scope": "cowork-bundle"},
    )
    AccessTokenRepository(conn).create(
        id=pat_id,
        user_id=user["id"],
        name="Agnes Cowork Setup (auto-generated)",
        token_hash=_hl.sha256(pat_jwt.encode()).hexdigest(),
        prefix=pat_id.replace("-", "")[:8],
        expires_at=expires_at,
    )

    _audit(conn, user["id"], "cowork_bundle.create", token_id,
           {"server_url": server_url})

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder_name = f"agnes-cowork-setup-{ts}"
    filename = f"{folder_name}.zip"

    zip_bytes = _build_bundle_zip(
        server_url=server_url,
        setup_token=raw_token,
        access_token=pat_jwt,
        user_email=user.get("email", ""),
        expires_at=expires_at,
        folder_name=folder_name,
    )

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@user_router.get("/setup-tokens", response_model=List[SetupTokenItem])
async def list_setup_tokens(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List the calling user's active (unexpired, unused) setup tokens."""
    rows = SetupTokenRepository(conn).list_active_for_user(user["id"])
    return [
        SetupTokenItem(
            id=r["id"],
            created_at=str(r["created_at"]),
            expires_at=str(r["expires_at"]),
        )
        for r in rows
    ]


@user_router.delete("/setup-tokens/{token_id}", status_code=204)
async def revoke_setup_token(
    token_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Revoke (delete) a setup token. Only the owner may revoke their own tokens."""
    repo = SetupTokenRepository(conn)
    rows = repo.list_active_for_user(user["id"])
    owned = {r["id"] for r in rows}
    if token_id not in owned:
        raise HTTPException(status_code=404, detail="Setup token not found")
    repo.delete(token_id)
    _audit(conn, user["id"], "cowork_bundle.revoke", token_id)


@auth_router.post("/exchange-setup-token", response_model=ExchangeResponse)
async def exchange_setup_token(
    payload: ExchangeRequest,
    request: Request,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Exchange a one-time setup token for a regular PAT.

    This endpoint requires **no prior authentication** — the setup token
    embedded in the bundle IS the authentication credential.

    Security properties:
    - Token is matched by SHA-256 hash, never stored or logged in plaintext.
    - Single-use: atomically marked used on first consumption.
    - Short-lived: 24 h TTL enforced server-side regardless of client clock.
    - On success, the setup token is consumed and cannot be replayed.
    """
    if not payload.setup_token.startswith("st_"):
        raise HTTPException(status_code=400, detail="Invalid token format")

    token_hash = _hash_token(payload.setup_token)
    repo = SetupTokenRepository(conn)
    row = repo.get_by_hash(token_hash)

    now = datetime.now(timezone.utc)

    if not row:
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")

    if row["expires_at"] and row["expires_at"].replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=401, detail="Setup token has expired")

    if row["used_at"] is not None:
        raise HTTPException(status_code=401, detail="Setup token has already been used")

    # Atomically claim the token (prevents concurrent replay)
    claimed = repo.mark_used(row["id"])
    if not claimed:
        raise HTTPException(status_code=401, detail="Setup token has already been used")

    # Fetch the user the token belongs to
    user_row = UserRepository(conn).get_by_id(row["user_id"])
    if not user_row or user_row.get("deleted_at"):
        raise HTTPException(status_code=401, detail="Account not found")

    # Mint a PAT for the user (90-day default, scope "cowork")
    token_id = str(uuid.uuid4())
    expires_delta = timedelta(days=90)
    jwt_token = create_access_token(
        user_id=user_row["id"],
        email=user_row["email"],
        token_id=token_id,
        typ="pat",
        expires_delta=expires_delta,
        extra_claims={"scope": "cowork"},
    )
    import hashlib as _hl
    pat_hash = _hl.sha256(jwt_token.encode()).hexdigest()
    prefix = token_id.replace("-", "")[:8]
    expires_at = now + expires_delta
    AccessTokenRepository(conn).create(
        id=token_id,
        user_id=user_row["id"],
        name="Agnes Cowork (auto-generated)",
        token_hash=pat_hash,
        prefix=prefix,
        expires_at=expires_at,
    )

    _audit(conn, user_row["id"], "cowork_bundle.exchange", row["id"],
           {"pat_id": token_id})

    server_url = (
        os.environ.get("AGNES_BASE_URL") or str(request.base_url)
    ).rstrip("/")
    return ExchangeResponse(
        access_token=jwt_token,
        server_url=server_url,
        user_email=user_row.get("email", ""),
    )
