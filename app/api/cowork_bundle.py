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

def _bundle_settings_json(server_url: str, access_token: str) -> str:
    """Return .claude/settings.json content for the setup bundle.

    Uses SSE transport so the MCP server works from Claude Desktop's cowork
    VM — which cannot reach localhost but CAN reach the Agnes public URL.

    The hook on first session open:
      1. Runs setup.py which exchanges the setup token for a 90-day PAT and
         updates settings.json with the long-lived token.
      2. Registers the stdio MCP server in Claude Desktop's global config for
         users who open the workspace directly in Claude Desktop (not cowork).
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
        # SSE transport: works from cowork VM (public Agnes URL) and from
        # CLI mode. setup.py replaces the short-lived token with a 90-day PAT.
        "mcpServers": {
            "agnes": {
                "type": "sse",
                "url": f"{server_url}/api/mcp/sse",
                "headers": {
                    "Authorization": f"Bearer {access_token}",
                },
            }
        },
    }
    return json.dumps(cfg, indent=2) + "\n"


def _bundle_mcp_server_py() -> str:
    """Return mcp_server.py — a bundled MCP launcher, pure stdlib + pip.

    Placed in the ZIP root so .claude/settings.json can reference it as
    ``python3 mcp_server.py`` without any absolute paths.  After setup.py
    runs, settings.json is updated to use the absolute path.

    On startup (called by Claude Code when the project is opened) it:

    1. **Bootstraps credentials** — if ``agnes-bundle.json`` is still present
       (setup.py hasn't run yet) it reads the pre-baked PAT and writes
       ``~/.config/agnes/config.yaml`` + ``token.json`` so the MCP tools
       can authenticate on first launch, before the SessionStart hook fires.

    2. **Installs the agnes package** if not already importable — runs
       ``pip install --user agnes-the-ai-analyst`` silently, then restarts
       itself so the new packages are on ``sys.path``.  No binary needed.

    3. **Runs the MCP server** by importing ``cli.mcp.server`` and calling
       ``run()`` — no binary search, no hard-coded paths.

    This solves the chicken-and-egg problem: MCP is live on the FIRST session
    open even before setup.py has run, so the analyst can ask
    "What data do I have access to?" immediately.
    """
    return textwrap.dedent("""\
        #!/usr/bin/env python3
        \"\"\"Agnes MCP launcher — bundled in the Cowork ZIP.

        Called by Claude Code on project open via .claude/settings.json:
            mcpServers.agnes.command = python3 (absolute after setup)
            mcpServers.agnes.args    = [<absolute path to this file>]

        Bootstraps credentials from agnes-bundle.json if needed, installs
        the agnes package if not present, then runs the MCP server so
        Claude has Agnes tools from the very first session — no Terminal needed.
        \"\"\"
        from __future__ import annotations
        import json, pathlib, site, subprocess as _sp, sys, threading

        HERE = pathlib.Path(__file__).resolve().parent
        BUNDLE_FILE = HERE / "agnes-bundle.json"
        CONFIG_DIR  = pathlib.Path.home() / ".config" / "agnes"

        # ── 1. Bootstrap credentials from bundle if not already configured ──
        # MCP starts BEFORE the SessionStart hook fires, so we seed
        # ~/.config/agnes/ here to make tools usable on the very first open.
        token_file = CONFIG_DIR / "token.json"
        if BUNDLE_FILE.exists() and not token_file.exists():
            try:
                bundle = json.loads(BUNDLE_FILE.read_text())
                server_url = bundle.get("server_url", "").rstrip("/")
                pat = bundle.get("access_token", "")
                if server_url and pat:
                    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                    (CONFIG_DIR / "config.yaml").write_text(f"server: {server_url}\\n")
                    (CONFIG_DIR / "config.json").write_text(
                        json.dumps({"server": server_url}, indent=2)
                    )
                    token_file.write_text(json.dumps({"access_token": pat}, indent=2))
            except Exception:
                pass  # best-effort; setup.py will fix it on next hook run

        # ── 2. Fast path: Agnes already installed → start full MCP server ─────
        try:
            from cli.mcp.server import run as _run_mcp
            _run_mcp()   # blocking; process stays here
            sys.exit(0)
        except ImportError:
            pass

        # ── 3. Agnes not installed — placeholder MCP + background pip install ──
        # Claude Code has a short MCP-init timeout; we must respond to
        # `initialize` immediately.  Start a minimal pure-stdlib server now,
        # install agnes-the-ai-analyst in a background thread, then tell the
        # user to restart Claude Code once the install is done.
        _done  = threading.Event()
        _err: list[str] = []

        def _bg() -> None:
            try:
                _sp.run(
                    [sys.executable, "-m", "pip", "install",
                     "--quiet", "--user", "agnes-the-ai-analyst"],
                    check=True, stdout=_sp.DEVNULL,
                )
                _user = site.getusersitepackages()
                if _user not in sys.path:
                    sys.path.insert(0, _user)
            except Exception as e:
                _err.append(str(e))
            finally:
                _done.set()

        threading.Thread(target=_bg, daemon=True).start()

        def _send(obj: dict) -> None:
            sys.stdout.write(json.dumps(obj) + "\\n")
            sys.stdout.flush()

        _TOOL = {
            "name": "status",
            "description": (
                "Check Agnes setup status. Agnes is installing in the background. "
                "Call this to see if the install is done, then restart Claude Code."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        }

        for _raw in sys.stdin:
            _raw = _raw.strip()
            if not _raw:
                continue
            try:
                _msg = json.loads(_raw)
            except ValueError:
                continue
            _m  = _msg.get("method", "")
            _id = _msg.get("id")
            if _m == "initialize":
                _send({"jsonrpc": "2.0", "id": _id, "result": {
                    "protocolVersion": _msg.get("params", {}).get(
                        "protocolVersion", "2024-11-05"
                    ),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Agnes", "version": "installing"},
                }})
            elif _m == "tools/list":
                _send({"jsonrpc": "2.0", "id": _id, "result": {"tools": [_TOOL]}})
            elif _m == "tools/call":
                if _err:
                    _text = (
                        f"Agnes install failed: {_err[0]}\\n"
                        "Open Terminal and run: pip install agnes-the-ai-analyst"
                    )
                elif _done.is_set():
                    _text = (
                        "Agnes installed! Restart Claude Code to load all Agnes tools."
                    )
                else:
                    _text = (
                        "Agnes is installing in the background (first run takes ~60s). "
                        "Restart Claude Code when done."
                    )
                _send({"jsonrpc": "2.0", "id": _id, "result": {
                    "content": [{"type": "text", "text": _text}]
                }})
            elif _id is not None:
                _send({"jsonrpc": "2.0", "id": _id,
                       "error": {"code": -32601, "message": "Method not found"}})
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

        # ── 1. Resolve PAT — prefer pre-baked access_token (no HTTP needed) ───
        # The bundle embeds a short-lived PAT so setup works without any
        # network access (e.g. from inside Claude Desktop's sandboxed shell).
        # Falls back to: --token flag, then setup_token HTTP exchange.
        pat = _override_token or bundle.get("access_token", "")
        user_email = bundle.get("user_email", "")

        if not pat:
            # Last resort: exchange setup_token via HTTP (Terminal fallback)
            setup_token = bundle.get("setup_token", "")
            if not setup_token:
                print("ERROR: No access_token or setup_token found in bundle.")
                sys.exit(1)
            print(f"Connecting to {{server_url}} ...")
            try:
                req = urllib.request.Request(
                    f"{{server_url}}/api/auth/exchange-setup-token",
                    data=json.dumps({{"setup_token": setup_token}}).encode(),
                    headers={{"Content-Type": "application/json"}},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read())
                pat = resp["access_token"]
                user_email = resp.get("user_email", user_email)
            except Exception as exc:
                print(f"ERROR: Cannot reach server and no pre-baked token: {{exc}}")
                print(f"  Re-run from Terminal: python3 setup.py --server-url {{server_url}} --token <PAT>")
                print(f"  Get your PAT at: {{server_url}}/tokens")
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
        print("Credentials saved.")

        # 3. Replace the one-time setup hook with a permanent pull hook.
        #    Switch mcpServers to SSE transport with the long-lived 90-day PAT
        #    so MCP keeps working after the short-lived bundle token expires.
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
                    "type": "sse",
                    "url": f"{{server_url}}/api/mcp/sse",
                    "headers": {{"Authorization": f"Bearer {{pat}}"}},
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

    Instructs Claude to use MCP tools directly and not ask the user
    to install anything or run terminal commands.
    """
    exp_str = expires_at.strftime("%Y-%m-%d %H:%M UTC")
    tokens_url = server_url.rstrip("/") + "/tokens"
    return textwrap.dedent(f"""\
        # Agnes Cowork

        This workspace connects you directly to the **Agnes** data platform.

        **Server:** {server_url}
        **Account:** {user_email}

        ## Agnes MCP tools (use these — no CLI needed)

        Agnes is connected as an MCP server. Call these tools directly:

        | Tool | Description |
        |------|-------------|
        | `catalog()` | List all tables you have access to — **start here** |
        | `schema(table_id)` | Column names, types, and SQL hints |
        | `describe(table_id)` | Schema + sample rows |
        | `query(sql)` | Run SQL against Agnes data (server-side, any query_mode) |
        | `query_local(sql)` | Query local parquet cache offline (after `pull()`) |
        | `pull()` | Sync fresh data from the server to local cache |
        | `server_info()` | Check connectivity and your account |

        **Always call `catalog()` first** to discover what tables exist.
        Then `schema(table_id)` before writing any SQL.

        ## Rules for this workspace

        - **Use MCP tools for all Agnes data queries.** Do NOT use the Bash tool
          for data access — Agnes tools have direct server access, Bash does not.
        - **Do NOT ask the user to install anything or run terminal commands.**
          The Agnes package installs itself automatically in the background.
        - **Do NOT ask about the Agnes CLI.** Everything works through MCP.

        ### If only a `status` tool is available (first-time setup)

        Agnes is still installing its packages. Call `status()` to check progress,
        then tell the user:

        > "Agnes is installing in the background (first run ~60s). I'll let you know
        > when it's ready — just restart Claude Code once the install is done and
        > all Agnes tools will be available."

        Do NOT try to use Bash for data access. Do NOT ask for Terminal commands.

        ## Typical workflow

        ```
        catalog()                        # discover tables
        schema("crm_accounts")           # understand columns
        query("SELECT COUNT(*) FROM crm_accounts")  # run SQL
        ```

        ## Bundle info

        Expires {exp_str}.
        Download a fresh bundle at {tokens_url} if expired.
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
        ├── setup.py                  ← pure file I/O setup (no network needed)
        ├── .claude/
        │   └── settings.json         ← SessionStart: ``python3 setup.py``
        └── CLAUDE.md                 ← user + agent guidance

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
        zf.writestr(f"{folder_name}/.claude/settings.json", _bundle_settings_json(server_url, access_token))
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
    server_url = str(request.base_url).rstrip("/")

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

    server_url = str(request.base_url).rstrip("/")
    return ExchangeResponse(
        access_token=jwt_token,
        server_url=server_url,
        user_email=user_row.get("email", ""),
    )
