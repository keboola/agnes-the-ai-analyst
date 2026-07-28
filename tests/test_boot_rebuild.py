import os
from pathlib import Path
import duckdb

from app.main import _maybe_rebuild_on_boot


def _make_demo_extract(extracts_dir: Path) -> None:
    src = extracts_dir / "demo"
    (src / "data").mkdir(parents=True, exist_ok=True)
    pq = src / "data" / "t.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT 1 AS a, 'x' AS b")
    con.execute(f"COPY t TO '{pq}' (FORMAT parquet)")
    con.close()
    ext = duckdb.connect(str(src / "extract.duckdb"))
    ext.execute("CREATE TABLE _meta(table_name VARCHAR, description VARCHAR, rows BIGINT, size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR)")
    ext.execute("INSERT INTO _meta VALUES ('t','demo',1,0,now(),'local')")
    ext.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{pq}')")
    ext.close()


def test_rebuild_on_boot_gated_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGNES_REBUILD_ON_BOOT", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert _maybe_rebuild_on_boot() is False  # gate off → no-op


def test_rebuild_on_boot_builds_views(monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_REBUILD_ON_BOOT", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _make_demo_extract(tmp_path / "extracts")
    assert _maybe_rebuild_on_boot() is True
    analytics = tmp_path / "analytics" / "server.duckdb"
    con = duckdb.connect(str(analytics), read_only=True)
    names = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    con.close()
    assert "t" in names
