"""Issue #81 Group C — view-name collision detection across connectors.

Two connectors with overlapping `_meta.table_name` used to silently
overwrite each other in the master analytics DB. This file exercises:

- Schema v10's `view_ownership` table exists after migration.
- `ViewOwnershipRepository.claim` is first-come-first-served.
- `ViewOwnershipRepository.reconcile` releases stale ownerships.
- The orchestrator refuses to overwrite a view owned by a different
  source, logs an ERROR, but keeps publishing views for the winner.
"""

from __future__ import annotations

import os
import duckdb
import pytest

from src.repositories.view_ownership import ViewOwnershipRepository


# --------------------------------------------------------------------------
# Repository unit tests
# --------------------------------------------------------------------------


@pytest.fixture
def fresh_system_db(tmp_path, monkeypatch):
    """Set DATA_DIR to a fresh temp dir and trigger a v10 schema build."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db
    conn = get_system_db()
    yield conn
    conn.close()


class TestViewOwnershipRepository:
    def test_claim_first_succeeds(self, fresh_system_db):
        repo = ViewOwnershipRepository(fresh_system_db)
        assert repo.claim("orders", "keboola") is True
        assert repo.get_owner("orders") == "keboola"

    def test_claim_same_source_idempotent(self, fresh_system_db):
        repo = ViewOwnershipRepository(fresh_system_db)
        repo.claim("orders", "keboola")
        # Re-claiming by the same source is fine — rebuild is idempotent.
        assert repo.claim("orders", "keboola") is True

    def test_claim_different_source_refused(self, fresh_system_db):
        repo = ViewOwnershipRepository(fresh_system_db)
        repo.claim("orders", "keboola")
        # Second source asks for the same name — refused.
        assert repo.claim("orders", "bigquery") is False
        # Original owner unchanged.
        assert repo.get_owner("orders") == "keboola"

    def test_release(self, fresh_system_db):
        repo = ViewOwnershipRepository(fresh_system_db)
        repo.claim("orders", "keboola")
        assert repo.release("orders", "keboola") is True
        assert repo.get_owner("orders") is None

    def test_release_wrong_source_no_op(self, fresh_system_db):
        repo = ViewOwnershipRepository(fresh_system_db)
        repo.claim("orders", "keboola")
        # Release should not delete a row owned by a different source.
        assert repo.release("orders", "bigquery") is False
        assert repo.get_owner("orders") == "keboola"

    def test_reconcile_drops_stale_pairs(self, fresh_system_db):
        repo = ViewOwnershipRepository(fresh_system_db)
        repo.claim("orders", "keboola")
        repo.claim("users", "keboola")
        repo.claim("traffic", "bigquery")

        # Next rebuild claims orders + traffic only — users should be released.
        live = [("keboola", "orders"), ("bigquery", "traffic")]
        dropped = repo.reconcile(live)
        assert dropped == [("keboola", "users")]
        assert repo.get_owner("users") is None
        assert repo.get_owner("orders") == "keboola"
        assert repo.get_owner("traffic") == "bigquery"

    def test_list_for_source(self, fresh_system_db):
        repo = ViewOwnershipRepository(fresh_system_db)
        repo.claim("orders", "keboola")
        repo.claim("users", "keboola")
        repo.claim("traffic", "bigquery")
        assert repo.list_for_source("keboola") == ["orders", "users"]
        assert repo.list_for_source("bigquery") == ["traffic"]


# --------------------------------------------------------------------------
# Orchestrator behaviour test
# --------------------------------------------------------------------------


def _make_extract_db(path: str, table_names: list[str]) -> None:
    """Create a minimal extract.duckdb with `_meta` rows + a view per table.

    Returns nothing — the file at `path` is the connector's output.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Recreate from scratch — round-2 tests rebuild the same connector dir
    # with different tables, so wipe any prior file.
    if os.path.exists(path):
        os.unlink(path)
    wal = path + ".wal"
    if os.path.exists(wal):
        os.unlink(wal)
    conn = duckdb.connect(path)
    conn.execute(
        "CREATE TABLE _meta ("
        "table_name VARCHAR, description VARCHAR, rows BIGINT, "
        "size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR)"
    )
    for t in table_names:
        # Each table is a tiny in-memory CTAS — query_mode='local' so the
        # orchestrator picks it up the same way it does parquet-backed views.
        conn.execute(f'CREATE TABLE "{t}" AS SELECT 1 AS x')
        conn.execute(
            "INSERT INTO _meta VALUES (?, ?, 1, 0, current_timestamp, 'local')",
            [t, ""],
        )
    conn.close()


class TestOrchestratorCollisionRefusal:
    def test_first_source_wins_second_source_skipped(
        self, tmp_path, monkeypatch
    ):
        """Two sources both publish a view named `orders`. The first one
        the orchestrator visits (alphabetical order) keeps the name; the
        other is logged as a collision and its view is NOT created."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        # Sources visited alphabetically: alpha first, beta second.
        _make_extract_db(
            str(tmp_path / "extracts" / "alpha" / "extract.duckdb"),
            ["orders", "alpha_only"],
        )
        _make_extract_db(
            str(tmp_path / "extracts" / "beta" / "extract.duckdb"),
            ["orders", "beta_only"],
        )

        from src.orchestrator import SyncOrchestrator
        orch = SyncOrchestrator()
        result = orch.rebuild()

        # alpha got both its views (it ran first); beta only got its non-colliding one.
        assert "orders" in result["alpha"]
        assert "alpha_only" in result["alpha"]
        assert "orders" not in result["beta"], (
            "beta should NOT have published a colliding `orders` view"
        )
        assert "beta_only" in result["beta"]

        # Ownership records persisted in system DB.
        from src.db import get_system_db
        sys_conn = get_system_db()
        try:
            repo = ViewOwnershipRepository(sys_conn)
            assert repo.get_owner("orders") == "alpha"
            assert repo.get_owner("alpha_only") == "alpha"
            assert repo.get_owner("beta_only") == "beta"
        finally:
            sys_conn.close()

    def test_partial_collision_does_not_block_other_tables(
        self, tmp_path, monkeypatch
    ):
        """Source A publishes [orders, alpha_only_a, alpha_only_b]; source
        B publishes [orders, beta_only]. The collision on `orders` (A wins,
        first by alphabet) must NOT prevent B from publishing `beta_only`,
        nor prevent A from publishing its other two."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        _make_extract_db(
            str(tmp_path / "extracts" / "alpha" / "extract.duckdb"),
            ["orders", "alpha_only_a", "alpha_only_b"],
        )
        _make_extract_db(
            str(tmp_path / "extracts" / "beta" / "extract.duckdb"),
            ["orders", "beta_only"],
        )

        from src.orchestrator import SyncOrchestrator
        result = SyncOrchestrator().rebuild()

        assert set(result["alpha"]) == {"orders", "alpha_only_a", "alpha_only_b"}
        assert set(result["beta"]) == {"beta_only"}

    def test_pre_scan_failure_does_not_release_ownership(
        self, tmp_path, monkeypatch
    ):
        """When `_scan_meta_pairs` cannot read source B (corrupt
        extract.duckdb, transient I/O), the orchestrator must SKIP
        reconcile this rebuild — otherwise B's name would be released and
        another source could silently steal it. Issue #81 Group C
        review-2."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        # Round 1: alpha owns orders.
        _make_extract_db(
            str(tmp_path / "extracts" / "alpha" / "extract.duckdb"),
            ["orders"],
        )
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        # Round 2: alpha's extract.duckdb is unreadable (simulate corrupt
        # file by writing garbage). beta now publishes `orders`. The
        # reconcile should be SKIPPED because the scan was incomplete;
        # alpha keeps ownership; beta is refused.
        alpha_db = tmp_path / "extracts" / "alpha" / "extract.duckdb"
        alpha_wal = tmp_path / "extracts" / "alpha" / "extract.duckdb.wal"
        alpha_db.write_bytes(b"NOT A REAL DUCKDB FILE")
        if alpha_wal.exists():
            alpha_wal.unlink()
        _make_extract_db(
            str(tmp_path / "extracts" / "beta" / "extract.duckdb"),
            ["orders"],
        )
        result = SyncOrchestrator().rebuild()

        # alpha did not contribute a view (file unreadable); beta did NOT
        # get to claim `orders` because reconcile was skipped.
        assert "orders" not in result.get("beta", []), (
            "beta should have been refused `orders` — reconcile must skip "
            "when pre-scan is incomplete"
        )

        # Ownership unchanged.
        from src.db import get_system_db
        sys_conn = get_system_db()
        try:
            repo = ViewOwnershipRepository(sys_conn)
            assert repo.get_owner("orders") == "alpha"
        finally:
            sys_conn.close()

    def test_owner_releases_name_after_rename(self, tmp_path, monkeypatch):
        """If the previous owner of a name no longer publishes it, the
        next rebuild releases the name — a different source can then
        claim it without operator intervention."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        # Round 1: alpha owns orders.
        _make_extract_db(
            str(tmp_path / "extracts" / "alpha" / "extract.duckdb"),
            ["orders"],
        )
        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator().rebuild()

        # Round 2: alpha renames its table to alpha_orders, beta wants `orders`.
        _make_extract_db(
            str(tmp_path / "extracts" / "alpha" / "extract.duckdb"),
            ["alpha_orders"],
        )
        _make_extract_db(
            str(tmp_path / "extracts" / "beta" / "extract.duckdb"),
            ["orders"],
        )
        result = SyncOrchestrator().rebuild()

        assert "alpha_orders" in result["alpha"]
        assert "orders" in result["beta"], (
            "beta should now own `orders` after alpha released it"
        )

        from src.db import get_system_db
        sys_conn = get_system_db()
        try:
            repo = ViewOwnershipRepository(sys_conn)
            assert repo.get_owner("orders") == "beta"
            assert repo.get_owner("alpha_orders") == "alpha"
        finally:
            sys_conn.close()
