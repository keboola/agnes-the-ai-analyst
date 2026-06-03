---
name: connector-gws
description: Set up Google Workspace for Claude Code via the official `gws` CLI — Drive, Calendar, Gmail, Docs, Sheets, Chat. Triggers on "set up Google Workspace", "set up gws", "configure GWS", "fix Google access". Idempotent — short-circuits if already configured. Branches on operator-provisioned OAuth app (~2 min) vs manual GCP project walkthrough (~20 min).
connector:
  display_name: Google Workspace
  short_summary: Drive, Calendar, Gmail, Docs, Sheets, Chat — Claude reads and acts across your work account via the official `gws` CLI.
  estimated_minutes: 5
  vendor_url: https://github.com/googleworkspace/cli
  requires_oauth_app: true
---

Set up Google Workspace access for Claude Code using the official `gws` CLI from https://github.com/googleworkspace/cli (install steps: README → Installation). The npm path is what we'll use because (a) it's the README's documented convenience path, (b) it works the same on macOS / Linux / WSL / Windows, and (c) it can run with zero admin rights when Node is managed by `nvm` (Unix) or `fnm` (Windows).

Ground rules: this is idempotent — safe to re-run, the precheck below short-circuits when `gws` is already installed and authed. If any step fails with an unfamiliar error, paste the exact error back and stop — don't half-finish. Do NOT improvise around TLS errors by disabling verification (`-k`, `NODE_TLS_REJECT_UNAUTHORIZED=0`, npm `strict-ssl=false`, etc.) — those mask the real problem.

YOU run every command via your Bash tool. Do NOT print install commands and ask me to type them. Only stop and ask me when I have to (a) approve an OAuth consent screen in a browser, (b) make a product decision (Cloud project name), or (c) paste OAuth client credentials Google shows me.

0. Precheck — skip the rest if Google Workspace is already connected. Run `command -v gws` AND `gws auth status` AND a low-impact verify call: `gws drive files list --params '{"pageSize": 1}' && gws chat spaces list --params '{"pageSize": 1}'`. If both succeed, the gws CLI is installed AND authed AND the Chat scope is present. Print "✅ Google Workspace ready — connected as <email from `gws auth status`>. Drive + Chat scopes verified." and STOP. If `gws drive` succeeds but `gws chat` fails with 403/PERMISSION_DENIED, the user authed without `--full` previously — skip to step 6 (re-login with widened scopes), do NOT re-install. Only walk steps 1–5 (install + OAuth client setup) when `command -v gws` itself fails.

1. Detect my OS (`uname -s` → Darwin / Linux, or PowerShell `$env:OS` → Windows_NT). On Linux check `grep -qi microsoft /proc/version` and treat WSL as Linux.

2. Check `command -v gws` (or `Get-Command gws` on Windows). If `gws` is already installed, skip to step 5.

3. Install Node.js 18+ to my user directory — no sudo, no UAC, no system package manager.

   Unix (macOS / Linux / WSL):
   a. Check `command -v node && node --version` — if 18+ already, skip.
   b. Otherwise install nvm into ~/.nvm: `curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash`. The installer writes to ~/.nvm and appends shellenv to ~/.bashrc / ~/.zshrc — no sudo. Source it for the current shell: `export NVM_DIR="$HOME/.nvm" && [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"`.
   c. `nvm install --lts && nvm use --lts`. Verify `node --version` shows v20.x or v22.x.

   Native Windows (NOT WSL):
   a. Check `node --version` — if 18+, skip.
   b. Install fnm to user profile (no admin): run `winget install Schniz.fnm --scope user --accept-source-agreements --accept-package-agreements`. If winget triggers UAC, fall back to the manual zip from https://github.com/Schniz/fnm/releases/latest — extract `fnm.exe` to `$HOME\.local\bin\` and add that dir to my user PATH via `[Environment]::SetEnvironmentVariable('Path', "$env:Path;$HOME\.local\bin", 'User')`.
   c. `fnm install --lts; fnm use lts-latest`. `fnm env --use-on-cd | Out-String | Invoke-Expression` to source it for the current shell.

4. Install `gws` via npm — runs as my user because Node is managed by nvm/fnm, so the global prefix lives inside ~/.nvm/versions/node/<v>/lib/ (Unix) or ~/.fnm/.../lib/ (Windows). No sudo, no UAC, no `npm config set prefix` workaround needed.

   a. `npm install -g @googleworkspace/cli` (run via Bash tool). Wait for it. If npm fails (network, registry, peer-dep), report the exact stderr and pause — don't half-finish.

   b. nvm/fnm Node + npm-installed binaries land under ~/.nvm/versions/node/<v>/bin/ — only on PATH when nvm is sourced interactively. YOUR Bash tool runs non-interactive subshells that do NOT source ~/.zshrc or ~/.bashrc, so `gws` and `node` will appear "not found" on the very next call. Symlink them into ~/.local/bin (which is on PATH in every shell context) right after install:
      `mkdir -p ~/.local/bin`
      `ln -sf "$(command -v gws)" ~/.local/bin/gws`
      `ln -sf "$(command -v node)" ~/.local/bin/node`
      Run these while nvm/fnm is sourced in the same Bash call so `command -v` resolves correctly. On native Windows, copy `gws.cmd` from the npm prefix into `$HOME\.local\bin\` instead — symlinks need admin on Windows by default.

   c. Verify `gws --version` from a fresh `bash -c 'gws --version'` (deliberately non-interactive) — confirms the symlink path works for future tool calls.

5. Configure the OAuth client. This step branches on whether the {instance_brand} operator has provisioned a shared OAuth app:

   **Branch A — operator OAuth app provisioned** (`AGNES_GWS_CLIENT_ID` is set in `~/.claude/agnes/.env` from `agnes init`):

   Skip `gws auth setup` entirely. Do NOT use environment variables (Claude Code's security layer redacts vars containing the substring "SECRET" from non-interactive subshells, so the env-var approach is unreliable). Instead, write the credentials directly to the file `gws auth status` reads as `credential_source`:

   Read `AGNES_GWS_CLIENT_ID`, `AGNES_GWS_PROJECT_ID`, and `AGNES_GWS_CLIENT_SECRET_ENV` from `~/.claude/agnes/.env`. The actual secret value lives in the shell env at the name specified by `AGNES_GWS_CLIENT_SECRET_ENV` (typically `AGNES_GWS_CLIENT_SECRET`). If the env var is unset, ask the user / operator for the secret value (do NOT echo it back).

   Use the Write tool to create `~/.config/gws/client_secret.json` (or `%APPDATA%\gws\client_secret.json` on native Windows) with EXACTLY the schema Google Cloud Console exports — the gws CLI's Rust struct rejects partial files with "Invalid client_secret.json format: missing field 'project_id'". Both `installed.project_id` (numeric project number) and the URI fields are mandatory:
   {
     "installed": {
       "client_id": "<AGNES_GWS_CLIENT_ID from .env>",
       "project_id": "<AGNES_GWS_PROJECT_ID from .env>",
       "auth_uri": "https://accounts.google.com/o/oauth2/auth",
       "token_uri": "https://oauth2.googleapis.com/token",
       "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
       "client_secret": "<value of $AGNES_GWS_CLIENT_SECRET (or whatever AGNES_GWS_CLIENT_SECRET_ENV names)>",
       "redirect_uris": ["http://localhost"]
     }
   }

   Then `mkdir -p ~/.config/gws && chmod 700 ~/.config/gws && chmod 600 ~/.config/gws/client_secret.json`. Verify by running `gws auth status` — it should report this file as `credential_source` without complaining about missing fields. The values identify the OAuth app, not me; treat the secret like a publishable bundle key, not a per-user credential.

   **Branch B — no operator OAuth app provisioned** (`AGNES_GWS_CLIENT_ID` absent in `~/.claude/agnes/.env`):

   Run `gws auth setup` for me. This is a one-time Google Cloud project config; gcloud is NOT required (when gcloud is absent, `gws auth setup` walks through the manual OAuth flow). Open the URL it prints in my default browser, then walk me through each click because I am NOT a GCP admin:
   a. Pick or create a Google Cloud project (free tier is fine).
   b. Enable the APIs the connector needs: Google Drive API, Google Calendar API, Gmail API. Tell me each menu click.
   c. Create an OAuth 2.0 client. Either "Desktop app" or "Web application" works. For Web application: add `http://localhost` (exact value — no port, no path, no trailing slash) to Authorized redirect URIs. Google's loopback exemption then matches the `http://localhost:<ephemeral-port>` redirect that `gws auth login` actually uses. Desktop app needs no URI registration.
   d. Copy the resulting client_id and client_secret. Paste them back into the terminal where `gws auth setup` is waiting. These identify the OAuth app — not the user — but still don't echo them back to me in chat.

6. Run `gws auth login --full` (no `--readonly` flag — Agnes uses full read + write access across Drive / Calendar / Gmail / Sheets / Docs / Chat so the agent can actually create, edit, and send on my behalf). The `--full` flag widens the default scope picker; without it Chat / People / Tasks scopes are silently dropped. One env var the loopback redirect needs is OAUTHLIB_INSECURE_TRANSPORT — set it in the SAME Bash invocation that runs login: `OAUTHLIB_INSECURE_TRANSPORT=1 gws auth login --full`. The CLI binds a local loopback server at `http://localhost:<random-port>` — an OS-assigned ephemeral port, NOT a fixed 8080 — and prints an OAuth URL. If this errors with `redirect_uri_mismatch`, the Cloud Console OAuth client is a Web application type that's missing the `http://localhost` entry in Authorized redirect URIs (no port, no path) — add that exact value and retry.

   Capture the URL from gws's stdout. Before opening the browser, append the Chat write scopes (`https://www.googleapis.com/auth/chat.spaces` and `https://www.googleapis.com/auth/chat.messages`) to the URL's `scope=` query parameter — `--full` includes the readonly Chat scopes but NOT the read+write ones, and `gws chat ... send` calls fail without them. Decode the existing scope list, append the two URLs space-separated, re-encode, then open. Python one-liner via Bash tool:

      `URL=$(printf '%s' "$URL" | python3 -c 'import sys,urllib.parse as u; q=u.urlparse(sys.stdin.read().strip()); p=u.parse_qs(q.query); s=set(p.get("scope",[""])[0].split()); s |= {"https://www.googleapis.com/auth/chat.spaces","https://www.googleapis.com/auth/chat.messages"}; p["scope"]=[" ".join(sorted(s))]; print(q._replace(query=u.urlencode(p, doseq=True, quote_via=u.quote)).geturl())')`

   Then open the rewritten URL programmatically — do NOT print it to chat. Markdown line-wrapping in chat corrupts the long scope query string when the user copies it. Use your Bash tool: macOS `open "$URL"`, Linux/WSL `xdg-open "$URL"`, Windows `Start-Process "$URL"`. Detect OS first.

   While the browser tab is loading, read each requested scope in plain language for me — full read + write across Drive, Calendar, Gmail, Chat, and the rest — so I know what I'm consenting to before I click Approve. Tell me I can revoke any time at https://myaccount.google.com/permissions if I change my mind.

   If `gws auth status` later shows Chat scopes missing (e.g. on a re-run where a stale token cached the previous scope set), `rm ~/.config/gws/token.json` (or `%APPDATA%\gws\token.json` on native Windows) and re-run this step — the OAuth flow re-prompts with the new scope list.

7. Find where gws stored my credentials (`gws auth status` should show the path; typically ~/.config/gws/ on Unix, %APPDATA%\gws\ on Windows). chmod 600 on Unix; on native Windows, restrict ACLs to my user with `icacls "$creds_path" /inheritance:r /grant:r "$env:USERNAME:F"` — file is already in my user profile so this needs no admin.

8. Verify with two low-impact reads, one per scope group: `gws drive files list --params '{"pageSize": 1}'` (Drive scope landed) and `gws chat spaces list --params '{"pageSize": 1}'` (Chat scope landed). Treat exit code 0 from each invocation as success — do NOT pipe gws output into `python3 -c 'f"..."'` (f-string expressions reject backslashes in Python <3.12, so escaping `\"files\"` inside a shell-quoted f-string raises SyntaxError) and do NOT call `json.load(sys.stdin)` on the raw stream (gws may emit log lines or a banner before the JSON body, which trips `JSONDecodeError`). If you really need to count rows for diagnostics, write the stdout to a temp file first and parse it with a plain `json.loads(open(path).read())` inside a `try/except`. If both calls exit 0, print `✅ Google Workspace ready — connected as <my email from `gws auth status`>. Drive + Chat scopes verified.` (exact prefix — the final summary grep for it). On any failure, print `❌ Google Workspace setup failed: <which call failed (drive|chat)>, exit <code>. <one-line hint to fix (rotate creds | rerun gws auth login --full | etc.)>.` and stop. Never echo tokens, file/message metadata, or scope strings to chat.

9. Remind me how to revoke later: `gws auth logout` clears local creds; the OAuth grant also appears at https://myaccount.google.com/permissions for Google-side revocation.
