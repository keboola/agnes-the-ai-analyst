"""Single source of truth for the "Setup a new Claude Code" clipboard payload.

Both the JS-embedded clipboard renderer (`_claude_setup_instructions.jinja`)
and the read-only HTML preview on the dashboard and /install pages consume
these lines. Keep it in Python so there is exactly ONE place that edits.

Placeholders `{server_url}` and `{token}` are substituted at render time.
For the preview we substitute `{token}` with a user-visible placeholder
string styled distinctly in the HTML preview.
"""

from __future__ import annotations

SETUP_INSTRUCTIONS_LINES: list[str] = [
    "Set up the Agnes CLI on this machine.",
    "",
    "Server: {server_url}",
    "Personal access token: {token}",
    "(Just generated; treat as a secret.)",
    "",
    "Run these, in order. If anything fails, paste the exact error back and stop.",
    "",
    "1) Install the CLI (uv fetches the wheel directly from the server URL):",
    "   uv tool install --force {server_url}/cli/agnes.whl",
    "",
    "   If uv is not installed:",
    "     curl -LsSf https://astral.sh/uv/install.sh | sh",
    "",
    "   If `da --version` fails after install because ~/.local/bin is not on PATH:",
    "     export PATH=\"$HOME/.local/bin:$PATH\"",
    "     # persist: append the same export to your ~/.zshrc or ~/.bashrc",
    "",
    "2) Log in (this also saves the server URL):",
    "   da auth import-token --token \"{token}\" --server \"{server_url}\"",
    "",
    "3) Verify:",
    "   da auth whoami",
    "",
    "4) Skills — ask the user first:",
    "   The CLI ships with markdown skills (setup, connectors, corporate-memory,",
    "   deploy, notifications, security, troubleshoot), listable via",
    "   `da skills list` and readable via `da skills show <name>`.",
    "",
    "   Ask the user: \"Copy the skills into ~/.claude/skills/agnes/ so they're",
    "   always loaded, or pull them on-demand via `da skills show`?\"",
    "",
    "   If they say copy:",
    "     mkdir -p ~/.claude/skills/agnes",
    "     for s in $(da skills list | awk '{print $1}'); do",
    "       da skills show \"$s\" > ~/.claude/skills/agnes/\"$s\".md",
    "     done",
    "",
    "5) Confirm \"Agnes CLI is ready\" and tell me the `da auth whoami` output",
    "   plus whether skills were copied.",
]


def render_setup_instructions(server_url: str, token: str) -> str:
    """Render the setup instructions as a single string.

    Used server-side for tests and any non-JS rendering path. The browser
    clipboard flow uses the JS renderer embedded in the Jinja partial; both
    must produce byte-identical output for a given (server_url, token).
    """
    text = "\n".join(SETUP_INSTRUCTIONS_LINES)
    return text.replace("{server_url}", server_url).replace("{token}", token)
