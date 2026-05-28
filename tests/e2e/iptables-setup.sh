#!/usr/bin/env bash
# iptables-setup.sh — OWNER egress rules for the chat sandbox.
#
# These rules mirror the operator setup in docs/cloud-chat.md: the
# sandboxed user can talk to localhost (for the Agnes REST API the
# in-process agent calls via the bundled `agnes` CLI), to
# api.anthropic.com (for the LLM call), and to api.github.com (for
# marketplace pulls). Everything else is dropped.
#
# Requires CAP_NET_ADMIN — the compose file sets `cap_add: [NET_ADMIN]`.
# Without the capability iptables refuses to install rules and this
# script exits non-zero, which fails the container start (intentional —
# silent egress-allow defeats the sandbox guarantee).

set -euo pipefail

SANDBOX_UID="$(id -u agnes-sandbox)"

# Localhost — Agnes REST API the in-process agent reaches through `agnes` CLI.
iptables -A OUTPUT -m owner --uid-owner "${SANDBOX_UID}" -d 127.0.0.1/32 -j ACCEPT

# Anthropic API — LLM calls.
iptables -A OUTPUT -m owner --uid-owner "${SANDBOX_UID}" \
    -p tcp --dport 443 -d api.anthropic.com -j ACCEPT

# GitHub API — marketplace plugin pulls.
iptables -A OUTPUT -m owner --uid-owner "${SANDBOX_UID}" \
    -p tcp --dport 443 -d api.github.com -j ACCEPT

# DNS — required to resolve the two allow-listed hostnames. Without
# this the ACCEPTs above never see traffic (resolution fails first).
iptables -A OUTPUT -m owner --uid-owner "${SANDBOX_UID}" \
    -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -m owner --uid-owner "${SANDBOX_UID}" \
    -p tcp --dport 53 -j ACCEPT

# Drop everything else for the sandbox uid.
iptables -A OUTPUT -m owner --uid-owner "${SANDBOX_UID}" -j DROP

echo "[iptables-setup] OWNER rules installed for uid=${SANDBOX_UID}"
