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

Ground rules: this is idempotent — safe to re-run, the precheck below short-circuits when `gws` is already installed and authed. If any step fails with an unfamiliar error, paste the exact error back and stop — don't half-finish. If a TLS error appears, find its cause — corporate proxy, internal CA, clock skew — rather than lowering certificate verification; that masks the problem instead of solving it.

Run the commands yourself via your Bash tool, but show me what you are about to run before anything that installs software or writes credentials. Stop and ask whenever I have to approve an OAuth consent screen in a browser, make a product decision (Cloud project name), or supply OAuth client credentials.

0. Precheck — skip the rest if Google Workspace is already connected. Run `command -v gws` AND `gws auth status` AND a low-impact verify call: `gws drive files list --params '{"pageSize": 1}' && gws chat spaces list --params '{"pageSize": 1}'`. If both succeed, the gws CLI is installed AND authed AND the Chat scope is present. Print "✅ Google Workspace ready — connected as <email from `gws auth status`>. Drive + Chat scopes verified." and stop. (The account e-mail is the `user` field of the `gws auth status` output — not `user_email` or `account`.) If `gws drive` succeeds but `gws chat` fails with 403/PERMISSION_DENIED, the user authed without `--full` previously — skip to step 6 (re-login with widened scopes), don't re-install. If either call fails with `Caller does not have required permission to use project …` / `serviceusage.serviceUsageConsumer`, the stored `client_secret.json` carries a non-empty `project_id` (older installs wrote one) — edit `~/.config/gws/client_secret.json` (`%APPDATA%\gws\client_secret.json` on native Windows) so `installed.project_id` is `""`, then re-run the two verify calls; the saved token stays valid, don't re-install or re-auth. Only walk steps 1–5 (install + OAuth client setup) when `command -v gws` itself fails.

1. Detect my OS (`uname -s` → Darwin / Linux, or PowerShell `$env:OS` → Windows_NT). On Linux check `grep -qi microsoft /proc/version` and treat WSL as Linux.

2. Check `command -v gws` (or `Get-Command gws` on Windows). If `gws` is already installed, skip to step 5.

3. Install Node.js 18+ to my user directory — no sudo, no UAC, no system package manager.

   Unix (macOS / Linux / WSL):
   a. Check `command -v node && node --version` — if 18+ already, skip.
   b. Otherwise offer to install nvm into ~/.nvm, and install it only after I agree. Download the installer to a file and show me the command before running it: `curl -fL -o /tmp/nvm-install.sh https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh`, then run `bash /tmp/nvm-install.sh`. The installer writes to ~/.nvm and appends shellenv to ~/.bashrc / ~/.zshrc — no sudo. Source it for the current shell: `export NVM_DIR="$HOME/.nvm" && [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"`.
   c. `nvm install --lts && nvm use --lts`. Verify `node --version` shows v20.x or v22.x.

   Native Windows (NOT WSL):
   a. Check `node --version` — if 18+, skip.
   b. Install fnm to user profile (no admin): run `winget install Schniz.fnm --scope user` (show me the command first). If winget triggers UAC, fall back to the manual zip from https://github.com/Schniz/fnm/releases/latest — extract `fnm.exe` to `$HOME\.local\bin\` and add that dir to my user PATH via `[Environment]::SetEnvironmentVariable('Path', "$env:Path;$HOME\.local\bin", 'User')`.
   c. `fnm install --lts; fnm use lts-latest`. `fnm env --use-on-cd | Out-String | Invoke-Expression` to source it for the current shell.

4. Install `gws` via npm — runs as my user because Node is managed by nvm/fnm, so the global prefix lives inside ~/.nvm/versions/node/<v>/lib/ (Unix) or ~/.fnm/.../lib/ (Windows). No sudo, no UAC, no `npm config set prefix` workaround needed.

   a. `npm install -g @googleworkspace/cli` (run via Bash tool). Wait for it. If npm fails (network, registry, peer-dep), report the exact stderr and pause — don't half-finish.

   b. nvm/fnm Node + npm-installed binaries land under ~/.nvm/versions/node/<v>/bin/ — only on PATH when nvm is sourced interactively. YOUR Bash tool runs non-interactive subshells that do not source ~/.zshrc or ~/.bashrc, so `gws` and `node` will appear "not found" on the very next call. Symlink them into ~/.local/bin (which is on PATH in every shell context) right after install:
      `mkdir -p ~/.local/bin`
      `ln -sf "$(command -v gws)" ~/.local/bin/gws`
      `ln -sf "$(command -v node)" ~/.local/bin/node`
      Run these while nvm/fnm is sourced in the same Bash call so `command -v` resolves correctly. On native Windows, copy `gws.cmd` from the npm prefix into `$HOME\.local\bin\` instead — symlinks need admin on Windows by default.

   c. Verify `gws --version` from a fresh `bash -c 'gws --version'` (deliberately non-interactive) — confirms the symlink path works for future tool calls.

5. Configure the OAuth client. This step branches on whether the {instance_brand} operator has provisioned a shared OAuth app:

   The operator params file is `<workspace>/.claude/agnes/.env` — `agnes init` writes it into the analyst WORKSPACE, not the home directory. From inside a Claude Code session the workspace is the current working directory, so read `./.claude/agnes/.env`; if it's not there, resolve the workspace via the `workspace_root` key in `~/.config/agnes/config.yaml` (or `$AGNES_CONFIG_DIR/config.yaml`), and only then fall back to checking `~/.claude/agnes/.env` (older installs).

   **Branch A — operator OAuth app provisioned** (`AGNES_GWS_CLIENT_ID` is set in the params file above):

   Skip `gws auth setup` entirely. Environment variables are not a workable carrier either: Claude Code's security layer redacts vars whose name contains the substring "SECRET" from non-interactive subshells. What works is writing the file `gws auth status` reports as `credential_source` directly.

   One property shapes how that file gets written: the client secret value stays out of the session transcript. Sessions upload to the Agnes server on `agnes push`, and a value that never enters the model's context can't land there. So the file is built by a small script that reads `.claude/agnes/.env` itself — no Write tool carrying the value, no `echo`/`cat` of the value, and the failure paths report the reason without it. The script's whole output is one line: `✅ GWS client config written (client id …<last chars>)` or `❌ <reason>`.

   These credentials identify the operator-provisioned OAuth app rather than any individual analyst — closer to a publishable bundle key than a per-user credential — so the goal here is transcript hygiene, not secrecy theatre. The per-user grant is collected in step 6.

   Take the first of these three that applies:

   **A1 — the params file carries a value** (`AGNES_GWS_CLIENT_SECRET=<something>` in `./.claude/agnes/.env`). This is the normal case on a current install: the Agnes server writes the value into the params file. Run the transform below, then continue with `gws auth status` and step 6.

   **A2 — the key is there but carries no value, or is absent entirely.** Usually a params file written before the server started delivering the value (it may still carry the legacy `AGNES_GWS_CLIENT_SECRET_ENV=<name>` pointer, which nothing on the laptop populates — read it as a leftover key and move on). Refresh the params file once with `agnes update --quiet`, then read `.claude/agnes/.env` again. If the value is now present, go to A1.

   **A3 — still no value after the refresh.** The instance has no GWS OAuth app provisioned yet, so the params file has nothing to deliver. Continue with Branch B below.

   The file the transform writes uses the schema Google Cloud Console exports — the gws CLI's Rust struct rejects partial files with "Invalid client_secret.json format: missing field 'project_id'", so every field below is present. `project_id` is present and empty (`""`) on purpose: a non-empty value makes `gws` send an `x-goog-user-project` header on every API call, and Google then requires each analyst to hold `roles/serviceusage.serviceUsageConsumer` on the operator's GCP project — the operator's own account works, every other analyst gets "Caller does not have required permission to use project …". With `""` the header is dropped and quota bills to the OAuth client's own project, no per-user IAM grant needed. (`AGNES_GWS_PROJECT_ID` may also sit in the params file — older seeds copied it in here; leaving it out is the fix for exactly that error.)

   Transform, macOS / Linux / WSL / Git Bash — run it through your Bash tool from the workspace root:

   ```bash
   python3 - <<'PY'
   import json, os, pathlib, sys

   env_path = pathlib.Path(".claude/agnes/.env")
   vals = {}
   for raw in env_path.read_text(encoding="utf-8").splitlines():
       line = raw.strip()
       if not line or line.startswith("#") or "=" not in line:
           continue
       key, value = line.split("=", 1)
       vals[key.strip()] = value.strip().strip('"').strip("'")

   client_id = vals.get("AGNES_GWS_CLIENT_ID", "")
   secret = vals.get("AGNES_GWS_CLIENT_SECRET", "")
   if not client_id or not secret:
       missing = " and ".join(n for n, v in (("client id", client_id), ("secret", secret)) if not v)
       print(f"❌ no {missing} value in {env_path} — see A2")
       sys.exit(1)

   cfg_dir = pathlib.Path(os.path.expanduser("~/.config/gws"))
   cfg_dir.mkdir(parents=True, exist_ok=True)
   os.chmod(cfg_dir, 0o700)
   cfg = cfg_dir / "client_secret.json"
   cfg.write_text(json.dumps({"installed": {
       "client_id": client_id,
       "project_id": "",
       "auth_uri": "https://accounts.google.com/o/oauth2/auth",
       "token_uri": "https://oauth2.googleapis.com/token",
       "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
       "client_secret": secret,
       "redirect_uris": ["http://localhost"],
   }}, indent=2) + "\n", encoding="utf-8")
   os.chmod(cfg, 0o600)
   print(f"✅ GWS client config written (client id …{client_id[-12:]})")
   PY
   ```

   Transform, native Windows (PowerShell) — same shape, `%APPDATA%\gws` instead of `~/.config/gws`, ACLs instead of chmod:

   ```powershell
   $envFile = ".claude\agnes\.env"
   $vals = @{}
   foreach ($raw in Get-Content $envFile) {
     $line = $raw.Trim()
     if (-not $line -or $line.StartsWith('#') -or ($line -notmatch '=')) { continue }
     $parts = $line.Split('=', 2)
     $vals[$parts[0].Trim()] = $parts[1].Trim().Trim('"').Trim("'")
   }
   $clientId = $vals['AGNES_GWS_CLIENT_ID']; $secret = $vals['AGNES_GWS_CLIENT_SECRET']
   if (-not $clientId -or -not $secret) { Write-Output "❌ no client id or secret value in $envFile — see A2"; exit 1 }
   $dir = Join-Path $env:APPDATA 'gws'
   New-Item -ItemType Directory -Force -Path $dir | Out-Null
   $cfg = Join-Path $dir 'client_secret.json'
   [ordered]@{ installed = [ordered]@{
     client_id = $clientId
     project_id = ''
     auth_uri = 'https://accounts.google.com/o/oauth2/auth'
     token_uri = 'https://oauth2.googleapis.com/token'
     auth_provider_x509_cert_url = 'https://www.googleapis.com/oauth2/v1/certs'
     client_secret = $secret
     redirect_uris = @('http://localhost')
   } } | ConvertTo-Json -Depth 4 | Set-Content -Path $cfg -Encoding utf8
   icacls $cfg /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null
   Write-Output "✅ GWS client config written (client id …$($clientId.Substring([Math]::Max(0, $clientId.Length - 12))))"
   ```

   The snippets read the params file at `./.claude/agnes/.env`; when the workspace resolution above landed somewhere else (home-directory fallback, `workspace_root` from the agnes config), point `env_path` / `$envFile` at that path instead.

   On `✅`, run `gws auth status` — it reports the written file as `credential_source` and no longer complains about missing fields — then go to step 6. On `❌`, the reason is in the line itself; A2 and A3 above cover the two ways the value can be absent. If a snippet fails for some other reason (unreadable params file, unwritable config dir), report that error and pause rather than retrying blind.

   **Branch B — no operator OAuth app provisioned** (`AGNES_GWS_CLIENT_ID` absent in the params file — after checking BOTH the workspace and home locations above):

   First, offer the operator path — don't jump straight into the Google Cloud console walkthrough. Most analysts are not GCP admins and cannot finish it. Tell me: the {instance_brand} operator hasn't provisioned a shared Google OAuth app yet; the fastest fix is to ask them to provision it ONCE for the whole instance (server env vars `AGNES_GWS_CLIENT_ID` + `AGNES_GWS_CLIENT_SECRET`, the admin vault, or the `connectors:` / `instance.gws` section of `instance.yaml` — documented in the Agnes server's `config/instance.yaml.example`). After the operator confirms, I re-run `agnes init` and this skill — it will then take the ~2-minute Branch A. Ask me explicitly: "Message your operator and pause here (recommended), or continue with the manual Google Cloud walkthrough yourself (~20 min, needs permission to create a GCP project + OAuth client)?" Pause and stop here unless I explicitly choose the manual walkthrough.

   Manual walkthrough (only on my explicit choice): run `gws auth setup` for me. This is a one-time Google Cloud project config; gcloud is not required (when gcloud is absent, `gws auth setup` walks through the manual OAuth flow). Open the URL it prints in my default browser, then walk me through each click because I am not a GCP admin:
   a. Pick or create a Google Cloud project (free tier is fine).
   b. Enable the APIs the connector needs: Google Drive API, Google Calendar API, Gmail API. Tell me each menu click.
   c. Create an OAuth 2.0 client. Either "Desktop app" or "Web application" works. For Web application: add `http://localhost` (exact value — no port, no path, no trailing slash) to Authorized redirect URIs. Google's loopback exemption then matches the `http://localhost:<ephemeral-port>` redirect that `gws auth login` actually uses. Desktop app needs no URI registration.
   d. Copy the resulting client_id and client_secret. Paste them back into the terminal where `gws auth setup` is waiting. These identify the OAuth app — not the user — but still don't echo them back to me in chat.

6. Run `gws auth login --full` (no `--readonly` flag — Agnes uses full read + write access across Drive / Calendar / Gmail / Sheets / Docs / Chat so the agent can actually create, edit, and send on my behalf). The `--full` flag widens the default scope picker; without it Chat / People / Tasks scopes are silently dropped. One env var the loopback redirect needs is OAUTHLIB_INSECURE_TRANSPORT — set it in the SAME Bash invocation that runs login: `OAUTHLIB_INSECURE_TRANSPORT=1 gws auth login --full`. The CLI binds a local loopback server at `http://localhost:<random-port>` — an OS-assigned ephemeral port, NOT a fixed 8080 — and prints an OAuth URL. If this errors with `redirect_uri_mismatch`, the Cloud Console OAuth client is a Web application type that's missing the `http://localhost` entry in Authorized redirect URIs (no port, no path) — add that exact value and retry.

   Capture the URL from gws's stdout. Before opening the browser, tell me that two Chat write scopes need to be added to the consent URL — `https://www.googleapis.com/auth/chat.spaces` and `https://www.googleapis.com/auth/chat.messages` — because `--full` requests the readonly Chat scopes but not the read+write ones, and `gws chat ... send` calls fail without them. Add them only after I agree: decode the existing scope list, append the two URLs space-separated, re-encode. Python one-liner via Bash tool:

      `URL=$(printf '%s' "$URL" | python3 -c 'import sys,urllib.parse as u; q=u.urlparse(sys.stdin.read().strip()); p=u.parse_qs(q.query); s=set(p.get("scope",[""])[0].split()); s |= {"https://www.googleapis.com/auth/chat.spaces","https://www.googleapis.com/auth/chat.messages"}; p["scope"]=[" ".join(sorted(s))]; print(q._replace(query=u.urlencode(p, doseq=True, quote_via=u.quote)).geturl())')`

   Show me the login URL and open it in the browser as well. (It is long — if you copy it by hand, line wrapping will corrupt the scope query string, so use the opened tab.) Use your Bash tool: macOS `open "$URL"`, Linux/WSL `xdg-open "$URL"`, Windows `Start-Process "$URL"`. Detect OS first.

   While the browser tab is loading, read each requested scope in plain language for me — full read + write across Drive, Calendar, Gmail, Chat, and the rest — so I know what I'm consenting to before I click Approve. Tell me I can revoke any time at https://myaccount.google.com/permissions if I change my mind.

   If `gws auth status` later shows Chat scopes missing (e.g. on a re-run where a stale token cached the previous scope set), `rm ~/.config/gws/token.json` (or `%APPDATA%\gws\token.json` on native Windows) and re-run this step — the OAuth flow re-prompts with the new scope list.

7. Find where gws stored my credentials (`gws auth status` should show the path; typically ~/.config/gws/ on Unix, %APPDATA%\gws\ on Windows). chmod 600 on Unix; on native Windows, restrict ACLs to my user with `icacls "$creds_path" /inheritance:r /grant:r "$env:USERNAME:F"` — file is already in my user profile so this needs no admin.

8. Verify with two low-impact reads, one per scope group: `gws drive files list --params '{"pageSize": 1}'` (Drive scope landed) and `gws chat spaces list --params '{"pageSize": 1}'` (Chat scope landed). Treat exit code 0 from each invocation as success — don't pipe gws output into `python3 -c 'f"..."'` (f-string expressions reject backslashes in Python <3.12, so escaping `\"files\"` inside a shell-quoted f-string raises SyntaxError) and don't call `json.load(sys.stdin)` on the raw stream (gws may emit log lines or a banner before the JSON body, which trips `JSONDecodeError`). If you really need to count rows for diagnostics, write the stdout to a temp file first and parse it with a plain `json.loads(open(path).read())` inside a `try/except`. If both calls exit 0, print `✅ Google Workspace ready — connected as <my email from `gws auth status`>. Drive + Chat scopes verified.` (exact prefix — the final summary grep for it). On any failure, print `❌ Google Workspace setup failed: <which call failed (drive|chat)>, exit <code>. <one-line hint to fix (rotate creds | rerun gws auth login --full | etc.)>.` and stop. Never echo tokens, file/message metadata, or scope strings to chat.

9. Remind me how to revoke later: `gws auth logout` clears local creds; the OAuth grant also appears at https://myaccount.google.com/permissions for Google-side revocation.
