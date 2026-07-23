"""Repository for ``data_apps`` (v96) — the hosted user web apps registry.

Task 1 of the Data Apps feature: a data app is a user-owned web app hosted
by Agnes (internal template or an external git repo), deployed to a
runtime container, and put to sleep after an idle timeout. This repo is
the foundation everything else in the feature builds on — deploy
orchestration, idle-reaper, and the admin/API surface all go through
``data_apps_repo()``.

``create`` follows the same ``<prefix>_<uuid12>`` id-generation idiom as
``MemoryDomainsRepository`` (``md_``) — here ``app_``. Slug uniqueness is
enforced by the ``UNIQUE`` constraint on the column, surfaced as
``duckdb.ConstraintException`` on collision (see ``memory_domains.create``
for the same pattern).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import duckdb

_UPDATABLE = {
    "name",
    "description",
    "repo_url",
    "repo_branch",
    "runtime_tag",
    "secrets_enc",
    "env",
    "cpu_limit",
    "mem_limit",
    "idle_timeout_s",
    "sleep_mode",
    "service_token_id",
}


class DataAppsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    _COLS = [
        "id",
        "slug",
        "name",
        "description",
        "owner_user_id",
        "repo_mode",
        "repo_url",
        "repo_branch",
        "deployed_sha",
        "runtime_tag",
        "state",
        "state_detail",
        "secrets_enc",
        "env",
        "cpu_limit",
        "mem_limit",
        "idle_timeout_s",
        "sleep_mode",
        "service_token_id",
        "last_request_at",
        "last_deploy_at",
        "created_at",
        "updated_at",
    ]
    _SELECT = ", ".join(_COLS)

    def create(
        self,
        *,
        slug: str,
        name: str,
        owner_user_id: str,
        description: str = "",
        repo_mode: str = "internal",
        repo_url: str = "",
        repo_branch: str = "main",
        idle_timeout_s: int = 1800,
        sleep_mode: str = "recreate",
        env: str = "{}",
    ) -> str:
        """Insert a new data app; returns the generated id (``app_<uuid12>``).

        Raises ``duckdb.ConstraintException`` if ``slug`` collides — UNIQUE
        on the column is the source of truth.
        """
        app_id = "app_" + uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO data_apps"
            "(id, slug, name, description, owner_user_id, repo_mode,"
            " repo_url, repo_branch, idle_timeout_s, sleep_mode, env) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                app_id,
                slug,
                name,
                description,
                owner_user_id,
                repo_mode,
                repo_url,
                repo_branch,
                idle_timeout_s,
                sleep_mode,
                env,
            ],
        )
        return app_id

    def get(self, app_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(f"SELECT {self._SELECT} FROM data_apps WHERE id = ?", [app_id]).fetchone()
        return dict(zip(self._COLS, row)) if row else None

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(f"SELECT {self._SELECT} FROM data_apps WHERE slug = ?", [slug]).fetchone()
        return dict(zip(self._COLS, row)) if row else None

    def list(
        self, *, owner_user_id: Optional[str] = None, state: Optional[str] = None, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            params.append(owner_user_id)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM data_apps {where} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def update(self, app_id: str, **fields: Any) -> bool:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"non-updatable fields: {sorted(bad)}")
        if not fields:
            return False
        if self.get(app_id) is None:
            return False
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE data_apps SET {sets}, updated_at = now() WHERE id = ?", [*fields.values(), app_id])
        return True

    def set_state(self, app_id: str, state: str, detail: str = "") -> None:
        self.conn.execute(
            "UPDATE data_apps SET state = ?, state_detail = ?, updated_at = now() WHERE id = ?", [state, detail, app_id]
        )

    def record_deploy(self, app_id: str, sha: str) -> None:
        self.conn.execute(
            "UPDATE data_apps SET deployed_sha = ?, last_deploy_at = now(), updated_at = now() WHERE id = ?",
            [sha, app_id],
        )

    def touch_last_request(self, app_id: str) -> None:
        self.conn.execute("UPDATE data_apps SET last_request_at = now() WHERE id = ?", [app_id])

    def list_idle(self, older_than_s: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM data_apps WHERE state = 'running' "
            "AND last_request_at IS NOT NULL "
            "AND last_request_at < now() - (? * INTERVAL 1 SECOND)",
            [older_than_s],
        ).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def delete(self, app_id: str) -> bool:
        existed = self.get(app_id) is not None
        self.conn.execute("DELETE FROM data_apps WHERE id = ?", [app_id])
        return existed
