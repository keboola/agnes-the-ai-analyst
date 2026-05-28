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
2. Set `ANTHROPIC_API_KEY` in the Agnes server env; the runner subprocess
   inherits it via the scrub allowlist (see
   `app/chat/subprocess_provider.py::_ENV_ALLOWLIST`). Without it the
   real-agent path silently fails on its first Anthropic API call.
3. Verify the host meets the floor (see § Host requirements).
4. Restart the Agnes server.
5. Visit `/chat` while logged in.

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

## Operator setup

### Dedicated sandbox host user (recommended for production)

By default the nsjail subprocess runs under Agnes's own host UID
(`os.getuid()` of the server process). That's fine for single-tenant
development, but production deployments should isolate the sandbox under
a dedicated host user so:

1. Agnes itself does not need to run as root.
2. The iptables OWNER rules below can scope to that user only.

```bash
# As root: create the sandbox user (no login, no home dir needed).
sudo useradd --system --no-create-home --shell /usr/sbin/nologin agnes-sandbox
SANDBOX_UID=$(id -u agnes-sandbox)
```

Then set `chat.sandbox_uid: <SANDBOX_UID>` in your `instance.yaml`
(under the `chat:` block). When unset, the SubprocessProvider falls back
to `os.getuid()` — same behaviour as v1.

### Network egress allowlist

nsjail restricts filesystem access and syscalls but does **not** enforce
network egress on its own — it relies on the host kernel's netfilter
(iptables/nftables) rules filtered by the process UID that runs the
`agnes-sandbox` subprocess.

Without operator-configured iptables rules, sandboxed sessions have
full host-network access. Add the following rules (as root) using the
`SANDBOX_UID` from the section above:

```bash
# As root — SANDBOX_UID is the uid of the agnes-sandbox user created above
# (or os.getuid() of the Agnes server process if you skipped that step).

# Allow loopback (agnes CLI talking back to the Agnes server)
sudo iptables -A OUTPUT -m owner --uid-owner $SANDBOX_UID -d 127.0.0.1 -j ACCEPT

# Allow outbound HTTPS to the Anthropic API
sudo iptables -A OUTPUT -m owner --uid-owner $SANDBOX_UID -p tcp --dport 443 \
    -d api.anthropic.com -j ACCEPT

# Allow outbound HTTPS to the GitHub API (agnes CLI, marketplace updates)
sudo iptables -A OUTPUT -m owner --uid-owner $SANDBOX_UID -p tcp --dport 443 \
    -d api.github.com -j ACCEPT

# Drop everything else from this UID
sudo iptables -A OUTPUT -m owner --uid-owner $SANDBOX_UID -j DROP
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
