# VPN-only Agnes: exposing agents (or the full connector) via tunnel

Recipe for issue #1024: a VPN/intranet-only Agnes instance is invisible to
cloud-based AI clients (Claude.ai, ChatGPT) because those run outside your
corporate network. This is **not** an Agnes feature — it's operator-run
infrastructure. Agnes itself never owns or creates a Cloudflare or Tailscale
account; you run your own outbound tunnel, and the tunnel's ingress rules
decide which paths it forwards to this instance while everything else stays
on the VPN. Full write-up: [`docs/DEPLOYMENT.md`](../../../docs/DEPLOYMENT.md)
(search "Private-network-only deployments").

Two exposure patterns, same underlying tunnel software, different path
allowlists:

## Option A — agent-only tunnel (recommended for VPN-only instances)

Exposes **only** the agent-as-API runtime — `app/api/agent_runtime.py` and
`app/api/agent_sessions.py`, both mounted at `/api/v1`. That surface is
Bearer-**PAT**-authenticated (never a browser session) and scoped to exactly
one `'selected'`-mode agent, whose effective authority is the intersection of
its own declared scope and its owner's grants — enforced live on every
brokered request (`src/agent_scope_intersection.py`), so it can never exceed
what the agent's owner could already do. This is the option to reach for when
you want some external automation to call one scoped agent without exposing
anything else.

Allowlisted paths:

```
POST   /api/v1/agents/*/responses
GET    /api/v1/agents/*/usage
POST   /api/v1/agents/*/sessions
POST   /api/v1/sessions/*/messages
GET    /api/v1/sessions/*
POST   /api/v1/sessions/*/cancel
DELETE /api/v1/sessions/*
GET    /api/v1/sessions/*/artifacts
GET    /api/v1/sessions/*/artifacts/*
GET    /api/v1/jobs/*
```

**Deliberately never exposed**, even though they live under the same
`/api/v1/agents/*` prefix — every one is management-only and already gated by
`require_session_token` (`app/api/agents_admin.py`, `app/api/agent_webhooks.py`),
which rejects a bare PAT outright:

```
/api/v1/agents                    (bare create/list)
/api/v1/agents/{id}                (get/put/delete)
/api/v1/agents/{id}/scope
/api/v1/agents/{id}/tokens
/api/v1/agents/{id}/memories*
/api/v1/agents/{slug}/webhooks
```

The allowlist excludes them at the network edge anyway, as defense-in-depth:
don't let the tunnel forward them in the first place, even though the app's
own auth layer would already reject a bare PAT that reached them.

PAT minting (`POST /api/v1/agents/{id}/tokens`, via `agnes agent token <slug>
--name <label>`) happens **inside** the VPN, by the agent's owner — only the
resulting token, never a management endpoint, gets handed to the external
automation that calls the tunnel URL. No `SERVER_URL`/`PUBLIC_URL`/
`AGNES_BASE_URL` change is needed for this path: neither router derives
anything from the instance's public origin (that machinery,
`app/auth/public_url.py`, backs the unrelated MCP-OAuth issuer used by
Option B below).

Templates:

- [`cloudflared-ingress.yml`](cloudflared-ingress.yml) — path-based ingress
  rules implementing the allowlist above exactly, with a `404` catch-all so
  nothing unlisted (including every excluded management route) ever reaches
  Agnes.
- [`tailscale-serve.sh`](tailscale-serve.sh) — the same intent via `tailscale
  serve` + `funnel`. Read the header comment first: Tailscale's `serve`
  mounts route by path **prefix**, not by regex, so it cannot carve the
  excluded management routes out of `/api/v1/agents/*` the way Cloudflare's
  ingress can — see that file for the trade-off and how it's handled.

## Option B — full-connector tunnel (existing option, more exposure)

For operators who want the complete "Claude as my assistant" experience
despite being VPN-only: expose `/api/mcp/http*` and `/.well-known/*` instead.
This is the same general, OAuth-authenticated MCP connector already
documented as the normal manual-connector flow at `/how-it-works#connect` —
full RBAC access as whichever user authenticates through it. Nothing new to
build here; a tunnel in front of the existing connector is the only new
part, and it is a materially bigger exposure than Option A (every
RBAC-visible resource for every user who connects, not one agent's scoped
authority). No template in this directory for it — point either tool's
ingress at `/api/mcp/http*` + `/.well-known/*` the same way Option A's
templates point at the agent-API allowlist.

## Not exposing anything

If you'd rather not run a tunnel at all, hide the "connect your AI client"
instructions instead — on a VPN-only instance nobody can tunnel into, they
only mislead. Set `AGNES_MCP_CONNECTOR_UI_ENABLED=0` (or
`mcp.connector_ui_enabled: false` in `instance.yaml`) — see
[`docs/feature-flags.md`](../../../docs/feature-flags.md).
