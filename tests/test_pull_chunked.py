"""Tests for range-based chunked download in cli/client.py:stream_download.

Background — the previous diagnosis measured `agnes pull` on a single 5.1 GB
materialized parquet at 0.29 MB/s on a corp VPN with per-flow rate-limiting;
4 parallel range requests over the same connection sustained 1.65 MB/s
aggregate. Existing `AGNES_PULL_PARALLELISM=4` parallelizes across files,
not within a file, so a manifest with 1 large materialized parquet + 10
remote tables yields 1 active worker = single-stream throughput.

These tests exercise the chunking code path: HEAD probe, Range-request
splitting, fallback when the server doesn't honor ranges, cleanup on
chunk failure, and the small-file bypass.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Fake HTTP layer ─────────────────────────────────────────────────────
# The real httpx Client / AsyncClient surface is large; we mock at the
# client-method level. Our `stream_download` should:
#   1. Call HEAD to learn `content-length` + `accept-ranges`.
#   2. If ranges supported and size > threshold, issue N parallel
#      `GET` with `Range: bytes=A-B`, each returning 206 + body chunk.
#   3. Concatenate part files into the destination.

class _FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None,
                 body: bytes = b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self,
            )

    def iter_bytes(self, chunk_size: int = 65536):
        # Yield in chunk_size pieces so the sink loop runs realistically.
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeClient:
    """Captures calls + returns canned responses."""

    def __init__(self, *, body: bytes, accept_ranges: bool = True,
                 reject_range_with_200: bool = False,
                 fail_chunk_indices: tuple[int, ...] = (),
                 head_status: int = 200):
        self._body = body
        self._accept_ranges = accept_ranges
        self._reject_range_with_200 = reject_range_with_200
        self._fail_chunk_indices = set(fail_chunk_indices)
        self._head_status = head_status
        self.head_calls = 0
        self.range_calls: list[tuple[int, int]] = []
        self.full_get_calls = 0
        self._lock = threading.Lock()
        self._chunk_attempt_counts: dict[tuple[int, int], int] = {}

    # `stream_download` calls `client.head(path)` once to probe.
    def head(self, path: str, **kwargs):
        with self._lock:
            self.head_calls += 1
        if self._head_status >= 400:
            return _FakeResponse(self._head_status)
        headers = {"content-length": str(len(self._body))}
        if self._accept_ranges:
            headers["accept-ranges"] = "bytes"
        return _FakeResponse(200, headers=headers)

    # `stream_download` uses `client.stream("GET", path, headers=...)`
    # for both the chunked and full-file paths. Range header presence
    # tells us which one.
    def stream(self, method: str, path: str, *, headers: dict | None = None,
               **kwargs):
        rng = (headers or {}).get("Range") or (headers or {}).get("range")
        if rng:
            # bytes=START-END
            spec = rng.split("=", 1)[1]
            start_s, end_s = spec.split("-", 1)
            start = int(start_s)
            end = int(end_s)
            with self._lock:
                self.range_calls.append((start, end))
                key = (start, end)
                attempt = self._chunk_attempt_counts.get(key, 0)
                self._chunk_attempt_counts[key] = attempt + 1
            # Determine chunk index (in order of unique starts).
            # We map by start to a stable index for fail-injection.
            chunk_idx = self._chunk_index_for_start(start)
            # Should this attempt fail? Fail only on first attempt for
            # listed indices — retry succeeds.
            if chunk_idx in self._fail_chunk_indices and attempt == 0:
                import httpx
                raise httpx.ReadError("simulated chunk failure")
            if self._reject_range_with_200:
                # Server ignored Range — returns full body with 200.
                return _FakeResponse(200, body=self._body)
            piece = self._body[start:end + 1]
            return _FakeResponse(
                206,
                headers={"content-range": f"bytes {start}-{end}/{len(self._body)}"},
                body=piece,
            )
        # Full-file GET (single-stream fallback).
        with self._lock:
            self.full_get_calls += 1
        return _FakeResponse(200, body=self._body)

    def _chunk_index_for_start(self, start: int) -> int:
        # Unique sorted starts so fail_chunk_indices is deterministic.
        starts = sorted({s for s, _ in self.range_calls})
        try:
            return starts.index(start)
        except ValueError:
            return -1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


# ── Test fixtures ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "_cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg))


@pytest.fixture(autouse=True)
def _reset_shared_client(monkeypatch):
    """Reset the persistent shared httpx.Client between tests so each
    test starts from a known state. Tests that need to inject a fake
    client also stub `_get_shared_client` directly via the
    `_inject_fake_client` helper below."""
    import cli.client as cc
    if hasattr(cc, "_SHARED_CLIENT"):
        monkeypatch.setattr(cc, "_SHARED_CLIENT", None, raising=False)
    yield
    if hasattr(cc, "_SHARED_CLIENT"):
        monkeypatch.setattr(cc, "_SHARED_CLIENT", None, raising=False)


def _inject_fake_client(monkeypatch, fake):
    """Patch both client factories to return the same fake. Tests target
    `_get_shared_client` (the path stream_download actually takes) and
    also `get_client` so the fallback path also lands on the fake."""
    monkeypatch.setattr("cli.client.get_client", lambda timeout=300.0: fake)
    monkeypatch.setattr("cli.client._get_shared_client",
                        lambda: fake, raising=False)


# ── Tests ───────────────────────────────────────────────────────────────

def test_chunked_download_success(tmp_path, monkeypatch):
    """Server advertises ranges, file is large enough — 4 chunks, assembled
    correctly into target."""
    body = bytes(range(256)) * 2048  # 512 KB
    threshold = 1024  # 1 KB so 512 KB is "large"
    monkeypatch.setenv("AGNES_PULL_CHUNK_THRESHOLD_BYTES", str(threshold))
    monkeypatch.setenv("AGNES_PULL_CHUNK_PARALLELISM", "4")

    fake = _FakeClient(body=body, accept_ranges=True)
    _inject_fake_client(monkeypatch, fake)

    from cli.client import stream_download
    target = tmp_path / "out.parquet"
    progress_bytes = []
    total = stream_download("/api/data/x/download", str(target),
                            progress_callback=lambda n: progress_bytes.append(n))

    assert total == len(body)
    assert target.read_bytes() == body
    # 4 distinct ranges issued (no overlaps; last one carries remainder).
    assert len(set(fake.range_calls)) == 4
    assert fake.head_calls == 1
    assert fake.full_get_calls == 0
    # Progress callback was called and total bytes match.
    assert sum(progress_bytes) == len(body)
    # Chunk parts cleaned up.
    leftovers = list(tmp_path.glob("*.part*"))
    assert leftovers == [], f"orphan part files: {leftovers}"


def test_chunked_download_fallback_when_server_ignores_range(
    tmp_path, monkeypatch,
):
    """Server returns 200 instead of 206 on the first range probe — abort
    chunked path, fall back to single-stream. No corrupt output."""
    body = b"X" * 200_000
    monkeypatch.setenv("AGNES_PULL_CHUNK_THRESHOLD_BYTES", "1024")
    monkeypatch.setenv("AGNES_PULL_CHUNK_PARALLELISM", "4")

    # accept_ranges=True (HEAD lies), but every Range GET returns 200
    # with the full body — that's the "server ignored Range" path.
    fake = _FakeClient(body=body, accept_ranges=True,
                       reject_range_with_200=True)
    _inject_fake_client(monkeypatch, fake)

    from cli.client import stream_download
    target = tmp_path / "out.bin"
    total = stream_download("/api/data/x/download", str(target))

    assert total == len(body)
    assert target.read_bytes() == body
    # Fell back to a single full-body GET.
    assert fake.full_get_calls >= 1


def test_small_file_uses_single_stream_path(tmp_path, monkeypatch):
    """Below threshold → no HEAD probe needed (or HEAD short-circuits),
    no Range requests, plain single-stream download."""
    body = b"x" * 500  # tiny
    monkeypatch.setenv("AGNES_PULL_CHUNK_THRESHOLD_BYTES", "10000")  # 10 KB
    monkeypatch.setenv("AGNES_PULL_CHUNK_PARALLELISM", "4")

    fake = _FakeClient(body=body, accept_ranges=True)
    _inject_fake_client(monkeypatch, fake)

    from cli.client import stream_download
    target = tmp_path / "out.bin"
    total = stream_download("/api/data/x/download", str(target))

    assert total == len(body)
    assert target.read_bytes() == body
    assert fake.range_calls == [], "small file must not split into ranges"
    assert fake.full_get_calls >= 1


def test_chunked_download_no_accept_ranges_falls_back(tmp_path, monkeypatch):
    """HEAD doesn't advertise byte-range support → skip chunked path,
    plain single-stream."""
    body = b"y" * 200_000
    monkeypatch.setenv("AGNES_PULL_CHUNK_THRESHOLD_BYTES", "1024")
    monkeypatch.setenv("AGNES_PULL_CHUNK_PARALLELISM", "4")

    fake = _FakeClient(body=body, accept_ranges=False)
    _inject_fake_client(monkeypatch, fake)

    from cli.client import stream_download
    target = tmp_path / "out.bin"
    total = stream_download("/api/data/x/download", str(target))

    assert total == len(body)
    assert target.read_bytes() == body
    assert fake.range_calls == []
    assert fake.full_get_calls >= 1


def test_chunked_download_one_chunk_retries_then_succeeds(
    tmp_path, monkeypatch,
):
    """One chunk fails on first attempt; retry path completes the file."""
    body = bytes(range(256)) * 1024  # 256 KB
    monkeypatch.setenv("AGNES_PULL_CHUNK_THRESHOLD_BYTES", "1024")
    monkeypatch.setenv("AGNES_PULL_CHUNK_PARALLELISM", "4")
    monkeypatch.setenv("AGNES_STREAM_RETRIES", "2")

    fake = _FakeClient(body=body, accept_ranges=True,
                       fail_chunk_indices=(1,))  # second chunk blips once
    _inject_fake_client(monkeypatch, fake)

    from cli.client import stream_download
    target = tmp_path / "out.bin"
    total = stream_download("/api/data/x/download", str(target))

    assert total == len(body)
    assert target.read_bytes() == body
    # Cleanup of all part files.
    assert list(tmp_path.glob("*.part*")) == []


def test_chunked_download_failure_cleans_up_part_files(tmp_path, monkeypatch):
    """All retries exhausted on a chunk → no destination file, no orphan
    part files."""
    body = b"z" * 200_000
    monkeypatch.setenv("AGNES_PULL_CHUNK_THRESHOLD_BYTES", "1024")
    monkeypatch.setenv("AGNES_PULL_CHUNK_PARALLELISM", "4")
    monkeypatch.setenv("AGNES_STREAM_RETRIES", "0")

    # Inject a permanent failure on chunk 2 (retries=0 → first failure
    # is fatal).
    class _ChronicFail(_FakeClient):
        def stream(self, method, path, *, headers=None, **kwargs):
            rng = (headers or {}).get("Range")
            if rng:
                spec = rng.split("=", 1)[1]
                start = int(spec.split("-", 1)[0])
                # Permanently fail the chunk starting at exactly half.
                if start >= len(body) // 4 and start <= len(body) // 2:
                    import httpx
                    raise httpx.ReadError("permanent")
                return super().stream(method, path, headers=headers, **kwargs)
            return super().stream(method, path, headers=headers, **kwargs)

    fake = _ChronicFail(body=body, accept_ranges=True)
    _inject_fake_client(monkeypatch, fake)

    from cli.client import stream_download
    target = tmp_path / "out.bin"
    with pytest.raises(Exception):
        stream_download("/api/data/x/download", str(target))

    assert not target.exists(), "no destination file after total failure"
    # No orphan parts.
    assert list(tmp_path.glob("*.part*")) == []
    assert not (tmp_path / "out.bin.tmp").exists()


def test_progress_callback_aggregates_across_chunks(tmp_path, monkeypatch):
    """The progress callback should fire with byte deltas summing to the
    full file across all chunks — caller treats one file as one task."""
    body = bytes(range(256)) * 4096  # 1 MB
    monkeypatch.setenv("AGNES_PULL_CHUNK_THRESHOLD_BYTES", "1024")
    monkeypatch.setenv("AGNES_PULL_CHUNK_PARALLELISM", "4")

    fake = _FakeClient(body=body, accept_ranges=True)
    _inject_fake_client(monkeypatch, fake)

    from cli.client import stream_download
    target = tmp_path / "out.bin"
    advances = []
    stream_download("/api/data/x/download", str(target),
                    progress_callback=lambda n: advances.append(n))
    assert sum(advances) == len(body)
