# Agnes chat sandbox template — runs claude-agent-sdk + the agnes CLI inside
# an E2B ephemeral microVM. The per-session runner code (app/chat/runner.py)
# is uploaded at spawn time via files.write — NOT baked into the image, so
# we can iterate on runner behavior without rebuilding the template.
#
# Tag: agnes-chat:latest  (Q2 — see plan; single mutable tag).
#
# IMPORTANT: No firewall / iptables rules are baked into the template — per
# Q4 the egress allowlist lives only in the PreToolUse hook bundled in the
# workspace template (`.claude/hooks/pre_tool_use.py`). This is fail-open
# at the network layer; a prompt injection that rewrites the hook can
# bypass it. Tradeoff accepted for operator-setup simplicity.
FROM e2bdev/python:latest

# claude-agent-sdk + agnes CLI runtime deps. Pin the SDK to the same range
# the Agnes server depends on so a template rebuild stays in lockstep with
# the runner version.
RUN pip install --no-cache-dir \
    "claude-agent-sdk>=0.2.87,<0.3.0" \
    "duckdb>=1.5.2" \
    "httpx>=0.27.0" \
    "pyyaml>=6.0" \
    "PyJWT>=2.8.0" \
    "typer>=0.12.0" \
    "rich>=13.0.0"

# Per-session working dir; populated at spawn time by e2b_workspace_sync.
RUN mkdir -p /work && chmod 755 /work

# The runner script is uploaded at spawn time via sandbox.files.write —
# do NOT bake it here.
