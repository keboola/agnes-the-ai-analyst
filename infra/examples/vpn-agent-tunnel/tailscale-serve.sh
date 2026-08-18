#!/usr/bin/env bash
# Tailscale Funnel — Option A, agent-only exposure (issue #1024).
#
# Run this on a host already joined to your tailnet, with tailscaled
# running, on the same network as this Agnes instance (127.0.0.1 below —
# adjust if Agnes runs elsewhere). `tailscale serve` + `tailscale funnel`
# open an outbound connection to Tailscale's relay, same idea as
# cloudflared-ingress.yml: nothing to open on your firewall.
#
#     chmod +x tailscale-serve.sh && ./tailscale-serve.sh
#
# Flags shown match Tailscale CLI >= 1.70 (`tailscale serve --set-path=...`).
# Run `tailscale serve --help` first to confirm the exact flags on your
# installed version before relying on this in production. This script
# assumes `--set-path` strips the mount prefix before proxying, so the
# path repeated in the target URL below round-trips back to the same
# path Agnes expects; a version that instead preserves the incoming path
# would double it (e.g. `/api/v1/sessions/api/v1/sessions/{id}`) and every
# route would 404. Confirm this before trusting the tunnel — after running
# the mounts below, smoke-test one allowlisted path end-to-end:
#   curl -I https://<your-funnel-hostname>/api/v1/jobs/does-not-exist
# A 401/404 *from Agnes* (check for its response body/headers) means the
# path arrived intact; a bare 404 with no such body means it never reached
# this instance — the path was likely mangled before it got here.
#
# IMPORTANT — read before using this instead of the Cloudflare recipe:
# `tailscale serve` mounts route by path PREFIX, not by regex. That is
# enough for `/api/v1/sessions/*` and `/api/v1/jobs/*` below, where every
# path under the prefix is on the allowlist with no exceptions. It is NOT
# enough for `/api/v1/agents/*`: the allowlist there needs `.../responses`,
# `.../usage` and `.../sessions` exposed while sibling suffixes on the
# very same dynamic prefix (`.../scope`, `.../tokens`, `.../memories*`,
# `.../webhooks`, and the bare agent CRUD routes) stay excluded — a
# distinction Cloudflare's regex `path:` ingress can express and a plain
# path-prefix mount cannot.
#
# This is not just "a bare PAT gets rejected on those routes" — it's a
# bigger trade-off than that. Those excluded routes are gated by
# `require_session_token` (app/auth/dependencies.py), which accepts any
# INTERACTIVE session credential (a normal logged-in user's cookie/JWT),
# with no additional network-location check. Before this script runs,
# that credential only works from inside the VPN/tailnet; once Funnel is
# on, `tailscale serve --set-path=/api/v1/agents` makes those same
# cookie-authenticated management routes (mint/revoke PATs, change an
# agent's scope, inspect its memories, bare CRUD) reachable from the
# public internet — the VPN-only network boundary that was implicitly
# protecting them is gone for this whole prefix, not just for a PAT.
# If that matters for your threat model, use `cloudflared-ingress.yml`
# instead — its regex ingress keeps those routes edge-404'd and off the
# public internet entirely, which is why Option A's README recommends it.

set -euo pipefail

AGNES_LOCAL="http://127.0.0.1:8000"

# Narrow prefix mounts — the entire subtree under each is on the allowlist,
# so a plain prefix mount is exact here, no caveat.
tailscale serve --bg --set-path=/api/v1/sessions "${AGNES_LOCAL}/api/v1/sessions"
tailscale serve --bg --set-path=/api/v1/jobs "${AGNES_LOCAL}/api/v1/jobs"

# Broad prefix mount — see the IMPORTANT header comment above. Covers
# .../responses, .../usage, .../sessions (allowed) AND .../scope,
# .../tokens, .../memories*, .../webhooks, bare agent CRUD (excluded by
# the plan, but not excludable by a path-prefix mount). This moves those
# require_session_token-gated, cookie-authenticated management routes
# from tailnet-only to public-internet-reachable once Funnel is on below
# — Agnes's own auth layer (rejects a bare PAT, still requires a valid
# interactive session) is what actually protects them, not the network.
tailscale serve --bg --set-path=/api/v1/agents "${AGNES_LOCAL}/api/v1/agents"

# Publish the mounts above to the public internet over HTTPS on :443.
# Everything NOT mounted above (including the rest of Agnes) stays
# tailnet-only — Funnel only ever serves what `tailscale serve` mounted.
tailscale funnel 443 on
