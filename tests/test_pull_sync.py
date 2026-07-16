"""Tests for ``cli/lib/pull_sync.py`` — per-type sync engine (Phase 7,
Task 7.5).

Covers Section 10.3 of the unified-stack spec:

  - First pull from empty.
  - Add package with overlap (shared parquet reused).
  - Remove package no overlap.
  - Remove package with overlap (shared parquet retained).
  - MD5 update.
  - Idempotent re-pull.
  - Orphan parquet detection.
  - Broken symlink auto-heal.
  - Windows fallback (symlink → hardlink → copy strategy tracking).
  - Memory bundle write + md5 short-circuit.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Dict, List
from unittest.mock import patch

import pytest

from cli.lib.pull_sync import (
    PullStackOptions,
    SyncReport,
    _count_references,
    _link_or_copy,
    audit_invariants,
    run_stack_sync,
    sync_data_packages,
    sync_direct_tables,
    sync_memory_domains,
)


# ---------------------------------------------------------------------------
# Fake server fixtures
# ---------------------------------------------------------------------------


class _FakeServer:
    """In-memory parquet + bundle catalog. ``fetcher`` writes bytes; the
    canonical "parquet body" is just ``b"PAR1" + table_id`` so md5s differ
    per id deterministically."""

    def __init__(self):
        self.fetch_calls: List[tuple] = []
        self.bundle_calls: List[str] = []
        # Map url → bytes; if missing, the fetcher uses a stub based on the
        # last path segment.
        self.responses: Dict[str, bytes] = {}
        self.bundles: Dict[str, bytes] = {}
        self.fail_url: str = ""

    def make_fetcher(self):
        def _fetcher(url: str, target: Path) -> None:
            self.fetch_calls.append((url, str(target)))
            if url == self.fail_url:
                raise RuntimeError("fetch failed")
            body = self.responses.get(url) or (b"PAR1" + url.encode())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        return _fetcher

    def make_bundle_fetcher(self):
        def _bundle_fetcher(slug: str) -> bytes:
            self.bundle_calls.append(slug)
            return self.bundles.get(slug, f"# {slug} bundle\n".encode())
        return _bundle_fetcher

    def make_md5(self):
        def _md5(p: Path) -> str:
            return hashlib.md5(Path(p).read_bytes()).hexdigest()
        return _md5


def _table(id_: str, name: str, md5: str = "", query_mode: str = "local") -> dict:
    return {
        "id": id_,
        "name": name,
        "md5": md5 or hashlib.md5((b"PAR1" + f"/api/data/{id_}/download".encode())).hexdigest(),
        "query_mode": query_mode,
        "parquet_url": f"/api/data/{id_}/download",
    }


@pytest.fixture
def server():
    return _FakeServer()


@pytest.fixture
def local_dir(tmp_path):
    return tmp_path / "local"


# ---------------------------------------------------------------------------
# Sync — direct tables
# ---------------------------------------------------------------------------


class TestSyncDirectTables:
    def test_first_pull_writes_shared_and_reference(self, server, local_dir):
        local_dir.mkdir(parents=True, exist_ok=True)
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        tables = [_table("t1", "orders")]
        state, report = sync_direct_tables(
            server_tables=tables,
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 1
        assert report.removed == 0
        assert (local_data / "_shared" / "t1.parquet").exists()
        assert (local_data / "_direct" / "orders.parquet").exists()
        assert state["orders"]["table_id"] == "t1"
        assert state["orders"]["strategy"] in ("symlink", "hardlink", "copy")

    def test_idempotent_repull_no_fetch(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        tables = [_table("t1", "orders")]
        fetcher = server.make_fetcher()
        md5 = server.make_md5()
        state1, _ = sync_direct_tables(
            server_tables=tables, local_data_dir=local_data,
            prev_state={}, fetcher=fetcher, md5_of=md5,
        )
        assert len(server.fetch_calls) == 1
        state2, report = sync_direct_tables(
            server_tables=tables, local_data_dir=local_data,
            prev_state=state1, fetcher=fetcher, md5_of=md5,
        )
        # No new fetch on idempotent re-pull.
        assert len(server.fetch_calls) == 1
        assert report.added == 0
        assert report.updated == 0
        assert report.removed == 0

    def test_md5_change_refetches(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        t = _table("t1", "orders")
        fetcher = server.make_fetcher()
        md5 = server.make_md5()
        state1, _ = sync_direct_tables(
            server_tables=[t], local_data_dir=local_data,
            prev_state={}, fetcher=fetcher, md5_of=md5,
        )
        # Server flips md5 + payload.
        t2 = dict(t)
        t2["md5"] = "newhash"
        server.responses[t2["parquet_url"]] = b"PAR1" + b"\x99" + b"new payload"

        def _md5_aware(p: Path) -> str:
            content = Path(p).read_bytes()
            if content.startswith(b"PAR1" + b"\x99"):
                return "newhash"
            return hashlib.md5(content).hexdigest()

        state2, report = sync_direct_tables(
            server_tables=[t2], local_data_dir=local_data,
            prev_state=state1, fetcher=fetcher, md5_of=_md5_aware,
        )
        assert report.updated == 1
        assert report.added == 0
        # Two fetches total now.
        assert len(server.fetch_calls) == 2

    def test_remove_drops_reference_and_shared(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        t = _table("t1", "orders")
        state1, _ = sync_direct_tables(
            server_tables=[t], local_data_dir=local_data,
            prev_state={}, fetcher=server.make_fetcher(), md5_of=server.make_md5(),
        )
        # Server drops the table.
        state2, report = sync_direct_tables(
            server_tables=[], local_data_dir=local_data,
            prev_state=state1, fetcher=server.make_fetcher(), md5_of=server.make_md5(),
        )
        assert report.removed == 1
        assert not (local_data / "_direct" / "orders.parquet").exists()
        # No other reference → shared parquet also removed.
        assert not (local_data / "_shared" / "t1.parquet").exists()

    def test_remote_mode_tables_skipped(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        t = _table("t_remote", "remote_tbl", query_mode="remote")
        state, report = sync_direct_tables(
            server_tables=[t], local_data_dir=local_data,
            prev_state={}, fetcher=server.make_fetcher(), md5_of=server.make_md5(),
        )
        assert report.added == 0
        assert server.fetch_calls == []
        assert "remote_tbl" not in state


# ---------------------------------------------------------------------------
# Sync — data packages
# ---------------------------------------------------------------------------


class TestSyncDataPackages:
    def test_two_packages_with_overlap_share_parquet(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        shared_table = _table("t_cust", "customers")
        pkg_sales = {
            "slug": "sales-bundle",
            "tables": [_table("t_orders", "orders"), shared_table],
        }
        pkg_marketing = {
            "slug": "marketing-bundle",
            "tables": [shared_table, _table("t_camp", "campaigns")],
        }
        state, report = sync_data_packages(
            server_packages=[pkg_sales, pkg_marketing],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        # 3 unique parquets fetched (customers, orders, campaigns).
        unique_urls = {c[0] for c in server.fetch_calls}
        assert len(unique_urls) == 3
        # Both packages have a customers.parquet reference.
        assert (local_data / "sales-bundle" / "customers.parquet").exists()
        assert (local_data / "marketing-bundle" / "customers.parquet").exists()
        # Shared store has 3 entries.
        shared_files = list((local_data / "_shared").iterdir())
        assert len(shared_files) == 3
        # Report sums: 4 added (orders + customers in sales, customers + campaigns in mkt = 4 refs).
        assert report.added == 4

    def test_remove_package_with_overlap_keeps_shared(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        shared_table = _table("t_cust", "customers")
        pkg_sales = {
            "slug": "sales-bundle",
            "tables": [shared_table, _table("t_orders", "orders")],
        }
        pkg_marketing = {
            "slug": "marketing-bundle",
            "tables": [shared_table],
        }
        state1, _ = sync_data_packages(
            server_packages=[pkg_sales, pkg_marketing],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        # Remove sales but keep marketing — customers must stay because
        # marketing still references it.
        state2, report = sync_data_packages(
            server_packages=[pkg_marketing],
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.removed == 2  # orders + customers references in sales
        # Customer's _shared parquet still exists (referenced from marketing).
        assert (local_data / "_shared" / "t_cust.parquet").exists()
        # Orders' _shared parquet is gone (no other reference).
        assert not (local_data / "_shared" / "t_orders.parquet").exists()
        # Sales package dir is empty (and removed).
        assert not (local_data / "sales-bundle").exists()
        # Marketing kept its reference.
        assert (local_data / "marketing-bundle" / "customers.parquet").exists()

    def test_remove_package_no_overlap_drops_all(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        pkg = {
            "slug": "sales-bundle",
            "tables": [_table("t1", "orders"), _table("t2", "customers")],
        }
        state1, _ = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        state2, report = sync_data_packages(
            server_packages=[],
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.removed == 2
        assert not (local_data / "_shared" / "t1.parquet").exists()
        assert not (local_data / "_shared" / "t2.parquet").exists()

    def test_idempotent_package_repull(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        pkg = {"slug": "sales", "tables": [_table("t1", "orders")]}
        fetcher = server.make_fetcher()
        state1, _ = sync_data_packages(
            server_packages=[pkg], local_data_dir=local_data,
            prev_state={}, fetcher=fetcher, md5_of=server.make_md5(),
        )
        assert len(server.fetch_calls) == 1
        state2, report = sync_data_packages(
            server_packages=[pkg], local_data_dir=local_data,
            prev_state=state1, fetcher=fetcher, md5_of=server.make_md5(),
        )
        assert len(server.fetch_calls) == 1
        assert report.added + report.updated + report.removed == 0

    def test_unsafe_table_name_degrades_to_per_table_error(self, server, local_dir):
        """A table whose name fails the path-safety check (e.g. an internal
        table's display name with spaces) must NOT abort the whole
        stack_sync stage — it degrades to a per-table error while the
        remaining tables sync normally."""
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        pkg = {
            "slug": "agnes-internal",
            "tables": [_table("agnes_audit", "Agnes audit log"), _table("t1", "orders")],
        }
        state, report = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        # The good table synced.
        assert report.added == 1
        assert (local_data / "agnes-internal" / "orders.parquet").exists()
        # The bad name is a reported error, not an exception.
        assert len(report.errors) == 1
        assert "unsafe path segment" in report.errors[0]["error"]
        assert report.errors[0]["name"] == "Agnes audit log"

    def test_unsafe_package_slug_degrades_to_error(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        bad_pkg = {"slug": "bad slug", "tables": [_table("t1", "orders")]}
        good_pkg = {"slug": "sales", "tables": [_table("t2", "customers")]}
        state, report = sync_data_packages(
            server_packages=[bad_pkg, good_pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 1
        assert (local_data / "sales" / "customers.parquet").exists()
        assert len(report.errors) == 1
        assert report.errors[0]["package"] == "bad slug"

    def test_internal_mode_tables_skipped(self, server, local_dir):
        """query_mode='internal' tables live server-side only (no parquet)
        — the pull must skip them like remote-mode tables."""
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        pkg = {
            "slug": "agnes-internal",
            "tables": [_table("agnes_audit", "agnes_audit", query_mode="internal")],
        }
        state, report = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 0
        assert report.errors == []
        assert server.fetch_calls == []

    def test_server_only_tables_skipped(self, server, local_dir):
        """server_only tables (#607) stay server-side — no parquet download."""
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        t = _table("t_srv", "srv_tbl")
        t["server_only"] = True
        pkg = {"slug": "pkg", "tables": [t]}
        state, report = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 0
        assert report.errors == []
        assert server.fetch_calls == []


class TestSyncDirectTablesUnsafeNames:
    def test_unsafe_name_degrades_to_per_table_error(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        tables = [_table("agnes_audit", "Agnes audit log"), _table("t1", "orders")]
        state, report = sync_direct_tables(
            server_tables=tables,
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 1
        assert "orders" in state
        assert len(report.errors) == 1
        assert "unsafe path segment" in report.errors[0]["error"]


# ---------------------------------------------------------------------------
# Sync — memory domains
# ---------------------------------------------------------------------------


class TestSyncMemoryDomains:
    def test_first_pull_writes_bundle(self, server, local_dir):
        mem = local_dir / "memory"
        domain = {"slug": "sales-playbook", "md5": "h1"}
        state, report = sync_memory_domains(
            server_domains=[domain],
            local_memory_dir=mem,
            prev_state={},
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        assert report.added == 1
        assert (mem / "sales-playbook" / "bundle.md").exists()
        assert state["sales-playbook"]["md5"] == "h1"

    def test_idempotent_skips_refetch(self, server, local_dir):
        mem = local_dir / "memory"
        domain = {"slug": "sales-playbook", "md5": "h1"}
        bundle = server.make_bundle_fetcher()
        state1, _ = sync_memory_domains(
            server_domains=[domain], local_memory_dir=mem,
            prev_state={}, bundle_fetcher=bundle,
        )
        assert server.bundle_calls == ["sales-playbook"]
        state2, report = sync_memory_domains(
            server_domains=[domain], local_memory_dir=mem,
            prev_state=state1, bundle_fetcher=bundle,
        )
        assert len(server.bundle_calls) == 1  # no re-fetch
        assert report.added + report.updated == 0

    def test_md5_change_refetches(self, server, local_dir):
        mem = local_dir / "memory"
        bundle = server.make_bundle_fetcher()
        state1, _ = sync_memory_domains(
            server_domains=[{"slug": "x", "md5": "old"}],
            local_memory_dir=mem,
            prev_state={},
            bundle_fetcher=bundle,
        )
        state2, report = sync_memory_domains(
            server_domains=[{"slug": "x", "md5": "new"}],
            local_memory_dir=mem,
            prev_state=state1,
            bundle_fetcher=bundle,
        )
        assert report.updated == 1
        assert len(server.bundle_calls) == 2

    def test_unsafe_domain_slug_degrades_to_error(self, server, local_dir):
        mem = local_dir / "memory"
        state, report = sync_memory_domains(
            server_domains=[{"slug": "bad slug", "md5": "h"}, {"slug": "ok", "md5": "h"}],
            local_memory_dir=mem,
            prev_state={},
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        assert report.added == 1
        assert (mem / "ok" / "bundle.md").exists()
        assert len(report.errors) == 1
        assert "unsafe path segment" in report.errors[0]["error"]

    def test_remove_unlinks_bundle(self, server, local_dir):
        mem = local_dir / "memory"
        bundle = server.make_bundle_fetcher()
        state1, _ = sync_memory_domains(
            server_domains=[{"slug": "x", "md5": "h"}],
            local_memory_dir=mem, prev_state={}, bundle_fetcher=bundle,
        )
        state2, report = sync_memory_domains(
            server_domains=[],
            local_memory_dir=mem, prev_state=state1, bundle_fetcher=bundle,
        )
        assert report.removed == 1
        assert not (mem / "x" / "bundle.md").exists()


# ---------------------------------------------------------------------------
# Windows symlink fallback
# ---------------------------------------------------------------------------


class TestWindowsFallback:
    def test_symlink_fallback_to_hardlink(self, server, local_dir, monkeypatch):
        """When os.symlink raises, _link_or_copy must try os.link."""
        local_data = local_dir / "data"
        (local_data / "_shared").mkdir(parents=True)
        src = local_data / "_shared" / "t1.parquet"
        src.write_bytes(b"PAR1" + b"x")
        dst = local_data / "pkg" / "alias.parquet"

        monkeypatch.setattr("cli.lib.pull_sync.os.symlink",
                            lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))
        strategy = _link_or_copy(src, dst)
        assert strategy == "hardlink"
        assert dst.exists()
        # Same inode = real hardlink.
        assert dst.stat().st_ino == src.stat().st_ino

    def test_symlink_and_hardlink_fail_falls_back_to_copy(
        self, server, local_dir, monkeypatch,
    ):
        local_data = local_dir / "data"
        (local_data / "_shared").mkdir(parents=True)
        src = local_data / "_shared" / "t1.parquet"
        src.write_bytes(b"PAR1" + b"abc")
        dst = local_data / "pkg" / "alias.parquet"

        monkeypatch.setattr("cli.lib.pull_sync.os.symlink",
                            lambda *a, **kw: (_ for _ in ()).throw(OSError("a")))
        monkeypatch.setattr("cli.lib.pull_sync.os.link",
                            lambda *a, **kw: (_ for _ in ()).throw(OSError("b")))
        strategy = _link_or_copy(src, dst)
        assert strategy == "copy"
        assert dst.exists()
        assert dst.read_bytes() == src.read_bytes()
        # Different inode — independent file.
        assert dst.stat().st_ino != src.stat().st_ino


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


class TestInvariants:
    def test_orphan_shared_parquet_reported(self, local_dir):
        local_data = local_dir / "data"
        (local_data / "_shared").mkdir(parents=True)
        orphan = local_data / "_shared" / "junk.parquet"
        orphan.write_bytes(b"PAR1")
        violations = audit_invariants(local_data, {"data_packages": {}})
        assert any("orphan" in v for v in violations)

    def test_broken_reference_reported(self, local_dir):
        local_data = local_dir / "data"
        (local_data / "_shared").mkdir(parents=True)
        state = {
            "direct_tables": {
                "orders": {
                    "table_id": "t1",
                    "ref_path": str(local_data / "_direct" / "orders.parquet"),
                    "shared_path": str(local_data / "_shared" / "t1.parquet"),
                    "strategy": "symlink",
                }
            }
        }
        violations = audit_invariants(local_data, state)
        assert any("broken reference" in v for v in violations)
        assert any("dangling shared" in v for v in violations)

    def test_clean_state_no_violations(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        state, _ = sync_direct_tables(
            server_tables=[_table("t1", "orders")],
            local_data_dir=local_data, prev_state={},
            fetcher=server.make_fetcher(), md5_of=server.make_md5(),
        )
        violations = audit_invariants(local_data, {"direct_tables": state})
        assert violations == []


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


class TestRunStackSync:
    def test_full_first_pull(self, server, local_dir):
        manifest = {
            "direct_tables": [_table("t_direct", "ops")],
            "data_packages": [
                {
                    "slug": "sales",
                    "tables": [
                        _table("t1", "orders"),
                        _table("t_cust", "customers"),
                    ],
                },
                {
                    "slug": "marketing",
                    "tables": [
                        _table("t_cust", "customers"),
                        _table("t_camp", "campaigns"),
                    ],
                },
            ],
            "memory_domains": [
                {"slug": "playbook", "md5": "h1"},
            ],
        }
        opts = PullStackOptions(
            manifest=manifest,
            local_dir=local_dir,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        report = run_stack_sync(opts)
        assert report.direct_tables.added == 1
        assert report.data_packages.added == 4   # 2 + 2 references
        assert report.memory_domains.added == 1
        # Unique parquets in _shared: t_direct, t1, t_cust, t_camp.
        shared_files = list((local_dir / "data" / "_shared").iterdir())
        assert len(shared_files) == 4
        assert report.invariant_violations == []
        # sync_state.json persisted.
        assert (local_dir / "sync_state.json").exists()

    def test_idempotent_full_repull(self, server, local_dir):
        manifest = {
            "direct_tables": [_table("t1", "orders")],
            "data_packages": [],
            "memory_domains": [{"slug": "x", "md5": "h"}],
        }
        opts = PullStackOptions(
            manifest=manifest,
            local_dir=local_dir,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        run_stack_sync(opts)
        fetch_count_first = len(server.fetch_calls)
        bundle_count_first = len(server.bundle_calls)
        report2 = run_stack_sync(opts)
        assert len(server.fetch_calls) == fetch_count_first
        assert len(server.bundle_calls) == bundle_count_first
        assert report2.total_changes() == 0

    def test_remove_package_with_shared_overlap(self, server, local_dir):
        shared = _table("t_cust", "customers")
        manifest_v1 = {
            "direct_tables": [],
            "data_packages": [
                {"slug": "sales", "tables": [shared, _table("t1", "orders")]},
                {"slug": "marketing", "tables": [shared]},
            ],
            "memory_domains": [],
        }
        opts1 = PullStackOptions(
            manifest=manifest_v1, local_dir=local_dir,
            fetcher=server.make_fetcher(), md5_of=server.make_md5(),
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        run_stack_sync(opts1)
        # Phase 2: remove sales.
        manifest_v2 = {
            "direct_tables": [],
            "data_packages": [
                {"slug": "marketing", "tables": [shared]},
            ],
            "memory_domains": [],
        }
        opts2 = PullStackOptions(
            manifest=manifest_v2, local_dir=local_dir,
            fetcher=server.make_fetcher(), md5_of=server.make_md5(),
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        report = run_stack_sync(opts2)
        # Marketing still references customers → shared parquet kept.
        assert (local_dir / "data" / "_shared" / "t_cust.parquet").exists()
        assert not (local_dir / "data" / "_shared" / "t1.parquet").exists()
        assert report.data_packages.removed == 2

    def test_first_pull_with_overlap_writes_18_unique_shared(self, server, local_dir):
        """Spec example: 2 packages, 18 unique tables, package_a has 12,
        package_b has 9 (3 overlap). Verifies ref-count dedup at scale."""
        # 12 tables for pkg_a
        a_only = [_table(f"a{i}", f"a_tbl_{i}") for i in range(9)]
        # 3 overlap tables (shared between a and b)
        overlap = [_table(f"x{i}", f"x_tbl_{i}") for i in range(3)]
        # 6 tables for pkg_b
        b_only = [_table(f"b{i}", f"b_tbl_{i}") for i in range(6)]
        manifest = {
            "direct_tables": [],
            "data_packages": [
                {"slug": "pkg-a", "tables": a_only + overlap},
                {"slug": "pkg-b", "tables": b_only + overlap},
            ],
            "memory_domains": [],
        }
        opts = PullStackOptions(
            manifest=manifest, local_dir=local_dir,
            fetcher=server.make_fetcher(), md5_of=server.make_md5(),
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        run_stack_sync(opts)
        # 9 + 3 + 6 = 18 unique parquets in _shared.
        shared_files = list((local_dir / "data" / "_shared").iterdir())
        assert len(shared_files) == 18
        # pkg-a has 12 references.
        a_files = list((local_dir / "data" / "pkg-a").iterdir())
        assert len(a_files) == 12
        # pkg-b has 9 references.
        b_files = list((local_dir / "data" / "pkg-b").iterdir())
        assert len(b_files) == 9
