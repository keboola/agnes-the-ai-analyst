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
# path-prefix mount cannot. If you need the tunnel itself to enforce that
# exclusion (defense-in-depth, not just Agnes's own
# `require_session_token` gate on those routes), use
# `cloudflared-ingress.yml` instead. This script mounts the whole
# `/api/v1/agents` prefix and relies on Agnes's own auth layer — which
# already rejects a bare PAT on every excluded route — as the actual
# enforcement for that one prefix.

set -euo pipefail

AGNES_LOCAL="http://127.0.0.1:8000"

# Narrow prefix mounts — the entire subtree under each is on the allowlist,
# so a plain prefix mount is exact here, no caveat.
tailscale serve --bg --set-path=/api/v1/sessions "${AGNES_LOCAL}/api/v1/sessions"
tailscale serve --bg --set-path=/api/v1/jobs "${AGNES_LOCAL}/api/v1/jobs"

# Broad prefix mount — see the header comment above. Covers
# .../responses, .../usage, .../sessions (allowed) AND .../scope,
# .../tokens, .../memories*, .../webhooks, bare agent CRUD (excluded by
# the plan, but not excludable by a path-prefix mount) — Agnes's own
# require_session_token gate is what actually blocks a bare PAT on the
# latter group.
tailscale serve --bg --set-path=/api/v1/agents "${AGNES_LOCAL}/api/v1/agents"

# Publish the mounts above to the public internet over HTTPS on :443.
# Everything NOT mounted above (including the rest of Agnes) stays
# tailnet-only — Funnel only ever serves what `tailscale serve` mounted.
tailscale funnel 443 on
