---
name: connector-asana
description: Set up Asana for Claude Code — Personal Access Token in OS keychain, REST API access. Triggers on phrases like "set up Asana", "configure Asana", "fix Asana access". Idempotent — short-circuits if already configured.
connector:
  display_name: Asana
  short_summary: Read tasks and projects, comment, create updates — Claude works alongside your project boards without leaving the terminal.
  estimated_minutes: 3
  vendor_url: https://app.asana.com/0/developer-console/tokens
---

Set up an Asana Personal Access Token for Claude Code. Walk me through it step by step.

Ground rules: this is idempotent — safe to re-run, the precheck below short-circuits when Asana is already wired up. We hit Asana's flat REST API at https://app.asana.com/api/1.0 directly via `curl` — no MCP server. (We tried the hosted MCP earlier; it consumed ~5× the tokens per call because the agent reads the entire response envelope, so we reverted to PAT + REST where the agent reads only the JSON fields it needs.) If any step fails with an unfamiliar error, paste the exact error back and stop. Do NOT improvise around TLS errors by disabling verification (`-k`, `NODE_TLS_REJECT_UNAUTHORIZED=0`, `git -c http.sslVerify=false`, etc.) — those hide the real problem.

0. Precheck — skip the rest if Asana is already connected.
   a. Leftover MCP registration: run `claude mcp list 2>/dev/null | grep -q '^asana'`. If it matches, ASK me verbatim: "I see Asana's hosted MCP server is still registered with Claude Code. {instance_brand} now uses the Asana REST API directly (PAT + curl). Benefits over the MCP path: (1) ~5× fewer tokens per call — the agent reads only the JSON fields it needs from flat REST responses instead of unwrapping MCP envelopes on every tool use; (2) no third-party hop — requests go straight from your machine to api.asana.com, not through mcp.asana.com (also better in airgapped / corporate-proxy setups where mcp.asana.com may not be allowlisted); (3) no OAuth refresh dance — PATs are static, MCP grants need re-auth on token rotation; (4) deterministic cost — curl returns the same bytes every time, whereas MCP envelope shapes can drift across server versions. Remove the MCP registration now? (Y/n)". Treat empty/Enter as YES. On Y: run `claude mcp remove asana` and (best-effort) `claude logout asana 2>/dev/null || true` to drop the OAuth grant. On explicit n / no / skip: leave it; warn that the two surfaces will compete (Claude Code may try the MCP path first for any `mcp__asana__*` tool calls) and continue to step 0b.
   b. Detect my OS, then look up an existing keychain entry under the service name `agnes-asana-pat` and verify it against Asana's API. macOS: `t=$(security find-generic-password -s 'agnes-asana-pat' -w 2>/dev/null) && curl -fsS -H "Authorization: Bearer $t" https://app.asana.com/api/1.0/users/me | jq -r '.data | "✅ Asana ready — connected as \(.name). \(.workspaces | length) workspace(s) visible."' && exit 0`. Linux: `t=$(secret-tool lookup service agnes-asana-pat username "$USER" 2>/dev/null) && ...same curl...`. Windows PowerShell: `$cred = cmdkey /list:agnes-asana-pat 2>$null; if ($LASTEXITCODE -eq 0) { Write-Host "Asana cred entry found — verify in your real terminal before re-running setup." }` (Windows can't read the password back without a CredentialManager module — print a hint and let me confirm). If the verify call returns 200, print the one-line "✅ Asana ready" message and STOP. Only continue to step 1 when no cred exists OR the cached token returns 401.
1. Open the Asana developer tokens page in my default browser — use your Bash tool: `open https://app.asana.com/0/developer-console/tokens` on macOS, `xdg-open https://app.asana.com/0/developer-console/tokens` on Linux/WSL, or `Start-Process https://app.asana.com/0/developer-console/tokens` on Windows. Detect OS first. If that URL doesn't render the tokens UI (rare), tell me to click my avatar (top right) → Settings → "Apps" tab → "Manage Developer Apps" → Personal access tokens.
2. Tell me to click "+ New access token", name it "Claude Code — {instance_brand}", and click "Create token". Warn me the token is shown ONCE and Asana PATs do not expire — I'd need to revoke it from the same page if it leaks.
3. Prepare a helper script for me to run in my real terminal (so the token never enters the chat):
   a. Detect my OS. Use the Write tool (NOT a shell here-doc that echoes the body) to create `~/.claude/agnes/bin/store-asana.sh` on macOS/Linux, or `~/.claude/agnes/bin/store-asana.ps1` on Windows. chmod 700 the file. Body for macOS:
      #!/usr/bin/env bash
      set -e
      read -srp 'Paste Asana token (hidden): ' t; echo
      # Verify against the live API BEFORE storing — never write a bad token to the keychain.
      tmp=$(mktemp)
      status=$(curl -sS -o "$tmp" -w '%{http_code}' -H "Authorization: Bearer $t" https://app.asana.com/api/1.0/users/me || true)
      if [ "$status" != "200" ]; then
        if [ "$status" = "401" ]; then
          echo "❌ Asana setup failed: token rejected (HTTP 401). The PAT is wrong, revoked, or pasted with whitespace — re-mint at https://app.asana.com/0/developer-console/tokens and retry." >&2
        else
          echo "❌ Asana setup failed: API verification returned HTTP $status. Aborting without storing." >&2
        fi
        rm -f "$tmp"; unset t; exit 1
      fi
      display=$(python3 -c 'import sys,json;d=json.load(sys.stdin)["data"];print(d.get("name","?"))' < "$tmp")
      wcount=$(python3 -c 'import sys,json;print(len(json.load(sys.stdin)["data"].get("workspaces",[])))' < "$tmp")
      rm -f "$tmp"
      security add-generic-password -U -s 'agnes-asana-pat' -a "$USER" -w "$t"
      unset t
      echo "✅ Asana ready — connected as $display. $wcount workspace(s) visible."

      Linux variant: same shape; replace `security add-generic-password ...` with `printf %s "$t" | secret-tool store --label='Agnes Asana PAT' service agnes-asana-pat username "$USER"`. Windows .ps1: `$t = Read-Host 'Paste Asana token' -AsSecureString; $p = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($t))`; verify with `Invoke-RestMethod -Uri 'https://app.asana.com/api/1.0/users/me' -Headers @{Authorization = "Bearer $p"}` inside a try/catch; on success `cmdkey /generic:agnes-asana-pat /user:$env:USERNAME /pass:$p > $null` and emit the same `✅ Asana ready — ...` line; on failure emit `❌ Asana setup failed: ...` and exit 1 without writing.
   b. Tell me to open a real terminal (Terminal.app / iTerm / WSL / PowerShell — NOT Claude Code's `!` prefix, which has no TTY) and run `bash ~/.claude/agnes/bin/store-asana.sh` (or `pwsh ~/.claude/agnes/bin/store-asana.ps1` on Windows). The script will wait silently at the hidden prompt.
   c. Walk me through clipboard order: copy the launcher first, paste it in my terminal, press Enter (terminal now waiting). Switch to the Asana tab, copy the token from step 2 — use the panel's Copy button, NOT click-and-drag (which can truncate). Switch back to terminal, paste at the silent prompt, press Enter. Token enters via stdin only — not shown on screen, not in shell history, not in clipboard at the moment Claude is involved.
4. After I report the script printed `✅ Asana ready — ...`, surface that exact line back to me in the chat so the final summary can grep for it. If the script printed `❌ Asana setup failed: ...` instead, surface that line and stop — do not silently re-run or move on.
5. End-to-end test through your Bash tool — proves the integration works from inside Claude Code, not just from the user's real terminal where the store script ran. The store script's verify ran in MY shell; you (Claude Code) have not yet exercised the credential from your own sandbox. macOS: `t=$(security find-generic-password -s 'agnes-asana-pat' -w 2>/dev/null) && curl -fsS -H "Authorization: Bearer $t" https://app.asana.com/api/1.0/users/me | jq -r '.data | "✅ Asana integration verified — Claude Code can read as \(.name). \(.workspaces | length) workspace(s) visible."'`. Linux: `t=$(secret-tool lookup service agnes-asana-pat username "$USER" 2>/dev/null) && curl -fsS -H "Authorization: Bearer $t" https://app.asana.com/api/1.0/users/me | jq -r '.data | "✅ Asana integration verified — Claude Code can read as \(.name). \(.workspaces | length) workspace(s) visible."'`. Windows native: skip this active test — `cmdkey` does not expose the secret to a non-interactive subshell; instead tell me to run the verify from a real PowerShell terminal: `$c = Get-StoredCredential -Target 'agnes-asana-pat'; Invoke-RestMethod -Uri 'https://app.asana.com/api/1.0/users/me' -Headers @{Authorization = "Bearer $($c.GetNetworkCredential().Password)"} | % { $_.data.name }` (requires the CredentialManager PowerShell module — install with `Install-Module CredentialManager` if missing). On 200, print the `✅ Asana integration verified — ...` line verbatim; this is the line the final summary picks up. On any other status, print `❌ Asana integration test failed: HTTP <status>. Token stored but unreadable from Claude Code's bash — likely a keychain-access policy / TCC denial; check macOS Keychain Access app for a denied prompt, or re-grant Claude Code access to the keychain.` and stop.
6. Remind me where the token is stored and how to revoke: in macOS Keychain Access search "agnes-asana-pat" or run `security delete-generic-password -s 'agnes-asana-pat'`; on Asana, revoke from the same developer-console page.
