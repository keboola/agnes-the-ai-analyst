"""Connector setup prompts — single source of truth.

Two consumers share these strings:

1. ``app/web/templates/home_not_onboarded.html`` — renders each one inside
   a "Copy prompt" tile so an already-onboarded user can grab a single
   connector's prompt and paste it into Claude Code.
2. ``app/web/setup_instructions.py`` — inlines all three into the main
   "Setup a new Claude Code" script as step 9's interactive ask-then-
   inline-prompt block, so a fresh user gets connectors wired up in
   the same paste-and-go flow that installs Agnes.

Keeping them here (instead of duplicating across template + script) means
edits land in one place. The shape of each connector — slug, display
name, what the prompt instructs Claude to do — is invariant; the GWS
prompt is the only one that branches at render time (operator-provisioned
OAuth client vs manual ``gws auth setup``), which is why ``gws_prompt``
takes the credentials dict.

The text deliberately reads like a Claude Code prompt rather than a shell
script. The whole flow is "paste into Claude Code, let it do the work" —
the prompts tell Claude how to ask the user, where to write helper
scripts, and how to verify against live APIs before storing anything.
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Public registry — single place to add / remove / reorder a connector.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Connector:
    """One connector's identity, surfaced both in the /home tile registry
    and in the setup-script step. Adding a fourth connector means: one
    entry here, one ``<slug>_prompt()`` function below, one branch in
    :func:`all_connector_prompts`. No template or setup-script changes."""

    slug: str
    display_name: str
    description: str


CONNECTORS: list[Connector] = [
    Connector(
        slug="asana",
        display_name="Asana",
        description="Read tasks and projects, comment, create updates — Claude works alongside your project boards without leaving the terminal.",
    ),
    Connector(
        slug="gws",
        display_name="Google Workspace",
        description="Drive, Calendar, Gmail, Docs, Sheets, Chat — Claude reads and acts across your work account via the official `gws` CLI.",
    ),
    Connector(
        slug="atlassian",
        display_name="Atlassian (Jira / Confluence)",
        description="Read and write Jira issues, search Confluence pages — Claude pulls ticket context and posts updates without leaving the workspace.",
    ),
]


def all_connector_prompts(
    *,
    gws_oauth: dict | None = None,
    instance_admin_email: str = "",
) -> dict[str, str]:
    """Resolve every connector's prompt text with the operator's runtime
    config baked in. Caller (router._build_context, setup_instructions
    consumers) passes the already-resolved ``gws_oauth`` dict from
    :func:`app.instance_config.get_gws_oauth_credentials` and the admin
    email from :func:`get_instance_admin_email`. Returns a dict keyed by
    connector slug so both the template (``{{ connector_prompts.asana
    }}``) and the setup script (``connector_prompts['asana']``) read the
    same shape.

    ``instance_admin_email`` is currently unused inside the prompt bodies
    (the Email-admin button on /home is tile chrome, not prompt content)
    but is plumbed through anyway so a future GWS prompt branch that
    references the admin contact can add the string without changing the
    call sites.
    """
    gws_oauth = gws_oauth or {}
    return {
        "asana": asana_prompt(),
        "gws": gws_prompt(
            gws_oauth_configured=bool(gws_oauth.get("configured")),
            gws_client_id=str(gws_oauth.get("client_id") or ""),
            gws_client_secret=str(gws_oauth.get("client_secret") or ""),
            gws_project_id=str(gws_oauth.get("project_id") or ""),
            oauthlib_insecure_transport=str(
                gws_oauth.get("oauthlib_insecure_transport") or "1"
            ),
            instance_admin_email=instance_admin_email,
        ),
        "atlassian": atlassian_prompt(),
    }


# ---------------------------------------------------------------------------
# Individual prompt builders.
#
# Each returns the verbatim prompt body that Claude Code follows when the
# user pastes it. Strings are plain Python (real `<` / `>` / `&` chars) —
# the Jinja template re-escapes for HTML rendering, and the setup script
# inlines them straight into bash heredocs / numbered steps.
# ---------------------------------------------------------------------------

def asana_prompt() -> str:
    """Asana PAT setup. Stores token in OS keychain under
    ``agnes-asana-pat``. Idempotent — re-running short-circuits when the
    cached token still passes the Asana ``users/me`` probe."""
    return _ASANA_PROMPT


def gws_prompt(
    *,
    gws_oauth_configured: bool,
    gws_client_id: str = "",
    gws_client_secret: str = "",
    gws_project_id: str = "",
    oauthlib_insecure_transport: str = "1",
    instance_admin_email: str = "",  # noqa: ARG001 — plumbed for future use
) -> str:
    """Google Workspace setup via the official ``gws`` CLI.

    Step 5 branches on whether the operator has provisioned a shared
    OAuth app (``gws_oauth_configured=True``, set when both
    ``AGNES_GWS_CLIENT_ID`` + ``AGNES_GWS_CLIENT_SECRET`` are present).
    Configured → write ``client_secret.json`` directly, skip the
    ``gws auth setup`` walkthrough entirely (~2 min, zero clickops).
    Unconfigured → fall back to the manual GCP project walkthrough
    (~20 min, user needs GCP-admin help).

    ``oauthlib_insecure_transport`` only flows into step 6 because the
    gws CLI's loopback redirect is HTTP (Google's oauthlib refuses that
    without the env var set)."""
    if gws_oauth_configured:
        step5 = _GWS_STEP5_CONFIGURED_TEMPLATE.format(
            client_id=gws_client_id,
            project_id=gws_project_id,
            client_secret=gws_client_secret,
        )
        # When configured, step 6 reuses the operator's `oauthlib_insecure_transport`
        # setting verbatim — even though "1" is the always-safe default, an operator
        # MAY have flipped it off via instance.yaml and we honour that.
        oauth_env = oauthlib_insecure_transport or "1"
    else:
        step5 = _GWS_STEP5_MANUAL
        oauth_env = "1"
    return _GWS_PROMPT_HEAD + step5 + _GWS_PROMPT_TAIL_TEMPLATE.format(
        oauth_env=oauth_env,
    )


def atlassian_prompt() -> str:
    """Atlassian (Jira + Confluence) API token setup. Stores token in OS
    keychain under ``agnes-atlassian-api-token``, plus email + normalized
    base URL in ``~/.claude/agnes/secrets.env``. Jira-first / Confluence-
    fallback verify so Confluence-only sites still onboard."""
    return _ATLASSIAN_PROMPT


# ---------------------------------------------------------------------------
# Prompt bodies — kept as module-level constants so they're free of
# any per-call allocation cost and trivially diffable.
# ---------------------------------------------------------------------------

_ASANA_PROMPT = """Set up an Asana personal access token for Claude Code. Walk me through it step by step.

Ground rules: this is idempotent — safe to re-run, the precheck below short-circuits when Asana is already wired up. If any step fails with an unfamiliar error, paste the exact error back and stop. Do NOT improvise around TLS errors by disabling verification (`-k`, `NODE_TLS_REJECT_UNAUTHORIZED=0`, `git -c http.sslVerify=false`, etc.) — those hide the real problem.

0. Precheck — skip the rest if Asana is already connected. Detect my OS, then look up an existing keychain entry under the service name `agnes-asana-pat` and verify it against Asana's API. macOS: `t=$(security find-generic-password -s 'agnes-asana-pat' -w 2>/dev/null) && curl -fsS -H "Authorization: Bearer $t" https://app.asana.com/api/1.0/users/me | jq -r '.data | "Already connected as \\(.name) (\\(.workspaces | length) workspace(s)). Skipping setup."' && exit 0`. Linux: `t=$(secret-tool lookup service agnes-asana-pat username "$USER" 2>/dev/null) && ...same curl...`. Windows PowerShell: `$cred = cmdkey /list:agnes-asana-pat 2>$null; if ($LASTEXITCODE -eq 0) { Write-Host "Asana cred entry found — verify in your terminal before re-running setup." }` (Windows can't read the password back without a CredentialManager module — print a hint and let me confirm). If the verify call returns 200, print the one-line "Already connected" message and STOP. Only continue to step 1 when no cred exists OR the cached token returns 401.
1. Open the Asana developer tokens page in my default browser — use your Bash tool: `open https://app.asana.com/0/developer-console/tokens` on macOS, `xdg-open https://app.asana.com/0/developer-console/tokens` on Linux/WSL, or `Start-Process https://app.asana.com/0/developer-console/tokens` on Windows. Detect OS first. If that URL doesn't render the tokens UI (rare), tell me to click my avatar (top right) → Settings → "Apps" tab → "Manage Developer Apps" → Personal access tokens.
2. Tell me to click "+ New access token", name it "Claude Code — Agnes", and click "Create token". Warn me the token is shown ONCE and Asana PATs do not expire — I'd need to revoke it from the same page if it leaks.
3. Important: do NOT ask me to paste the token into the chat. Chat input is saved to ~/.claude/projects/.../*.jsonl. Instead, prepare a tiny helper script for me to run in my real terminal:
   a. Detect my OS. Use the Write/Edit tool (NOT a shell here-doc that prints the body) to create ~/.claude/agnes/bin/store-asana.sh on macOS/Linux, or ~/.claude/agnes/bin/store-asana.ps1 on Windows. chmod 700 the file. Body for macOS:
      #!/usr/bin/env bash
      set -e
      read -srp 'Paste Asana token (hidden): ' t; echo
      security add-generic-password -U -s 'agnes-asana-pat' -a "$USER" -w "$t"
      unset t
      echo 'Stored in macOS Keychain.'
      Linux variant: same shape but `printf %s "$t" | secret-tool store --label='Agnes Asana PAT' service agnes-asana-pat username "$USER"`. Windows .ps1: `$t = Read-Host 'Paste Asana token' -AsSecureString; $p = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($t)); cmdkey /generic:agnes-asana-pat /user:$env:USERNAME /pass:$p > $null; Remove-Variable p,t; 'Stored.'`
   b. Tell me to open a real terminal (Terminal.app / iTerm / WSL / PowerShell — NOT Claude Code's `!` prefix, which has no TTY) and run `bash ~/.claude/agnes/bin/store-asana.sh` (or `pwsh ~/.claude/agnes/bin/store-asana.ps1` on Windows). The script will wait silently at the hidden prompt.
   c. Walk me through the clipboard order: copy the launcher first, paste it in my terminal, press Enter (terminal now waiting). Switch to the Asana tab, copy the token from step 2. Switch back to terminal, paste at the silent prompt, press Enter. Token enters via stdin only — not shown on screen, not in shell history, not in clipboard at the moment Claude is involved.
4. After I report "Stored", verify by calling `curl -sS -H "Authorization: Bearer $(security find-generic-password -s 'agnes-asana-pat' -w)" https://app.asana.com/api/1.0/users/me | jq -r '.data | "\\(.name) — \\(.workspaces | length) workspace(s)"'` (macOS; Linux uses `secret-tool lookup` instead). Print only the one-line result. Never echo the token.
5. Remind me where the token is stored and how to revoke: in macOS Keychain Access search "agnes-asana-pat" or run `security delete-generic-password -s 'agnes-asana-pat'`; on Asana, revoke from the same developer-console page."""


_GWS_PROMPT_HEAD = """Set up Google Workspace access for Claude Code using the official `gws` CLI from https://github.com/googleworkspace/cli (install steps: README → Installation). The npm path is what we'll use because (a) it's the README's documented convenience path, (b) it works the same on macOS / Linux / WSL / Windows, and (c) it can run with zero admin rights when Node is managed by `nvm` (Unix) or `fnm` (Windows).

Ground rules: this is idempotent — safe to re-run, the precheck below short-circuits when `gws` is already installed and authed. If any step fails with an unfamiliar error, paste the exact error back and stop — don't half-finish. Do NOT improvise around TLS errors by disabling verification (`-k`, `NODE_TLS_REJECT_UNAUTHORIZED=0`, npm `strict-ssl=false`, etc.) — those mask the real problem.

YOU run every command via your Bash tool. Do NOT print install commands and ask me to type them. Only stop and ask me when I have to (a) approve an OAuth consent screen in a browser, (b) make a product decision (Cloud project name), or (c) paste OAuth client credentials Google shows me.

0. Precheck — skip the rest if Google Workspace is already connected. Run `command -v gws` AND `gws auth status` AND a low-impact verify call: `gws drive files list --params '{"pageSize": 1}' && gws chat spaces list --params '{"pageSize": 1}'`. If both succeed, the gws CLI is installed AND authed AND the Chat scope is present. Print "Already connected as <email from `gws auth status`> — Drive + Chat scopes verified. Skipping setup." and STOP. If `gws drive` succeeds but `gws chat` fails with 403/PERMISSION_DENIED, the user authed without `--full` previously — skip to step 6 (re-login with widened scopes), do NOT re-install. Only walk steps 1–5 (install + OAuth client setup) when `command -v gws` itself fails.

1. Detect my OS (`uname -s` → Darwin / Linux, or PowerShell `$env:OS` → Windows_NT). On Linux check `grep -qi microsoft /proc/version` and treat WSL as Linux.

2. Check `command -v gws` (or `Get-Command gws` on Windows). If `gws` is already installed, skip to step 5.

3. Install Node.js 18+ to my user directory — no sudo, no UAC, no system package manager.

   Unix (macOS / Linux / WSL):
   a. Check `command -v node && node --version` — if 18+ already, skip.
   b. Otherwise install nvm into ~/.nvm: `curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash`. The installer writes to ~/.nvm and appends shellenv to ~/.bashrc / ~/.zshrc — no sudo. Source it for the current shell: `export NVM_DIR="$HOME/.nvm" && [ -s "$NVM_DIR/nvm.sh" ] && \\. "$NVM_DIR/nvm.sh"`.
   c. `nvm install --lts && nvm use --lts`. Verify `node --version` shows v20.x or v22.x.

   Native Windows (NOT WSL):
   a. Check `node --version` — if 18+, skip.
   b. Install fnm to user profile (no admin): run `winget install Schniz.fnm --scope user --accept-source-agreements --accept-package-agreements`. If winget triggers UAC, fall back to the manual zip from https://github.com/Schniz/fnm/releases/latest — extract `fnm.exe` to `$HOME\\.local\\bin\\` and add that dir to my user PATH via `[Environment]::SetEnvironmentVariable('Path', "$env:Path;$HOME\\.local\\bin", 'User')`.
   c. `fnm install --lts; fnm use lts-latest`. `fnm env --use-on-cd | Out-String | Invoke-Expression` to source it for the current shell.

4. Install `gws` via npm — runs as my user because Node is managed by nvm/fnm, so the global prefix lives inside ~/.nvm/versions/node/<v>/lib/ (Unix) or ~/.fnm/.../lib/ (Windows). No sudo, no UAC, no `npm config set prefix` workaround needed.

   a. `npm install -g @googleworkspace/cli` (run via Bash tool). Wait for it. If npm fails (network, registry, peer-dep), report the exact stderr and pause — don't half-finish.

   b. nvm/fnm Node + npm-installed binaries land under ~/.nvm/versions/node/<v>/bin/ — only on PATH when nvm is sourced interactively. YOUR Bash tool runs non-interactive subshells that do NOT source ~/.zshrc or ~/.bashrc, so `gws` and `node` will appear "not found" on the very next call. Symlink them into ~/.local/bin (which is on PATH in every shell context) right after install:
      `mkdir -p ~/.local/bin`
      `ln -sf "$(command -v gws)" ~/.local/bin/gws`
      `ln -sf "$(command -v node)" ~/.local/bin/node`
      Run these while nvm/fnm is sourced in the same Bash call so `command -v` resolves correctly. On native Windows, copy `gws.cmd` from the npm prefix into `$HOME\\.local\\bin\\` instead — symlinks need admin on Windows by default.

   c. Verify `gws --version` from a fresh `bash -c 'gws --version'` (deliberately non-interactive) — confirms the symlink path works for future tool calls.

"""


_GWS_STEP5_CONFIGURED_TEMPLATE = """5. The Agnes operator has already provisioned a shared Google Workspace OAuth app for this instance. Skip `gws auth setup` entirely. Do NOT use environment variables (Claude Code's security layer redacts vars containing the substring "SECRET" from non-interactive subshells, so the env-var approach is unreliable). Instead, write the credentials directly to the file `gws auth status` reads as `credential_source`:

   Use the Write tool to create `~/.config/gws/client_secret.json` (or `%APPDATA%\\gws\\client_secret.json` on native Windows) with EXACTLY the schema Google Cloud Console exports — the gws CLI's Rust struct rejects partial files with "Invalid client_secret.json format: missing field 'project_id'". Both `installed.project_id` (numeric project number) and the URI fields are mandatory:
   {{
     "installed": {{
       "client_id": "{client_id}",
       "project_id": "{project_id}",
       "auth_uri": "https://accounts.google.com/o/oauth2/auth",
       "token_uri": "https://oauth2.googleapis.com/token",
       "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
       "client_secret": "{client_secret}",
       "redirect_uris": ["http://localhost"]
     }}
   }}

   Then `mkdir -p ~/.config/gws && chmod 700 ~/.config/gws && chmod 600 ~/.config/gws/client_secret.json`. Verify by running `gws auth status` — it should report this file as `credential_source` without complaining about missing fields. The values identify the OAuth app, not me; treat the secret like a publishable bundle key, not a per-user credential.
"""


_GWS_STEP5_MANUAL = """5. Run `gws auth setup` for me. This is a one-time Google Cloud project config; gcloud is NOT required (when gcloud is absent, `gws auth setup` walks through the manual OAuth flow). Open the URL it prints in my default browser, then walk me through each click because I am NOT a GCP admin:
   a. Pick or create a Google Cloud project (free tier is fine).
   b. Enable the APIs the connector needs: Google Drive API, Google Calendar API, Gmail API. Tell me each menu click.
   c. Create an OAuth 2.0 client. Either "Desktop app" or "Web application" works. For Web application: add `http://localhost` (exact value — no port, no path, no trailing slash) to Authorized redirect URIs. Google's loopback exemption then matches the `http://localhost:<ephemeral-port>` redirect that `gws auth login` actually uses. Desktop app needs no URI registration.
   d. Copy the resulting client_id and client_secret. Paste them back into the terminal where `gws auth setup` is waiting. These identify the OAuth app — not the user — but still don't echo them back to me in chat.
"""


_GWS_PROMPT_TAIL_TEMPLATE = """
6. Run `gws auth login --full` (no `--readonly` flag — Agnes uses full read + write access across Drive / Calendar / Gmail / Sheets / Docs / Chat so the agent can actually create, edit, and send on my behalf). The `--full` flag widens the default scope picker; without it Chat / People / Tasks scopes are silently dropped. One env var the loopback redirect needs is OAUTHLIB_INSECURE_TRANSPORT — set it in the SAME Bash invocation that runs login: `OAUTHLIB_INSECURE_TRANSPORT={oauth_env} gws auth login --full`. The CLI binds a local loopback server at `http://localhost:<random-port>` — an OS-assigned ephemeral port, NOT a fixed 8080 — and prints an OAuth URL. If this errors with `redirect_uri_mismatch`, the Cloud Console OAuth client is a Web application type that's missing the `http://localhost` entry in Authorized redirect URIs (no port, no path) — add that exact value and retry.

   Capture the URL from gws's stdout. Before opening the browser, append the Chat write scopes (`https://www.googleapis.com/auth/chat.spaces` and `https://www.googleapis.com/auth/chat.messages`) to the URL's `scope=` query parameter — `--full` includes the readonly Chat scopes but NOT the read+write ones, and `gws chat ... send` calls fail without them. Decode the existing scope list, append the two URLs space-separated, re-encode, then open. Python one-liner via Bash tool:

      `URL=$(printf '%s' "$URL" | python3 -c 'import sys,urllib.parse as u; q=u.urlparse(sys.stdin.read().strip()); p=u.parse_qs(q.query); s=set(p.get("scope",[""])[0].split()); s |= {{"https://www.googleapis.com/auth/chat.spaces","https://www.googleapis.com/auth/chat.messages"}}; p["scope"]=[" ".join(sorted(s))]; print(q._replace(query=u.urlencode(p, doseq=True, quote_via=u.quote)).geturl())')`

   Then open the rewritten URL programmatically — do NOT print it to chat. Markdown line-wrapping in chat corrupts the long scope query string when the user copies it. Use your Bash tool: macOS `open "$URL"`, Linux/WSL `xdg-open "$URL"`, Windows `Start-Process "$URL"`. Detect OS first.

   While the browser tab is loading, read each requested scope in plain language for me — full read + write across Drive, Calendar, Gmail, Chat, and the rest — so I know what I'm consenting to before I click Approve. Tell me I can revoke any time at https://myaccount.google.com/permissions if I change my mind.

   If `gws auth status` later shows Chat scopes missing (e.g. on a re-run where a stale token cached the previous scope set), `rm ~/.config/gws/token.json` (or `%APPDATA%\\gws\\token.json` on native Windows) and re-run this step — the OAuth flow re-prompts with the new scope list.

7. Find where gws stored my credentials (`gws auth status` should show the path; typically ~/.config/gws/ on Unix, %APPDATA%\\gws\\ on Windows). chmod 600 on Unix; on native Windows, restrict ACLs to my user with `icacls "$creds_path" /inheritance:r /grant:r "$env:USERNAME:F"` — file is already in my user profile so this needs no admin.

8. Verify with two low-impact reads, one per scope group: `gws drive files list --params '{{"pageSize": 1}}'` (Drive scope landed) and `gws chat spaces list --params '{{"pageSize": 1}}'` (Chat scope landed). Print only "Connected as <my email>" plus the file + space counts. Never echo tokens, file/message metadata, or scope strings to chat.

9. Remind me how to revoke later: `gws auth logout` clears local creds; the OAuth grant also appears at https://myaccount.google.com/permissions for Google-side revocation."""


_ATLASSIAN_PROMPT = """Set up Atlassian (Jira + Confluence) API access for Claude Code. Walk me through it step by step.

Ground rules: this is idempotent — safe to re-run, the precheck below short-circuits when Atlassian is already wired up. If any step fails with an unfamiliar error, paste the exact error back and stop. Do NOT improvise around TLS errors by disabling verification (`-k`, `NODE_TLS_REJECT_UNAUTHORIZED=0`, `git -c http.sslVerify=false`, etc.) — those hide the real problem.

0. Precheck — skip the rest if Atlassian is already connected. The setup script stores email + the *normalized* site root URL (no trailing slash, no `/wiki` suffix) in `~/.claude/agnes/secrets.env` and the API token in the OS keychain under `agnes-atlassian-api-token`. Verify all three exist + auth works against the LIVE Atlassian API before reinstalling, and probe BOTH Jira and Confluence — sites can have either product enabled, so Jira's `/rest/api/3/myself` returns 404 on Confluence-only sites and vice-versa. macOS: `[ -r ~/.claude/agnes/secrets.env ] && . ~/.claude/agnes/secrets.env && t=$(security find-generic-password -s 'agnes-atlassian-api-token' -a "$ATLASSIAN_EMAIL" -w 2>/dev/null) && B="${ATLASSIAN_BASE_URL%/}" && B="${B%/wiki}" && tmp=$(mktemp) && code=$(curl -sS -o "$tmp" -w '%{http_code}' -u "$ATLASSIAN_EMAIL:$t" "$B/rest/api/3/myself") && { [ "$code" = "404" ] && code=$(curl -sS -o "$tmp" -w '%{http_code}' -u "$ATLASSIAN_EMAIL:$t" "$B/wiki/rest/api/user/current"); :; } && [ "$code" = "200" ] && jq -r '"Already connected as \\(.displayName) (\\(.emailAddress // "no email scope")) on '"$B"'. Skipping setup."' < "$tmp" && rm -f "$tmp" && exit 0`. Linux: same shape but `t=$(secret-tool lookup service agnes-atlassian-api-token username "$ATLASSIAN_EMAIL")`. Windows: read `secrets.env`, then `cmdkey /list:agnes-atlassian-api-token` — if entry exists, print "Atlassian cred entry found — verify in your real terminal before re-running setup." and let me confirm rather than auto-skipping. If the verify call (either probe) returns 200, STOP with the "Already connected" line. Continue to step 1 only when secrets.env is missing OR keychain lookup fails OR BOTH probes return non-200. Treat 401 from either probe as "real auth failure — token is bad" and skip the second probe.
1. Ask me for my Atlassian Cloud site URL (looks like https://<myorg>.atlassian.net) and the email I sign in with. Site URL and email are NOT secrets — fine to type into chat. Don't proceed until I've given you both.
2. Open the Atlassian API tokens page in my default browser — use your Bash tool: `open https://id.atlassian.com/manage-profile/security/api-tokens` on macOS, `xdg-open ...` on Linux/WSL, or `Start-Process ...` on Windows. Detect OS first. If I land on a generic profile page, tell me: avatar (top right) → Manage account → Security → "Create and manage API tokens".
3. Tell me to click "Create API token" (NOT "Create API token with scopes" unless I specifically need fine-grained — one-line trade-off: scoped tokens are limited per project but expire and need rotation; unscoped is simplest for personal use). Label it "Claude Code — Agnes", click Create, copy the token. Warn me it is shown ONCE.
4. Important: do NOT ask me to paste the token into the chat. Prepare a helper script for me to run in my real terminal, with my email and site URL baked in as literals (so they're not re-prompted at runtime):
   a. Use the Write tool to create ~/.claude/agnes/bin/store-atlassian.sh on macOS/Linux (or .ps1 on Windows). chmod 700. The script must (i) reject obviously-truncated tokens via a length floor, (ii) NORMALIZE the base URL so the verify call hits a real endpoint, and (iii) verify the credentials against the live Atlassian API — trying Jira first, then Confluence on 404 — BEFORE writing anything to the keychain. The length guard exists because Atlassian's "shown ONCE" copy panel commonly truncates if the user click-copies instead of using the panel's Copy button — silently storing a 43-char fragment then discovering it later is the failure mode we're avoiding. The URL-normalization + product-fallback exists because `/rest/api/3/myself` only lives under Jira and returns 404 on Confluence-only sites (and vice-versa for `/wiki/rest/api/user/current`); previously a perfectly valid token paired with a Confluence-only URL or a URL the user pasted with a `/wiki` or trailing slash would 404 here and the prompt would falsely report the token as broken. Body for macOS:
      #!/usr/bin/env bash
      set -e
      EMAIL='<the email I gave you>'
      BASE_URL='<the site URL I gave you>'
      read -srp 'Paste Atlassian API token (hidden): ' t; echo

      # Guard 1 — Atlassian Cloud tokens are typically 192+ chars; sub-100
      # means a truncated copy. Bail before touching the keychain.
      tlen=$(printf %s "$t" | wc -c | tr -d ' ')
      if [ "$tlen" -lt 100 ]; then
        echo "Token looks too short ($tlen chars) — copy the full value via the Copy button on the Atlassian token page. Aborting." >&2
        unset t
        exit 1
      fi

      # Guard 2 — normalize the site root: strip a trailing slash, then a
      # trailing /wiki if present, so $BASE_URL is the bare site root.
      # `$BASE_URL/rest/api/3/myself` (Jira) and `$BASE_URL/wiki/rest/api/user/current`
      # (Confluence) both resolve correctly from the same normalized value.
      BASE_URL="${BASE_URL%/}"
      BASE_URL="${BASE_URL%/wiki}"

      # Guard 3 — verify against the live API before storing. Try Jira first
      # (most sites have it), fall back to Confluence on 404 only. On 401
      # we stop immediately: the token itself is bad, no point probing the
      # other product. Anything else (5xx, network) also aborts.
      tmp=$(mktemp)
      product=jira
      status=$(curl -sS -o "$tmp" -w '%{http_code}' -u "$EMAIL:$t" "$BASE_URL/rest/api/3/myself" || true)
      if [ "$status" = "404" ]; then
        product=confluence
        status=$(curl -sS -o "$tmp" -w '%{http_code}' -u "$EMAIL:$t" "$BASE_URL/wiki/rest/api/user/current" || true)
      fi
      if [ "$status" != "200" ]; then
        if [ "$status" = "401" ]; then
          echo "API verification failed (HTTP 401 — token rejected by Atlassian). The token is either wrong, revoked, or paired with the wrong email. Aborting without storing." >&2
        elif [ "$status" = "404" ]; then
          echo "API verification failed (HTTP 404 on both Jira and Confluence probes). The site URL '$BASE_URL' is reachable but exposes neither product to this token — double-check the URL (it should be your Atlassian Cloud site root, e.g. https://yourorg.atlassian.net) or that your account has access to Jira or Confluence on this site. Aborting without storing." >&2
        else
          echo "API verification failed (HTTP $status). Aborting without storing." >&2
        fi
        cat "$tmp" >&2 2>/dev/null || true
        rm -f "$tmp"; unset t
        exit 1
      fi
      display=$(python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("displayName","?"))' < "$tmp")
      rm -f "$tmp"

      # Verified — write token to Keychain + URL/email to secrets.env.
      security add-generic-password -U -s 'agnes-atlassian-api-token' -a "$EMAIL" -w "$t"
      umask 077; mkdir -p ~/.claude/agnes
      printf 'ATLASSIAN_EMAIL=%s\\nATLASSIAN_BASE_URL=%s\\n' "$EMAIL" "$BASE_URL" > ~/.claude/agnes/secrets.env
      chmod 600 ~/.claude/agnes/secrets.env
      unset t
      echo "Stored ($product). Verified as $display."

      Linux variant: replace `security add-generic-password ...` with `printf %s "$t" | secret-tool store --label='Agnes Atlassian token' service agnes-atlassian-api-token username "$EMAIL"`. All three guards (length floor, URL normalization, Jira-then-Confluence verification) stay identical — they run before the storage call. Windows .ps1: same control flow — `Read-Host -AsSecureString`, convert via `Marshal::PtrToStringAuto`, check `$t.Length -lt 100`, then `$BASE_URL = $BASE_URL.TrimEnd('/').TrimEnd('/wiki')` (or `if ($BASE_URL.EndsWith('/wiki')) { $BASE_URL = $BASE_URL.Substring(0, $BASE_URL.Length - 5) }`), `try { Invoke-RestMethod -Uri "$BASE_URL/rest/api/3/myself" -Authentication Basic -Credential (New-Object PSCredential($EMAIL, $secureToken)) } catch { if ($_.Exception.Response.StatusCode.value__ -eq 404) { Invoke-RestMethod -Uri "$BASE_URL/wiki/rest/api/user/current" -Authentication Basic -Credential ... } else { throw } }` — write to `cmdkey` + `secrets.env` only after a 200 lands from either probe.
   b. Tell me to open a real terminal (not Claude Code's `!`) and run `bash ~/.claude/agnes/bin/store-atlassian.sh` (or `pwsh ~/.claude/agnes/bin/store-atlassian.ps1` on Windows). The script will wait silently at the hidden prompt.
   c. Walk me through clipboard order: copy the launcher first, paste in terminal, Enter (terminal waiting). Switch to the Atlassian tab, copy the token from step 3 — use the panel's "Copy" button, NOT click-and-drag (which often truncates). Switch back to terminal, paste at the silent prompt, Enter. The script will print "Stored. Verified as <your name>." on success, or fail loudly with the exact reason (too short / HTTP 401 / etc.) without writing anything.
5. Register the on-demand Atlassian MCP under .claude/mcp/atlassian referencing the stored credentials (read token from keychain via `security find-generic-password -s 'agnes-atlassian-api-token' -w` at MCP startup).
6. The store script already verified the token end-to-end. If I want a second redacted readback later, hit `GET $BASE_URL/rest/api/3/myself` (Jira) or `GET $BASE_URL/wiki/rest/api/user/current` (Confluence) — try Jira first, fall back to Confluence on 404, same shape as the store script's verify. Print just displayName + accountId — never the token.
7. Remind me how to revoke: same API tokens page on Atlassian, plus `security delete-generic-password -s 'agnes-atlassian-api-token'` locally (macOS) / `secret-tool clear service agnes-atlassian-api-token` (Linux) / `cmdkey /delete:agnes-atlassian-api-token` (Windows)."""
