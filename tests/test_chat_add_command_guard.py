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

Most of these assert the source contract the way `test_tour_journey_flags.py`
and `test_design_system_contract.py` do. One — the trailing-punctuation
regression below — actually runs `maybeHandleAddCommand` under `node`,
because "a trailing full stop still resolves the item" is a claim about
what the search does, not about what the source says.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ONBOARDING_JS = Path("app/web/static/js/chat_onboarding.js")


def _src() -> str:
    return ONBOARDING_JS.read_text(encoding="utf-8")


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — the trailing-punctuation regression needs a runtime")
    return node


def _add_command_runtime_source() -> str:
    """The real `maybeHandleAddCommand` + its dependencies (`matchScore`, the
    two tuning constants) — for actually running the search rather than
    grepping the source. The trailing-punctuation bug only shows up once the
    query is built and matched for real; a source-level regex can't see it."""
    src = _src()
    const_start = src.index("const ADD_COMMAND_MAX_WORDS")
    fn_start = src.index("async function maybeHandleAddCommand")
    fn_end = src.index("\nfunction matchScore", fn_start)
    match_start = src.index("function matchScore")
    match_end = src.index("\n\n", match_start)
    return "\n".join(
        [
            src[const_start:fn_start],
            src[fn_start:fn_end],
            src[match_start:match_end],
        ]
    )


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


def test_add_shortcut_resolves_the_item_despite_a_trailing_full_stop():
    """ "add sales-package." must still resolve — and "!" / "?" too.

    The word-count guard tolerates a trailing sentence terminator
    (`subject.replace(/[.!?]+$/, "")`) so a short imperative that happens to
    end in punctuation isn't mistaken for prose. But the search term used to
    be built from the raw regex capture instead of that same normalized
    subject, so the dot rode along into `q`: the exact-name check failed
    (`"sales-package" !== "sales-package."`), the substring check failed
    (`hay` never contains the dot), and the per-token fallback failed too —
    the only token still carries the dot, so it never matches either. Score
    0, `scored` ends up empty, and the handler falls through as if nothing
    had matched, even though the identical message without the full stop
    resolves cleanly to an exact-name hit.
    """
    node = _node()
    runtime = _add_command_runtime_source()
    harness = f"""
"use strict";
{runtime}

const ITEM = {{
  id: "pkg-1",
  name: "sales-package",
  resource_type: "data_package",
  in_stack: false,
  description: "Sales figures",
}};
async function browseStack() {{ return [ITEM]; }}
let subscribeCalls = [];
async function subscribe(resourceType, resourceId) {{ subscribeCalls.push([resourceType, resourceId]); }}
async function patchJourney(_fields) {{}}
const rendered = [];
const hooks = {{ renderAssistant(msg) {{ rendered.push(msg); }} }};
function escapeHtml(s) {{ return String(s); }}

(async () => {{
  const results = {{}};
  for (const suffix of [".", "!", "?"]) {{
    subscribeCalls = [];
    const handled = await maybeHandleAddCommand(`add sales-package${{suffix}}`);
    results[suffix] = {{ handled, subscribeCalls }};
  }}
  process.stdout.write(JSON.stringify(results));
}})().catch((err) => {{
  console.error("harness error:", err);
  process.exitCode = 1;
}});
"""
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    results = json.loads(out.stdout)
    for suffix, result in results.items():
        assert result["handled"] is True, f"'add sales-package{suffix}' was not resolved as a command"
        assert result["subscribeCalls"] == [["data_package", "pkg-1"]], (
            f"'add sales-package{suffix}' must subscribe to the exact-name match, got {result['subscribeCalls']}"
        )
