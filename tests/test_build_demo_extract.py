import os
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


def test_demo_extract_resolves_after_mount_relocation(tmp_path, monkeypatch):
    """The demo extract is baked at build time under one path and ATTACHed at
    runtime under a possibly different /data mount. Reproduce that: build the
    extract, relocate the whole extract dir, change CWD, then ATTACH + query
    through the *orchestrator's* access pattern (`SELECT * FROM <src>."<t>"`).

    A baked absolute (or CWD-relative) parquet path would fail here; the
    embedded-table form resolves with zero filesystem dependency.
    """
    build_dir = tmp_path / "build" / "extracts" / "demo"
    build_demo_extract(str(build_dir))

    # Relocate the entire extract directory to simulate a different runtime
    # mount, and delete the original data/ so any baked path to it is dead.
    runtime_dir = tmp_path / "runtime" / "extracts" / "demo"
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    build_dir.rename(runtime_dir)

    # Run from a CWD that contains no `data/` dir, so a CWD-relative
    # read_parquet('data/...') view would also fail to resolve.
    cwd_sandbox = tmp_path / "elsewhere"
    cwd_sandbox.mkdir()
    monkeypatch.chdir(cwd_sandbox)

    db = runtime_dir / "extract.duckdb"
    con = duckdb.connect()
    # Mirror src/orchestrator.py:_attach_and_create_views.
    con.execute(f"ATTACH '{db}' AS demo (READ_ONLY)")
    inner = {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_catalog='demo'"
        ).fetchall()
    }
    for (tname,) in con.execute("SELECT table_name FROM demo._meta").fetchall():
        assert tname in inner, f"{tname} has no inner object in extract.duckdb"
        con.execute(
            f'CREATE OR REPLACE VIEW "{tname}" AS SELECT * FROM demo."{tname}"'
        )
        n = con.execute(f'SELECT count(*) FROM "{tname}"').fetchone()[0]
        assert n > 0, f"master view for {tname} resolved to 0 rows after relocation"
    con.close()
