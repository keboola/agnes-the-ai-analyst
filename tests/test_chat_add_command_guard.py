"""Static-source guards for the in-chat "add <thing>" shortcut.

`chat_onboarding.js::maybeHandleAddCommand` runs in the browser *before* the
message is sent to the model, and returning `true` swallows the turn whole.
That makes two failure modes possible, both observed against a live instance:

1. **The message never reaches the model.** The matcher was
   `/^(?:add|enable|install)\\s+(.+)/i` with a greedy tail, so
   *"Add ai-kit/sl-toolkit back to my stack. Then tell me whether you can
   publish a plugin…"* was taken as a request to add an item literally named
   *"ai-kit/sl-toolkit back to my stack. Then tell me whether you can publish
   a plugin…"*. Everything after the first clause was silently discarded and
   the user got a "couldn't find anything called <their whole sentence>".

2. **An unrequested write.** `matchScore` awards 12 points per word found
   anywhere in an item's name, id **or description**, and any score `> 0` was
   enough to call `subscribe()` with no confirmation — so one incidental word
   shared with a long description could add something to the user's Stack that
   they never named.

There is no JS runner in CI, so these assert the source contract the way
`test_tour_journey_flags.py` and `test_design_system_contract.py` do.
"""

import re
from pathlib import Path

ONBOARDING_JS = Path("app/web/static/js/chat_onboarding.js")


def _src() -> str:
    return ONBOARDING_JS.read_text(encoding="utf-8")


def _handler() -> str:
    """Body of `maybeHandleAddCommand`, comments stripped.

    The guards are about what the function *does*; the rationale above them
    names the same identifiers, so comments would satisfy these greps for free.
    """
    src = _src()
    start = src.index("async function maybeHandleAddCommand")
    end = src.index("\nfunction matchScore", start)
    body = src[start:end]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    body = re.sub(r"^\s*//.*$", "", body, flags=re.MULTILINE)
    return body


def test_add_shortcut_is_bounded_to_a_short_single_clause():
    """A multi-sentence or long message is prose for the model, not a command."""
    body = _handler()
    assert "ADD_COMMAND_MAX_WORDS" in body, (
        "the shortcut must cap how long a message it will claim — without it, "
        "everything after the first clause is silently discarded"
    )
    # A sentence break in the captured tail means the user wrote prose.
    assert re.search(r"return false", body), "the handler must be able to decline"
    assert re.search(r"\[.!?;:\]|\\n", body), (
        "the handler must detect a sentence break / newline in the captured subject and hand the turn back to the model"
    )


def test_add_shortcut_requires_a_confident_match_before_mutating():
    """A weak bag-of-words hit must not subscribe the user to anything."""
    body = _handler()
    assert "ADD_COMMAND_MIN_SCORE" in body, (
        "subscribing on any score > 0 lets one incidental word shared with an "
        "item's description add it to the user's Stack"
    )
    confident_at = body.index("confident")
    subscribe_at = body.index("await subscribe(")
    assert confident_at < subscribe_at, "the confidence check must run before the write, not after"


def test_add_shortcut_does_not_dead_end_on_no_match():
    """No match => fall through to the model, which can ask what was meant.

    The old branch rendered `I couldn't find anything called "<whole message>"`
    and returned true, which both looked broken and ate the question.
    """
    body = _handler()
    assert "couldn't find anything called" not in body, (
        "quoting the user's own sentence back as a missing item name is the symptom this guard exists to prevent"
    )


def test_min_score_is_at_least_the_substring_tier():
    """`matchScore` tiers: 100 exact name, 60 substring, else 12/word.

    The threshold has to sit at the substring tier or above, otherwise the
    bag-of-words path is still reachable for a multi-word sentence.
    """
    src = _src()
    m = re.search(r"ADD_COMMAND_MIN_SCORE\s*=\s*(\d+)", src)
    assert m, "ADD_COMMAND_MIN_SCORE must be a literal so this bound is checkable"
    assert int(m.group(1)) >= 60


def test_single_candidate_still_requires_the_confidence_bar():
    """A lone addable item on the instance must not bypass MIN_SCORE.

    Regression: `confident` used to accept
    `scored.length === 1 && scored[0].s > 0` — a single bag-of-words hit
    (12 points, one incidental word shared with an item's *description*)
    was enough to `subscribe()` whenever the instance exposed exactly one
    addable item. That is exactly the fresh-instance / first-run case this
    shortcut targets, so "never mutate on a weak match" held only when more
    than one candidate existed. The confidence bar must apply uniformly,
    regardless of how many candidates matched.
    """
    body = _handler()
    ci = body.index("const confident")
    confident_expr = body[ci : body.index(";", ci)]
    assert "> 0" not in confident_expr, (
        "confident must not fall back to a bare `s > 0` check when there is "
        "exactly one candidate — that lets a single 12-point bag-of-words "
        "hit subscribe the user whenever the instance has exactly one "
        "addable item; the ADD_COMMAND_MIN_SCORE bar must apply uniformly"
    )
