# Agnes chat sandbox template — runs claude-agent-sdk + the agnes CLI inside
# an E2B ephemeral microVM. The per-session runner code (app/chat/runner.py)
# is uploaded at spawn time via files.write — NOT baked into the image, so
# we can iterate on runner behavior without rebuilding the template.
#
# Tag: agnes-chat:latest  (Q2 — see plan; single mutable tag).
#
# IMPORTANT: No firewall / iptables rules are baked into the template, and
# none are needed — egress is enforced by the E2B platform, not from inside
# the sandbox: `E2BProvider.spawn` passes `deny_out=[ALL_TRAFFIC]` alongside
# the `chat.egress_allow_out` allowlist. The PreToolUse hook in the workspace
# template is defense-in-depth only; the VM-level deny-list survives an agent
# rewriting it. (Supersedes the original Q4 hook-only decision.)
FROM e2bdev/base:latest

# Python 3 + curl + Node.js (the latter for `@anthropic-ai/claude-code` CLI
# binary that claude-agent-sdk spawns under the hood for its MCP handshake).
# Without claude code installed, the SDK errors out with
# "Control request timeout: initialize" on first message.
RUN apt-get update && apt-get install -y python3 python3-pip curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Anthropic's Claude Code CLI — required by claude-agent-sdk's MCP loop.
RUN npm install -g @anthropic-ai/claude-code

# claude-agent-sdk + the agnes CLI's *runtime dependencies* — NOT the agnes
# package itself. The CLI is installed at spawn time from a wheel staged under
# /tmp/agnes-cli/ (the server's own build artifact — see
# app/chat/e2b_workspace_sync.upload_agnes_wheel + runner.py::_install_agnes_cli),
# so the in-sandbox CLI version always matches the running server. Baking the
# deps here means that spawn-time install can run `--no-deps` and stay fast.
#
# This list MUST cover the agnes CLI's full import closure, because cli/main.py
# imports every command module eagerly at startup — a missing dep makes the
# whole `agnes` binary crash on launch, not just one subcommand. The set below
# mirrors the CLI-relevant entries of [project.dependencies] in pyproject.toml;
# keep it in sync when the CLI gains a new top-level third-party import. (The
# heavier server-only deps — fastapi, google-cloud-*, … — are deliberately
# omitted: the CLI never imports them.)
#
# `matplotlib` is the exception: the CLI never imports it either, but the AGENT
# needs it and cannot obtain it. E2B's per-hostname egress allowlist (and
# `chat.docker_egress_mode: closed` on the Docker sibling) means an in-sandbox
# `pip install matplotlib` does not reach PyPI, so the agent improvises — the
# reported failure was an SVG written to `/tmp` and a user told to open a path
# that does not exist on their machine. The workspace CLAUDE.md carries the
# delivery half: a chart is returned as inline SVG in the reply, never a file.
# Pin the SDK to the same range the Agnes server depends on so a template
# rebuild stays in lockstep with the runner version.
RUN pip install --no-cache-dir --break-system-packages \
    "claude-agent-sdk>=0.2.87,<0.3.0" \
    "duckdb>=1.5.2" \
    "httpx>=0.27.0" \
    "pyyaml>=6.0" \
    "PyJWT>=2.13.0" \
    "typer>=0.12.0" \
    "rich>=13.0.0" \
    "filelock>=3.13,<4" \
    "pyarrow>=12.0.0" \
    "pandas>=2.0.0" \
    "numpy>=1.24.0" \
    "sqlglot>=30.0.0" \
    "matplotlib>=3.8"

# Per-session working dir; populated at spawn time by e2b_workspace_sync.
RUN mkdir -p /work && chmod 755 /work

# The runner script is uploaded at spawn time via sandbox.files.write —
# do NOT bake it here.
