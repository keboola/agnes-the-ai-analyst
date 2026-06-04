# Agnes chat — E2B sandbox template

This directory defines the E2B template that runs every cloud-chat
session. The template ships `claude-agent-sdk`, the `agnes` CLI
runtime, and the Python dependencies the in-sandbox runner needs. The
per-session runner code itself (`app/chat/runner.py`) is uploaded at
spawn time, not baked.

## Build the template (operator one-time setup)

```bash
# 1. Install the E2B CLI (Node-based; brew or npm)
npm install -g @e2b/cli

# 2. Log into the E2B org account the operator manages
e2b auth login

# 3. Build the template (from this directory)
cd app/initial_workspace_default/e2b-template
e2b template build
```

The build returns a `template_id` (something like
`base-agnes-chat-1a2b3c4d`). Copy it into `config/instance.yaml`:

```yaml
chat:
  enabled: true
  provider: e2b
  e2b_template_id: "agnes-chat"   # or the returned hashed id
```

Also set `E2B_API_KEY` in the Agnes server environment (`.env` or
container env) — Agnes mints the sandbox via the SDK at session-spawn
time using that key.

## `:latest` vs pinned tags

Per the owner decision on Q2, this template uses the mutable
`agnes-chat:latest` tag by default. Consequences:

- Any team member with E2B push access can rebuild the template and
  every live Agnes deployment picks up the new image on the next
  sandbox spawn — no Agnes redeploy required.
- A rebuild that ships an incompatible `claude-agent-sdk` version (or
  any other runtime dep) may break the runner silently. **Test rebuilds
  on a dev Agnes first.** For production rollouts consider pinning a
  content-hashed tag temporarily.

## What is *not* in the template

- **No firewall / iptables rules.** Per Q4 the operator chose
  ops-simplicity over network-layer defense-in-depth. Egress allowlist
  enforcement lives only in the PreToolUse hook bundled with the
  default workspace template
  (`.claude/hooks/pre_tool_use.py`). A prompt injection that rewrites
  the hook can therefore reach arbitrary hosts. Re-introduce E2B
  network policy here if the threat model changes.
- **No runner code.** `app/chat/runner.py` is uploaded by Agnes at
  sandbox spawn via `files.write` — change the runner code without
  rebuilding the template.
- **No host UID mapping, no nsjail config, no iptables.** E2B's
  microVM is the isolation boundary.

## Updating the dependency pins

When `pyproject.toml` bumps `claude-agent-sdk` or another runtime dep,
edit `Dockerfile` here to match, then `e2b template build` again.
