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
    # Patch 2 (PAT delivered out-of-band, not embedded in the prompt text):
    # the raw access token must never be a substitutable placeholder or a
    # literal JWT fragment inside the rendered body.
    "{token}",
    "eyJ",
]

# Facts that must survive de-escalation — the wording changed, not the
# underlying information.
#
# The list shrank when the prompt went thin (2026-08-19): the workspace
# triage facts it used to pin (`init-complete`, `agnes update`, the unsafe
# `$HOME` / `/tmp` directories, `--token-file`) are no longer prompt copy —
# `agnes onboard` owns those decisions and reports them at runtime. What the
# prompt still has to say is pinned below.
REQUIRED_FACTS: list[str] = [
    "$HOME",
    "idempotent",
    "~/.agnes/token",
    "agnes onboard",
    "--accept-dir",
]
