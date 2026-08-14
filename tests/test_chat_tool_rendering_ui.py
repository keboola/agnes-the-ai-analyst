"""Wave-1 tool-rendering guards: the reader sees labels, tables and buttons —
never raw JSON or raw markdown source — and JSON stays one click away.

Follows the pattern of tests/test_chat_sources_ui.py: content assertions pin
the call shapes that are easy to undo by accident; node-executed tests run the
shipped functions, not copies.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")
WORKSPACE_CLAUDE_MD = Path("app/initial_workspace_default/CLAUDE.md")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


# ── next_actions: the trailer is chrome, not content ─────────────────────────


def test_next_actions_helper_exists_and_both_paths_use_it():
    js = _read(CHAT_JS)
    assert "function renderAnswerMarkdown" in js
    assert "stripNextActionsFence(stripSourcesFence(" in js, (
        "renderAnswerMarkdown must strip both trailers, sources first"
    )
    assert js.count("renderAnswerMarkdown(") >= 3, "renderMessage, finalizeAssistantMessage and the helper definition"
    assert "renderMarkdownSafe(stripSourcesFence(" not in js, "render paths must go through renderAnswerMarkdown now"


def test_clipboard_strips_next_actions_but_keeps_sources():
    """Suggestions are chrome — a copied transcript without them loses nothing.
    Provenance is not chrome; the sources fence stays (see
    tests/test_chat_sources_ui.py for the full rationale)."""
    js = _read(CHAT_JS)
    assert "attachMessageActions(currentAssistantArticle, stripNextActionsFence(content))" in js
    assert 'attachMessageActions(article, stripNextActionsFence(m.content || ""))' in js
    assert "attachMessageActions(currentAssistantArticle, stripSourcesFence" not in js
    assert "attachMessageActions(article, stripSourcesFence" not in js


def test_extract_next_actions_executable():
    js = _read(CHAT_JS)
    fn = js[js.index("const _NEXT_ACTIONS_OPEN_RE") : js.index("function renderAnswerMarkdown")]
    cases = {
        "with_block": "Done.\n\n```next_actions\n- Break it down by country\n- Chart the trend\n```\n",
        "no_block": "Done.",
        "unterminated_kept": "Done.\n\n```next_actions\n- Break it down",
        "two_blocks": "a\n\n```next_actions\n- x\n```\n\nb\n\n```next_actions\n- y\n```\n",
        "code_block_kept": "See:\n\n```sql\nSELECT 1\n```\n\n```next_actions\n- Next\n```\n",
        "caps_capped": "t\n\n```next_actions\n- a\n- b\n- c\n- d\n- e\n```\n",
    }
    script = (
        fn
        + f"\nprocess.stdout.write(JSON.stringify(Object.fromEntries(Object.entries({json.dumps(cases)}).map(([k, v]) => [k, extractNextActions(v)]))));\n"
    )
    res = json.loads(_node_run(script))
    assert res["with_block"]["text"] == "Done."
    assert res["with_block"]["actions"] == ["Break it down by country", "Chart the trend"]
    assert res["no_block"] == {"text": "Done.", "actions": []}
    assert res["unterminated_kept"]["text"] == "Done.\n\n```next_actions\n- Break it down", (
        "an unterminated opener is not a block — stripping it would eat the answer"
    )
    assert res["unterminated_kept"]["actions"] == []
    assert "```next_actions" not in res["two_blocks"]["text"], "the pattern must be global"
    assert res["two_blocks"]["actions"] == ["x", "y"]
    assert "```sql" in res["code_block_kept"]["text"], "an ordinary code block must survive"
    assert len(res["caps_capped"]["actions"]) == 3, "at most 3 buttons"


def test_next_action_buttons_render_and_are_singular():
    js = _read(CHAT_JS)
    assert "function renderNextActions" in js
    assert "function _clearNextActions" in js
    body = js[js.index("function renderNextActions") : js.index("function _clearNextActions")]
    assert "textContent" in body and "innerHTML" not in body, "suggestion text is model output — textContent only"
    # finalize renders them; a new user message clears them.
    assert re.search(r"renderNextActions\(", js[js.index("function finalizeAssistantMessage") :]), (
        "finalizeAssistantMessage must render the chips"
    )
    assert js.count("_clearNextActions()") >= 2, "cleared on new turn AND before re-render"


def test_next_action_click_reuses_the_suggestion_flow():
    js = _read(CHAT_JS)
    body = js[js.index("function renderNextActions") : js.index("function _clearNextActions")]
    assert 'dispatchEvent(new SubmitEvent("submit"' in body, (
        "clicking a chip submits like the existing suggested-prompt buttons"
    )


def test_history_reload_restores_chips_only_when_the_answer_is_the_tail():
    """Live behavior: a new user message clears the chips ("they age out the
    moment the conversation moves on"). A reload must agree — restoring them
    under an answer that already has a user message after it (error-aborted
    turn, mid-turn full_refresh) would resurrect suggestions the conversation
    moved past."""
    js = _read(CHAT_JS)
    body = js[js.index("async function loadAndRenderHistory") : js.index("async function openSession")]
    assert "renderNextActions(" in body, "a reload must restore the chips"
    assert 'lastTurnMsg.role === "assistant"' in body, (
        "chips are restored only while the newest turn message is the assistant's answer"
    )


def test_next_action_chip_styles_use_ds_tokens():
    css = _read(CHAT_CSS)
    assert ".cloud-chat-next-actions" in css
    assert ".cloud-chat-next-action" in css


def test_the_prompt_mandates_the_next_actions_trailer():
    md = _read(WORKSPACE_CLAUDE_MD)
    flat = re.sub(r"\s+", " ", md)
    assert "```next_actions" in md
    assert "one-click buttons" in flat
    assert "Skip the block" in flat, "the prompt must say when NOT to emit it"


def test_the_template_carries_the_trailer_for_the_sandbox_only():
    """The LIVE sandbox prompt renders from config/claude_md_template.txt
    (app/main.py hands render_claude_md(is_sandbox=True) to WorkdirManager);
    the bundled CLAUDE.md is the fallback. Both must carry the contract, or
    the buttons never appear on a normally-deployed instance. The template's
    copy is sandbox-gated: a terminal session has nothing that lifts the
    fence, so mandating it there would put raw wire format on every answer.
    (Found by /agnes-review on the first cut of this change.)"""
    tpl = Path("config/claude_md_template.txt").read_text(encoding="utf-8")
    assert "```next_actions" in tpl, "the rendered sandbox prompt must mandate the trailer"
    section_start = tpl.index("## Offer the next step")
    guard_open = tpl.rindex("{% if is_sandbox %}", 0, section_start)
    guard_close = tpl.index("{% endif %}", section_start)
    body = tpl[guard_open:guard_close]
    assert "```next_actions" in body, "the section must sit inside an is_sandbox guard"
    assert "{% if" not in body[len("{% if is_sandbox %}") :], "no nested guard — the whole section is sandbox-only"


# ── push sinks: the trailer must never reach Slack as raw wire format ────────


def test_strip_next_actions_block_removes_the_fence_and_keeps_the_prose():
    from app.chat.sources import strip_next_actions_block

    content = "Done.\n\n```next_actions\n- Break it down by country\n```"
    assert strip_next_actions_block(content) == "Done."
    assert strip_next_actions_block("just prose") == "just prose"
    assert strip_next_actions_block("") == ""
    # An unterminated opener is not a block — same rule as the sources fence.
    half = "Done.\n\n```next_actions\n- half"
    assert strip_next_actions_block(half) == half
    # An ordinary code block survives.
    kept = "See:\n\n```sql\nSELECT 1\n```"
    assert strip_next_actions_block(kept) == kept


def test_the_slack_sink_strips_the_next_actions_trailer_on_both_post_paths():
    """Slack sessions run in the SAME sandbox as web chat
    (services/slack_bot/events.py creates them through the same ChatManager,
    whose workdir prompt mandates the trailer on every answer). The web client
    lifts the fence into buttons; Slack has no buttons wired to it, so without
    this strip every Slack reply would end in a fenced block of wire format —
    the exact failure mode the sources fence already solved there."""
    src = Path("services/slack_bot/sink.py").read_text(encoding="utf-8")
    assert src.count("strip_next_actions_block(strip_block(") == 2, (
        "both the streaming reply and the ephemeral responder must strip both trailers"
    )


# ── tool labels: a reader-facing verb, never a raw tool id ───────────────────


def test_tool_label_executable():
    js = _read(CHAT_JS)
    fn = js[js.index("const _TOOL_LABELS") : js.index("function renderApprovalRequest")]
    cases = [
        ["Bash", {"command": 'agnes query "SELECT 1"'}],
        ["Bash", {"command": "agnes catalog --json"}],
        ["Bash", {"command": "ls -la"}],
        ["Read", {"file_path": "/tmp/x"}],
        ["mcp__agnes__crm_search_accounts", {"q": "acme"}],
        ["totally_unknown_tool", {}],
        [None, None],
    ]
    script = fn + f"\nprocess.stdout.write(JSON.stringify({json.dumps(cases)}.map(([t, a]) => _toolLabel(t, a))));\n"
    res = json.loads(_node_run(script))
    assert res[0] == "Querying data"
    assert res[1] == "Reading the data catalog"
    assert res[2] == "Running a command"
    assert res[3] == "Reading a file"
    assert res[4] == "Crm search accounts", "mcp prefix stripped, words humanized"
    assert res[5] == "Totally unknown tool"
    assert res[6] == "tool"
    assert not any("mcp__" in r for r in res)


def test_live_and_history_headers_use_the_label():
    js = _read(CHAT_JS)
    start = js[js.index("function renderToolCallStart") : js.index("function renderToolCallEnd")]
    assert "_toolLabel(frame.tool, frame.args)" in start
    assert "name.title = frame.tool" in start, "the raw id stays reachable as a tooltip"
    assert "_toolLabel(tc.tool, tc.args)" in js, "history formatToolCall must agree"


def test_summarize_args_shows_the_command_line():
    js = _read(CHAT_JS)
    fn = js[js.index("function _summarizeArgs") : js.index("const _TOOL_LABELS")]
    cases = {"cmd": {"command": "agnes schema hr_headcount"}, "sql": {"sql": "SELECT 1"}}
    script = (
        fn
        + f"\nprocess.stdout.write(JSON.stringify(Object.fromEntries(Object.entries({json.dumps(cases)}).map(([k, v]) => [k, _summarizeArgs(v)]))));\n"
    )
    res = json.loads(_node_run(script))
    assert res["cmd"] == "agnes schema hr_headcount"
    assert res["sql"] == "SELECT 1"


# ── tool results: JSON is one click away, never the primary rendering ───────


def test_json_fallback_is_collapsed_behind_details():
    js = _read(CHAT_JS)
    body = js[
        js.index('wrap.className = "cloud-chat-tool-result is-json"') : js.index("function _coerceToTablePreview")
    ]
    assert 'document.createElement("details")' in body, "the JSON fallback must be a collapsed details, not a bare pre"
    assert "Structured result" in body, "the visible line is a summary, not the payload"
    det_pos = body.index('document.createElement("details")')
    pre_pos = body.index('document.createElement("pre")')
    assert det_pos < pre_pos, "the pre lives INSIDE the details"


def test_show_all_rows_is_a_table_not_json():
    js = _read(CHAT_JS)
    assert "function _buildResultTable" in js
    body = js[js.index("function _coerceToTablePreview") : js.index("// ---------- Data-app split-pane preview")]
    assert "Show all rows (JSON)" not in body, "the expansion is a table now"
    assert "_TOOL_RESULT_FULL_ROWS_MAX" in body, "a DOM cap must exist for huge results"
    assert body.count("_buildResultTable(") == 2, "preview and expansion share one builder"


# ── streaming: markdown renders as it arrives ────────────────────────────────


def test_streaming_renders_markdown_not_textcontent():
    js = _read(CHAT_JS)
    body = js[js.index("function appendToken") : js.index("function finalizeAssistantMessage")]
    assert "currentAssistantBody.textContent = currentAssistantText" not in body, (
        "raw markdown source must not sit on screen until turn end"
    )
    assert "_scheduleStreamRender()" in body
    stream = js[js.index("function _renderStreamingMarkdown") : js.index("function appendToken")]
    assert "renderAnswerMarkdown(" in stream, "streaming and finalize must share the pipeline"
    assert "renderMermaidBlocks" not in stream, "no mermaid on partial content"
    assert "enhanceCodeBlocks" not in stream, "heavy enhancement waits for finalize"


def test_finalize_clears_the_stream_timer():
    js = _read(CHAT_JS)
    fin = js[js.index("function finalizeAssistantMessage") : js.index("// ---------- Inline tool-call blocks")]
    assert "_streamRenderTimer" in fin, "a late tick must not repaint a finalized bubble"


def test_streaming_safe_text_executable():
    js = _read(CHAT_JS)
    fn = js[js.index("function _streamingSafeText") : js.index("function _scheduleStreamRender")]
    cases = {
        "plain": "Hello **world**",
        "open_lang_partial": "Answer.\n\n```sour",
        "open_next_actions": "Answer.\n\n```next_actions\n- half",
        "open_sql_with_body": "Answer.\n\n```sql\nSELECT 1",
        "closed_fence": "Answer.\n\n```sql\nSELECT 1\n```\n",
        "bare_open_no_newline": "Answer.\n\n```",
    }
    script = (
        fn
        + f"\nprocess.stdout.write(JSON.stringify(Object.fromEntries(Object.entries({json.dumps(cases)}).map(([k, v]) => [k, _streamingSafeText(v)]))));\n"
    )
    res = json.loads(_node_run(script))
    assert res["plain"] == "Hello **world**"
    assert res["open_lang_partial"] == "Answer.\n\n", "a partial wire-trailer id is hidden"
    assert res["open_next_actions"] == "Answer.\n\n", "a streaming wire trailer is hidden"
    assert "SELECT 1" in res["open_sql_with_body"], "a streaming CODE block stays visible"
    assert res["closed_fence"] == cases["closed_fence"], "closed fences pass through"
    assert res["bare_open_no_newline"] == "Answer.\n\n", "a bare open fence waits for its id"


# ── turn-stopping events: visible in the transcript, not only the status bar ─


def test_confirmation_required_is_handled():
    js = _read(CHAT_JS)
    sw = js[js.index("switch (frame.type)") : js.index("function applySessionRename")]
    assert 'case "confirmation_required":' in sw, (
        "the tool-budget stop used to be silently dropped — the turn just froze"
    )


def test_errors_and_cancels_reach_the_transcript():
    js = _read(CHAT_JS)
    sw = js[js.index("switch (frame.type)") : js.index("function applySessionRename")]
    assert sw.count("renderSystemNote(") >= 3, "confirmation_required, error, cancelled"
    body = js[js.index("function renderSystemNote") : js.index("function renderSystemNote") + 800]
    assert "textContent" in body and ".innerHTML" not in body


def test_system_note_styles_exist():
    css = _read(CHAT_CSS)
    assert ".cloud-chat-system-note" in css
