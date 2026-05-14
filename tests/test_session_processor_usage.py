"""Tests for UsageProcessor — fixture-driven, covers extraction, attribution, errors,
idempotency, and empty-session handling."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sessions" / "usage"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path, monkeypatch) -> duckdb.DuckDBPyConnection:
    """Fresh fully-migrated DuckDB in tmp_path (same idiom as test_session_pipeline.py)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    db_module._system_db_conn = None
    db_module._system_db_path = None
    return db_module.get_system_db()


def _seed_attribution(conn: duckdb.DuckDBPyConnection) -> None:
    """Seed attribution rows the fixtures reference."""
    conn.execute(
        "INSERT OR IGNORE INTO usage_attribution_skills (source, ref_id, skill_name)"
        " VALUES ('curated', 'mp/plug', 'my-skill')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO usage_attribution_skills (source, ref_id, skill_name)"
        " VALUES ('flea', 'entity-1', 'flea-skill')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO usage_attribution_agents (source, ref_id, agent_name)"
        " VALUES ('curated', 'mp/plug', 'my-agent')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO usage_attribution_commands (source, ref_id, command_name)"
        " VALUES ('curated', 'mp/plug', 'compound:debug')"
    )


def _process(fixture_name: str, conn: duckdb.DuckDBPyConnection) -> None:
    """Run UsageProcessor against a fixture file."""
    from services.session_processors.usage import UsageProcessor
    processor = UsageProcessor()
    path = FIXTURES_DIR / fixture_name
    result = processor.process_session(
        session_path=path,
        username="test-user",
        session_key=fixture_name,
        conn=conn,
    )
    return result


def _events(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM usage_events ORDER BY occurred_at ASC"
    ).fetchall()
    desc = [d[0] for d in conn.description]
    return [dict(zip(desc, row)) for row in rows]


def _summary(conn: duckdb.DuckDBPyConnection, session_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM usage_session_summary WHERE session_file = ?",
        [session_key],
    ).fetchone()
    if row is None:
        return None
    desc = [d[0] for d in conn.description]
    return dict(zip(desc, row))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSimpleBash:
    def test_extracts_one_event(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("simple_bash.jsonl", conn)
        evts = _events(conn)
        assert len(evts) == 1
        assert evts[0]["tool_name"] == "Bash"
        assert evts[0]["event_type"] == "tool_use"

    def test_builtin_source(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("simple_bash.jsonl", conn)
        evts = _events(conn)
        assert evts[0]["source"] == "builtin"
        assert evts[0]["ref_id"] is None

    def test_no_error_flag(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("simple_bash.jsonl", conn)
        evts = _events(conn)
        assert evts[0]["is_error"] is False

    def test_summary_written(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("simple_bash.jsonl", conn)
        s = _summary(conn, "simple_bash.jsonl")
        assert s is not None
        assert s["tool_calls"] == 1
        assert s["tool_errors"] == 0
        assert s["username"] == "test-user"


class TestMcpCall:
    def test_mcp_event_type(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("mcp_call.jsonl", conn)
        evts = _events(conn)
        assert len(evts) == 1
        assert evts[0]["event_type"] == "mcp_call"
        assert evts[0]["tool_name"] == "mcp__github__create_issue"

    def test_mcp_builtin_source(self, tmp_path, monkeypatch):
        """MCP tools not in attribution tables fall back to builtin."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("mcp_call.jsonl", conn)
        evts = _events(conn)
        # mcp__github__create_issue is not in the attribution tables → builtin fallback
        assert evts[0]["source"] == "builtin"

    def test_summary_mcp_count(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("mcp_call.jsonl", conn)
        s = _summary(conn, "mcp_call.jsonl")
        assert s["mcp_calls"] == 1


class TestCuratedSkill:
    def test_curated_attribution(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("skill_curated.jsonl", conn)
        row = conn.execute(
            "SELECT source, ref_id FROM usage_events WHERE skill_name = 'my-skill'"
        ).fetchone()
        assert row is not None
        assert row[0] == "curated"
        assert row[1] == "mp/plug"

    def test_skill_invocations_count(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("skill_curated.jsonl", conn)
        s = _summary(conn, "skill_curated.jsonl")
        assert s["skill_invocations"] == 1


class TestFleaSkill:
    def test_flea_attribution(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("skill_flea.jsonl", conn)
        row = conn.execute(
            "SELECT source, ref_id FROM usage_events WHERE skill_name = 'flea-skill'"
        ).fetchone()
        assert row is not None
        assert row[0] == "flea"
        assert row[1] == "entity-1"


class TestSlashCommand:
    def test_slash_command_extracted(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("slash_command.jsonl", conn)
        evts = _events(conn)
        slash_evts = [e for e in evts if e["event_type"] == "slash_command"]
        assert len(slash_evts) == 1
        assert slash_evts[0]["command_name"] == "compound:debug"

    def test_slash_command_attribution(self, tmp_path, monkeypatch):
        """compound:debug is in attribution_commands → should resolve to curated."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("slash_command.jsonl", conn)
        row = conn.execute(
            "SELECT source, ref_id FROM usage_events WHERE command_name = 'compound:debug'"
        ).fetchone()
        assert row is not None
        assert row[0] == "curated"
        assert row[1] == "mp/plug"

    def test_slash_commands_in_summary(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("slash_command.jsonl", conn)
        s = _summary(conn, "slash_command.jsonl")
        assert s["slash_commands"] == 1


class TestSubagent:
    def test_subagent_event_type(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("subagent.jsonl", conn)
        evts = _events(conn)
        assert len(evts) == 1
        assert evts[0]["event_type"] == "subagent"
        assert evts[0]["subagent_type"] == "my-agent"

    def test_subagent_attributed(self, tmp_path, monkeypatch):
        """my-agent is in attribution_agents → curated."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("subagent.jsonl", conn)
        evts = _events(conn)
        assert evts[0]["source"] == "curated"
        assert evts[0]["ref_id"] == "mp/plug"

    def test_subagent_dispatches_in_summary(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("subagent.jsonl", conn)
        s = _summary(conn, "subagent.jsonl")
        assert s["subagent_dispatches"] == 1


class TestToolError:
    def test_error_flagged_on_event(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("tool_error.jsonl", conn)
        evts = _events(conn)
        assert len(evts) == 1
        assert evts[0]["tool_name"] == "Bash"
        assert evts[0]["is_error"] is True

    def test_tool_errors_in_summary(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("tool_error.jsonl", conn)
        s = _summary(conn, "tool_error.jsonl")
        assert s["tool_errors"] == 1
        assert s["tool_calls"] == 1


class TestMixedSession:
    def test_mixed_event_counts(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("mixed.jsonl", conn)
        evts = _events(conn)
        types = [e["event_type"] for e in evts]
        # one slash_command + one tool_use (Bash) + one tool_use (Skill) +
        # one mcp_call + one subagent + one tool_use (Bash with error) = 6 events
        assert "slash_command" in types
        assert "tool_use" in types
        assert "mcp_call" in types
        assert "subagent" in types

    def test_mixed_summary_counts(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("mixed.jsonl", conn)
        s = _summary(conn, "mixed.jsonl")
        assert s is not None
        assert s["mcp_calls"] == 1
        assert s["subagent_dispatches"] == 1
        assert s["skill_invocations"] == 1
        assert s["slash_commands"] == 1
        assert s["tool_errors"] == 1

    def test_mixed_error_correlated(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("mixed.jsonl", conn)
        err_evts = conn.execute(
            "SELECT tool_name FROM usage_events WHERE is_error = TRUE"
        ).fetchall()
        assert len(err_evts) == 1
        assert err_evts[0][0] == "Bash"


class TestEmptySession:
    def test_zero_events_writes_summary(self, tmp_path, monkeypatch):
        """Empty session (only system/summary turns) yields 0 events but a summary row."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        result = _process("empty.jsonl", conn)
        evts = _events(conn)
        assert len(evts) == 0
        s = _summary(conn, "empty.jsonl")
        assert s is not None
        assert s["tool_calls"] == 0

    def test_processor_result_zero_items(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        result = _process("empty.jsonl", conn)
        assert result.items_count == 0


class TestIdempotency:
    def test_reprocess_same_event_count(self, tmp_path, monkeypatch):
        """INSERT OR IGNORE: processing the same session twice yields same event count."""
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("simple_bash.jsonl", conn)
        count_1 = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        _process("simple_bash.jsonl", conn)
        count_2 = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        assert count_1 == count_2 == 1

    def test_reprocess_mixed_idempotent(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)
        _process("mixed.jsonl", conn)
        n1 = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        _process("mixed.jsonl", conn)
        n2 = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        assert n1 == n2


class TestMultiToolTurnDedup:
    def test_two_tool_calls_in_same_turn_produce_two_events(self, tmp_path, monkeypatch):
        """Parallel Bash + Read in the same assistant turn must produce 2 distinct events.

        Regression — earlier bug: same event_uuid + same tool_name collided in id hash,
        so the second tool_use was silently dropped by INSERT OR IGNORE.
        """
        conn = _fresh_db(tmp_path, monkeypatch)
        _seed_attribution(conn)

        jsonl_path = tmp_path / "multi_tool_turn.jsonl"
        jsonl_path.write_text(
            json.dumps({
                "uuid": "turn-1",
                "parentUuid": None,
                "type": "assistant",
                "sessionId": "sess-multi",
                "timestamp": "2026-05-12T10:00:00Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-x",
                    "content": [
                        {"type": "tool_use", "id": "tu_a", "name": "Bash", "input": {"command": "ls"}},
                        {"type": "tool_use", "id": "tu_b", "name": "Bash", "input": {"command": "pwd"}},
                    ],
                },
            }) + "\n"
        )

        from services.session_processors.usage import UsageProcessor
        processor = UsageProcessor()
        processor.process_session(
            session_path=jsonl_path,
            username="alice",
            session_key="alice/multi_tool_turn.jsonl",
            conn=conn,
        )

        n = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE session_id='sess-multi'"
        ).fetchone()[0]
        assert n == 2, f"expected 2 events (one per tu_xxx), got {n}"


class TestCommandNameTagExtraction:
    """Slash invocations arrive as <command-name>/foo</command-name> embedded in
    user message content (Claude Code's wire format). Unit-test iter_events
    against synthetic turns so a future shape shift doesn't silently regress."""

    @staticmethod
    def _user_turn(content):
        return {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": "sess-cn",
            "timestamp": "2026-05-14T10:00:00.000Z",
            "cwd": "/workspace",
            "message": {"role": "user", "content": content},
        }

    def test_extracts_command_name_from_string_content(self):
        from services.session_processors.usage_lib import iter_events
        turn = self._user_turn(
            "<command-name>/clear</command-name>\n<command-args></command-args>"
        )
        events = list(iter_events([turn]))
        assert len(events) == 1
        assert events[0].event_type == "slash_command"
        assert events[0].command_name == "clear"

    def test_extracts_command_name_from_text_block(self):
        """Defensive: same regex behavior when content arrives as a list-of-blocks
        instead of a plain string, in case Claude Code's wire format shifts."""
        from services.session_processors.usage_lib import iter_events
        turn = self._user_turn(
            [{"type": "text", "text": "<command-name>/plugin:name</command-name>"}]
        )
        events = list(iter_events([turn]))
        assert len(events) == 1
        assert events[0].command_name == "plugin:name"

    def test_command_name_not_at_start_still_matches(self):
        """Real Claude Code prepends a <command-message> sibling before the
        <command-name> tag — regex must search, not anchor at start."""
        from services.session_processors.usage_lib import iter_events
        turn = self._user_turn(
            "<command-message>foo</command-message>\n"
            "<command-name>/foo</command-name>\n"
            "<command-args>some arg</command-args>"
        )
        events = list(iter_events([turn]))
        assert len(events) == 1
        assert events[0].command_name == "foo"

    def test_plain_text_without_tag_does_not_match(self):
        """A user message that happens to contain '/foo' as prose, but no
        <command-name> tag, must NOT yield a slash_command event — that's the
        whole point of switching from the old `^\\s*/<name>` regex."""
        from services.session_processors.usage_lib import iter_events
        turn = self._user_turn("Hello world, see /not-a-command-just-prose for context.")
        events = list(iter_events([turn]))
        assert events == []
