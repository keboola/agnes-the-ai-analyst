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

import ast
import hashlib
import inspect
import textwrap
from pathlib import Path

import httpx
import pytest

from app.api.data import _SELF_DESCRIBING_MAX_BYTES, _serve_part_self_describing
from cli.lib import pull as pull_lib
from src.distribution import CONTENT_MD5_HEADER
from tests.test_pull_chunked import _FakeResponse

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
        return _serve_part_self_describing(src, etag='"1"', is_range_request=bool(request.headers.get("range")))

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

    AST-anchored to the If that actually routes. An earlier source-index check
    matched the FIRST `if part is not None:` in the function — `download_table`
    has three — so a single-token inversion of the routing branch (whole tables
    buffered and hashed, parts served unhashed) passed it with every test green.
    """
    from app.api import data as data_api

    tree = ast.parse(textwrap.dedent(inspect.getsource(data_api.download_table)))
    routing = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(stmt, ast.Return)
            and isinstance(stmt.value, ast.Call)
            and getattr(stmt.value.func, "id", "") == "_serve_part_self_describing"
            for stmt in node.body
        )
    ]
    assert len(routing) == 1, "exactly one If must route to _serve_part_self_describing"
    assert ast.unparse(routing[0].test) == "part is not None", (
        "the routing branch must be exactly `if part is not None:` — truthiness "
        "would silently reroute `?part=` to the whole table"
    )
    # And the whole-table fallthrough still streams.
    assert any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "FileResponse"
        for node in ast.walk(tree)
    ), "whole-table path must still use FileResponse"


# --------------------------------------------------------------------------------
# The header plumbing itself: stream_download -> headers_out -> the pull layer.
# Previously zero coverage — a refactor dropping headers_out reverted every pull
# to manifest-only verification (served == "" is the supported old-server
# fallback) with the entire suite green.
# --------------------------------------------------------------------------------


class _FakeClient:
    """Scripted client: pops responses in order, records each request's headers.

    The response type is `test_pull_chunked._FakeResponse` — the suite's
    existing fake for exactly this code under test; it is its own context
    manager and its `raise_for_status` really raises on 4xx.
    """

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.requests: list[dict | None] = []

    def stream(self, method: str, path: str, headers: dict | None = None) -> _FakeResponse:
        self.requests.append(headers)
        return self._responses.pop(0)


def test_headers_out_carries_the_served_hash(tmp_path: Path) -> None:
    from cli import client as cli_client

    payload = b"PAR1-part-bytes"
    served = _md5(payload)
    fake = _FakeClient([_FakeResponse(200, {"x-agnes-content-md5": served}, body=payload)])
    headers_out: dict = {}

    total = cli_client._download_single_stream(fake, "/p", str(tmp_path / "part"), None, headers_out)

    assert total == len(payload)
    assert (tmp_path / "part").read_bytes() == payload
    # Case-insensitive lookup: the server spells it X-Agnes-Content-MD5.
    assert httpx.Headers(headers_out).get(CONTENT_MD5_HEADER) == served


def test_a_resumed_download_clears_the_stale_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Attempt 1's 200 populated headers_out with the md5 of the FULL
    representation, then died mid-body; the resumed 206 (served via
    FileResponse, so no md5 header) supplied the tail. The final file is
    spliced from two responses, so the stale header must be CLEARED —
    pairing it with the spliced bytes misreports a perfect transfer as
    corruption, or a corrupted one as perfect."""
    from cli import client as cli_client

    monkeypatch.setattr("cli.client._RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))
    full = b"0123456789abcdef"
    fake = _FakeClient(
        [
            _FakeResponse(200, {"x-agnes-content-md5": _md5(full)}, body=full, fail_after_bytes=8),
            _FakeResponse(206, {"content-range": "bytes 8-15/16"}, body=full[8:]),
        ]
    )
    headers_out: dict = {}

    cli_client._download_single_stream(fake, "/p", str(tmp_path / "part"), None, headers_out)

    assert (tmp_path / "part").read_bytes() == full
    assert fake.requests[1] == {"Range": "bytes=8-"}, "the retry did not resume from the kept prefix"
    assert httpx.Headers(headers_out).get(CONTENT_MD5_HEADER) is None, (
        "a stale header from the dead first attempt survived the resume"
    )


def test_the_pull_layer_returns_the_header_via_the_real_plumbing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_fetch_part_via_download` end to end: it must hand stream_download a
    headers_out dict and read the md5 back case-insensitively. This is the seam
    `run_pull`'s fetcher rides; dropping either half degrades every pull to
    manifest-only verification, silently."""
    payload = b"PAR1-body"

    def _fake_stream_download(path, target, progress_callback=None, headers_out=None):
        assert "part=month%3D2025-06/data.parquet" in path
        Path(target).write_bytes(payload)
        assert headers_out is not None, "the pull layer stopped passing headers_out"
        headers_out.clear()
        headers_out.update({"X-AGNES-CONTENT-MD5": _md5(payload)})
        return len(payload)

    monkeypatch.setattr(pull_lib, "stream_download", _fake_stream_download)
    dest = tmp_path / "data.parquet"

    served = pull_lib._fetch_part_via_download("issues", "month=2025-06/data.parquet", dest)

    assert served == _md5(payload)
    assert dest.read_bytes() == payload


def test_the_route_serves_parts_with_the_header_and_whole_tables_without(seeded_app) -> None:
    """The behavioral pin for the routing branch, end to end through the app:
    a ?part= download answers 200 + the exact bytes + X-Agnes-Content-MD5; the
    whole-table download streams with no header; an EMPTY ?part= is a 404, not
    a fallthrough to the whole table. The AST guard above is the structural
    canary; this is the test that fails when routing actually breaks."""
    from tests.conftest import create_mock_extract

    c = seeded_app["client"]
    env = seeded_app["env"]
    auth = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    create_mock_extract(env["extracts_dir"], "keboola", [{"name": "parts_probe", "data": [{"id": "1"}]}])
    c.post(
        "/api/admin/register-table",
        json={"name": "parts_probe", "source_type": "keboola"},
        headers=auth,
    )
    payload = b"PAR1-route-level-part-bytes"
    part_file = env["extracts_dir"] / "keboola" / "data" / "parts_probe" / "month=2026-06" / "data.parquet"
    part_file.parent.mkdir(parents=True, exist_ok=True)
    part_file.write_bytes(payload)

    part_resp = c.get(
        "/api/data/parts_probe/download",
        params={"part": "month=2026-06/data.parquet"},
        headers=auth,
    )
    assert part_resp.status_code == 200
    assert part_resp.content == payload
    assert part_resp.headers[CONTENT_MD5_HEADER] == _md5(payload)

    whole_resp = c.get("/api/data/parts_probe/download", headers=auth)
    assert whole_resp.status_code == 200
    assert CONTENT_MD5_HEADER not in whole_resp.headers, "whole tables must not take the buffered path"

    empty = c.get("/api/data/parts_probe/download?part=", headers=auth)
    assert empty.status_code == 404, "an empty part must not fall through to the whole table"
