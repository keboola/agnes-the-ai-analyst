"""Input validation for the Jira connector.

Two layers of defense for issue keys (which arrive from attacker-controlled
webhook payloads, see issue #83):

1. ``is_valid_issue_key`` — whitelist regex against the Jira format.
2. ``safe_join_under`` — Path.resolve() containment check, defense-in-depth
   against future regex relaxation, symlink shenanigans, or callers that
   forget the regex check.
"""

from __future__ import annotations

import re
from pathlib import Path

# Jira issue keys: project key + dash + issue number.
#
# Atlassian's project-key validator: first char must be a letter; the rest
# are letters and digits only. Underscores are NOT allowed in real project
# keys despite some informal docs suggesting otherwise — confirmed via the
# Atlassian project-creation form, which rejects `A_B`. Bounded length
# (32 chars on the project, 12 digits on the number) keeps regex evaluation
# cheap on adversarial input.
# `[0-9]` rather than `\d` — Python 3's `\d` matches any Unicode decimal
# (Arabic-Indic ٣, Bengali ৩, Devanagari ३, …), and a Jira issue key like
# `TEST-٣` is not real Jira input. ASCII-only here closes that bypass.
# `\Z` rather than `$` — Python's `$` matches before a trailing `\n`,
# so `re.match("…$", "TEST-1\n")` returns a match. `\Z` is hard
# end-of-string, so a CRLF-injection or trailing-newline payload is
# rejected as expected.
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{0,31}-[0-9]{1,12}\Z")


def is_valid_issue_key(key: object) -> bool:
    """Return True if ``key`` is a syntactically valid Jira issue key."""
    return isinstance(key, str) and bool(_ISSUE_KEY_RE.match(key))


def safe_join_under(base: Path, *parts: str) -> Path:
    """Join ``parts`` under ``base`` and verify the result stays within ``base``.

    Raises ValueError on any escape attempt. Use at every filesystem boundary
    that touches attacker-supplied path components, even when callers have
    already validated the components — this is defense-in-depth.
    """
    base_resolved = base.resolve()
    candidate = base.joinpath(*parts).resolve()
    if base_resolved != candidate and base_resolved not in candidate.parents:
        raise ValueError(
            f"Path traversal blocked: {candidate} is not under {base_resolved}"
        )
    return candidate
