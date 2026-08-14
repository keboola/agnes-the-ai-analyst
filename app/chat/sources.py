"""The `sources` block: what an answer claims, checked against what it ran.

The product promises, on the first card of the onboarding tour and again in the
chat greeting, that Agnes "always" says where an answer came from. A prompt rule
alone cannot make that true — a prompt is a request to a model, and the failure
mode is silent: an answer with no attribution looks exactly like an answer that
needed none.

So the workspace prompt asks for a machine-readable trailer instead of a
sentence, and this module turns it into something the reader can check:

    ```sources
    table: hr_headcount
    metric: headcount/active
    assumption: active employees only, contractors excluded
    ```

Two things follow from that being parseable rather than prose:

- **Absence becomes visible.** An answer that reports a figure and declares
  nothing renders as "no source declared" rather than as an ordinary answer.
- **A claim can be wrong, and we can say so.** The turn's tool calls are the
  record of what the agent actually ran. A `table:` no tool call touched is
  reported to the reader as unverified. This is the part that separates
  "the prompt asks for it" from "the answer is accountable for it".

What this deliberately is NOT: enforcement in the sense of refusing the answer.
That would need a second pass over the model on every turn, and it would trade
a visible gap for an invisible cost. The verdict is advisory and always
rendered — including when it is unflattering.

**The verdict is computed, never stored.** Both call sites already hold the
content and the tool calls together (`assistant_message` in
`app/chat/manager.py`, and `GET /sessions/{id}/messages` in `app/api/chat.py`),
so there is no column to add, no migration ladder to keep in step, and no
DuckDB/Postgres parity surface. A recomputed verdict also follows the code:
improving the matcher improves every historical message, which storing it at
write time would not.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: The fenced block the workspace prompt asks for. Tolerant on purpose — a
#: model writing ```` ```sources ```` with trailing spaces, or with the CRLF a
#: Windows-side paste can introduce, still parses. Non-greedy so an answer that
#: (wrongly) carries two blocks yields the first rather than swallowing the
#: prose between them.
#: Opening fence only. The BODY is located with `str.find`, never with a
#: non-greedy regex body: `` ```sources…(.*?)``` `` rescans to end-of-string
#: for every unterminated opening fence, so cost was O(occurrences x length)
#: over model output — which `verdict()` now walks on every assistant message
#: of every history read. The repo's rule is that regexes over untrusted text
#: stay linear; `find` is linear by construction and needs no reasoning about
#: backtracking. (Devin Review on this PR.)
_OPEN_RE = re.compile(r"```sources[ \t]*\r?\n", re.IGNORECASE)
_CLOSE = "```"

#: The prompt's SECOND wire-format trailer: ```next_actions, suggested
#: follow-up prompts the web client lifts into one-click buttons
#: (``extractNextActions`` in ``chat.js``). It lives here, next to the
#: sources fence, because the push sinks strip both in one breath — a sink
#: with no buttons must not end every answer in a fenced block of wire
#: format. Same tolerant shape, same linear-scan rules as `_OPEN_RE`.
_NEXT_ACTIONS_OPEN_RE = re.compile(r"```next_actions[ \t]*\r?\n", re.IGNORECASE)

#: One claim per line: `kind: ref`. Anything else in the block is ignored
#: rather than treated as an error — a stray blank line or a comment must not
#: cost the reader the whole block.
_CLAIM_RE = re.compile(r"^\s*(table|metric|assumption)\s*:\s*(.+?)\s*$", re.IGNORECASE)

#: Claim kinds that name something the agent should have touched, and can
#: therefore be checked. `assumption` is free text about the analyst's own
#: choices — there is nothing to check it against, and pretending otherwise
#: would render every honest assumption as "unverified".
VERIFIABLE_KINDS = frozenset({"table", "metric"})


@dataclass(frozen=True)
class SourceClaim:
    kind: str  # "table" | "metric" | "assumption"
    ref: str
    #: None for kinds that carry nothing to check (see VERIFIABLE_KINDS).
    verified: Optional[bool] = None


@dataclass(frozen=True)
class SourcesVerdict:
    #: A `sources` block was present at all.
    declared: bool
    claims: list[SourceClaim] = field(default_factory=list)

    @property
    def unverified(self) -> list[SourceClaim]:
        return [c for c in self.claims if c.verified is False]

    def to_dict(self) -> dict[str, Any]:
        return {
            "declared": self.declared,
            "claims": [{"kind": c.kind, "ref": c.ref, "verified": c.verified} for c in self.claims],
        }


def _first_block_span(content: str, open_re: re.Pattern[str] = _OPEN_RE):
    """``(start, body_start, body_end, end)`` of the first complete block.

    ``None`` when there is no opening fence, or when an opening fence is never
    closed — an unterminated block is not a block, and treating it as one
    would let a truncated answer swallow everything after it.
    """
    m = open_re.search(content)
    if not m:
        return None
    body_start = m.end()
    body_end = content.find(_CLOSE, body_start)
    if body_end == -1:
        return None
    return (m.start(), body_start, body_end, body_end + len(_CLOSE))


def extract_block(content: str) -> Optional[str]:
    """The raw body of the first `sources` block, or None."""
    if not content:
        return None
    span = _first_block_span(content)
    return content[span[1] : span[2]] if span else None


def strip_block(content: str) -> str:
    """The answer without its machine-readable ``sources`` fence.

    For the sinks that cannot render the block as anything better than a code
    block. The web client strips it too (``stripSourcesFence`` in
    ``chat.js``) and draws chips from the server's verdict instead; a push
    sink has no chips, so where the answer used to end in a readable
    ``Sources:`` line it would now end in a fenced block of machinery.

    Deliberately NOT applied before persistence, and not to the AG-UI/SSE
    surface. The verdict is *derived* rather than stored — `GET
    /sessions/{id}/messages` re-parses it out of the saved content — so
    stripping it on the way to the database would silently drop the chips on
    every reload. And a programmatic consumer of the agent API is exactly the
    caller the machine-readable form exists for. (Devin Review on this PR.)
    """
    if not content:
        return content
    return _strip_blocks(content, _OPEN_RE)


def strip_next_actions_block(content: str) -> str:
    """The answer without its ``next_actions`` trailer.

    The web client lifts this trailer into one-click follow-up buttons
    (``extractNextActions`` in ``chat.js``); a push sink has no buttons wired
    to it, so the raw fence would sit under every answer as a code block of
    wire format. Same persistence rule as ``strip_block``: never applied
    before the database — the web client re-extracts the buttons from the
    saved content on every history reload.
    """
    if not content:
        return content
    return _strip_blocks(content, _NEXT_ACTIONS_OPEN_RE)


def _strip_blocks(content: str, open_re: re.Pattern[str]) -> str:
    out = content
    while True:
        span = _first_block_span(out, open_re)
        if span is None:
            return out.rstrip()
        start, _body_start, _body_end, end = span
        out = out[:start] + out[end:]


def parse_claims(block_body: str) -> list[SourceClaim]:
    """Claims in the order written, deduplicated on (kind, ref).

    Order is the agent's own — the first table named is usually the one the
    figure came from — and duplicates are dropped because a model listing the
    same table twice should not show the reader the same chip twice.
    """
    seen: set[tuple[str, str]] = set()
    out: list[SourceClaim] = []
    for line in block_body.splitlines():
        m = _CLAIM_RE.match(line)
        if not m:
            continue
        kind = m.group(1).lower()
        ref = m.group(2).strip()
        # Strip the backticks a model reaches for out of markdown habit; the
        # ref is compared against tool-call text, where it appears bare.
        ref = ref.strip("`").strip()
        if not ref:
            continue
        key = (kind, ref)
        if key in seen:
            continue
        seen.add(key)
        out.append(SourceClaim(kind=kind, ref=ref))
    return out


def _tool_call_haystack(tool_calls: Optional[Iterable[Any]]) -> str:
    """Everything the turn's tool calls said, as one lowercase string.

    Deliberately crude. The alternative — parsing SQL out of each call and
    resolving table identifiers — would be precise about `agnes query` and
    blind to every other route to the same data (`agnes describe`, a snapshot,
    an MCP tool, a `--remote` push-down). Substring containment over the whole
    serialized call is wrong in the harmless direction: it can accept a table
    the agent only mentioned, and it does not invent a match that is not there.
    A false "verified" leaves the reader where they are today; a false
    "unverified" would teach them to ignore the badge.
    """
    if not tool_calls:
        return ""
    parts: list[str] = []
    for call in tool_calls:
        if isinstance(call, str):
            parts.append(call)
            continue
        try:
            parts.append(json.dumps(call, default=str))
        except (TypeError, ValueError):
            parts.append(str(call))
    return "\n".join(parts).lower()


def verify(claims: list[SourceClaim], tool_calls: Optional[Iterable[Any]]) -> list[SourceClaim]:
    haystack = _tool_call_haystack(tool_calls)
    out: list[SourceClaim] = []
    for c in claims:
        if c.kind not in VERIFIABLE_KINDS:
            out.append(c)
            continue
        ref = c.ref.lower()
        # A metric id is written `family/name` in the catalog and may be cited
        # either way round; accept the bare name too so a correct citation is
        # not reported as unverified over punctuation.
        needles = [ref]
        if c.kind == "metric" and "/" in ref:
            needles.append(ref.rsplit("/", 1)[-1])
        out.append(SourceClaim(kind=c.kind, ref=c.ref, verified=any(n in haystack for n in needles)))
    return out


def verdict(content: str, tool_calls: Optional[Iterable[Any]] = None) -> SourcesVerdict:
    """The whole pass: parse the block, check what can be checked.

    An answer with no block yields `declared=False` and no claims — which the
    UI renders only when the turn looks like it reported something (see
    `chat.js`), because "no source declared" under "hello" would be noise.
    """
    body = extract_block(content)
    if body is None:
        return SourcesVerdict(declared=False)
    return SourcesVerdict(declared=True, claims=verify(parse_claims(body), tool_calls))
