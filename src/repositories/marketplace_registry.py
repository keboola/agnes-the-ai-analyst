"""Repository for marketplace registry.

Mirrors TableRegistryRepository. One row per marketplace git repo that the
nightly sync should clone/update into ${DATA_DIR}/marketplaces/<slug>/.

Tokens never live here — only the name of the env var (`token_env`) that
holds the PAT. The secret itself is persisted to data/state/.env_overlay
by the admin API, same pattern as Keboola/BigQuery secrets.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb


class MarketplaceRegistryRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def register(
        self,
        id: str,
        name: str,
        url: str,
        branch: Optional[str] = None,
        token_env: Optional[str] = None,
        description: Optional[str] = None,
        registered_by: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO marketplace_registry
                (id, name, url, branch, token_env, description, registered_by, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name,
                url = excluded.url,
                branch = excluded.branch,
                token_env = excluded.token_env,
                description = excluded.description""",
            [id, name, url, branch, token_env, description, registered_by, now],
        )

    def unregister(self, marketplace_id: str) -> None:
        self.conn.execute(
            "DELETE FROM marketplace_registry WHERE id = ?", [marketplace_id]
        )

    def get(self, marketplace_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM marketplace_registry WHERE id = ?", [marketplace_id]
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def list_all(self) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM marketplace_registry ORDER BY name"
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def update_sync_status(
        self,
        marketplace_id: str,
        *,
        commit_sha: Optional[str] = None,
        synced_at: Optional[datetime] = None,
        error: Optional[str] = None,
    ) -> None:
        """Update last_synced_at / last_commit_sha / last_error after a sync attempt.

        Passing None for any field leaves it untouched — except `error`,
        which is always written (clear on success by passing error=None AND
        a non-None synced_at/commit_sha will null out the error column).
        """
        sets = []
        params: List[Any] = []
        if synced_at is not None:
            sets.append("last_synced_at = ?")
            params.append(synced_at)
        if commit_sha is not None:
            sets.append("last_commit_sha = ?")
            params.append(commit_sha)
        # last_error: clear on success (commit_sha present), otherwise write provided value
        if commit_sha is not None and error is None:
            sets.append("last_error = NULL")
        elif error is not None:
            sets.append("last_error = ?")
            params.append(error)
        if not sets:
            return
        params.append(marketplace_id)
        self.conn.execute(
            f"UPDATE marketplace_registry SET {', '.join(sets)} WHERE id = ?",
            params,
        )
