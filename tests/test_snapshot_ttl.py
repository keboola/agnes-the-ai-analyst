"""TTL expiry for local snapshots (#407).

Covers:
- `expires_at` is the LAST field on SnapshotMeta and defaults to None so a
  legacy `meta.json` (written before TTL existed) still deserializes.
- `sweep_expired_snapshots` removes only snapshots whose `expires_at` is in
  the past; `None` (no TTL) and future-dated snapshots survive.
- `agnes snapshot create --ttl 7d` stamps `expires_at ≈ now + 7d`.

All offline — no network, no live server.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from cli.snapshot_meta import (
    SnapshotMeta,
    write_meta,
    read_meta,
    list_snapshots,
    sweep_expired_snapshots,
)


@pytest.fixture
def snap_dir(tmp_path):
    d = tmp_path / "snapshots"
    d.mkdir()
    return d


def _make(snap_dir, name, *, expires_at):
    """Write a parquet + meta pair for `name` with the given expires_at."""
    (snap_dir / f"{name}.parquet").write_bytes(b"PAR1\x00\x00PAR1")
    write_meta(
        snap_dir,
        SnapshotMeta(
            name=name, table_id="t", select=None, where=None,
            limit=None, order_by=None,
            fetched_at="2026-01-01T00:00:00+00:00",
            effective_as_of="2026-01-01T00:00:00+00:00",
            rows=0, bytes_local=10,
            estimated_scan_bytes_at_fetch=0, result_hash_md5="",
            expires_at=expires_at,
        ),
    )


class TestExpiresAtField:
    def test_expires_at_defaults_to_none(self):
        """expires_at is optional — omitting it yields None (legacy-safe)."""
        meta = SnapshotMeta(
            name="x", table_id="t", select=None, where=None,
            limit=None, order_by=None,
            fetched_at="t", effective_as_of="t", rows=0, bytes_local=0,
            estimated_scan_bytes_at_fetch=0, result_hash_md5="",
        )
        assert meta.expires_at is None

    def test_legacy_meta_without_expires_at_still_loads(self, snap_dir):
        """A meta.json written before TTL existed has no `expires_at` key."""
        legacy = {
            "name": "legacy", "table_id": "t", "select": None, "where": None,
            "limit": None, "order_by": None,
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "effective_as_of": "2026-01-01T00:00:00+00:00",
            "rows": 5, "bytes_local": 100,
            "estimated_scan_bytes_at_fetch": 0, "result_hash_md5": "deadbeef",
        }
        (snap_dir / "legacy.meta.json").write_text(json.dumps(legacy), encoding="utf-8")
        got = read_meta(snap_dir, "legacy")
        assert got is not None
        assert got.name == "legacy"
        assert got.expires_at is None
        # list_snapshots tolerates it too
        assert [s.name for s in list_snapshots(snap_dir)] == ["legacy"]


class TestSweepExpired:
    def test_sweep_removes_only_expired(self, snap_dir):
        now = datetime.now(timezone.utc)
        past = (now - timedelta(days=1)).isoformat()
        future = (now + timedelta(days=7)).isoformat()

        _make(snap_dir, "expired", expires_at=past)
        _make(snap_dir, "no_ttl", expires_at=None)
        _make(snap_dir, "future", expires_at=future)

        swept = sweep_expired_snapshots(snap_dir)

        assert swept == ["expired"]
        # expired parquet + meta gone
        assert not (snap_dir / "expired.parquet").exists()
        assert not (snap_dir / "expired.meta.json").exists()
        # the other two survive untouched
        for keep in ("no_ttl", "future"):
            assert (snap_dir / f"{keep}.parquet").exists()
            assert (snap_dir / f"{keep}.meta.json").exists()
        survivors = sorted(s.name for s in list_snapshots(snap_dir))
        assert survivors == ["future", "no_ttl"]

    def test_sweep_empty_dir_is_noop(self, tmp_path):
        # non-existent dir → empty list, no crash
        assert sweep_expired_snapshots(tmp_path / "does-not-exist") == []

    def test_sweep_tolerates_unparsable_expires_at(self, snap_dir):
        """A garbage expires_at must not crash the sweep — leave it in place."""
        _make(snap_dir, "garbage", expires_at="not-a-date")
        assert sweep_expired_snapshots(snap_dir) == []
        assert (snap_dir / "garbage.parquet").exists()


class TestCreateTtl:
    def test_create_help_lists_ttl(self):
        from typer.testing import CliRunner
        from cli.commands.snapshot import snapshot_app
        import re as _re

        result = CliRunner().invoke(snapshot_app, ["create", "--help"])
        assert result.exit_code == 0
        assert "--ttl" in _re.sub(r"\x1b\[[0-9;]*m", "", result.output)

    def test_create_ttl_stamps_expires_at(self, tmp_path, monkeypatch):
        """`create --ttl 7d` writes expires_at ≈ now + 7d.

        We drive create_cmd() far enough to write meta without a live server
        by stubbing the estimate + arrow fetch and the DuckDB view registration.
        """
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))

        # Pre-create the local DuckDB so the bootstrap guard passes.
        db = tmp_path / "user" / "duckdb" / "analytics.duckdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"")

        table = pa.table({"a": [1, 2, 3]})
        monkeypatch.setattr(snap_mod, "api_post_arrow", lambda *a, **k: table)
        monkeypatch.setattr(snap_mod, "api_post_json", lambda *a, **k: {"estimated_scan_bytes": 0})

        class _FakeConn:
            def execute(self, *a, **k):
                return self

            def close(self):
                pass

        monkeypatch.setattr(snap_mod, "_open_duckdb", lambda *a, **k: _FakeConn())

        before = datetime.now(timezone.utc)
        snap_mod.create_cmd(
            table_id="t", select=None, where=None, limit=None, order_by=None,
            as_name="ttl_snap", estimate=False, no_estimate=True, force=False,
            ttl="7d",
        )
        after = datetime.now(timezone.utc)

        meta = read_meta(tmp_path / "user" / "snapshots", "ttl_snap")
        assert meta is not None
        assert meta.expires_at is not None
        exp = datetime.fromisoformat(meta.expires_at)
        # expires_at should sit within [before+7d, after+7d]
        assert before + timedelta(days=7) - timedelta(seconds=5) <= exp
        assert exp <= after + timedelta(days=7) + timedelta(seconds=5)

    def test_create_no_ttl_leaves_expires_at_none(self, tmp_path, monkeypatch):
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        db = tmp_path / "user" / "duckdb" / "analytics.duckdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"")

        table = pa.table({"a": [1]})
        monkeypatch.setattr(snap_mod, "api_post_arrow", lambda *a, **k: table)
        monkeypatch.setattr(snap_mod, "api_post_json", lambda *a, **k: {"estimated_scan_bytes": 0})

        class _FakeConn:
            def execute(self, *a, **k):
                return self

            def close(self):
                pass

        monkeypatch.setattr(snap_mod, "_open_duckdb", lambda *a, **k: _FakeConn())

        snap_mod.create_cmd(
            table_id="t", select=None, where=None, limit=None, order_by=None,
            as_name="plain", estimate=False, no_estimate=True, force=False,
            ttl=None,
        )

        meta = read_meta(tmp_path / "user" / "snapshots", "plain")
        assert meta is not None
        assert meta.expires_at is None


class TestPullLazySweep:
    """`agnes pull` sweeps expired snapshots before refreshing (#407)."""

    def _drive_pull(self, tmp_path, monkeypatch, args):
        from typer.testing import CliRunner
        import cli.commands.pull as pull_mod
        from cli.lib.pull import PullResult

        # Stub run_pull so the wrapper executes without network/disk churn.
        monkeypatch.setattr(
            pull_mod, "run_pull",
            lambda *a, **k: PullResult(duration_s=0.0),
        )
        monkeypatch.setenv("AGNES_SERVER", "http://localhost:0")
        monkeypatch.setenv("AGNES_TOKEN", "dummy")
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        return CliRunner().invoke(pull_mod.pull_app, args)

    def test_pull_sweeps_expired_snapshot(self, tmp_path, monkeypatch):
        snaps = tmp_path / "user" / "snapshots"
        snaps.mkdir(parents=True)
        _make(snaps, "expired", expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
        _make(snaps, "keep", expires_at=None)

        result = self._drive_pull(tmp_path, monkeypatch, [])
        assert result.exit_code == 0
        assert not (snaps / "expired.parquet").exists()
        assert (snaps / "keep.parquet").exists()
        # quiet notice goes to stderr (CliRunner merges into output)
        assert "swept expired snapshot: expired" in (result.output + (result.stderr or ""))

    def test_pull_dry_run_does_not_sweep(self, tmp_path, monkeypatch):
        snaps = tmp_path / "user" / "snapshots"
        snaps.mkdir(parents=True)
        _make(snaps, "expired", expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())

        result = self._drive_pull(tmp_path, monkeypatch, ["--dry-run"])
        assert result.exit_code == 0
        # --dry-run writes nothing — the expired snapshot survives.
        assert (snaps / "expired.parquet").exists()


class TestPruneExpired:
    """`agnes snapshot prune --expired` reuses the sweep helper (#407)."""

    def test_prune_expired_drops_only_expired(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from cli.commands.snapshot import snapshot_app

        snaps = tmp_path / "user" / "snapshots"
        snaps.mkdir(parents=True)
        _make(snaps, "old", expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        _make(snaps, "fresh", expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        _make(snaps, "forever", expires_at=None)

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        result = CliRunner().invoke(snapshot_app, ["prune", "--expired"])
        assert result.exit_code == 0
        assert "dropped: old" in result.output
        assert not (snaps / "old.parquet").exists()
        assert (snaps / "fresh.parquet").exists()
        assert (snaps / "forever.parquet").exists()

    def test_prune_expired_dry_run_keeps_files(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from cli.commands.snapshot import snapshot_app

        snaps = tmp_path / "user" / "snapshots"
        snaps.mkdir(parents=True)
        _make(snaps, "old", expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        result = CliRunner().invoke(snapshot_app, ["prune", "--expired", "--dry-run"])
        assert result.exit_code == 0
        assert "would drop: old" in result.output
        assert (snaps / "old.parquet").exists()
