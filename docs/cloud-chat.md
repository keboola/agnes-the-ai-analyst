# Cloud-hosted Claude Code (`/chat` + Slack)

This page documents the cloud chat surface — what end users see, how
admins enable it, and what to know about cost / isolation.

## What it is

A zero-install web chat at `/chat` and a Slack DM bot, both backed by
the same `claude-agent-sdk` Python subprocess running inside an
nsjail-isolated sandbox on the Agnes server. Users get the full Agnes
harness (skills, marketplace, slash commands, `agnes` CLI,
sub-agents) without installing anything locally.

## Enabling on an instance

Default is **off**. To enable:

1. Set `chat.enabled: true` in `${DATA_DIR}/state/instance.yaml`.
2. Verify the host meets the floor (see § Host requirements).
3. Restart the Agnes server.
4. Visit `/chat` while logged in.

## Host requirements

Per the spec (§ Deployment requirements), each active session reserves
up to 1 GB RAM × 1 CPU under nsjail rlimits. For 10 active users at
the default 3 sessions/user cap, the floor is ~16 GB RAM / 12 vCPU.
For smaller hosts, lower `chat.concurrency_per_user` in
`/admin/server-config` before enabling.

**Single-worker constraint.** ChatManager state is in-memory. The
server refuses to enable chat if `UVICORN_WORKERS > 1` — it logs an
error at startup and leaves `chat_manager` as `None` (all chat endpoints
return 503). HA support (manager state in DuckDB/Redis) is a follow-up
spec.

**nsjail.** Linux only. macOS dev mode runs unjailed and the server
refuses to start with `chat.require_isolation: true` (the default).
For local dev, set `chat.require_isolation: false` explicitly.

## Slack install

1. At api.slack.com/apps → Create New App → From manifest, paste
   `services/slack_bot/manifest.yaml` (replace `YOUR-AGNES-HOST` with
   your server's public hostname).
2. Install to your workspace; copy the Bot User OAuth Token to
   `SLACK_BOT_TOKEN` and the Signing Secret to `SLACK_SIGNING_SECRET`
   in Agnes env.
3. Slack users DM the bot to receive a 6-digit verification code,
   which they paste at `/setup` while logged into Agnes.

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

## Security model

Single-tenant: all users in one Agnes instance trust each other.
nsjail bounds FS / network / syscalls; the bundled PreToolUse hook
refuses workspace-destructive bash and prompts for admin mutations.
**Warehouse data is sent to Anthropic by design** — do not store data
the operator does not want Anthropic to process.

## Network egress allowlist (operator setup)

nsjail restricts filesystem access and syscalls but does **not** enforce
network egress on its own — it relies on the host kernel's netfilter
(iptables/nftables) rules filtered by the process UID that runs the
`agnes-sandbox` subprocess.

Without operator-configured iptables rules, sandboxed sessions have
full host-network access. Add the following rules (as root) after
determining the host UID that runs the Agnes server process:

```bash
# As root — replace $UID with the host UID running the agnes-sandbox process
# (e.g. `id -u agnes` if you run under a dedicated system account).

# Allow loopback (agnes CLI talking back to the Agnes server)
sudo iptables -A OUTPUT -m owner --uid-owner $UID -d 127.0.0.1 -j ACCEPT

# Allow outbound HTTPS to the Anthropic API
sudo iptables -A OUTPUT -m owner --uid-owner $UID -p tcp --dport 443 \
    -d api.anthropic.com -j ACCEPT

# Allow outbound HTTPS to the GitHub API (agnes CLI, marketplace updates)
sudo iptables -A OUTPUT -m owner --uid-owner $UID -p tcp --dport 443 \
    -d api.github.com -j ACCEPT

# Drop everything else from this UID
sudo iptables -A OUTPUT -m owner --uid-owner $UID -j DROP
```

These rules must be applied after every host reboot (add to a
startup script or systemd unit) and before Agnes starts. Adjust the
destination hosts to match any additional services your instance needs
to reach (e.g. your BigQuery endpoint if remote-mode tables are in use).

## Known limitations (v1)

- No cloud↔local workspace sync. A user with local Claude Code and
  cloud chat has two independent workspaces.
- Slack: DM only. Channel `@agnes` mentions land in a follow-up PR.
- Single uvicorn worker only (see § Host requirements).
