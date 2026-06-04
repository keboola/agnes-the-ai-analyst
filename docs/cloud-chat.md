# Cloud-hosted Claude Code (`/chat` + Slack)

This page documents the cloud chat surface — what end users see, how
admins enable it, and what to know about cost / isolation.

## What it is

A zero-install web chat at `/chat` and a Slack DM bot, both backed by
the same `claude-agent-sdk` Python runner spawned inside an **E2B
ephemeral microVM**. Each session gets its own fresh sandbox with the
per-user workspace synced in at spawn time. Users get the full Agnes
harness (skills, marketplace, slash commands, `agnes` CLI, sub-agents)
without installing anything locally.

## Enabling on an instance

Default is **off**. To enable:

1. **Obtain an E2B account + API key.** E2B is the cloud microVM
   provider — sign up at https://e2b.dev, copy the API key from the
   dashboard.
2. **Build the chat sandbox template.** Run `e2b auth login` then `e2b
   template build` inside
   `app/initial_workspace_default/e2b-template/` (see that directory's
   README). The returned template id goes into `instance.yaml`.
3. **Edit `${DATA_DIR}/state/instance.yaml`:**

   ```yaml
   chat:
     enabled: true
     provider: e2b
     e2b_template_id: "agnes-chat"        # from step 2
     e2b_workspace_max_bytes: 104857600    # 100 MB (default)
   ```
4. **Set environment variables on the Agnes server:**

   - `ANTHROPIC_API_KEY` — required; the in-sandbox runner calls
     Anthropic directly via this key.
   - `E2B_API_KEY` — required; Agnes mints sandboxes via the E2B SDK
     using this key.
   - `JWT_SECRET_KEY` — 32+ bytes; mints session JWTs the in-sandbox
     `agnes` CLI uses to auth back to the Agnes REST API.

5. **Restart the Agnes server.** Watch the log for
   `ChatManager started (provider=e2b, template=...)`.
6. **Visit `/chat` while logged in.**

If any of the gates fail (API keys missing, template id missing,
`UVICORN_WORKERS > 1`), the manager refuses to start with a fatal log
line and all chat endpoints return 503 `chat_disabled`.

## Host requirements

Because the sandboxed runner now lives in E2B's cloud, the Agnes host
itself only needs RAM/CPU for the FastAPI app, ChatManager state, the
chat_repo (DuckDB) and any open WebSockets. A 2 GB / 1 vCPU box is
plenty for a small team. Per-sandbox compute floors (CPU/memory) are
set in `app/initial_workspace_default/e2b-template/e2b.toml` and billed
in the operator's E2B dashboard.

**Single-worker constraint.** ChatManager state is still in-memory; the
server refuses to enable chat if `UVICORN_WORKERS > 1`. HA support
(manager state in DuckDB/Redis) is a follow-up spec.

## Slack install

1. At api.slack.com/apps → Create New App → From manifest, paste
   `services/slack_bot/manifest.yaml` (replace `YOUR-AGNES-HOST` with
   your server's public hostname).
2. Install to your workspace; copy the Bot User OAuth Token to
   `SLACK_BOT_TOKEN` and the Signing Secret to `SLACK_SIGNING_SECRET`
   in Agnes env.
3. Slack users DM the bot to receive a 6-digit verification code,
   which they paste at `/setup` while logged into Agnes.

### Slash commands

The manifest also registers three slash commands, all pointed at
`https://YOUR-AGNES-HOST/api/slack/commands` (a separate Request URL
from the Events endpoint):

| Command | What it does |
|---|---|
| `/agnes <question>` | Asks Agnes; runs on your persistent DM session, so the answer also appears on web `/chat`. |
| `/agnes-new` | Archives your current Agnes DM session so the next `/agnes` starts fresh. |
| `/agnes-status` | Shows your active session count vs. the per-user cap, plus a `/chat` deep link. |
| `/agnes help` | Lists these commands (answered inline, no async work). |

Each command acks within Slack's 3 s budget and delivers its answer
asynchronously (ephemerally) via the command's `response_url`. Under
Socket Mode the commands arrive over the socket instead of the HTTP
Request URL — no manifest `url:` is needed in that mode.

### Manifest stanzas: HTTP vs Socket Mode

Two transports, two manifest shapes. Pick the one matching your
`chat.slack.transport` setting. Replace `<your-host>` with your public
Agnes hostname.

**HTTP (default).** Slack delivers events, slash commands and interactivity
over HTTPS to your public endpoints:

```yaml
settings:
  event_subscriptions:
    request_url: "https://<your-host>/api/slack/events"
    bot_events: [app_mention, message.im]
  interactivity:
    is_enabled: true
    request_url: "https://<your-host>/api/slack/interactivity"
  socket_mode_enabled: false
```

**Socket Mode (optional).** All three event classes arrive over one
WebSocket; no public `request_url` is needed — interactivity routes through
the same `dispatch_interaction`:

```yaml
settings:
  event_subscriptions:
    bot_events: [app_mention, message.im]
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
```

## Cost & limits

Per-user defaults (configurable in `/admin/server-config`):

| Setting | Default |
|---|---|
| Concurrent sessions per user | 3 |
| Idle TTL | 30 min |
| Anthropic spend cap | $20 / day |
| Cumulative tokens per session | 200 k |
| Per-tool-call wall clock | 90 s |
| BigQuery scan per session | 20 GiB |
| Workspace push cap | 100 MB |
| Kill sandbox on WS disconnect | yes |

**Sandbox billing.** Each chat session spawns a fresh E2B microVM
billed for its lifetime by E2B. The session ends when (a) the WebSocket
closes (`e2b_kill_on_ws_disconnect`, default on, per Q3), (b) the idle
TTL fires, (c) the max-session-seconds wall-clock fires, or (d) the
runner exits. Operators monitor cost in the E2B dashboard — Agnes does
not yet surface per-session sandbox cost in its admin UI.

## Security model

Single-tenant: all users in one Agnes instance trust each other. The
E2B microVM bounds FS / process / kernel isolation. The bundled
PreToolUse hook in the workspace template
(`.claude/hooks/pre_tool_use.py`) refuses workspace-destructive bash,
prompts for admin mutations, and enforces the egress allowlist. **Per
Q4 the egress allowlist exists only in the hook** — there is no
firewall layer baked into the E2B template, so a prompt injection that
rewrites the hook can reach arbitrary external hosts. The template's
README documents this trade-off and how to flip it.

**Warehouse data is sent to Anthropic by design** — do not store data
the operator does not want Anthropic to process.

## Operator setup details

### `agnes-chat:latest` is a mutable tag

Per Q2 the E2B template uses the mutable `:latest` tag rather than per-
release content hashes. Any teammate with E2B push access can rebuild
the template; the next sandbox spawn on every Agnes deployment picks up
the new image. **Test rebuilds on a dev Agnes first** — an incompatible
`claude-agent-sdk` bump will break the runner silently.

### Extending the E2B template

Edit `app/initial_workspace_default/e2b-template/Dockerfile` to add
runtime dependencies the runner needs, then `e2b template build` again.
The template README walks through the full flow.

### Per-user workspace size

Workspaces live on the Agnes host at
`${DATA_DIR}/users/<email>/workspace`. The 100 MB push cap
(`chat.e2b_workspace_max_bytes`) bounds the per-spawn upload to keep
session-start latency under a few seconds. Users who exceed the cap
get a `workspace_too_large` error frame; raise the cap or have them
trim local files.

## Known limitations (v1)

- No cloud↔local workspace sync. A user with local Claude Code and
  cloud chat has two independent workspaces.
- Slack: DM only. Channel `@agnes` mentions land in a follow-up PR.
- Single uvicorn worker only (see § Host requirements).
- **Bundled workspace ships no sub-agents.** `app/initial_workspace_default/.claude/agents/` is empty. Sub-agent dispatch (Task tool) requires the operator to install marketplace plugins that ship `agnes-*.md` agent definitions; without them the chat agent will answer directly without sub-agent delegation. The E2E test `tests/e2e/test_sub_agent_dispatch.py::F.9` auto-skips when no agents are present in the workspace.
- **`ANTHROPIC_API_KEY` + `E2B_API_KEY` + `chat.e2b_template_id` are gate-checked at startup.** Any missing value refuses chat with a clear log line.
- **Egress is fail-open at the network layer** (Q4 owner decision). The PreToolUse hook is the only barrier between the agent and `evil.example.com`. A prompt injection that rewrites the hook bypasses it. Defense-in-depth (re-introducing E2B firewall rules) is a follow-up.
- **`audit_log.user_id` for chat rows holds the user email, not the user UUID.** Joining `audit_log` to `users` for chat events requires `audit_log.user_id = users.email` for `action LIKE 'chat.%'` and the usual `audit_log.user_id = users.id` for everything else. Documented in `app/chat/audit.py::write_audit`.
- **`_real_agent_loop` enforces a turn-level wall-clock cap, not per-tool.** `claude-agent-sdk` 0.2.x doesn't expose per-tool dispatch hooks; the runner enforces `tool_calls_per_turn_budget` and a turn-level timeout instead of per-tool granularity. Revisit when the SDK ships per-tool hooks.
- **E2B SDK 1.x uses the mutable `:latest` template tag.** Per Q2 a teammate rebuild propagates to every live deployment on its next spawn — test rebuilds on a dev Agnes first.
- **E2B API outage → chat unavailable, no fallback.** Per Q6 there is no `SubprocessProvider` fallback; chat returns 503 until the E2B SDK recovers. Operators monitor E2B status separately.
- **Per-session E2B billing is operator-visible only in the E2B dashboard**, not yet in Agnes admin UI.
