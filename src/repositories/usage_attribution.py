"""Repository for usage_attribution_{skills,agents,commands} tables (schema v41).

Attribution maps a skill / agent / slash-command name to its source plugin so
the UsageProcessor (Phase A.3) can attribute invocations without re-walking
plugin clones at processor time.

Replace-on-write semantics:
- `replace_for_curated(marketplace_id, plugin_name, skills, agents, commands)`
  — wipes prior rows for `(source='curated', ref_id='<marketplace_id>/<plugin>')`
  then re-inserts the new lists. Called from `src.marketplace._refresh_plugin_cache`
  after every successful sync.
- `replace_for_flea(entity_id, skills, agents, commands)` — same shape, scoped
  to `(source='flea', ref_id=entity_id)`. Called from `app/api/store.py` on
  entity create / approve / update.
- `delete_for_flea(entity_id)` — for soft-delete / hard-delete cleanup.
- `lookup(skill_name=..., agent_name=..., command_name=...) -> tuple[source, ref_id] | None`
  — exactly one kwarg must be non-None; returns the first match (curated wins
  over flea — 'curated' sorts before 'flea' alphabetically).
"""
from __future__ import annotations

from typing import Iterable, Optional

import duckdb


class UsageAttributionRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def replace_for_curated(
        self,
        marketplace_id: str,
        plugin_name: str,
        *,
        skills: Iterable[str] = (),
        agents: Iterable[str] = (),
        commands: Iterable[str] = (),
    ) -> None:
        """Overwrite all attribution rows for a curated plugin.

        ref_id is ``<marketplace_id>/<plugin_name>``.
        """
        ref_id = f"{marketplace_id}/{plugin_name}"
        self._replace("curated", ref_id, skills, agents, commands)

    def replace_for_flea(
        self,
        entity_id: str,
        *,
        skills: Iterable[str] = (),
        agents: Iterable[str] = (),
        commands: Iterable[str] = (),
    ) -> None:
        """Overwrite all attribution rows for a flea (store) entity."""
        self._replace("flea", entity_id, skills, agents, commands)

    def delete_for_flea(self, entity_id: str) -> None:
        """Remove all attribution rows for a flea entity (archive / hard-delete)."""
        self._delete("flea", entity_id)

    def lookup(
        self,
        *,
        skill_name: Optional[str] = None,
        agent_name: Optional[str] = None,
        command_name: Optional[str] = None,
    ) -> Optional[tuple[str, str]]:
        """Return ``(source, ref_id)`` for the given name.

        Exactly one kwarg must be non-None. When multiple sources map the
        same name (e.g. a curated plugin and a flea entity share a skill
        name), curated wins because ``'curated' < 'flea'`` alphabetically
        and the query uses ``ORDER BY source LIMIT 1``.
        """
        if sum(x is not None for x in (skill_name, agent_name, command_name)) != 1:
            raise ValueError(
                "lookup() requires exactly one of skill_name / agent_name / command_name"
            )
        if skill_name is not None:
            table, col, val = "usage_attribution_skills", "skill_name", skill_name
        elif agent_name is not None:
            table, col, val = "usage_attribution_agents", "agent_name", agent_name
        else:
            table, col, val = "usage_attribution_commands", "command_name", command_name
        row = self.conn.execute(
            f"SELECT source, ref_id FROM {table} WHERE {col} = ? ORDER BY source LIMIT 1",
            [val],
        ).fetchone()
        return (row[0], row[1]) if row else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _replace(
        self,
        source: str,
        ref_id: str,
        skills: Iterable[str],
        agents: Iterable[str],
        commands: Iterable[str],
    ) -> None:
        skills_l = sorted({s for s in skills if s})
        agents_l = sorted({a for a in agents if a})
        commands_l = sorted({c for c in commands if c})

        self.conn.execute("BEGIN")
        try:
            for table in (
                "usage_attribution_skills",
                "usage_attribution_agents",
                "usage_attribution_commands",
            ):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE source = ? AND ref_id = ?",
                    [source, ref_id],
                )
            for n in skills_l:
                self.conn.execute(
                    "INSERT INTO usage_attribution_skills (source, ref_id, skill_name)"
                    " VALUES (?, ?, ?)",
                    [source, ref_id, n],
                )
            for n in agents_l:
                self.conn.execute(
                    "INSERT INTO usage_attribution_agents (source, ref_id, agent_name)"
                    " VALUES (?, ?, ?)",
                    [source, ref_id, n],
                )
            for n in commands_l:
                self.conn.execute(
                    "INSERT INTO usage_attribution_commands (source, ref_id, command_name)"
                    " VALUES (?, ?, ?)",
                    [source, ref_id, n],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _delete(self, source: str, ref_id: str) -> None:
        self.conn.execute("BEGIN")
        try:
            for table in (
                "usage_attribution_skills",
                "usage_attribution_agents",
                "usage_attribution_commands",
            ):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE source = ? AND ref_id = ?",
                    [source, ref_id],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
