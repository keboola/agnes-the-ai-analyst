"""Generate a self-contained demo extract.duckdb honoring the connector
_meta contract. Baked into the demo image; ATTACHed by the orchestrator at
boot (AGNES_REBUILD_ON_BOOT). Vendor-neutral synthetic data only."""

from __future__ import annotations
import os
from pathlib import Path
import duckdb

_TABLES = {
    "orders_demo": "Synthetic orders for the demo instance.",
    "customers_demo": "Synthetic customers for the demo instance.",
}


def build_demo_extract(out_dir: str) -> str:
    out = Path(out_dir)
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    db_path = out / "extract.duckdb"
    if db_path.exists():
        db_path.unlink()

    gen = duckdb.connect()
    # Standalone script: pin the session timezone to UTC so the `now()`
    # written into _meta.extracted_at is stored UTC-naive (matches the
    # server's pinned DB). See src/duckdb_conn._open_duckdb.
    gen.execute("SET GLOBAL TimeZone='UTC'")
    gen.execute(
        "CREATE TABLE orders_demo AS "
        "SELECT i AS order_id, (i % 500) AS customer_id, "
        "       DATE '2026-01-01' + CAST(i % 120 AS INTEGER) AS order_date, "
        "       round(10 + (i * 7 % 990) / 10.0, 2) AS amount "
        "FROM range(5000) t(i)"
    )
    gen.execute(
        "CREATE TABLE customers_demo AS "
        "SELECT i AS customer_id, 'Customer ' || i AS name, "
        "       ['CZ','US','DE','GB'][1 + (i % 4)] AS country "
        "FROM range(500) t(i)"
    )

    ext = duckdb.connect(str(db_path))
    ext.execute("SET GLOBAL TimeZone='UTC'")
    ext.execute(
        "CREATE TABLE _meta(table_name VARCHAR, description VARCHAR, rows BIGINT, "
        "size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR)"
    )
    for tname, desc in _TABLES.items():
        pq = data / f"{tname}.parquet"
        # Emit the parquet for distribution (agnes pull) + sync_state hashing,
        # exactly like a real connector's local-mode output.
        gen.execute(f"COPY {tname} TO '{pq}' (FORMAT parquet)")
        rows = gen.execute(f"SELECT count(*) FROM {tname}").fetchone()[0]
        size = os.path.getsize(pq)
        ext.execute(
            "INSERT INTO _meta VALUES (?, ?, ?, ?, now(), 'local')",
            [tname, desc, rows, size],
        )
        # The orchestrator exposes a master view as `SELECT * FROM <source>."<tname>"`,
        # i.e. it queries this inner object — it never re-reads the parquet path
        # baked here. Real connectors back the inner object with
        # `read_parquet('<abs path>')`, which is fine for them because the parquet
        # lives under the same live DATA_DIR they query from. The demo extract is
        # different: it is baked into the image at *build* time (DEMO_EXTRACT_DIR,
        # /data/extracts/demo) and ATTACHed at *boot*, so any baked absolute (or
        # CWD-relative) parquet path breaks the moment the runtime /data mount or
        # DATA_DIR differs from build time (DuckDB resolves a view's relative
        # read_parquet against the process CWD, not the extract.duckdb location).
        # Embed the data as a real table inside extract.duckdb instead: the inner
        # object is then self-contained and mount-independent, while the parquet
        # on disk still serves distribution. Read back from the parquet so the
        # embedded table is byte-for-byte the distributed data.
        safe_pq = str(pq).replace("'", "''")
        ext.execute(
            f'CREATE TABLE "{tname}" AS SELECT * FROM read_parquet(\'{safe_pq}\')'
        )
    ext.close()
    gen.close()
    return str(db_path)


if __name__ == "__main__":
    target = os.environ.get("DEMO_EXTRACT_DIR", "/data/extracts/demo")
    print("Built demo extract at:", build_demo_extract(target))
