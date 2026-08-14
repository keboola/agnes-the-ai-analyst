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
