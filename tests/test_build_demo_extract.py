from pathlib import Path
import duckdb
from scripts.build_demo_extract import build_demo_extract


def test_build_demo_extract_contract(tmp_path):
    out = tmp_path / "extracts" / "demo"
    build_demo_extract(str(out))
    db = out / "extract.duckdb"
    assert db.exists()
    con = duckdb.connect(str(db), read_only=True)
    cols = [c[1] for c in con.execute("PRAGMA table_info('_meta')").fetchall()]
    assert cols == ["table_name", "description", "rows", "size_bytes", "extracted_at", "query_mode"]
    meta = con.execute("SELECT table_name, query_mode FROM _meta").fetchall()
    assert ("orders_demo", "local") in meta
    # every _meta row must resolve as a queryable view
    for (tname,) in con.execute("SELECT table_name FROM _meta").fetchall():
        n = con.execute(f'SELECT count(*) FROM "{tname}"').fetchone()[0]
        assert n > 0
    con.close()
