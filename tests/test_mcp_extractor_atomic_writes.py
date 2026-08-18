"""`connectors/mcp/extractor.py` publish sites (#1359):

* `_materialize_one_tool_async` (Group A) writes the successful-call result
  straight to the served ``<tool>.parquet`` path via ``df.to_parquet``.
* `_write_zero_row_parquet_like` (Group B) already staged through a temp +
  ``os.replace``, but with a SHARED (non-per-process) temp name and no
  ``chmod`` — the two defects `src.parquet_publish.atomic_publish` exists to
  prevent (incidents #1274 / #203).

Both are exercised directly (not through the full `extract_source` pipeline
`tests/test_mcp_extractor_empty_upstream.py` covers) so these tests stay fast
and focused on the publish contract. Modeled on `tests/test_parquet_publish.py`
/ `tests/test_jira_atomic_parquet_writes.py`: the failure stub writes the
footerless bytes a killed writer leaves AT WHATEVER PATH IT WAS GIVEN, then
raises — a stub that raises without touching the filesystem would pass
against the unfixed (direct-write) code too.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import connectors.mcp.client as mcp_client
from connectors.mcp import extractor as mcp_extractor
from connectors.mcp.client import ToolCallResult
from connectors.mcp.extractor import _write_zero_row_parquet_like

FOOTERLESS = b"PAR1" + b"\x00" * 64


def _boom_to_parquet(self, where, *a, **kw):
    Path(where).write_bytes(FOOTERLESS)
    raise OSError("disk full mid-write")


def _fake_upstream(monkeypatch, payload):
    async def _call(source, tool_name, arguments=None, *, caller_user_id=None):
        return ToolCallResult(text=json.dumps(payload), data=payload, is_error=False)

    monkeypatch.setattr(mcp_client, "call_tool_async", _call)


def _run_materialize(tmp_path):
    source = {"name": "crm"}
    tool = {"original_name": "list_accounts", "exposed_name": "accounts"}
    return asyncio.run(mcp_extractor._materialize_one_tool_async(source=source, tool=tool, output_path=tmp_path))


# --------------------------------------------------------------------------
# _materialize_one_tool_async — Group A, straight df.to_parquet onto the
# served path.
# --------------------------------------------------------------------------


def test_materialize_one_tool_publishes_the_rows(tmp_path, monkeypatch):
    _fake_upstream(monkeypatch, {"accounts": [{"id": "a-1"}, {"id": "a-2"}]})

    rows, size_bytes = _run_materialize(tmp_path)

    assert rows == 2
    pq_path = tmp_path / "data" / "accounts.parquet"
    assert pq_path.exists()
    assert size_bytes == pq_path.stat().st_size


def test_materialize_one_tool_killed_write_leaves_previous_publish_intact(tmp_path, monkeypatch):
    pq_path = tmp_path / "data" / "accounts.parquet"
    pq_path.parent.mkdir(parents=True)
    pq_path.write_bytes(b"previously published")

    _fake_upstream(monkeypatch, {"accounts": [{"id": "a-1"}]})
    monkeypatch.setattr(mcp_extractor.pd.DataFrame, "to_parquet", _boom_to_parquet)

    with pytest.raises(OSError):
        _run_materialize(tmp_path)

    assert pq_path.read_bytes() == b"previously published"


def test_materialize_one_tool_killed_write_leaves_no_temp_behind(tmp_path, monkeypatch):
    _fake_upstream(monkeypatch, {"accounts": [{"id": "a-1"}]})
    monkeypatch.setattr(mcp_extractor.pd.DataFrame, "to_parquet", _boom_to_parquet)

    with pytest.raises(OSError):
        _run_materialize(tmp_path)

    data_dir = tmp_path / "data"
    assert not (data_dir / "accounts.parquet").exists()
    assert list(data_dir.glob("*.tmp")) == []


def test_materialize_one_tool_published_mode_is_0644_under_restrictive_umask(tmp_path, monkeypatch):
    _fake_upstream(monkeypatch, {"accounts": [{"id": "a-1"}]})

    previous = os.umask(0o077)
    try:
        _run_materialize(tmp_path)
    finally:
        os.umask(previous)

    pq_path = tmp_path / "data" / "accounts.parquet"
    assert oct(pq_path.stat().st_mode & 0o777) == oct(0o644)


def test_materialize_one_tool_temp_is_per_process_and_never_matches_the_parquet_glob(tmp_path, monkeypatch):
    _fake_upstream(monkeypatch, {"accounts": [{"id": "a-1"}]})

    seen: list[Path] = []
    real = mcp_extractor.pd.DataFrame.to_parquet

    def _record(self, where, *a, **kw):
        seen.append(Path(where))
        return real(self, where, *a, **kw)

    monkeypatch.setattr(mcp_extractor.pd.DataFrame, "to_parquet", _record)
    _run_materialize(tmp_path)

    assert seen, "to_parquet was never called"
    assert seen[0].name != "accounts.parquet", "wrote straight onto the live path"
    assert str(os.getpid()) in seen[0].name
    data_dir = tmp_path / "data"
    assert [p.name for p in data_dir.glob("*.parquet")] == ["accounts.parquet"]
    assert not list(data_dir.glob("*.parquet.*"))


# --------------------------------------------------------------------------
# _write_zero_row_parquet_like — Group B, shared temp name + no chmod.
# --------------------------------------------------------------------------


def _seed_parquet(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows or [{"id": "placeholder"}])
    if not rows:
        table = table.slice(0, 0)
    pq.write_table(table, path)


def test_write_zero_row_resets_to_the_previous_schema_with_no_rows(tmp_path):
    pq_path = tmp_path / "accounts.parquet"
    _seed_parquet(pq_path, [{"id": "a-1"}, {"id": "a-2"}])

    size = _write_zero_row_parquet_like(pq_path)

    assert size == pq_path.stat().st_size
    table = pq.read_table(pq_path)
    assert table.num_rows == 0
    assert table.schema.field("id").type == pa.string()


def test_write_zero_row_killed_write_leaves_previous_publish_intact(tmp_path, monkeypatch):
    pq_path = tmp_path / "accounts.parquet"
    _seed_parquet(pq_path, [{"id": "a-1"}])
    original = pq_path.read_bytes()

    monkeypatch.setattr(pq, "write_table", _boom_write_table)
    with pytest.raises(OSError):
        _write_zero_row_parquet_like(pq_path)

    assert pq_path.read_bytes() == original


def test_write_zero_row_killed_write_leaves_no_temp_behind(tmp_path, monkeypatch):
    pq_path = tmp_path / "accounts.parquet"
    _seed_parquet(pq_path, [{"id": "a-1"}])

    monkeypatch.setattr(pq, "write_table", _boom_write_table)
    with pytest.raises(OSError):
        _write_zero_row_parquet_like(pq_path)

    assert list(tmp_path.glob("*.tmp")) == []


def test_write_zero_row_published_mode_is_0644_under_restrictive_umask(tmp_path):
    pq_path = tmp_path / "accounts.parquet"
    _seed_parquet(pq_path, [{"id": "a-1"}])

    previous = os.umask(0o077)
    try:
        _write_zero_row_parquet_like(pq_path)
    finally:
        os.umask(previous)

    assert oct(pq_path.stat().st_mode & 0o777) == oct(0o644)


def test_write_zero_row_temp_is_per_process_and_never_matches_the_parquet_glob(tmp_path, monkeypatch):
    pq_path = tmp_path / "accounts.parquet"
    _seed_parquet(pq_path, [{"id": "a-1"}])

    seen: list[Path] = []
    real = pq.write_table

    def _record(table, where, **kw):
        seen.append(Path(where))
        return real(table, where, **kw)

    monkeypatch.setattr(pq, "write_table", _record)
    _write_zero_row_parquet_like(pq_path)

    assert seen, "write_table was never called"
    assert seen[0].name != "accounts.parquet"
    assert str(os.getpid()) in seen[0].name
    # `*.parquet` must resolve to exactly the published table — the `.prev`
    # backup (`accounts.parquet.prev`) doesn't end in `.parquet` so it's not
    # in this glob, and any leftover temp must not be either.
    assert [p.name for p in tmp_path.glob("*.parquet")] == ["accounts.parquet"]
    assert not list(tmp_path.glob("*.tmp"))


def test_write_zero_row_still_retains_the_prev_snapshot_before_reset(tmp_path):
    """The `.prev` backup (a DIFFERENT, deliberate sibling — not the atomic
    publish temp) must still be written before the reset commits, unaffected
    by the temp-naming change."""
    pq_path = tmp_path / "accounts.parquet"
    _seed_parquet(pq_path, [{"id": "a-1"}, {"id": "a-2"}])

    _write_zero_row_parquet_like(pq_path)

    prev_path = tmp_path / "accounts.parquet.prev"
    assert prev_path.exists()
    assert pq.read_table(prev_path).num_rows == 2


def _boom_write_table(table, where, **kw):
    Path(where).write_bytes(FOOTERLESS)
    raise OSError("disk full mid-write")
