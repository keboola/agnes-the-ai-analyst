"""A downloaded part is verified against the bytes the server says it sent.

The manifest hash is a CACHE KEY — "has this part changed since I last pulled" —
captured when `sync_state` was last rebuilt. The manifest and the parts are fetched
in separate requests, so on a dataset that is being rewritten between them the two
can legitimately disagree. `agnes pull` used to read that disagreement as
corruption and abort the whole table, which is the user-visible failure that
started this work:

    part month=2025-06/data.parquet hash mismatch: expected 6fa1fa…, got 79ad93…

No amount of re-hashing on the server closes that window — it is inherent to
fetching the plan and the data in two requests. So the bytes describe themselves:
the part response carries `X-Agnes-Content-MD5`, the md5 of the bytes that response
actually sent, and the client trusts THAT for integrity while still using the
manifest hash to decide what to fetch.

The header is trustworthy because the server reads the file ONCE and both hashes
and serves that single buffer — with partition writes publishing via `os.replace`,
a second read by path could see a different inode entirely.
"""

import hashlib
import inspect
from pathlib import Path

import pytest

from app.api.data import _SELF_DESCRIBING_MAX_BYTES, _serve_part_self_describing
from cli.lib import pull as pull_lib
from src.distribution import CONTENT_MD5_HEADER

STALE = "0" * 32


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry paths here are about which branch is taken, not how long it
    waits; without this each failing case burns a real backoff."""
    monkeypatch.setattr(pull_lib.time, "sleep", lambda _s: None)


def _write(dest: Path, payload: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)


def _fetcher(payload: bytes, served: str = "", calls: list | None = None):
    """A `fetch_part` writing *payload* and claiming the server sent *served*.

    ``served=""`` models a server old enough not to send the header at all.
    """

    def fetch_part(relpath: str, dest: Path) -> str:
        if calls is not None:
            calls.append(relpath)
        _write(dest, payload)
        return served

    return fetch_part


def _md5(payload: bytes) -> str:
    return hashlib.md5(payload).hexdigest()


# --------------------------------------------------------------------------------
# Client: the served hash is the arbiter, the manifest hash is the cache key.
# --------------------------------------------------------------------------------


def test_a_stale_manifest_hash_no_longer_fails_the_part(tmp_path: Path) -> None:
    """The exact reported failure: the manifest predicted one hash, the server
    served different-but-intact bytes, and the transfer was perfect."""
    payload = b"PAR1-rewritten-since-the-manifest-was-built"
    calls: list[str] = []
    dest = tmp_path / "part.parquet"

    err = pull_lib._fetch_part_with_retry(
        _fetcher(payload, _md5(payload), calls), "month=2025-06/data.parquet", dest, STALE
    )

    assert err is None, err
    assert dest.read_bytes() == payload
    assert len(calls) == 1, "a stale manifest must not even cost a retry"


def test_bytes_matching_neither_are_rejected_and_not_blamed_on_staleness(tmp_path: Path) -> None:
    """Genuine corruption: the body matches neither the manifest nor what the
    server said it sent. It must still fail — the header widens what counts as
    valid, it does not disable verification.

    And it must NOT be reported as "the published hash is stale ... needs a
    rebuild". That diagnosis belongs to a header-less server; conflating the two
    sends an operator to entirely the wrong place.
    """
    err = pull_lib._fetch_part_with_retry(
        _fetcher(b"corrupted-in-flight", _md5(b"what-the-server-meant-to-send")),
        "month=2025-06/data.parquet",
        tmp_path / "p.parquet",
        STALE,
    )

    assert err is not None
    assert "hash mismatch" in err
    assert "stale" not in err, f"corruption misreported as a stale manifest: {err}"


def test_a_server_without_the_header_keeps_the_old_behaviour(tmp_path: Path) -> None:
    """Older server: no header, so the manifest hash is the only arbiter. A
    mismatch is still an error, and still gets the stale-manifest diagnosis that
    names the real cause."""
    payload = b"PAR1-body"

    err = pull_lib._fetch_part_with_retry(
        _fetcher(payload), "month=2025-06/data.parquet", tmp_path / "p.parquet", STALE
    )
    assert err is not None
    assert "hash mismatch" in err and "stale" in err

    # ...and when the manifest IS current it verifies exactly as before.
    assert (
        pull_lib._fetch_part_with_retry(
            _fetcher(payload), "month=2025-06/data.parquet", tmp_path / "q.parquet", _md5(payload)
        )
        is None
    )


def test_the_local_record_keeps_the_manifest_hash_not_the_served_one(tmp_path: Path) -> None:
    """The record is only ever compared against the NEXT manifest (`_diff_parts`),
    so it answers "which published version have I reconciled against". Recording
    the served hash instead would make every later pull see local != server and
    re-fetch the same part until the server happened to rebuild.
    """
    payload = b"PAR1-fresher-than-the-manifest"
    parquet_dir = tmp_path / "parquet"
    parquet_dir.mkdir()
    part = {"path": "month=2025-06/data.parquet", "hash": STALE, "size_bytes": len(payload)}

    entry, changed, err = pull_lib._sync_partitioned_table(
        "issues", [part], {}, parquet_dir, _fetcher(payload, _md5(payload)), "rollup"
    )

    assert err is None, err
    assert changed is True
    assert entry["parts"]["month=2025-06/data.parquet"] == STALE
    assert (parquet_dir / "issues" / "month=2025-06" / "data.parquet").read_bytes() == payload


def test_a_second_pull_against_the_same_stale_manifest_is_a_no_op(tmp_path: Path) -> None:
    """Follows from the record above: the part is not re-fetched while the server
    keeps publishing the hash we already reconciled against."""
    payload = b"PAR1-settled"
    fetches: list[str] = []
    parquet_dir = tmp_path / "parquet"
    parquet_dir.mkdir()
    part = {"path": "month=2025-06/data.parquet", "hash": STALE, "size_bytes": len(payload)}
    fetch = _fetcher(payload, _md5(payload), fetches)

    entry, _, err = pull_lib._sync_partitioned_table("issues", [part], {}, parquet_dir, fetch, "rollup")
    assert err is None

    _, changed2, err2 = pull_lib._sync_partitioned_table("issues", [part], entry["parts"], parquet_dir, fetch, "rollup")

    assert err2 is None
    assert changed2 is False, "nothing changed; this must be a no-op"
    assert len(fetches) == 1, f"re-fetched a part it already held: {fetches}"


# --------------------------------------------------------------------------------
# Server: the header describes the bytes actually sent.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, 5000, 1024 * 1024 + 7])
def test_server_tags_the_part_with_the_md5_of_what_it_sent(tmp_path: Path, size: int) -> None:
    payload = bytes(i % 251 for i in range(size))
    src = tmp_path / "data.parquet"
    src.write_bytes(payload)

    response = _serve_part_self_describing(src, etag='"1"', is_range_request=False)

    assert response.body == payload
    assert response.headers[CONTENT_MD5_HEADER] == _md5(payload)
    assert response.headers["content-length"] == str(len(payload))


def test_a_part_too_large_to_buffer_falls_back_instead_of_growing_unbounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_resolve_part_path` imposes no size bound, and the buffered read is linear
    in part size. Above the cap the part is served the pre-existing way — no
    header, client verifies against the manifest — rather than trading a bounded
    cost for an unbounded one."""
    monkeypatch.setattr("app.api.data._SELF_DESCRIBING_MAX_BYTES", 16)
    src = tmp_path / "data.parquet"
    src.write_bytes(b"x" * 64)

    response = _serve_part_self_describing(src, etag='"1"', is_range_request=False)

    assert CONTENT_MD5_HEADER not in response.headers
    assert response.__class__.__name__ == "FileResponse"


def test_a_range_request_still_gets_a_206_and_only_the_bytes_asked_for(tmp_path: Path) -> None:
    """`cli/client.py::_probe_range_support` probes EVERY download with
    `Range: bytes=0-0` and drains whatever comes back. A buffered response cannot
    answer a range, and silently returning 200 with the full body would make every
    part transfer twice per pull — on exactly the tables this change is for. Range
    requests therefore fall back to `FileResponse`, which answers them properly.
    """
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    payload = b"x" * 100_000
    src = tmp_path / "data.parquet"
    src.write_bytes(payload)

    app = FastAPI()

    @app.get("/p")
    def _p(request: Request):
        return _serve_part_self_describing(
            src, etag='"1"', is_range_request=bool(request.headers.get("range"))
        )

    client = TestClient(app)
    probe = client.get("/p", headers={"Range": "bytes=0-0"})
    assert probe.status_code == 206, "a ranged probe must not get the whole body"
    assert len(probe.content) == 1, f"probe drained {len(probe.content)} bytes"
    assert probe.headers.get("accept-ranges") == "bytes"

    # The real fetch sends no Range, so it still gets the digest.
    full = client.get("/p")
    assert full.status_code == 200
    assert full.content == payload
    assert full.headers[CONTENT_MD5_HEADER] == _md5(payload)


def test_the_cap_leaves_real_parts_far_below_it() -> None:
    """Partition parts run ~5 KB to ~450 KB. The cap is headroom, not a limit
    anyone should meet — and it sits well under the client's own 50 MiB chunking
    threshold, above which it stops reading the header anyway."""
    assert _SELF_DESCRIBING_MAX_BYTES >= 8 * 1024 * 1024


def test_only_part_downloads_take_the_self_describing_path() -> None:
    """Whole-table downloads can be hundreds of MB and must keep `FileResponse`.
    Asserted by position rather than exact spelling, so a rename or a reflow does
    not masquerade as a behaviour change."""
    from app.api import data as data_api

    src = inspect.getsource(data_api.download_table)
    assert src.index("if part is not None:") < src.index("_serve_part_self_describing")
    assert "FileResponse(" in src, "whole-table path must still use FileResponse"
