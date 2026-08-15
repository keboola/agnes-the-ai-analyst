"""`app.utils.resolve_local_parquet_glob` must not stay silent when a table
has BOTH a flat `<table>.parquet` file and a `<table>/` partition directory
at once (#1339).

The flat file wins — unchanged by this fix, see the TODO(#1339) at the
detection site — but `/api/v2/schema`, `/api/v2/scan` and the catalog all
resolve through this helper, so a silent flat-file win can serve stale data
indefinitely with no operator-visible signal. This pins the ERROR log and
the unchanged return value (served bytes identical to before).
"""

import logging

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    d = tmp_path / "extracts" / "keboola" / "data"
    d.mkdir(parents=True)
    return d


def test_both_layouts_logs_error_and_still_serves_the_flat_file(data_dir, caplog):
    flat = data_dir / "orders.parquet"
    flat.write_bytes(b"flat-bytes")
    part_dir = data_dir / "orders"
    part_dir.mkdir()
    (part_dir / "2025_11.parquet").write_bytes(b"partitioned-bytes")

    from app.utils import resolve_local_parquet_glob

    with caplog.at_level(logging.ERROR, logger="app.utils"):
        result = resolve_local_parquet_glob("orders", "keboola")

    # Served target unchanged: the flat file still wins.
    assert result == str(flat)

    collision_records = [
        r
        for r in caplog.records
        if r.levelname == "ERROR"
        and "orders" in r.getMessage()
        and str(flat) in r.getMessage()
        and str(part_dir) in r.getMessage()
    ]
    assert collision_records, (
        "expected an ERROR log naming the table id and BOTH concrete paths; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )


def test_both_layouts_source_type_agnostic_lookup_also_detects_the_collision(data_dir, caplog):
    """The `source_type`-omitted (rglob fallback) path must detect the
    collision too — the detection follows wherever `single` resolved."""
    flat = data_dir / "orders.parquet"
    flat.write_bytes(b"flat-bytes")
    part_dir = data_dir / "orders"
    part_dir.mkdir()
    (part_dir / "2025_11.parquet").write_bytes(b"partitioned-bytes")

    from app.utils import resolve_local_parquet_glob

    with caplog.at_level(logging.ERROR, logger="app.utils"):
        result = resolve_local_parquet_glob("orders")

    assert result == str(flat)
    assert [r for r in caplog.records if r.levelname == "ERROR" and "orders" in r.getMessage()]


def test_flat_only_is_not_flagged_as_a_collision(data_dir, caplog):
    """Regression pin: the ordinary single-file case must keep behaving
    exactly as before."""
    flat = data_dir / "orders.parquet"
    flat.write_bytes(b"flat-bytes")

    from app.utils import resolve_local_parquet_glob

    with caplog.at_level(logging.ERROR, logger="app.utils"):
        result = resolve_local_parquet_glob("orders", "keboola")

    assert result == str(flat)
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_dir_only_is_not_flagged_as_a_collision(data_dir, caplog):
    """Regression pin: the ordinary partitioned-only case (no flat sibling)
    must keep behaving exactly as before."""
    part_dir = data_dir / "orders"
    part_dir.mkdir()
    (part_dir / "2025_11.parquet").write_bytes(b"partitioned-bytes")

    from app.utils import resolve_local_parquet_glob

    with caplog.at_level(logging.ERROR, logger="app.utils"):
        result = resolve_local_parquet_glob("orders", "keboola")

    assert result == str(part_dir / "*.parquet")
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
