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
SERVER_DEFAULT_TEMPLATE = Path("config/claude_md_template.txt")
DOCKER_SANDBOX = Path("app/initial_workspace_default/docker-sandbox/Dockerfile")
E2B_TEMPLATE = Path("app/initial_workspace_default/e2b-template/Dockerfile")
CLOUD_CHAT_DOC = Path("docs/cloud-chat.md")
TOUR_JS = Path("app/web/static/js/tour.js")
CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")
CHAT_HTML = Path("app/web/templates/chat.html")
MARKED_JS = Path("app/web/static/vendor/marked.min.js")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _collapse_ws(text: str) -> str:
    """Text with runs of whitespace collapsed — prose assertions must not
    break because a sentence got re-wrapped at 79 columns."""
    return re.sub(r"\s+", " ", text)


def _prose(p: Path) -> str:
    return _collapse_ws(_read(p))


def _rendered_server_default_claude_md() -> str:
    """Render config/claude_md_template.txt the same way production does on
    the common (no admin override, no Initial Workspace Template) path:
    WorkdirManager.run_init's ``_render_workspace_prompt`` callable
    (app/main.py) calls ``render_claude_md``, which falls back to
    ``compute_default_claude_md`` — the exact function under test here. This
    overwrites the bundled ``WORKSPACE_CLAUDE_MD`` in the workspace, and is
    also what a laptop's ``agnes init`` writes via ``GET /api/welcome``. So
    the bundled file alone is not sufficient evidence the agent ever sees a
    rule — this is what actually reaches it on the default path.
    """
    from unittest.mock import patch

    import duckdb

    from src.claude_md import compute_default_claude_md
    from src.db import _ensure_schema

    conn = duckdb.connect(":memory:")
    try:
        _ensure_schema(conn)
        # RBAC reads inside compute_default_claude_md go through the repo
        # factory (e.g. get_accessible_tables -> data_packages_repo()), which
        # resolves its connection via src.repositories.get_system_db — not the
        # conn passed to compute_default_claude_md directly. Redirect that
        # name to this connection, same as tests/test_claude_md_renderer.py's
        # fixture, or the factory opens (and may not find) the real system DB.
        with patch("src.repositories.get_system_db", lambda: conn):
            user = {
                "id": "u1",
                "email": "alice@example.com",
                "name": "Alice",
                "is_admin": False,
                "groups": ["Everyone"],
            }
            return compute_default_claude_md(conn, user=user, server_url="https://example.com")
    finally:
        conn.close()


def _section(text: str, heading: str) -> str:
    """Body of a ``## <heading>`` markdown section, up to the next ``## ``
    heading or end of file."""
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=\n## |\Z)", text, re.DOTALL | re.MULTILINE)
    assert m, f"section {heading!r} not found"
    return m.group(1).strip()


# ── 1. Contract ─────────────────────────────────────────────────────────────


def test_the_tour_still_promises_provenance():
    """The premise of every assertion below. If this copy is ever softened,
    the prompt rule it justifies should be revisited rather than silently kept."""
    tour = _read(TOUR_JS)
    assert "always show where the answer came from" in tour
    assert "cite it in my answers" in tour


def _assert_promises_provenance(md: str) -> None:
    assert "Sources:" in md, "the workspace prompt must ask for a sources line"
    assert re.search(
        r"never report a (number|figure) whose origin you cannot name",
        md,
        re.IGNORECASE,
    ), "the rule needs the hard edge, not just the happy path"
    assert "metric" in md.lower(), "a canonical metric must be citable alongside the table"
    # The reply is rendered as markdown, so an assumption written on its own
    # line without a blank line before it silently joins the Sources line —
    # seen in the rendered bubble, not in any assertion. The rule keeps it to
    # one line and says why.
    assert "blank line" in md, "the prompt must say why the Sources line stands alone"


def test_the_workspace_prompt_makes_the_promise_true():
    """The gap Petr reported: the tour promised provenance and nothing in the
    agent's instructions asked for it. This pins the bundled fallback file —
    see the sibling test below for the content that actually reaches the
    sandbox on the common path."""
    _assert_promises_provenance(_prose(WORKSPACE_CLAUDE_MD))


def test_the_server_rendered_default_makes_the_promise_true():
    """The bundled ``CLAUDE.md`` above is only the initial file dropped into a
    fresh workspace — in DEFAULT mode (no admin override, no Initial Workspace
    Template) ``WorkdirManager.run_init`` immediately overwrites it with the
    server-rendered default (``config/claude_md_template.txt``), and that is
    also what a laptop's ``agnes init`` writes. A prompt rule that lives only
    in the bundled file never reaches an agent on that — the common — path."""
    _assert_promises_provenance(_collapse_ws(_rendered_server_default_claude_md()))


def _assert_names_chart_channel(md: str) -> None:
    assert "inline SVG" in md
    assert "svg.fonttype" in md, "without this matplotlib emits glyph outlines and the SVG is huge"
    assert "Never tell the user to open a file path." in md
    assert "data:" in md, "the failing alternative has to be named to be refused"
    assert "broken image" in md, "say what the user sees, not just that it is forbidden"


def test_the_workspace_prompt_names_the_only_chart_channel():
    """Inline SVG is the delivery mechanism; a file path and a data: URI are the
    two plausible-looking things that silently fail. All three must be stated —
    the agent reached for exactly the failing ones. Pins the bundled fallback
    file — see the sibling test below for the server-rendered default."""
    _assert_names_chart_channel(_prose(WORKSPACE_CLAUDE_MD))


def test_the_server_rendered_default_names_the_only_chart_channel():
    """Same gap as the provenance rule, for the chart rule: the bundled file
    is not what an agent sees once ``render_workspace_prompt`` succeeds
    (the common path in production) — the server-rendered default is."""
    _assert_names_chart_channel(_collapse_ws(_rendered_server_default_claude_md()))


@pytest.mark.parametrize("heading", ["Say where every number came from", "Charts"])
def test_the_bundled_and_server_default_sections_do_not_drift(heading: str):
    """The bundled ``CLAUDE.md`` and ``config/claude_md_template.txt`` are two
    independent files with no shared source for this prose — copy-pasted
    rather than factored out, because one is a static file and the other a
    Jinja2 template with a very different overall shape (see CONTRIBUTING.md's
    sync-map row for this pair). That makes them free to drift silently: an
    edit to one rule's wording in one file, forgotten in the other, would
    leave the sandbox and the laptop CLI disagreeing about the rules with no
    test failure anywhere — exactly how the section went missing from this
    file the first time. Pin them equal verbatim so a future edit is forced to
    touch both or explain why not."""
    bundled = _section(_read(WORKSPACE_CLAUDE_MD), heading)
    server_default = _section(_read(SERVER_DEFAULT_TEMPLATE), heading)
    assert bundled == server_default, (
        f"the {heading!r} section text differs between {WORKSPACE_CLAUDE_MD} and "
        f"{SERVER_DEFAULT_TEMPLATE} — keep them byte-identical or this guard will always fail"
    )


@pytest.mark.parametrize("dockerfile", [DOCKER_SANDBOX, E2B_TEMPLATE], ids=["docker", "e2b"])
def test_both_sandbox_images_carry_matplotlib(dockerfile: Path):
    """`pip install matplotlib` inside the sandbox cannot reach PyPI, so the
    prompt rule above is unfulfillable unless the image ships it. The two images
    are siblings and drift between them is a per-provider bug."""
    body = _read(dockerfile)
    assert "matplotlib>=" in body, f"{dockerfile} must bake matplotlib in"


def test_the_contract_label_matches_what_the_docs_tell_operators_to_expect():
    """The operator note in docs/cloud-chat.md tells the reader to rebuild the
    sandbox image after upgrading Agnes and confirm the new contract with
    `docker inspect … agnes.chat-sandbox.contract`. That check is worthless if
    the label never moves: this PR added `matplotlib` to the image's pip
    install block (a real contract change — a pre-upgrade image cannot draw a
    chart) without bumping `LABEL agnes.chat-sandbox.contract`, so a stale and
    a current image reported the identical value — exactly the case the
    paragraph exists to catch. Pins the two sides of that check to each other
    so they can't drift apart silently again."""
    label = re.search(r'LABEL agnes\.chat-sandbox\.contract="(\d+)"', _read(DOCKER_SANDBOX))
    assert label, "the contract LABEL moved — re-point this guard"
    doc = re.search(r"confirm the contract label reads `(\d+)`", _read(CLOUD_CHAT_DOC))
    assert doc, "docs/cloud-chat.md no longer states the expected contract label value"
    assert label.group(1) == doc.group(1), (
        f"Dockerfile LABEL is {label.group(1)!r} but the docs tell the operator to expect "
        f"{doc.group(1)!r} — bump whichever one is stale"
    )


def _dangerous_tags() -> set[str]:
    block = re.search(r"_DANGEROUS_TAGS = new Set\(\[(.*?)\]\)", _read(CHAT_JS), re.DOTALL)
    assert block, "_DANGEROUS_TAGS moved — re-point this guard"
    return set(re.findall(r'"([a-z]+)"', block.group(1)))


def test_the_sanitizer_keeps_the_chart_channel_open():
    """`svg` in `_DANGEROUS_TAGS` would close the only route a chart has to the
    user — silently, since the answer would still 'render'."""
    tags = _dangerous_tags()
    assert "svg" not in tags, "blocking <svg> removes the only chart channel — see this test's docstring"
    # The blocklist is still doing its job; this is not an argument for a laxer one.
    assert {"script", "iframe", "style", "object"} <= tags
    # …and the attribute pass is what makes an <svg> from an untrusted turn safe.
    assert 'name.startsWith("on")' in _read(CHAT_JS), "inline handlers must still be stripped"


def test_smil_and_foreignobject_stay_blocked():
    """Admitting `<svg>` while stripping `on*` handlers and unsafe URL schemes
    is not sufficient on its own, because SMIL defers both checks to runtime:
    they inspect the attributes an element HAS, and SMIL sets one it does not.

    Measured against this sanitizer before these five names were added, all
    three of these survived it completely intact — element, `attributeName`
    and payload:

        <a href="#x"><animate attributeName="href" values="javascript:…"></a>
        <a><animate attributeName="xlink:href" to="javascript:…"></a>
        <set attributeName="onload" to="…">

    The scheme allowlist never sees the first two (`values`/`to` are not
    URL-bearing attribute *names*) and the `on*` strip never sees the third
    (there the handler name is an attribute *value*). `foreignObject` is
    HTML-in-SVG and the usual container in these chains.

    The threat model is NOT the chart author. A co-presence peer's message
    renders through the same `renderMarkdownSafe`, and no prompt rule binds
    them — which is also why "matplotlib never emits SMIL" is not a defence.
    It is, however, why this costs nothing: measured on real matplotlib
    output, a figure contains zero SMIL elements and no foreignObject.
    """
    tags = _dangerous_tags()
    missing = {"animate", "animatetransform", "animatemotion", "set", "foreignobject"} - tags
    assert not missing, (
        f"{sorted(missing)} dropped from _DANGEROUS_TAGS — a chat message can set an "
        "href or an event handler at runtime, which the attribute pass cannot see"
    )


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
    assert "tool: ${call.label}" in chat, "tool calls are the provenance — a transcript without them is undiagnosable"


def test_the_clipboard_write_is_issued_synchronously_from_the_click():
    """`navigator.clipboard.writeText`/`.write` need an unbroken chain of
    synchronous calls back to the user gesture on WebKit; an `await` before
    the call — such as `await fetchTranscriptMarkdown(...)`, a real network
    round-trip — drops that transient activation, and the button reports
    "Couldn't copy to clipboard" even though the fetch and the write would
    both have succeeded. A promise-based `ClipboardItem` is the fix: the
    *value* can resolve after the fetch, but `navigator.clipboard.write(...)`
    itself must be *called* with nothing awaited first."""
    chat = _read(CHAT_JS)
    fn = re.search(r"function wireCopyTranscript\(\) \{.*?\n\}\n", chat, re.DOTALL)
    assert fn, "wireCopyTranscript moved — re-point this guard"
    body = fn.group(0)
    assert "ClipboardItem" in body, "the standards-track fix (a promise-based ClipboardItem) is gone"
    before_write, _, after = body.partition("await navigator.clipboard.write(")
    assert after, "navigator.clipboard.write(...) call not found in wireCopyTranscript"
    assert "const md = fetchTranscriptMarkdown(chatId);" in before_write, (
        "the transcript fetch must be a promise handed to ClipboardItem, not awaited first"
    )
    assert "await fetchTranscriptMarkdown" not in before_write, (
        "an `await` before the clipboard write drops the click's user-activation on WebKit"
    )


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


def test_a_tool_call_row_with_no_tool_name_is_skipped_not_rendered_as_undefined():
    """The only `tool_calls` a persisted message ever actually carries on the
    live agent path are manager.py's cancelled/interrupted markers
    (`{"cancelled": true}`, `{"interrupted": true, "reason": …}`) — real tool
    calls are never re-attached to the stored assistant message. Before
    `formatToolCall()` existed, both the transcript export and the live
    renderer built `tool: ${tc.tool}` unconditionally: `tc.tool` is
    `undefined` for a marker, and `JSON.stringify(undefined, …)` is also
    `undefined`, so the row rendered as the literal text "tool: undefined"
    with an empty ```json``` fence. Runs the real function out of chat.js."""
    node = _node()
    chat = _read(CHAT_JS)
    decl = re.search(r"function formatToolCall\(tc\) \{.*?\n\}\n", chat, re.DOTALL)
    assert decl, "formatToolCall moved — re-point this guard"
    cases = [
        {"tool": "agnes_query", "args": {"sql": "SELECT 1"}},
        {"cancelled": True},
        {"interrupted": True, "reason": "kicked"},
        {},
    ]
    script = decl.group(0) + "\n" + f"process.stdout.write(JSON.stringify({json.dumps(cases)}.map(formatToolCall)));\n"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    real_call, cancelled, interrupted, empty = json.loads(out.stdout)
    assert real_call == {"label": "agnes_query", "argsJson": json.dumps({"sql": "SELECT 1"}, indent=2)}
    assert cancelled is None, "a cancelled marker has no `tool` name and must be skipped, not stringified"
    assert interrupted is None, "an interrupted marker has no `tool` name and must be skipped, not stringified"
    assert empty is None
