"""Postgres-backed repository for ``data_apps``.

Mirrors ``src/repositories/data_apps.py`` (the DuckDB impl) on the
``DataAppsRepository`` public surface. Cross-engine parity is covered by
``tests/db_pg/test_data_apps_contract.py``.

Implementation differences vs. DuckDB:

- ``list_idle``'s idle-window predicate uses ``make_interval(secs => ...)``
  in place of DuckDB's ``? * INTERVAL 1 SECOND`` arithmetic — same
  semantics, PG flavor.
- Slug uniqueness surfaces as ``sqlalchemy.exc.IntegrityError`` (UNIQUE
  constraint violation) rather than ``duckdb.ConstraintException``; the
  contract test asserts on the per-backend exception type.
- ``update``/``delete`` use ``rowcount`` to report success rather than a
  pre-SELECT existence check, following the ``memory_domains_pg`` idiom.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine

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


class DataAppsPgRepository:
    """Postgres twin of ``DataAppsRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

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

        Raises ``sqlalchemy.exc.IntegrityError`` if ``slug`` collides — the
        UNIQUE constraint on ``data_apps.slug`` is the source of truth.
        """
        app_id = "app_" + uuid4().hex[:12]
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO data_apps
                      (id, slug, name, description, owner_user_id, repo_mode,
                       repo_url, repo_branch, idle_timeout_s, sleep_mode, env)
                    VALUES
                      (:id, :slug, :name, :description, :owner_user_id, :repo_mode,
                       :repo_url, :repo_branch, :idle_timeout_s, :sleep_mode, :env)
                    """
                ),
                {
                    "id": app_id,
                    "slug": slug,
                    "name": name,
                    "description": description,
                    "owner_user_id": owner_user_id,
                    "repo_mode": repo_mode,
                    "repo_url": repo_url,
                    "repo_branch": repo_branch,
                    "idle_timeout_s": idle_timeout_s,
                    "sleep_mode": sleep_mode,
                    "env": env,
                },
            )
        return app_id

    def get(self, app_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM data_apps WHERE id = :id"),
                    {"id": app_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM data_apps WHERE slug = :slug"),
                    {"slug": slug},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def list(
        self, *, owner_user_id: Optional[str] = None, state: Optional[str] = None, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: Dict[str, Any] = {"limit": limit}
        if owner_user_id is not None:
            clauses.append("owner_user_id = :owner_user_id")
            params["owner_user_id"] = owner_user_id
        if state is not None:
            clauses.append("state = :state")
            params["state"] = state
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(f"SELECT * FROM data_apps {where} ORDER BY created_at DESC LIMIT :limit"),
                    params,
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def update(self, app_id: str, **fields: Any) -> bool:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"non-updatable fields: {sorted(bad)}")
        if not fields:
            return False
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        params = {**fields, "id": app_id}
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(f"UPDATE data_apps SET {sets}, updated_at = now() WHERE id = :id"),
                params,
            )
            return (result.rowcount or 0) > 0

    def set_state(self, app_id: str, state: str, detail: str = "") -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE data_apps SET state = :state, state_detail = :detail, updated_at = now() WHERE id = :id"
                ),
                {"state": state, "detail": detail, "id": app_id},
            )

    def record_deploy(self, app_id: str, sha: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE data_apps SET deployed_sha = :sha, last_deploy_at = now(), "
                    "updated_at = now() WHERE id = :id"
                ),
                {"sha": sha, "id": app_id},
            )

    def touch_last_request(self, app_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE data_apps SET last_request_at = now() WHERE id = :id"),
                {"id": app_id},
            )

    def list_idle(self, older_than_s: int) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM data_apps WHERE state = 'running' "
                        "AND last_request_at IS NOT NULL "
                        "AND last_request_at < now() - make_interval(secs => :older)"
                    ),
                    {"older": older_than_s},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def delete(self, app_id: str) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM data_apps WHERE id = :id"),
                {"id": app_id},
            )
            return (result.rowcount or 0) > 0
