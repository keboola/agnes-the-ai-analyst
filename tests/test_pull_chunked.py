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

import pytest


# ── Fake HTTP layer ─────────────────────────────────────────────────────
# The real httpx Client / AsyncClient surface is large; we mock at the
# client-method level. Our `stream_download` should:
#   1. Call HEAD to learn `content-length` + `accept-ranges`.
#   2. If ranges supported and size > threshold, issue N parallel
#      `GET` with `Range: bytes=A-B`, each returning 206 + body chunk.
#   3. Concatenate part files into the destination.


class _FakeResponse:
    def __init__(
        self, status_code: int, headers: dict | None = None, body: bytes = b"", fail_after_bytes: int | None = None
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        # When set, `iter_bytes` yields exactly this many bytes of `body`
        # and then raises — simulating a connection that drops mid-transfer
        # (as opposed to failing before any byte reaches the caller, which
        # is what raising directly from `stream()` simulates). Used by the
        # resume tests: the sink loop in `_download_chunk` /
        # `_download_single_stream` writes whatever was yielded before the
        # exception, leaving a genuinely partial file on disk.
        self._fail_after_bytes = fail_after_bytes

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=None,
                response=self,
            )

    def iter_bytes(self, chunk_size: int = 65536):
        # Yield in chunk_size pieces so the sink loop runs realistically.
        emitted = 0
        for i in range(0, len(self._body), chunk_size):
            piece = self._body[i : i + chunk_size]
            if self._fail_after_bytes is not None and emitted + len(piece) > self._fail_after_bytes:
                piece = piece[: self._fail_after_bytes - emitted]
                if piece:
                    yield piece
                import httpx

                raise httpx.ReadError("simulated mid-stream drop")
            emitted += len(piece)
            yield piece

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeClient:
    """Captures calls + returns canned responses."""

    def __init__(
        self,
        *,
        body: bytes,
        accept_ranges: bool = True,
        reject_range_with_200: bool = False,
        reject_range_with_416: bool = False,
        fail_chunk_indices: tuple[int, ...] = (),
        head_status: int = 200,
    ):
        self._body = body
        self._accept_ranges = accept_ranges
        self._reject_range_with_200 = reject_range_with_200
        self._reject_range_with_416 = reject_range_with_416
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
    def stream(self, method: str, path: str, *, headers: dict | None = None, **kwargs):
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
            if self._reject_range_with_416:
                # Requested range not satisfiable.
                return _FakeResponse(416)
            piece = self._body[start : end + 1]
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


class _ScriptedRangeClient(_FakeClient):
    """A `_FakeClient` whose response to each chunk Range request can be
    scripted per attempt, for the resume-on-retry tests (issue #1309).

    `script` maps a chunk's fixed `end` byte offset — invariant across
    that chunk's resume retries, since only `start` shifts forward — to a
    list of per-attempt directives. Each entry is one of:
      - ``"drop:<n>"``       stream `n` bytes of the correct slice, then
                             drop the connection (a genuine partial
                             write, not a failure before any byte lands).
      - ``"ignore_range"``   respond 200 with the full body (server
                             stopped honoring Range).
      - ``"not_satisfiable"`` respond 416.
    Attempts past the scripted list — or a chunk with no entry at all —
    get the normal 206 response for whatever Range was actually asked
    for (correct resume offset and all).
    """

    def __init__(self, *, script: dict[int, list[str]] | None = None, **kw):
        super().__init__(**kw)
        self._script = script or {}
        self._attempt_by_end: dict[int, int] = {}

    def stream(self, method: str, path: str, *, headers: dict | None = None, **kwargs):
        rng = (headers or {}).get("Range")
        if not rng:
            with self._lock:
                self.full_get_calls += 1
            return _FakeResponse(200, body=self._body)
        spec = rng.split("=", 1)[1]
        start_s, end_s = spec.split("-", 1)
        start, end = int(start_s), int(end_s)
        with self._lock:
            self.range_calls.append((start, end))
            attempt = self._attempt_by_end.get(end, 0)
            self._attempt_by_end[end] = attempt + 1
        directives = self._script.get(end, [])
        directive = directives[attempt] if attempt < len(directives) else None
        if directive == "ignore_range":
            return _FakeResponse(200, body=self._body)
        if directive == "not_satisfiable":
            return _FakeResponse(416)
        piece = self._body[start : end + 1]
        fail_after = None
        if directive and directive.startswith("drop:"):
            fail_after = int(directive.split(":", 1)[1])
        return _FakeResponse(
            206,
            headers={"content-range": f"bytes {start}-{end}/{len(self._body)}"},
            body=piece,
            fail_after_bytes=fail_after,
        )


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
    monkeypatch.setattr("cli.client._get_shared_client", lambda: fake, raising=False)


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
    total = stream_download("/api/data/x/download", str(target), progress_callback=lambda n: progress_bytes.append(n))

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
    tmp_path,
    monkeypatch,
):
    """Server returns 200 instead of 206 on the first range probe — abort
    chunked path, fall back to single-stream. No corrupt output."""
    body = b"X" * 200_000
    monkeypatch.setenv("AGNES_PULL_CHUNK_THRESHOLD_BYTES", "1024")
    monkeypatch.setenv("AGNES_PULL_CHUNK_PARALLELISM", "4")

    # accept_ranges=True (HEAD lies), but every Range GET returns 200
    # with the full body — that's the "server ignored Range" path.
    fake = _FakeClient(body=body, accept_ranges=True, reject_range_with_200=True)
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
    tmp_path,
    monkeypatch,
):
    """One chunk fails on first attempt; retry path completes the file."""
    body = bytes(range(256)) * 1024  # 256 KB
    monkeypatch.setenv("AGNES_PULL_CHUNK_THRESHOLD_BYTES", "1024")
    monkeypatch.setenv("AGNES_PULL_CHUNK_PARALLELISM", "4")
    monkeypatch.setenv("AGNES_STREAM_RETRIES", "2")

    fake = _FakeClient(body=body, accept_ranges=True, fail_chunk_indices=(1,))  # second chunk blips once
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
    stream_download("/api/data/x/download", str(target), progress_callback=lambda n: advances.append(n))
    assert sum(advances) == len(body)


def test_dead_pid_leftovers_are_reaped(tmp_path, monkeypatch):
    """Devil's-advocate R3 finding #1: PID-suffixed `<target>.{pid}.tmp`
    and `.partN` files from a SIGKILL'd previous pull must be reaped on
    the next pull, otherwise they accumulate on disk indefinitely.

    PID 1 (init) is always alive, so a file with pid=1 must NOT be
    reaped. PID 99999999 (~10⁸) is essentially guaranteed not-alive on
    any modern Linux/macOS — used as the dead-PID marker.
    """
    target = tmp_path / "out.bin"

    # Live-PID leftover (pid=1 = init, always alive). Must NOT be reaped.
    live_path = tmp_path / "out.bin.1.tmp"
    live_path.write_bytes(b"live process leftover")

    # Dead-PID leftovers — both .tmp and .part0 forms.
    dead_tmp = tmp_path / "out.bin.99999999.tmp"
    dead_tmp.write_bytes(b"dead process leftover tmp")
    dead_part = tmp_path / "out.bin.99999999.part0"
    dead_part.write_bytes(b"dead process leftover part")

    # Bare-name leftover (no PID suffix) — pre-existing pattern, NOT
    # touched by the new reaper. Reaper only matches `.{digits}.tmp`
    # / `.{digits}.partN` exactly.
    bare_tmp = tmp_path / "out.bin.tmp"
    bare_tmp.write_bytes(b"bare leftover")

    from cli.client import _reap_dead_pid_leftovers

    _reap_dead_pid_leftovers(str(target))

    assert live_path.exists(), "live-PID leftover must be preserved"
    assert not dead_tmp.exists(), "dead-PID .tmp must be reaped"
    assert not dead_part.exists(), "dead-PID .partN must be reaped"
    assert bare_tmp.exists(), "bare-name leftover is out of scope for the reaper"


def test_reap_handles_garbage_in_filename(tmp_path):
    """Files in the parquet dir whose names happen to glob-match but
    don't conform to the PID-suffix shape must be skipped without
    raising."""
    target = tmp_path / "out.bin"
    weird = tmp_path / "out.bin.garbage.tmp"
    weird.write_bytes(b"x")

    from cli.client import _reap_dead_pid_leftovers

    # Must not raise even though the filename has no integer PID.
    _reap_dead_pid_leftovers(str(target))
    assert weird.exists(), "non-PID-shaped file must not be reaped"


# ── Resume-on-retry (issue #1309) ───────────────────────────────────────
# The bug: a retry re-requested the IDENTICAL `Range: bytes=start-end`
# and reopened the part/tmp file in "wb" (chunked) — or unconditionally
# unlinked-then-restreamed with no Range at all (single-stream) — every
# time, discarding whatever bytes the previous attempt already streamed
# and restarting from byte 0. These tests exercise `_download_chunk` /
# `_download_single_stream` directly (the precise offset + append-vs-
# truncate decision) and, for the retry-loop wiring, `stream_download`
# end-to-end with a client that scripts a genuine mid-stream drop
# (partial bytes actually land on disk — unlike `fail_chunk_indices`,
# which fails before any byte is written).


def test_download_chunk_fresh_request_matches_pre_resume_behavior(tmp_path):
    """No part file on disk yet — the Range header and write mode are
    byte-for-byte what `_download_chunk` sent before resume support."""
    from cli.client import _download_chunk

    body = bytes(range(256)) * 40  # 10240 bytes
    fake = _FakeClient(body=body, accept_ranges=True)
    part_path = tmp_path / "out.bin.123.part0"
    progress: list[int] = []

    _download_chunk(fake, "/x", 100, 5099, part_path, progress.append)

    assert fake.range_calls == [(100, 5099)]
    assert part_path.read_bytes() == body[100:5100]
    assert sum(progress) == 5000


def test_download_chunk_resumes_appends_missing_tail(tmp_path):
    """A part file already holds a genuine PREFIX of the chunk (as if an
    earlier attempt streamed that much before dying) — the retry must
    request only the missing tail and append, not re-fetch + overwrite."""
    from cli.client import _download_chunk

    body = bytes(range(256)) * 40  # 10240 bytes
    start, end = 100, 5099  # 5000-byte chunk
    have_bytes = body[start : start + 2000]  # first 2000 bytes already down

    fake = _FakeClient(body=body, accept_ranges=True)
    part_path = tmp_path / "out.bin.123.part0"
    part_path.write_bytes(have_bytes)
    progress: list[int] = []

    _download_chunk(fake, "/x", start, end, part_path, progress.append)

    assert fake.range_calls == [(start + 2000, end)]
    assert part_path.read_bytes() == body[start : end + 1]
    # Only the NEW bytes are reported — the 2000 already-downloaded bytes
    # were reported by the (failed) earlier attempt, not this one.
    assert sum(progress) == (end - start + 1) - 2000


def test_download_chunk_oversized_part_file_restarts_from_scratch(tmp_path):
    """A part file bigger than the requested range is a stale/corrupt
    leftover — must restart at `start`, never compute a negative or
    inverted range from it."""
    from cli.client import _download_chunk

    body = bytes(range(256)) * 40
    start, end = 100, 5099  # 5000-byte range
    fake = _FakeClient(body=body, accept_ranges=True)
    part_path = tmp_path / "out.bin.123.part0"
    part_path.write_bytes(b"\xff" * 9000)  # bigger than the 5000-byte range
    progress: list[int] = []

    _download_chunk(fake, "/x", start, end, part_path, progress.append)

    assert fake.range_calls == [(start, end)]  # original range, not negative
    assert part_path.read_bytes() == body[start : end + 1]
    assert sum(progress) == end - start + 1


def test_download_chunk_exact_size_leftover_treated_as_already_complete(tmp_path):
    """The (rare) case where the leftover part file is EXACTLY the
    expected chunk size — nothing left to fetch, no request issued, no
    corruption from computing an inverted `start > end` range."""
    from cli.client import _download_chunk

    body = bytes(range(256)) * 40
    start, end = 100, 5099
    fake = _FakeClient(body=body, accept_ranges=True)
    part_path = tmp_path / "out.bin.123.part0"
    part_path.write_bytes(body[start : end + 1])
    progress: list[int] = []

    _download_chunk(fake, "/x", start, end, part_path, progress.append)

    assert fake.range_calls == []
    assert part_path.read_bytes() == body[start : end + 1]
    assert progress == []


def test_download_chunk_server_ignores_range_on_resume_does_not_corrupt(tmp_path):
    """Resuming, but the server answers 200 (ignored the Range) instead
    of 206 — `_download_chunk` must raise `_RangeNotHonored` WITHOUT
    touching the part file. Appending a 200 full-body response onto the
    partial bytes already on disk would silently corrupt the output."""
    from cli.client import _RangeNotHonored, _download_chunk

    body = bytes(range(256)) * 40
    start, end = 100, 5099
    fake = _FakeClient(body=body, accept_ranges=True, reject_range_with_200=True)
    part_path = tmp_path / "out.bin.123.part0"
    existing = body[start : start + 1000]
    part_path.write_bytes(existing)

    with pytest.raises(_RangeNotHonored):
        _download_chunk(fake, "/x", start, end, part_path, None)

    assert part_path.read_bytes() == existing, "must not touch the file on a 200 fallback"


def test_download_chunk_fresh_server_ignores_range_still_raises(tmp_path):
    """Regression: a FRESH (non-resume) request that gets 200 instead of
    206 still raises `_RangeNotHonored`, same as before resume support."""
    from cli.client import _RangeNotHonored, _download_chunk

    body = bytes(range(256)) * 40
    fake = _FakeClient(body=body, accept_ranges=True, reject_range_with_200=True)
    part_path = tmp_path / "out.bin.123.part0"

    with pytest.raises(_RangeNotHonored):
        _download_chunk(fake, "/x", 100, 5099, part_path, None)

    assert not part_path.exists()


def test_download_chunk_416_raises_without_touching_partial_file(tmp_path):
    """A 416 to the resume request means our offset no longer lines up
    with what the server has — `_download_chunk` raises so the CALLER
    (its retry loop) can decide to drop the leftover and retry; the
    function itself must not touch the file."""
    from cli.client import _RangeNotSatisfiable, _download_chunk

    body = bytes(range(256)) * 40
    start, end = 100, 5099
    fake = _FakeClient(body=body, accept_ranges=True, reject_range_with_416=True)
    part_path = tmp_path / "out.bin.123.part0"
    existing = body[start : start + 1000]
    part_path.write_bytes(existing)

    with pytest.raises(_RangeNotSatisfiable):
        _download_chunk(fake, "/x", start, end, part_path, None)

    assert part_path.read_bytes() == existing


def test_chunked_resume_after_mid_stream_drop(tmp_path, monkeypatch):
    """The literal bug report, end-to-end: a chunk's connection drops
    after some bytes already landed on disk. The retry must resume from
    that byte offset (`Range: bytes={start+have}-{end}`, append) instead
    of restarting the whole chunk from `start`."""
    monkeypatch.setattr("cli.client._RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))
    body = bytes((i * 7) % 256 for i in range(400_000))
    monkeypatch.setenv("AGNES_PULL_CHUNK_THRESHOLD_BYTES", "1024")
    monkeypatch.setenv("AGNES_PULL_CHUNK_PARALLELISM", "4")

    chunk_size = len(body) // 4
    target_end = chunk_size * 2 - 1  # chunk index 1's fixed end offset
    fake = _ScriptedRangeClient(
        body=body,
        accept_ranges=True,
        script={target_end: ["drop:30000"]},  # first attempt: 30 KB then drop
    )
    _inject_fake_client(monkeypatch, fake)

    from cli.client import stream_download

    target = tmp_path / "out.bin"
    total = stream_download("/api/data/x/download", str(target))

    assert total == len(body)
    assert target.read_bytes() == body
    # The resumed request for chunk 1 must start at its original start +
    # the 30_000 bytes already on disk, not restart at the chunk's start.
    resumed_starts = [s for s, e in fake.range_calls if e == target_end]
    assert resumed_starts == [chunk_size, chunk_size + 30_000]
    assert list(tmp_path.glob("*.part*")) == []


def test_chunked_416_on_resume_restarts_that_chunk_cleanly(tmp_path, monkeypatch):
    """A chunk's resumed Range request comes back 416 (its offset no
    longer lines up with what the server has) — the retry loop must drop
    the partial bytes and re-request the ORIGINAL full range on the next
    attempt, not propagate a hard failure while attempts remain."""
    monkeypatch.setattr("cli.client._RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))
    body = bytes((i * 3) % 256 for i in range(400_000))
    monkeypatch.setenv("AGNES_PULL_CHUNK_THRESHOLD_BYTES", "1024")
    monkeypatch.setenv("AGNES_PULL_CHUNK_PARALLELISM", "4")

    chunk_size = len(body) // 4
    target_end = chunk_size - 1  # chunk index 0
    fake = _ScriptedRangeClient(
        body=body,
        accept_ranges=True,
        script={target_end: ["drop:20000", "not_satisfiable"]},
    )
    _inject_fake_client(monkeypatch, fake)

    from cli.client import stream_download

    target = tmp_path / "out.bin"
    total = stream_download("/api/data/x/download", str(target))

    assert total == len(body)
    assert target.read_bytes() == body
    starts = [s for s, e in fake.range_calls if e == target_end]
    # attempt 0 (start=0): drops after 20_000 bytes.
    # attempt 1 (start=20_000, the resume): scripted 416.
    # attempt 2 (start=0): the 416 handling cleared the partial file, so
    # this is the ORIGINAL range again — and it succeeds.
    assert starts == [0, 20_000, 0]
    assert list(tmp_path.glob("*.part*")) == []


def test_chunked_200_on_resume_falls_back_to_single_stream_with_correct_bytes(
    tmp_path,
    monkeypatch,
):
    """A chunk drops mid-stream, and its RESUME attempt gets 200 instead
    of 206 (server stopped honoring Range) — must never append the
    full-body 200 response onto the partial bytes already on disk. The
    whole chunked path aborts and falls back to a clean single-stream
    download; the final file is still byte-correct."""
    monkeypatch.setattr("cli.client._RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))
    body = bytes((i * 5) % 256 for i in range(400_000))
    monkeypatch.setenv("AGNES_PULL_CHUNK_THRESHOLD_BYTES", "1024")
    monkeypatch.setenv("AGNES_PULL_CHUNK_PARALLELISM", "4")

    chunk_size = len(body) // 4
    target_end = chunk_size * 3 - 1  # chunk index 2
    fake = _ScriptedRangeClient(
        body=body,
        accept_ranges=True,
        script={target_end: ["drop:15000", "ignore_range"]},
    )
    _inject_fake_client(monkeypatch, fake)

    from cli.client import stream_download

    target = tmp_path / "out.bin"
    total = stream_download("/api/data/x/download", str(target))

    assert total == len(body)
    assert target.read_bytes() == body
    assert fake.full_get_calls >= 1
    assert list(tmp_path.glob("*.part*")) == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_download_single_stream_fresh_sends_no_range_header(tmp_path):
    """No pre-existing tmp file — `_download_single_stream` sends the
    exact same request as before resume support: no Range header."""
    from cli.client import _download_single_stream

    body = b"hello world" * 500
    fake = _FakeClient(body=body, accept_ranges=True)
    target = tmp_path / "out.bin"
    progress: list[int] = []

    total = _download_single_stream(fake, "/x", str(target), progress.append)

    assert total == len(body)
    assert target.read_bytes() == body
    assert fake.range_calls == []
    assert fake.full_get_calls == 1
    assert sum(progress) == len(body)


def test_download_single_stream_resumes_from_partial_tmp_file(tmp_path):
    """A `.tmp` file left over from an earlier (failed) attempt inside
    THIS SAME retry loop already holds a genuine prefix of the body — the
    retry must request only the missing tail and append."""
    from cli.client import _download_single_stream

    body = b"abcdefgh" * 4096  # 32768 bytes
    have_bytes = body[:10_000]

    class _ResumeAwareClient(_FakeClient):
        def stream(self, method, path, *, headers=None, **kwargs):
            rng = (headers or {}).get("Range")
            if rng:
                start = int(rng.split("=", 1)[1].split("-", 1)[0])
                with self._lock:
                    self.range_calls.append((start, len(self._body) - 1))
                return _FakeResponse(
                    206,
                    headers={"content-range": f"bytes {start}-{len(self._body) - 1}/{len(self._body)}"},
                    body=self._body[start:],
                )
            return super().stream(method, path, headers=headers, **kwargs)

    fake = _ResumeAwareClient(body=body, accept_ranges=True)
    target = tmp_path / "out.bin"
    tmp_on_disk = Path(f"{target}.{os.getpid()}.tmp")
    tmp_on_disk.write_bytes(have_bytes)
    progress: list[int] = []

    total = _download_single_stream(fake, "/x", str(target), progress.append)

    assert total == len(body)
    assert target.read_bytes() == body
    assert fake.range_calls == [(10_000, len(body) - 1)]
    assert sum(progress) == len(body) - 10_000
    assert not tmp_on_disk.exists()


def test_download_single_stream_200_on_resume_truncates_and_rewrites(tmp_path):
    """Resuming, but the server answers 200 (ignored Range) — must
    truncate and rewrite with the FULL body, never append it onto the
    stale partial bytes."""
    from cli.client import _download_single_stream

    body = b"Z" * 20_000
    stale_partial = b"\x00" * 5_000  # garbage, NOT a prefix of `body`

    class _IgnoresRangeClient(_FakeClient):
        def stream(self, method, path, *, headers=None, **kwargs):
            with self._lock:
                self.full_get_calls += 1
            return _FakeResponse(200, body=self._body)

    fake = _IgnoresRangeClient(body=body, accept_ranges=True)
    target = tmp_path / "out.bin"
    tmp_on_disk = Path(f"{target}.{os.getpid()}.tmp")
    tmp_on_disk.write_bytes(stale_partial)

    total = _download_single_stream(fake, "/x", str(target), None)

    assert total == len(body)
    assert target.read_bytes() == body


def test_download_single_stream_416_restarts_cleanly(tmp_path, monkeypatch):
    """The resumed request 416s (our offset doesn't line up with what
    the server has any more) — the internal retry loop must drop the
    stale bytes and succeed on the next attempt, not propagate a hard
    failure while attempts remain."""
    from cli.client import _download_single_stream

    monkeypatch.setattr("cli.client._RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))
    body = b"Q" * 20_000
    stale_partial = b"\x00" * 5_000
    calls = {"n": 0}

    class _OneShot416Client(_FakeClient):
        def stream(self, method, path, *, headers=None, **kwargs):
            rng = (headers or {}).get("Range")
            calls["n"] += 1
            if rng and calls["n"] == 1:
                return _FakeResponse(416)
            with self._lock:
                self.full_get_calls += 1
            return _FakeResponse(200, body=self._body)

    fake = _OneShot416Client(body=body, accept_ranges=True)
    target = tmp_path / "out.bin"
    tmp_on_disk = Path(f"{target}.{os.getpid()}.tmp")
    tmp_on_disk.write_bytes(stale_partial)

    total = _download_single_stream(fake, "/x", str(target), None)

    assert total == len(body)
    assert target.read_bytes() == body
    assert calls["n"] == 2


def test_download_single_stream_oversized_tmp_restarts_via_416(tmp_path, monkeypatch):
    """A `.tmp` leftover bigger than the actual remote object has no
    fixed "range" to bounds-check against up front (single-stream
    doesn't know the total size) — it relies on the server's 416 for an
    offset beyond the resource, which the retry loop still turns into a
    clean restart."""
    from cli.client import _download_single_stream

    monkeypatch.setattr("cli.client._RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))
    body = b"M" * 10_000
    oversized_stale = b"\x00" * 50_000  # bigger than the real 10_000-byte body

    class _OversizedThen200(_FakeClient):
        def stream(self, method, path, *, headers=None, **kwargs):
            rng = (headers or {}).get("Range")
            if rng:
                return _FakeResponse(416)
            with self._lock:
                self.full_get_calls += 1
            return _FakeResponse(200, body=self._body)

    fake = _OversizedThen200(body=body, accept_ranges=True)
    target = tmp_path / "out.bin"
    tmp_on_disk = Path(f"{target}.{os.getpid()}.tmp")
    tmp_on_disk.write_bytes(oversized_stale)

    total = _download_single_stream(fake, "/x", str(target), None)

    assert total == len(body)
    assert target.read_bytes() == body
