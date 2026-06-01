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
    ext.execute(
        "CREATE TABLE _meta(table_name VARCHAR, description VARCHAR, rows BIGINT, "
        "size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR)"
    )
    for tname, desc in _TABLES.items():
        pq = data / f"{tname}.parquet"
        gen.execute(f"COPY {tname} TO '{pq}' (FORMAT parquet)")
        rows = gen.execute(f"SELECT count(*) FROM {tname}").fetchone()[0]
        size = os.path.getsize(pq)
        ext.execute(
            "INSERT INTO _meta VALUES (?, ?, ?, ?, now(), 'local')",
            [tname, desc, rows, size],
        )
        ext.execute(f"CREATE VIEW \"{tname}\" AS SELECT * FROM read_parquet('{pq}')")
    ext.close()
    gen.close()
    return str(db_path)


if __name__ == "__main__":
    target = os.environ.get("DEMO_EXTRACT_DIR", "/data/extracts/demo")
    print("Built demo extract at:", build_demo_extract(target))
