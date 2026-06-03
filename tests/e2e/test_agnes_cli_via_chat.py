"""F.2–F.5 — assistant discovers + queries Agnes data via the `agnes` CLI.

These tests share one chat session — by the time we land on F.5 the
assistant has already discovered the schema in F.2/F.3, so prompting
it for a SUM groups the LLM context up nicely (mirroring how a real
user would converse).

All four scenarios are ``@pytest.mark.real_llm`` because the
behavior under test is "the model picks the right `agnes` CLI
sub-command for the user's natural-language prompt." Fake-agent's
``echo:`` reply doesn't exercise that decision.

Gated by:
  * AGNES_E2E=1               — docker-compose stack
  * AGNES_E2E_ANTHROPIC=1     — real LLM (skips this whole file otherwise)
  * ANTHROPIC_API_KEY=sk-...  — actual key the runner forwards inside

The sample-data loader (tests/e2e/load-sample-data.py) primes the
analytics warehouse with ``sales`` (10k rows, 3 regions) and
``customers`` (500 rows, 4 countries) at container startup.
"""

from __future__ import annotations

import os
import re

import duckdb
import pytest

from tests.e2e._helpers import (
    E2E_USER_EMAIL,
    E2E_USER_PASSWORD,
    bootstrap_admin,
    docker_exec,
    pump_until,
)


pytestmark = pytest.mark.real_llm


try:
    from websockets.sync.client import connect as ws_connect

    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover
    ws_connect = None  # type: ignore[assignment]
    _WS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Fixtures shared across F.2–F.5
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def admin_client(docker_e2e_agnes: str):
    """Same bootstrap pattern as F.1 — module-scoped admin client."""
    return bootstrap_admin(
        docker_e2e_agnes, email=E2E_USER_EMAIL, password=E2E_USER_PASSWORD,
    )


@pytest.fixture(scope="module")
def chat_session(admin_client) -> dict:
    """One long-lived chat session for F.2–F.5.

    A real user's catalog/schema/describe/query journey is one
    coherent conversation; sharing the WS reduces both the test wall
    clock and the LLM spend (the runner keeps its system prompt
    cached between turns).

    Yielded value is the POST /sessions response (id + ws_url +
    ws_ticket). The fixture intentionally does NOT open the WS — each
    test wants its own connect/disconnect so that a hang in one
    scenario doesn't poison the next.
    """
    return admin_client.create_chat_session(surface="web")


def _send_and_collect(
    admin_client, chat_session: dict, prompt: str, *, max_frames: int = 400,
) -> tuple[list[str], list[dict], list[dict]]:
    """Send one prompt, return ``(assistant_texts, tool_calls, tool_results)``.

    Loops over WS frames until an ``assistant_message`` with non-empty
    text arrives AND no further tool_call appears within a small
    buffer window. Real-LLM runs interleave many tool_call frames
    before the final answer; we collect them all so individual tests
    can grep over the tool_calls list for a specific CLI sub-command.
    """
    if not _WS_AVAILABLE:
        pytest.skip("websockets.sync.client unavailable — old python?")

    ws_url = admin_client.ws_url_for(chat_session)
    asst_texts: list[str] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []

    with ws_connect(ws_url, open_timeout=15) as ws:
        pump_until(ws, predicate=lambda f: f.get("type") in ("ready", "runner_ready"))
        ws.send('{"type":"user_msg","text":' + _json_str(prompt) + "}")

        # Pump until we see ``assistant_message`` content. A single turn
        # may emit multiple — we keep the LAST one, which is the agent's
        # final reply, per the cloud-chat WS protocol.
        seen_terminal = False
        for _ in range(max_frames):
            try:
                raw = ws.recv(timeout=120.0)
            except TimeoutError:
                break
            try:
                frame = _safe_json(raw)
            except ValueError:
                continue
            t = frame.get("type")
            if t == "tool_call":
                tool_calls.append(frame)
            elif t == "tool_result":
                tool_results.append(frame)
            elif t == "assistant_message":
                content = frame.get("content", "") or ""
                if content.strip():
                    asst_texts.append(content)
                    # End-of-turn heuristic: claude-agent-sdk emits a
                    # blank ``assistant_message`` (stop_reason=end_turn)
                    # after the final reply, or just stops streaming.
                    # We treat the first non-empty assistant_message as
                    # terminal because there's only one per user turn.
                    seen_terminal = True
                    break

        if not seen_terminal:
            raise AssertionError(
                f"never saw a non-empty assistant_message for prompt {prompt!r}; "
                f"got {len(tool_calls)} tool_calls and {len(asst_texts)} assistant texts"
            )

    return asst_texts, tool_calls, tool_results


def _json_str(s: str) -> str:
    """Inline-encode a string for embedding in a hand-built JSON frame."""
    import json
    return json.dumps(s)


def _safe_json(raw):
    import json
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw)


def _bash_commands(tool_calls: list[dict]) -> list[str]:
    """Extract the ``command`` arg from every Bash tool_call frame."""
    out: list[str] = []
    for tc in tool_calls:
        if (tc.get("tool") or "").lower() == "bash":
            args = tc.get("args") or {}
            cmd = args.get("command") or ""
            if cmd:
                out.append(cmd)
    return out


# ---------------------------------------------------------------------------
# F.2 — catalog discovery
# ---------------------------------------------------------------------------


def test_f2_catalog_discovery_via_natural_language(
    docker_e2e_agnes: str, admin_client, chat_session,
) -> None:
    """User asks 'what tables are available' → agent runs `agnes catalog`."""
    asst, tool_calls, _ = _send_and_collect(
        admin_client, chat_session,
        "What tables do you have access to in Agnes?",
    )

    bash_cmds = _bash_commands(tool_calls)
    assert any("agnes catalog" in c for c in bash_cmds), (
        f"expected an `agnes catalog` invocation; got: {bash_cmds!r}"
    )

    final = asst[-1]
    assert "sales" in final and "customers" in final, (
        f"expected both fixture tables in the answer; got: {final!r}"
    )


# ---------------------------------------------------------------------------
# F.3 — schema inspection
# ---------------------------------------------------------------------------


def test_f3_schema_inspection_for_sales_table(
    docker_e2e_agnes: str, admin_client, chat_session,
) -> None:
    """User asks 'what columns in sales' → agent runs `agnes schema sales`.

    Asserts both the tool-call shape AND the rendered answer mentions
    the actual column names — sales has ``order_date``, ``region``,
    ``amount_cents``. If the agent fabricated columns or skipped the
    CLI call entirely, both assertions catch it.
    """
    asst, tool_calls, _ = _send_and_collect(
        admin_client, chat_session,
        "What columns does the sales table have?",
    )

    bash_cmds = _bash_commands(tool_calls)
    assert any("agnes schema" in c and "sales" in c for c in bash_cmds), (
        f"expected `agnes schema sales`; got: {bash_cmds!r}"
    )

    final = asst[-1].lower()
    for col in ("order_date", "region", "amount_cents"):
        assert col in final, (
            f"expected column {col!r} in answer; got: {asst[-1]!r}"
        )


# ---------------------------------------------------------------------------
# F.4 — describe rows
# ---------------------------------------------------------------------------


def test_f4_describe_sample_rows_from_customers(
    docker_e2e_agnes: str, admin_client, chat_session,
) -> None:
    """User asks 'show me 3 example rows from customers' → describe call.

    `agnes describe customers -n 3` is the canonical phrasing; we
    accept any `-n`/`--limit` variant the model picks so the test
    isn't brittle to LLM word choice.
    """
    asst, tool_calls, _ = _send_and_collect(
        admin_client, chat_session,
        "Show me 3 example rows from the customers table.",
    )

    bash_cmds = _bash_commands(tool_calls)
    assert any("agnes describe" in c and "customers" in c for c in bash_cmds), (
        f"expected `agnes describe customers`; got: {bash_cmds!r}"
    )

    final = asst[-1]
    # The fixture seeds names as `customer_<id>` — at least one of the
    # low IDs should appear after a `-n 3` style describe call.
    assert re.search(r"customer_\d+", final), (
        f"expected a `customer_<n>` row in the reply; got: {final!r}"
    )


# ---------------------------------------------------------------------------
# F.5 — local query + numeric verification
# ---------------------------------------------------------------------------


def _expected_region_a_sum() -> int:
    """Compute the ground-truth SUM(amount_cents) WHERE region='A' from a
    DuckDB built off the same SQL fixture loaded into the container.

    We run the fixture SQL against an in-memory DuckDB on the test
    host so the assertion is independent of the container's DuckDB.
    The fixture uses pure-DuckDB syntax (range(), DATE arithmetic),
    so executing it locally gives the exact same row set.
    """
    sql_path = (
        os.path.dirname(__file__) + "/sample-data/sales.sql"
    )
    with open(sql_path, encoding="utf-8") as fh:
        sql = fh.read()
    conn = duckdb.connect(":memory:")
    try:
        conn.execute(sql)
        return int(conn.execute(
            "SELECT SUM(amount_cents) FROM sales WHERE region = 'A'"
        ).fetchone()[0])
    finally:
        conn.close()


def test_f5_local_sum_query_matches_warehouse(
    docker_e2e_agnes: str, admin_client, chat_session,
) -> None:
    """User asks total amount for region A → assistant returns a number
    that matches what DuckDB computes from the same fixture SQL."""
    expected = _expected_region_a_sum()

    asst, tool_calls, _ = _send_and_collect(
        admin_client, chat_session,
        "What's the total amount_cents in sales for region 'A'?",
    )

    bash_cmds = _bash_commands(tool_calls)
    assert any("agnes query" in c for c in bash_cmds), (
        f"expected an `agnes query` invocation; got: {bash_cmds!r}"
    )

    # The LLM's reply usually formats the number with commas
    # ("42,123,456"), without commas ("42123456"), or as a thousands-
    # grouped USD-like form. Pull every integer-looking run out of the
    # reply and check the expected digits appear in at least one of
    # them. This is robust to currency formatting choices.
    digits_only = re.findall(r"\d[\d,]*", asst[-1])
    expected_str = str(expected)
    matches = [d for d in digits_only if d.replace(",", "") == expected_str]
    assert matches, (
        f"expected the digit string {expected_str!r} (region-A SUM) in the "
        f"reply; got numbers {digits_only!r} and reply {asst[-1]!r}"
    )

    # Double-check: a chat.tool_call row should now exist for this
    # session in the container's audit_log. We don't snapshot the
    # whole table — just confirm at least one row mentions the
    # session id, which the manager writes inside the pump loop.
    grep = docker_exec(
        [
            "/opt/venv/bin/python",
            "-c",
            "import duckdb;"
            "c=duckdb.connect('/data/state/system.duckdb', read_only=True);"
            f"print(c.execute(\"SELECT COUNT(*) FROM audit_log WHERE action='chat.tool_call' AND details LIKE '%{chat_session['id']}%'\").fetchone()[0])",
        ],
        timeout=30.0,
    )
    assert grep.returncode == 0, (
        f"audit_log probe failed: {grep.stderr.decode('utf-8', 'replace')!r}"
    )
    count = int(grep.stdout.decode("utf-8", "replace").strip() or "0")
    assert count > 0, (
        f"expected at least one chat.tool_call audit row for session "
        f"{chat_session['id']}; got {count}"
    )
