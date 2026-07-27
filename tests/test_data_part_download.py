"""Part-path resolution for partitioned-table downloads (partitioned
distribution). `_resolve_part_path` maps a manifest `part` relpath to a
safe on-disk file under the table's data dir, rejecting path traversal.
"""
from __future__ import annotations


def _mk_table(tmp_path):
    tdir = tmp_path / "extracts" / "jira" / "data" / "issues" / "month=2026-06"
    tdir.mkdir(parents=True)
    (tdir / "data.parquet").write_bytes(b"hello-part")
    (tmp_path / "secret.txt").write_bytes(b"TOPSECRET")
    return tmp_path / "extracts"


def test_resolve_valid_hive_part(tmp_path):
    from app.api.data import _resolve_part_path
    extracts = _mk_table(tmp_path)
    p = _resolve_part_path(extracts, "issues", "month=2026-06/data.parquet")
    assert p is not None and p.read_bytes() == b"hello-part"


def test_resolve_rejects_traversal(tmp_path):
    from app.api.data import _resolve_part_path
    extracts = _mk_table(tmp_path)
    for bad in [
        "../../../secret.txt",
        "/etc/passwd",
        "month=2026-06/../../../../secret.txt",
        "..",
        "month=2026-06/..%2f..%2fsecret.txt",  # url-encoded slashes as literal
        "month=2026-06/\\..\\secret.txt",
    ]:
        assert _resolve_part_path(extracts, "issues", bad) is None, bad


def test_resolve_missing_part_is_none(tmp_path):
    from app.api.data import _resolve_part_path
    extracts = _mk_table(tmp_path)
    assert _resolve_part_path(extracts, "issues", "month=2099-01/data.parquet") is None


def test_resolve_unknown_table_is_none(tmp_path):
    from app.api.data import _resolve_part_path
    extracts = _mk_table(tmp_path)
    assert _resolve_part_path(extracts, "nope", "month=2026-06/data.parquet") is None
