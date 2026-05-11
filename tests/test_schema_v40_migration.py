"""v39 → v40 migration: add params_before, client_ip, client_kind,
correlation_id columns to audit_log + three indices."""
import duckdb
from src.db import _ensure_schema as init_database, SCHEMA_VERSION


def test_schema_version_is_40():
    assert SCHEMA_VERSION == 40


def test_v40_columns_exist_after_init(tmp_path):
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
    assert "params_before" in cols
    assert "client_ip" in cols
    assert "client_kind" in cols
    assert "correlation_id" in cols
    conn.close()


def test_v40_indices_exist(tmp_path):
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    idx_names = {row[0] for row in conn.execute(
        "SELECT index_name FROM duckdb_indexes WHERE table_name='audit_log'"
    ).fetchall()}
    assert "idx_audit_timestamp_desc" in idx_names
    assert "idx_audit_user_time" in idx_names
    assert "idx_audit_action_time" in idx_names
    conn.close()


def test_v39_to_v40_is_idempotent(tmp_path):
    """Running the migration twice in a row is a no-op the second time."""
    db_path = tmp_path / "twice.duckdb"
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    conn.close()
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == 40
    conn.close()


def test_v30_db_ladders_all_the_way_up(tmp_path):
    """Representative evolved-DB test: an instance hand-rolled at v30 must
    ladder through to v40 without data loss, mirroring a customer who's
    been upgrading regularly since older releases."""
    db_path = tmp_path / "v30.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("""
        CREATE TABLE audit_log (
            id VARCHAR PRIMARY KEY,
            timestamp TIMESTAMP NOT NULL DEFAULT current_timestamp,
            user_id VARCHAR,
            action VARCHAR NOT NULL,
            resource VARCHAR,
            params JSON,
            result VARCHAR,
            duration_ms INTEGER
        )
    """)
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER, "
        "applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (30)")
    conn.execute("INSERT INTO audit_log (id, action) VALUES ('vintage', 'test.x')")
    conn.close()

    conn = duckdb.connect(str(db_path))
    init_database(conn)
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == 40
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE id='vintage'").fetchone()[0] == 1
    conn.close()


def test_v39_db_upgrades_cleanly(tmp_path):
    """A DB hand-rolled at v39 (audit_log without the four new columns)
    must upgrade to v40 without data loss."""
    db_path = tmp_path / "v39.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("""
        CREATE TABLE audit_log (
            id VARCHAR PRIMARY KEY,
            timestamp TIMESTAMP NOT NULL DEFAULT current_timestamp,
            user_id VARCHAR,
            action VARCHAR NOT NULL,
            resource VARCHAR,
            params JSON,
            result VARCHAR,
            duration_ms INTEGER
        )
    """)
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER, "
        "applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (39)")
    conn.execute("INSERT INTO audit_log (id, action) VALUES ('row1', 'test.action')")
    conn.close()

    conn = duckdb.connect(str(db_path))
    init_database(conn)
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == 40
    cnt = conn.execute("SELECT COUNT(*) FROM audit_log WHERE id='row1'").fetchone()[0]
    assert cnt == 1
    row = conn.execute(
        "SELECT params_before, client_ip, client_kind, correlation_id FROM audit_log WHERE id='row1'"
    ).fetchone()
    assert row == (None, None, None, None)
    conn.close()
