"""Pure helpers for UsageProcessor — event extraction from Claude Code session jsonls.

Session JSONL shape (as documented in dev_docs/session_explore.md and verified
against live samples):

Each line is a top-level event dict with:
  {
    "type": "user" | "assistant" | "progress" | "system" |
            "tool_use_result" | "summary" | "file-history-snapshot" |
            "queue-operation" | ...,
    "uuid": "event-uuid",
    "parentUuid": "parent-event-uuid",
    "sessionId": "session-uuid",
    "timestamp": "2026-05-12T07:30:00.000Z",
    "cwd": "/path/to/cwd",
    "message": {
      "role": "user" | "assistant",
      "model": "claude-...",       # present on assistant turns
      "content": [                 # array or plain string on user turns
        {"type": "text", "text": "..."},
        {"type": "tool_use", "id": "tu_123", "name": "Bash", "input": {...}},
        {"type": "tool_result", "tool_use_id": "tu_123", "is_error": false, "content": [...]}
      ]
    }
  }

Tool results appear as:
- Inline content items of type "tool_result" inside a user-role message, OR
- As top-level events of type "tool_use_result" (older Claude Code versions)

is_error correlation: build a map of {tool_use_id: True} from tool_result
items on the first pass, then apply to matching tool_use events.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Iterator

USAGE_PROCESSOR_VERSION = 1

BUILTIN_TOOLS = frozenset({
    "Bash", "Read", "Edit", "Write", "Grep", "Glob", "TodoWrite",
    "Task", "Agent", "NotebookEdit", "WebFetch", "WebSearch", "ExitPlanMode",
    "LS",  # also built-in
})

# Slash commands: "/something" or "/namespace:something" at start of user text
SLASH_RE = re.compile(r"^\s*/([A-Za-z][\w:-]*)")

# Event types to skip entirely
_SKIP_TYPES = frozenset({
    "system", "summary", "file-history-snapshot",
    "queue-operation", "progress",
})


@dataclass(frozen=True)
class ParsedEvent:
    event_uuid: str | None
    parent_uuid: str | None
    event_type: str            # 'tool_use' | 'slash_command' | 'subagent' | 'mcp_call'
    tool_name: str | None
    skill_name: str | None
    subagent_type: str | None
    command_name: str | None
    is_error: bool
    model: str | None
    cwd: str | None
    occurred_at: datetime


def _parse_ts(ts_str: str | None) -> datetime | None:
    """Parse ISO 8601 timestamp to aware datetime. Returns None on failure."""
    if not ts_str:
        return None
    try:
        ts_str = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def _collect_error_map(turns: list[dict]) -> dict[str, bool]:
    """First-pass: collect tool_use_id → is_error from all tool_result items.

    Tool results appear in two places:
    1. As content items inside user-role messages (type='tool_result')
    2. As top-level events of type='tool_use_result'
    """
    errors: dict[str, bool] = {}
    for turn in turns:
        turn_type = turn.get("type", "")

        # Top-level tool_use_result events (older Claude Code)
        if turn_type == "tool_use_result":
            tu_id = turn.get("tool_use_id") or turn.get("toolUseId")
            if tu_id and turn.get("is_error"):
                errors[tu_id] = True

        # Inline tool_result content blocks inside user messages
        msg = turn.get("message", {}) or {}
        content = msg.get("content", [])
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_result":
                    tu_id = item.get("tool_use_id")
                    if tu_id and item.get("is_error"):
                        errors[tu_id] = True

    return errors


def iter_events(turns: list[dict]) -> Iterator[ParsedEvent]:
    """Walk parsed JSONL turns and yield ParsedEvent for each observable event.

    Recognises:
    - Assistant tool_use blocks → event_type='tool_use' (or 'subagent'/'mcp_call')
    - Skill tool → also extracts skill_name
    - Task/Agent tools → event_type='subagent'
    - mcp__* tools → event_type='mcp_call'
    - User messages starting with '/' → event_type='slash_command'

    Skips: system, summary, file-history-snapshot, queue-operation, progress.
    """
    error_map = _collect_error_map(turns)

    for turn in turns:
        turn_type = turn.get("type", "")
        if turn_type in _SKIP_TYPES:
            continue

        ts = _parse_ts(turn.get("timestamp")) or datetime.now(timezone.utc)
        cwd = turn.get("cwd")
        event_uuid = turn.get("uuid")
        parent_uuid = turn.get("parentUuid")

        msg = turn.get("message", {}) or {}
        content = msg.get("content", [])
        model = msg.get("model")

        if turn_type == "assistant":
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") != "tool_use":
                        continue

                    tool_id = item.get("id", "")
                    tool_name = item.get("name") or ""
                    inp = item.get("input") or {}
                    is_error = error_map.get(tool_id, False)

                    # Classify event type
                    skill_name: str | None = None
                    subagent_type: str | None = None
                    command_name: str | None = None

                    if tool_name == "Skill":
                        event_type = "tool_use"
                        # Real Skill input shape varies; check both keys
                        skill_name = inp.get("skill") or inp.get("name") or None
                    elif tool_name in ("Task", "Agent"):
                        event_type = "subagent"
                        subagent_type = inp.get("subagent_type") or tool_name
                    elif tool_name.startswith("mcp__"):
                        event_type = "mcp_call"
                    else:
                        event_type = "tool_use"

                    yield ParsedEvent(
                        event_uuid=event_uuid,
                        parent_uuid=parent_uuid,
                        event_type=event_type,
                        tool_name=tool_name or None,
                        skill_name=skill_name,
                        subagent_type=subagent_type,
                        command_name=command_name,
                        is_error=is_error,
                        model=model,
                        cwd=cwd,
                        occurred_at=ts,
                    )

        elif turn_type == "user":
            # Slash-command detection from text content
            if isinstance(content, str):
                text_parts = [content]
            elif isinstance(content, list):
                text_parts = [
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
            else:
                text_parts = []

            for text in text_parts:
                if not text:
                    continue
                m = SLASH_RE.match(text)
                if m:
                    yield ParsedEvent(
                        event_uuid=event_uuid,
                        parent_uuid=parent_uuid,
                        event_type="slash_command",
                        tool_name=None,
                        skill_name=None,
                        subagent_type=None,
                        command_name=m.group(1),
                        is_error=False,
                        model=None,
                        cwd=cwd,
                        occurred_at=ts,
                    )


class AttributionLookup:
    """Preloads attribution tables into memory for O(1) event attribution.

    Resolves (source, ref_id) for each event. Built-in tools and unknowns
    return ('builtin', None). curated wins over flea (alphabetical ordering
    means 'curated' < 'flea' → first-write-wins when iterating ORDER BY source).
    """

    def __init__(self, conn):
        self._skills: dict[str, tuple[str, str]] = {}     # name -> (source, ref_id)
        self._agents: dict[str, tuple[str, str]] = {}
        self._commands: dict[str, tuple[str, str]] = {}

        for row in conn.execute(
            "SELECT skill_name, source, ref_id FROM usage_attribution_skills ORDER BY source ASC"
        ).fetchall():
            self._skills.setdefault(row[0], (row[1], row[2]))

        for row in conn.execute(
            "SELECT agent_name, source, ref_id FROM usage_attribution_agents ORDER BY source ASC"
        ).fetchall():
            self._agents.setdefault(row[0], (row[1], row[2]))

        for row in conn.execute(
            "SELECT command_name, source, ref_id FROM usage_attribution_commands ORDER BY source ASC"
        ).fetchall():
            self._commands.setdefault(row[0], (row[1], row[2]))

    def attribute(self, event: ParsedEvent) -> tuple[str, str | None]:
        """Resolve (source, ref_id). Returns ('builtin', None) for built-ins or unknowns.

        Lookup order:
        1. Skill invocations → skill attribution table (bypasses BUILTIN_TOOLS check
           because Skill tool is built-in but the *skill name* identifies the plugin).
        2. Subagent dispatches → agent attribution table (bypasses BUILTIN_TOOLS check
           because Task/Agent are built-in but the *subagent_type* identifies the plugin).
        3. Slash commands → command attribution table.
        4. Built-in tool names → ('builtin', None).
        5. Unknown tool names → ('builtin', None) fallback.
        """
        # Skill name takes priority over tool_name check
        if event.skill_name and event.skill_name in self._skills:
            return self._skills[event.skill_name]

        # Subagent type takes priority over tool_name check
        if event.subagent_type and event.subagent_type in self._agents:
            return self._agents[event.subagent_type]

        # Slash command attribution
        if event.command_name and event.command_name in self._commands:
            return self._commands[event.command_name]

        # Built-in tool names (Task/Agent fall through to here only when
        # their subagent_type is not in the attribution table)
        if event.tool_name in BUILTIN_TOOLS:
            return ("builtin", None)

        # Unknown tool name → builtin fallback
        return ("builtin", None)


def compute_active_seconds(timestamps: list[datetime]) -> int:
    """Sum of intra-block durations. Gap >10 minutes = new block."""
    if not timestamps:
        return 0
    timestamps = sorted(timestamps)
    GAP = 600  # 10 minutes
    blocks = []
    block_start = timestamps[0]
    prev = timestamps[0]
    for ts in timestamps[1:]:
        gap = (ts - prev).total_seconds()
        if gap > GAP:
            blocks.append((block_start, prev))
            block_start = ts
        prev = ts
    blocks.append((block_start, prev))
    return int(sum((end - start).total_seconds() for start, end in blocks))


def compute_summary(turns: list[dict], events: list[dict]) -> dict:
    """Build the usage_session_summary row dict from parsed turns and event rows.

    Caller must fill in 'session_file' and 'username' after calling this.
    events is a list of dicts (as produced by UsageProcessor, not ParsedEvent).
    """
    # session_id: first turn with a sessionId field
    session_id = None
    for t in turns:
        sid = t.get("sessionId")
        if sid:
            session_id = sid
            break

    # Timestamps from all turns that have one
    timestamps: list[datetime] = []
    user_messages = 0
    assistant_messages = 0
    model_counter: Counter = Counter()

    for t in turns:
        ts = _parse_ts(t.get("timestamp"))
        if ts:
            timestamps.append(ts)
        turn_type = t.get("type", "")
        if turn_type == "user":
            user_messages += 1
        elif turn_type == "assistant":
            assistant_messages += 1
            msg = t.get("message", {}) or {}
            m = msg.get("model")
            if m:
                model_counter[m] += 1

    started_at = min(timestamps) if timestamps else None
    ended_at = max(timestamps) if timestamps else None
    wall_seconds = (
        int((ended_at - started_at).total_seconds()) if started_at and ended_at else 0
    )
    active_seconds = compute_active_seconds(timestamps)

    # Aggregate counts from events
    tool_calls = sum(1 for e in events if e["event_type"] == "tool_use")
    tool_errors = sum(1 for e in events if e.get("is_error"))
    skill_invocations = sum(1 for e in events if e.get("skill_name"))
    subagent_dispatches = sum(1 for e in events if e["event_type"] == "subagent")
    mcp_calls = sum(1 for e in events if e["event_type"] == "mcp_call")
    slash_commands = sum(1 for e in events if e["event_type"] == "slash_command")
    distinct_tools = len({e["tool_name"] for e in events if e.get("tool_name")})
    distinct_skills = len({e["skill_name"] for e in events if e.get("skill_name")})
    primary_model = model_counter.most_common(1)[0][0] if model_counter else None

    return {
        "session_id": session_id or "",
        "started_at": started_at,
        "ended_at": ended_at,
        "active_seconds": active_seconds,
        "wall_seconds": wall_seconds,
        "user_messages": user_messages,
        "assistant_messages": assistant_messages,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "skill_invocations": skill_invocations,
        "subagent_dispatches": subagent_dispatches,
        "mcp_calls": mcp_calls,
        "slash_commands": slash_commands,
        "distinct_tools": distinct_tools,
        "distinct_skills": distinct_skills,
        "primary_model": primary_model,
        "processor_version": USAGE_PROCESSOR_VERSION,
    }


def rebuild_rollups(conn, *, since_day=None) -> None:
    """Rebuild daily rollups from usage_events.

    Default since_day = CURRENT_DATE - 7 (incremental refresh on every tick).
    Pass since_day=None to do full rebuild on reprocess.
    """
    if since_day is None:
        since_day = (datetime.now(timezone.utc) - timedelta(days=7)).date()

    conn.execute("DELETE FROM usage_tool_daily WHERE day >= ?", [since_day])
    conn.execute(
        """
        INSERT INTO usage_tool_daily
            (day, tool_name, source, invocations, error_count, distinct_users, distinct_sessions)
        SELECT
            CAST(occurred_at AS DATE) AS day,
            tool_name,
            source,
            COUNT(*) AS invocations,
            SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS error_count,
            COUNT(DISTINCT username) AS distinct_users,
            COUNT(DISTINCT session_id) AS distinct_sessions
        FROM usage_events
        WHERE CAST(occurred_at AS DATE) >= ?
          AND tool_name IS NOT NULL
        GROUP BY day, tool_name, source
        """,
        [since_day],
    )

    conn.execute("DELETE FROM usage_plugin_daily WHERE day >= ?", [since_day])
    conn.execute(
        """
        INSERT INTO usage_plugin_daily
            (day, source, ref_id, invocations, distinct_users, distinct_sessions)
        SELECT
            CAST(occurred_at AS DATE) AS day,
            source,
            ref_id,
            COUNT(*),
            COUNT(DISTINCT username),
            COUNT(DISTINCT session_id)
        FROM usage_events
        WHERE CAST(occurred_at AS DATE) >= ?
          AND ref_id IS NOT NULL
          AND source IN ('curated', 'flea')
        GROUP BY day, source, ref_id
        """,
        [since_day],
    )
