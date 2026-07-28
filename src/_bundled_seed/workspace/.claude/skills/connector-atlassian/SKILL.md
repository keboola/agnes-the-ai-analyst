---
name: connector-atlassian
description: Set up Atlassian (Jira + Confluence) for Claude Code — API token in OS keychain, email + base URL in ~/.claude/agnes/secrets.env, hosted Remote MCP registered. Triggers on "set up Jira", "set up Atlassian", "configure Confluence", "fix Jira access". Idempotent — short-circuits if already configured.
connector:
  display_name: Atlassian (Jira / Confluence)
  short_summary: Read and write Jira issues, search Confluence pages — Claude pulls ticket context and posts updates without leaving the workspace.
  estimated_minutes: 4
  vendor_url: https://id.atlassian.com/manage-profile/security/api-tokens
---

Set up Atlassian (Jira + Confluence) API access for Claude Code. Walk me through it step by step.

Ground rules: this is idempotent — safe to re-run, the precheck below short-circuits when Atlassian is already wired up. If any step fails with an unfamiliar error, paste the exact error back and stop. Do NOT improvise around TLS errors by disabling verification (`-k`, `NODE_TLS_REJECT_UNAUTHORIZED=0`, `git -c http.sslVerify=false`, etc.) — those hide the real problem.

0. Precheck — skip the rest if Atlassian is already connected. The setup script stores email + the *normalized* site root URL (no trailing slash, no `/wiki` suffix) in `~/.claude/agnes/secrets.env` and the API token in the OS keychain under `agnes-atlassian-api-token`. Verify all three exist + auth works against the LIVE Atlassian API before reinstalling, and probe BOTH Jira and Confluence — sites can have either product enabled, so Jira's `/rest/api/3/myself` returns 404 on Confluence-only sites and vice-versa. macOS: `[ -r ~/.claude/agnes/secrets.env ] && . ~/.claude/agnes/secrets.env && t=$(security find-generic-password -s 'agnes-atlassian-api-token' -a "$ATLASSIAN_EMAIL" -w 2>/dev/null) && B="${ATLASSIAN_BASE_URL%/}" && B="${B%/wiki}" && tmp=$(mktemp) && code=$(curl -sS -o "$tmp" -w '%{http_code}' -u "$ATLASSIAN_EMAIL:$t" "$B/rest/api/3/myself") && { [ "$code" = "404" ] && code=$(curl -sS -o "$tmp" -w '%{http_code}' -u "$ATLASSIAN_EMAIL:$t" "$B/wiki/rest/api/user/current"); :; } && [ "$code" = "200" ] && jq -r '"✅ Atlassian ready — connected as \(.displayName) on '"$B"'."' < "$tmp" && rm -f "$tmp" && exit 0`. Linux: same shape but `t=$(secret-tool lookup service agnes-atlassian-api-token username "$ATLASSIAN_EMAIL")`. Windows: read `secrets.env`, then `cmdkey /list:agnes-atlassian-api-token` — if entry exists, print "Atlassian cred entry found — verify in your real terminal before re-running setup." and let me confirm rather than auto-skipping. If the verify call (either probe) returns 200, STOP with the "✅ Atlassian ready — ..." line. Continue to step 1 only when secrets.env is missing OR keychain lookup fails OR BOTH probes return non-200. Treat 401 from either probe as "real auth failure — token is bad" and skip the second probe.
1. Ask me for my Atlassian Cloud site URL (looks like https://<myorg>.atlassian.net) and the email I sign in with. Site URL and email are NOT secrets — fine to type into chat. Don't proceed until I've given you both.
2. Open the Atlassian API tokens page in my default browser — use your Bash tool: `open https://id.atlassian.com/manage-profile/security/api-tokens` on macOS, `xdg-open ...` on Linux/WSL, or `Start-Process ...` on Windows. Detect OS first. If I land on a generic profile page, tell me: avatar (top right) → Manage account → Security → "Create and manage API tokens".
3. Tell me to click "Create API token" (NOT "Create API token with scopes" unless I specifically need fine-grained — one-line trade-off: scoped tokens are limited per project but expire and need rotation; unscoped is simplest for personal use). Label it "Claude Code — {instance_brand}". **In the "Expires" / validity dropdown, pick the longest option Atlassian offers (today that's "1 year") — Atlassian's default sets short-lived expiry and the re-mint friction is the #1 reason this connector goes stale.** There is NO query-parameter hook on `id.atlassian.com/manage-profile/security/api-tokens` to pre-select the expiry, so the user has to click it; just tell them which option to pick. Click Create, copy the token. Warn me it is shown ONCE.
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
        echo "❌ Atlassian setup failed: token looks too short ($tlen chars). Use the panel's Copy button on the Atlassian token page (NOT click-and-drag, which can truncate) and re-run." >&2
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
          echo "❌ Atlassian setup failed: token rejected (HTTP 401). The token is wrong, revoked, or paired with the wrong email — re-mint at https://id.atlassian.com/manage-profile/security/api-tokens and retry. Aborting without storing." >&2
        elif [ "$status" = "404" ]; then
          echo "❌ Atlassian setup failed: HTTP 404 on both Jira and Confluence probes. The site URL '$BASE_URL' is reachable but exposes neither product to this token — double-check the URL (Atlassian Cloud site root, e.g. https://yourorg.atlassian.net) or that your account has access to Jira or Confluence on this site. Aborting without storing." >&2
        else
          echo "❌ Atlassian setup failed: API verification returned HTTP $status. Aborting without storing." >&2
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
      printf 'ATLASSIAN_EMAIL=%s\nATLASSIAN_BASE_URL=%s\n' "$EMAIL" "$BASE_URL" > ~/.claude/agnes/secrets.env
      chmod 600 ~/.claude/agnes/secrets.env
      unset t
      echo "✅ Atlassian ready — connected as $display on $BASE_URL ($product)."

      Linux variant: replace `security add-generic-password ...` with `printf %s "$t" | secret-tool store --label='Agnes Atlassian token' service agnes-atlassian-api-token username "$EMAIL"`. All three guards (length floor, URL normalization, Jira-then-Confluence verification) stay identical — they run before the storage call. Windows .ps1: same control flow — `Read-Host -AsSecureString`, convert via `Marshal::PtrToStringAuto`, check `$t.Length -lt 100`, then `$BASE_URL = $BASE_URL.TrimEnd('/').TrimEnd('/wiki')` (or `if ($BASE_URL.EndsWith('/wiki')) { $BASE_URL = $BASE_URL.Substring(0, $BASE_URL.Length - 5) }`), `try { Invoke-RestMethod -Uri "$BASE_URL/rest/api/3/myself" -Authentication Basic -Credential (New-Object PSCredential($EMAIL, $secureToken)) } catch { if ($_.Exception.Response.StatusCode.value__ -eq 404) { Invoke-RestMethod -Uri "$BASE_URL/wiki/rest/api/user/current" -Authentication Basic -Credential ... } else { throw } }` — write to `cmdkey` + `secrets.env` only after a 200 lands from either probe.
   b. Tell me to open a real terminal (not Claude Code's `!`) and run `bash ~/.claude/agnes/bin/store-atlassian.sh` (or `pwsh ~/.claude/agnes/bin/store-atlassian.ps1` on Windows). The script will wait silently at the hidden prompt.
   c. Walk me through clipboard order: copy the launcher first, paste in terminal, Enter (terminal waiting). Switch to the Atlassian tab, copy the token from step 3 — use the panel's "Copy" button, NOT click-and-drag (which often truncates). Switch back to terminal, paste at the silent prompt, Enter. The script will print `✅ Atlassian ready — connected as <your name> on <site> (<product>).` on success, or `❌ Atlassian setup failed: <reason>` and exit non-zero without writing anything. Surface that exact line back to me in the chat so the final summary can grep for it.
5. Register the hosted Atlassian Remote MCP so Claude Code can read Jira tickets and Confluence pages on demand: `claude mcp add --transport sse atlassian https://mcp.atlassian.com/v1/sse || true`. Idempotent — the `|| true` swallows the "server already exists" error from re-runs. OAuth is handled by Claude Code the first time it actually queries the MCP (it'll open a browser tab; approve once). The PAT stored in step 4 stays for direct `curl` calls (e.g. the precheck) — the MCP path uses its own OAuth grant, not the PAT.
6. End-to-end test through your Bash tool — proves the integration works from inside Claude Code, not just from the user's real terminal where the store script ran. The store script's verify ran in MY shell; you (Claude Code) have not yet exercised the credential from your own sandbox. macOS: `. ~/.claude/agnes/secrets.env && t=$(security find-generic-password -s 'agnes-atlassian-api-token' -a "$ATLASSIAN_EMAIL" -w 2>/dev/null) && B="${ATLASSIAN_BASE_URL%/}" && B="${B%/wiki}" && tmp=$(mktemp) && code=$(curl -sS -o "$tmp" -w '%{http_code}' -u "$ATLASSIAN_EMAIL:$t" "$B/rest/api/3/myself") && { [ "$code" = "404" ] && code=$(curl -sS -o "$tmp" -w '%{http_code}' -u "$ATLASSIAN_EMAIL:$t" "$B/wiki/rest/api/user/current"); :; } && [ "$code" = "200" ] && jq -r '"✅ Atlassian integration verified — Claude Code can read as \(.displayName) on '"$B"'."' < "$tmp" && rm -f "$tmp"`. Linux: same shape but `t=$(secret-tool lookup service agnes-atlassian-api-token username "$ATLASSIAN_EMAIL")`. Windows native: skip this active test — instead tell me to run an equivalent `Invoke-RestMethod` against `$BASE_URL/rest/api/3/myself` (Jira) or `$BASE_URL/wiki/rest/api/user/current` (Confluence on 404) from a real PowerShell terminal where the CredentialManager module can read the stored token back. On 200, print the `✅ Atlassian integration verified — ...` line verbatim; that is the marker the final summary picks up. On any other status, print `❌ Atlassian integration test failed: HTTP <status>. Token stored but unreadable from Claude Code's bash (likely keychain-access policy / TCC denial), or the API endpoint shifted — check macOS Keychain Access for a denied prompt, or that $BASE_URL still resolves to Jira/Confluence.` and stop.
7. Remind me how to revoke: same API tokens page on Atlassian, plus `security delete-generic-password -s 'agnes-atlassian-api-token'` locally (macOS) / `secret-tool clear service agnes-atlassian-api-token` (Linux) / `cmdkey /delete:agnes-atlassian-api-token` (Windows).
