"""Flag knowledge-note text that will read as an *instruction* once delivered.

Approved corporate-memory items are written verbatim by ``agnes pull`` into
``<workspace>/.claude/rules/km_*.md`` (``cli/lib/pull.py``), and Claude Code
loads that directory at session start as **project rules** — an instruction
channel, not a reference one. A note phrased as a next step therefore stops
being a note: it becomes a standing directive in every analyst's session.

That gap is what this module surfaces. An admin approving an item is deciding
"this is worth remembering"; the delivery channel turns it into "every agent
must do this". Nothing in the approval UI said so, so a perfectly ordinary
session recap — *"Next step is to type /exit and rerun claude from /srv … with
recaps disabled in /config"* — shipped to every workspace as an order, where an
agent correctly refused to follow it and reported the file as untrusted.

**Signal, not gate.** Same posture as ``src/store_guardrails/static_scan.py``:
findings annotate the approval surfaces and never block an action. Precision is
worth more than recall here — an approver who sees a warning on most items
stops reading warnings — so the patterns only match text aimed at the agent's
own harness (its slash commands, its session, its configuration, its
willingness to report). Ordinary imperative knowledge ("you must exclude test
accounts", "run the rebuild before querying") is left alone: it is advice about
the work, which is exactly what corporate memory is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Claude Code's own slash commands. Matched anywhere, since a bare `/compact`
# in prose is already the shape of an instruction.
# The lookbehind keeps URLs and paths out — `example.com/config` and
# `/data/config` are not instructions, `/config` on its own is.
#: Claude Code commands whose names are not also ordinary web paths. A bare
#: mention of one of these in prose is already the shape of an instruction.
_UNAMBIGUOUS_SLASH = "exit|quit|clear|compact|permissions|resume|doctor|hooks|agnes-private"

#: …and the ones that collide with this product's OWN pages and with ordinary
#: prose: `/agents` is the agent-builder page, `/status`, `/cost`, `/review`,
#: `/config`, `/init`, `/model`, `/help` all read as paths or plain words. A
#: note saying "agent profiles are managed at /agents" is documentation, not
#: an instruction to the reading agent — flagging it taught admins to ignore
#: the warning. These count only when a verb tells someone to run them, which
#: `_VERB_SLASH_RE` below already catches. (Devin Review on #1258.)
_AMBIGUOUS_SLASH = "agents|status|cost|review|config|init|model|help|login|logout"

# The trailing guard mirrors the leading one: `/hooks/setup` and
# `/permissions/admin` are ordinary web addresses that merely BEGIN with a
# command name, and flagging them puts false alarms in front of the approver
# on plain documentation. A command mention ENDS the token — punctuation and
# whitespace are fine, another path segment or a dotted suffix is not.
# (Devin Review on #1258.)
_KNOWN_SLASH_RE = re.compile(
    rf"(?<![\w/])/(?:{_UNAMBIGUOUS_SLASH})\b(?![-/]|\.\w)",
    re.IGNORECASE,
)

# An unknown slash token becomes interesting once a verb tells someone to run
# it. The negative lookahead keeps filesystem paths and URLs out: `/data/x` and
# `/srv/app.py` both continue past the token, a slash command does not.
_VERB_SLASH_RE = re.compile(
    r"\b(?:type|run|press|enter|invoke|execute|use)\s+[`'\"]?(/[a-z][a-z0-9-]{1,30})(?![/.\w])",
    re.IGNORECASE,
)

# Restarting or leaving the agent's own session.
_SESSION_CONTROL_RE = re.compile(
    r"\b(?:re-?run|re-?start|re-?launch|re-?open)\s+(?:the\s+)?(?:claude(?:\s+code)?|session|agent)\b"
    # `end`/`kill` are out on purpose: "session" is also a data concept here
    # (web_sessions), and "end the session" is ordinary analytics prose.
    r"|\b(?:exit|quit)\s+(?:the\s+|this\s+)?session\b"
    r"|\brun\s+claude\s+from\b",
    re.IGNORECASE,
)

# Turning the harness's own machinery on or off. The nouns are deliberately
# specific — a bare "disable" or "settings" appears constantly in legitimate
# operational notes.
_HARNESS_VERB = r"disabl\w+|enabl\w+|turn(?:ing|ed)?\s+(?:off|on)|switch(?:ing|ed)?\s+(?:off|on)"
# `telemetry` and bare `permissions` are deliberately absent — product
# telemetry and RBAC permissions are both data topics here, so they would fire
# on ordinary notes. Only the harness's own machinery is named.
_HARNESS_NOUN_WORDS = r"recaps?|hooks?|permission[- ]modes?|classifiers?|guardrails?|safety|auto[- ]?mode|settings\.json"
# `/config` is a SLASH token, not a word, so it cannot ride the same `\b`: a
# leading `\b` before `/` requires a WORD character in front of the slash —
# precisely the URL/path shape this module says it excludes. The effect was
# inverted: `example.com/config was disabled` fired, and the standalone
# `disable the /config area` it was added for never did. It gets its own
# boundary — nothing word-ish, no slash and no dot before it, and nothing
# path-like after — while the word nouns keep theirs. (Devin Review on #1258.)
_HARNESS_NOUN = rf"(?:\b(?:{_HARNESS_NOUN_WORDS})\b|(?<![\w/.])/config(?!\.\w)(?![\w/]))"
# Both directions: "disable the recaps" and "with recaps disabled" are the same
# instruction, and the incident note used the second one.
_HARNESS_CONFIG_RE = re.compile(
    rf"\b(?:{_HARNESS_VERB})\b[^.\n]{{0,48}}?{_HARNESS_NOUN}"
    rf"|{_HARNESS_NOUN}[^.\n]{{0,48}}?\b(?:{_HARNESS_VERB})\b",
    re.IGNORECASE,
)

# Text asking the agent to stay quiet or to disregard what it was told.
_SAFETY_SUPPRESSION_RE = re.compile(
    r"\b(?:do\s+not|don'?t|never)\s+(?:warn|ask|tell|mention|flag|report|surface)\b"
    r"|\bignore\s+(?:(?:the|any|all|previous|prior|earlier)\s+)+(?:instruction|rule|guidance|warning|message)",
    re.IGNORECASE,
)

_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "slash_command",
        _KNOWN_SLASH_RE,
        "names a Claude Code slash command",
    ),
    (
        "slash_command",
        _VERB_SLASH_RE,
        "tells the reader to run a slash command",
    ),
    (
        "session_control",
        _SESSION_CONTROL_RE,
        "tells the reader to restart or leave the session",
    ),
    (
        "harness_config",
        _HARNESS_CONFIG_RE,
        "tells the reader to change the agent's configuration",
    ),
    (
        "safety_suppression",
        _SAFETY_SUPPRESSION_RE,
        "tells the reader not to report or to disregard guidance",
    ),
)

_EXCERPT_LIMIT = 200

# One sentence, shared by every approval surface, so the web page, the CLI and
# a raw API caller describe the consequence identically.
DELIVERY_NOTICE = (
    "Approved and required items are written into every analyst's workspace as Claude Code "
    "project rules (.claude/rules/km_*.md), where an agent reads them as instructions rather "
    "than as reference material."
)


@dataclass(frozen=True)
class DirectiveFinding:
    """One span of note text that reads as an instruction to the agent."""

    kind: str
    reason: str
    excerpt: str
    line: int
    fields: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "excerpt": self.excerpt,
            "line": self.line,
        }


def _sentence_around(text: str, start: int, end: int) -> str:
    """The sentence containing [start, end), trimmed for display.

    Sentence rather than line: the delivered file re-wraps, and an approver
    judging "is this an instruction?" needs the clause, not 80 columns of it.
    """
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start))
    right_candidates = [i for i in (text.find(".", end), text.find("\n", end)) if i != -1]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    excerpt = text[left + 1 : right].strip()
    if len(excerpt) > _EXCERPT_LIMIT:
        excerpt = excerpt[: _EXCERPT_LIMIT - 1].rstrip() + "…"
    return excerpt


def scan_for_agent_directives(content: str) -> list[DirectiveFinding]:
    """Return the spans of ``content`` that read as harness-directed orders.

    Empty list means nothing matched — which is not a safety verdict, only the
    absence of the specific shapes above.
    """
    if not content:
        return []

    seen: set[tuple[str, str]] = set()
    findings: list[DirectiveFinding] = []
    for kind, pattern, reason in _PATTERNS:
        for match in pattern.finditer(content):
            # Two patterns can hit the same CLAUSE (a sentence that names a
            # slash command *and* tells you to run it). Report the clause
            # once — keyed on the sentence, not on the line: a note written as
            # one paragraph is one line, so a line key collapsed every later
            # finding in it and an approver saw only the first.
            # (Devin Review on #1258.)
            excerpt = _sentence_around(content, match.start(), match.end())
            key = (kind, excerpt)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                DirectiveFinding(
                    kind=kind,
                    reason=reason,
                    excerpt=excerpt,
                    line=content.count("\n", 0, match.start()) + 1,
                )
            )
    return sorted(findings, key=lambda f: (f.line, f.kind))


def scan_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Scan a knowledge-item row's delivered text, title included.

    ``_item_to_md`` and the ``km_approved.md`` rollup both write the title as a
    heading above the content, so a directive parked in the title reaches the
    rules file exactly like one in the body.
    """
    parts = [str(item.get("title") or ""), str(item.get("content") or "")]
    return [f.to_dict() for f in scan_for_agent_directives("\n".join(p for p in parts if p))]
