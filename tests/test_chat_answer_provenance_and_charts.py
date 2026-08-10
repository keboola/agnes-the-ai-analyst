"""The onboarding tour's two promises, and the mechanics that have to back them.

The newcomer walkthrough tells a first-time user, on the very first card, that
Agnes "always shows where the answer came from", and on the upload card that
she will "cite it in my answers". Neither was true: the chat agent's system
prompt is the `claude_code` preset plus the workspace ``CLAUDE.md``, and that
file said nothing about naming a source. The only provenance on screen was the
raw inline tool-call blocks — a log, not a citation.

The chart half is the same shape of gap seen from the other side. Asked for a
trend, the agent reached for matplotlib, could not install it (the sandbox has
no route to PyPI), improvised an SVG written to ``/tmp``, and told the user to
open a path that does not exist on their machine. Two separate holes: the
library was absent, and there is no file-delivery channel out of a chat session
at all (``app/chat/artifact_harvest.py`` is deliberately wired only into the
one-shot agent API, not into interactive chat).

The one channel that does work is inline ``<svg>`` in the reply. That is not a
feature anyone built — it is a property of two existing decisions (marked passes
raw HTML through; ``svg`` is not in the sanitizer's blocklist) that nothing
records. So the tests below pin it: the day someone adds ``svg`` to
``_DANGEROUS_TAGS`` for a good security reason, this file tells them they are
also removing the only way an answer can show a chart.

Layers, in the house style:

1. Contract — the prompt rules exist and name the mechanism, the sandbox images
   carry matplotlib, the renderer keeps the channel open.
2. Executable — the markdown parser and the URL-scheme allowlist run under
   ``node``, because "inline SVG renders and a data: URI does not" is a claim
   about code, not about a comment.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WORKSPACE_CLAUDE_MD = Path("app/initial_workspace_default/CLAUDE.md")
DOCKER_SANDBOX = Path("app/initial_workspace_default/docker-sandbox/Dockerfile")
E2B_TEMPLATE = Path("app/initial_workspace_default/e2b-template/Dockerfile")
TOUR_JS = Path("app/web/static/js/tour.js")
CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")
CHAT_HTML = Path("app/web/templates/chat.html")
MARKED_JS = Path("app/web/static/vendor/marked.min.js")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _prose(p: Path) -> str:
    """File text with runs of whitespace collapsed — prose assertions must not
    break because a sentence got re-wrapped at 79 columns."""
    return re.sub(r"\s+", " ", _read(p))


# ── 1. Contract ─────────────────────────────────────────────────────────────


def test_the_tour_still_promises_provenance():
    """The premise of every assertion below. If this copy is ever softened,
    the prompt rule it justifies should be revisited rather than silently kept."""
    tour = _read(TOUR_JS)
    assert "always show where the answer came from" in tour
    assert "cite it in my answers" in tour


def test_the_workspace_prompt_makes_the_promise_true():
    """The gap Petr reported: the tour promised provenance and nothing in the
    agent's instructions asked for it."""
    md = _prose(WORKSPACE_CLAUDE_MD)
    assert "Sources:" in md, "the workspace prompt must ask for a sources line"
    assert re.search(
        r"never report a (number|figure) whose origin you cannot name",
        md,
        re.IGNORECASE,
    ), "the rule needs the hard edge, not just the happy path"
    assert "metric" in md.lower(), "a canonical metric must be citable alongside the table"


def test_the_workspace_prompt_names_the_only_chart_channel():
    """Inline SVG is the delivery mechanism; a file path and a data: URI are the
    two plausible-looking things that silently fail. All three must be stated —
    the agent reached for exactly the failing ones."""
    md = _prose(WORKSPACE_CLAUDE_MD)
    assert "inline SVG" in md
    assert "svg.fonttype" in md, "without this matplotlib emits glyph outlines and the SVG is huge"
    assert "Never tell the user to open a file path." in md
    assert "data:" in md, "the failing alternative has to be named to be refused"
    assert "broken image" in md, "say what the user sees, not just that it is forbidden"


@pytest.mark.parametrize("dockerfile", [DOCKER_SANDBOX, E2B_TEMPLATE], ids=["docker", "e2b"])
def test_both_sandbox_images_carry_matplotlib(dockerfile: Path):
    """`pip install matplotlib` inside the sandbox cannot reach PyPI, so the
    prompt rule above is unfulfillable unless the image ships it. The two images
    are siblings and drift between them is a per-provider bug."""
    body = _read(dockerfile)
    assert "matplotlib>=" in body, f"{dockerfile} must bake matplotlib in"


def test_the_sanitizer_keeps_the_chart_channel_open():
    """`svg` in `_DANGEROUS_TAGS` would close the only route a chart has to the
    user — silently, since the answer would still 'render'."""
    chat = _read(CHAT_JS)
    block = re.search(r"_DANGEROUS_TAGS = new Set\(\[(.*?)\]\)", chat, re.DOTALL)
    assert block, "_DANGEROUS_TAGS moved — re-point this guard"
    tags = set(re.findall(r'"([a-z]+)"', block.group(1)))
    assert "svg" not in tags, "blocking <svg> removes the only chart channel — see this test's docstring"
    # The blocklist is still doing its job; this is not an argument for a laxer one.
    assert {"script", "iframe", "style", "object"} <= tags
    # …and the attribute pass is what makes an <svg> from an untrusted turn safe.
    assert 'name.startsWith("on")' in chat, "inline handlers must still be stripped"


def test_a_chart_svg_is_contained_by_the_bubble():
    """matplotlib stamps an absolute pt width on the root element; unconstrained
    it takes the page's horizontal scroll with it."""
    css = _read(CHAT_CSS)
    assert ".msg-body > svg" in css
    assert ".msg-body > p > svg" in css, "a one-line SVG comes through wrapped in <p>"


def test_the_thread_title_reserves_room_for_the_header_action():
    """Measured on the rendered page: without this cap a title long enough to
    fill the row keeps its full width and the action is painted over its last
    ~109px, hiding the ellipsis. The title is the header's only flexible item
    and the action's `margin-left:auto` consumes the free space before flex
    shrinking runs, so neither `min-width:0` nor dropping the auto margin fixes
    it — see the rationale in chat.css."""
    css = _read(CHAT_CSS)
    assert "max-width: calc(100% - 130px)" in css, (
        "the thread title's reserve for .cloud-chat-thread-action is gone — a long title "
        "will render its ellipsis underneath the button"
    )


def test_copy_transcript_is_wired():
    """The answer to 'can I report this session?' — sessions are owner-only, so
    the clipboard is the only way a conversation leaves Agnes."""
    assert 'id="chat-copy-transcript"' in _read(CHAT_HTML)
    chat = _read(CHAT_JS)
    assert "wireCopyTranscript()" in chat, "the button must be wired at boot, not just declared"
    assert "/api/chat/sessions/${encodeURIComponent(chatId)}/messages" in chat
    assert "tool: ${tc.tool}" in chat, "tool calls are the provenance — a transcript without them is undiagnosable"


# ── 2. Executable ───────────────────────────────────────────────────────────


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — the rendering-path tests need a runtime")
    return node


def test_marked_passes_a_matplotlib_shaped_svg_through_intact():
    """Half one of 'inline SVG renders': the markdown parser must not mangle a
    multi-line figure. Shaped like real matplotlib output — pt dimensions, a
    <defs><style> block, <g>/<path>/<text> — because a single-line <svg> would
    take a different path through marked (inline, wrapped in <p>)."""
    node = _node()
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="460pt" height="200pt" viewBox="0 0 460 200">\n'
        " <defs>\n"
        '  <style type="text/css">*{stroke-linecap:butt;}</style>\n'
        " </defs>\n"
        ' <g id="figure_1">\n'
        '  <path d="M 10 10 L 50 90" style="stroke:#1f77b4;"/>\n'
        '  <text x="20" y="30">Q1</text>\n'
        " </g>\n"
        "</svg>"
    )
    md = f"Headcount over time:\n\n{svg}\n\nSources: hr_headcount\n"
    script = (
        f"const m = require({json.dumps(str(MARKED_JS.resolve()))});\n"
        "const parse = m.parse || m.marked.parse;\n"
        f"process.stdout.write(parse({json.dumps(md)}));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    html = out.stdout
    assert '<text x="20" y="30">Q1</text>' in html, "the figure body was mangled"
    assert '<path d="M 10 10 L 50 90"' in html
    # Top-level block, not swallowed into a paragraph — which is why the CSS
    # containment rule above targets `.msg-body > svg`.
    assert "<p><svg" not in html


def test_the_scheme_allowlist_rejects_a_data_uri_image():
    """Half two: the obvious alternative (`![](data:image/png;base64,…)`) is
    stripped to a broken image, which is why the prompt rule forbids it. Runs
    the real regex out of chat.js, not a copy of it."""
    node = _node()
    chat = _read(CHAT_JS)
    decl = re.search(r"const _SAFE_URL_SCHEME_RE = .*?;", chat)
    assert decl, "_SAFE_URL_SCHEME_RE moved — re-point this guard"
    cases = [
        "data:image/png;base64,AAA",
        "https://example.com/chart.png",
        "/static/img/chart.png",
        "javascript:alert(1)",
    ]
    script = (
        decl.group(0)
        + "\n"
        + f"process.stdout.write(JSON.stringify({json.dumps(cases)}.map(v => _SAFE_URL_SCHEME_RE.test(v))));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    data_ok, https_ok, relative_ok, js_ok = json.loads(out.stdout)
    assert data_ok is False, "a data: image would render — the prompt rule could be relaxed"
    assert js_ok is False, "javascript: must stay refused"
    assert https_ok is True and relative_ok is True, "ordinary image sources must still work"
