"""Shared phrase lists for the install-prompt de-escalation guard.

Imported by both `scripts/dev/check_prompt.py` (manual verification loop) and
`tests/test_install_prompt_banned_phrases.py` (the regression guard) so the
two never drift.
"""

from __future__ import annotations

# Force-style / coercive phrasing the rendered prompt must never contain.
# Each was present in the pre-de-escalation prompt and is a real trigger for
# Claude Code's safety classifier stalling mid-install.
BANNED_PHRASES: list[str] = [
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "http.sslVerify",
    "strict-ssl=false",
    "| sh",
    "| bash",
    "--silent",
    "verbatim",
    "REFUSE",
    "PROCEED SILENTLY",
    "Treat empty/Enter",
    "do NOT ask the user",
    "GOCSPX-",
]

# Facts that must survive de-escalation — the wording changed, not the
# underlying information.
REQUIRED_FACTS: list[str] = [
    "init-complete",
    "agnes update",
    "$HOME",
    "/tmp",
    "idempotent",
    "--token-file",
]
