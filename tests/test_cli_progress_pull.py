"""Progress emitter never reports >100% even when the advertised
`total` is wrong (Issue #258)."""

from io import StringIO


def test_progress_pct_capped_at_100_when_total_underestimates():
    """When bytes received exceed advertised total, the emitted
    percentage clamps to 100% — operator never sees '174%'."""
    from cli.lib.pull import _TextualProgress

    stream = StringIO()
    emitter = _TextualProgress(
        stream=stream,
        total_files=1,
        file_sizes={"orders": 1_000_000},  # advertised: 1 MB
    )

    # Force-emit every line: tighten cadence so any 10% boundary counts.
    emitter._interval_seconds = 0.0
    emitter._interval_bytes = 1

    # Push 1.7 MB (170% of advertised) in chunks.
    for _ in range(17):
        emitter.advance("orders", 100_000)
    emitter.finish()

    output = stream.getvalue()
    # Find every printed percentage and assert <= 100.
    import re
    pcts = [int(m.group(1)) for m in re.finditer(r"orders: (\d+)%", output)]
    assert pcts, f"no percentage lines emitted: {output!r}"
    assert all(p <= 100 for p in pcts), (
        f"percentages exceeded 100%: {pcts}\nfull output: {output}"
    )


def test_progress_pct_normal_when_total_accurate():
    """Sanity: when bytes match advertised total, emitter still walks 0→100."""
    from cli.lib.pull import _TextualProgress

    stream = StringIO()
    emitter = _TextualProgress(
        stream=stream,
        total_files=1,
        file_sizes={"t": 1_000_000},
    )
    emitter._interval_seconds = 0.0
    emitter._interval_bytes = 1
    for _ in range(10):
        emitter.advance("t", 100_000)
    emitter.finish()

    import re
    pcts = [int(m.group(1)) for m in re.finditer(r"t: (\d+)%", stream.getvalue())]
    assert max(pcts) == 100


def test_failed_transfer_is_not_reported_as_done():
    """A file that never received its bytes must not print "100% done".

    Live symptom (analyst persona, `agnes pull` against a table whose
    download 403'd): the summary read

        [1/1 files] orders: 100% done (0 B in 0.0s, 0 B/s)

    with the real error buried below it. A green completion line for a
    failure is worse than no line — the analyst reported the pull as
    successful and went looking for the data.
    """
    from io import StringIO

    from cli.lib.pull import _TextualProgress

    stream = StringIO()
    emitter = _TextualProgress(
        stream=stream,
        total_files=1,
        file_sizes={"orders": 1_000_000},
    )
    # No advance() at all — this is the 403 shape: bytes never arrived.
    emitter.finish()

    output = stream.getvalue()
    assert "100% done" not in output, output
    assert "FAILED" in output, output
    assert "orders" in output


def test_partial_transfer_is_not_reported_as_done():
    """Same guard for a transfer that started and then died mid-flight.

    Worded INCOMPLETE rather than FAILED: unlike the zero-byte case, this
    verdict is inferred from the manifest's `size_bytes`, and this module
    already documents that the manifest and the streamed length can disagree.
    A hard "FAILED — see the error below" with no error below it would be a
    lie in the opposite direction to the one the guard removes.
    """
    from io import StringIO

    from cli.lib.pull import _TextualProgress

    stream = StringIO()
    emitter = _TextualProgress(
        stream=stream,
        total_files=1,
        file_sizes={"orders": 1_000_000},
    )
    emitter.advance("orders", 250_000)
    emitter.finish()

    output = stream.getvalue()
    assert "100% done" not in output, output
    assert "INCOMPLETE" in output, output
    assert "see the error below" not in output, "asserts an error the pull may never print"


def test_a_file_with_no_declared_size_is_not_reported_as_done_either():
    """Devin Review on this PR: the guard was inert without a manifest size.

    `if total and bytes_ < total` skips every row the server reported without
    a size — precisely the files the printer can say the least about — so a
    failed download of one still printed "100% done (0 B in 0.0s, 0 B/s)",
    which is the exact line this change set exists to remove.
    """
    from io import StringIO

    from cli.lib.pull import _TextualProgress

    stream = StringIO()
    # `cli/lib/pull.py` builds this as `int(row.get("size_bytes") or 0)`, so a
    # manifest row without a size lands here as 0 — NOT as a missing key.
    emitter = _TextualProgress(stream=stream, total_files=1, file_sizes={"orders": 0})
    emitter.finish()

    output = stream.getvalue()
    assert "100% done" not in output, output
    assert "FAILED" in output, output


def test_a_completed_transfer_of_unknown_size_still_reads_as_done():
    """The guard must not turn every size-less row into a false failure."""
    from io import StringIO

    from cli.lib.pull import _TextualProgress

    stream = StringIO()
    emitter = _TextualProgress(stream=stream, total_files=1, file_sizes={"orders": 0})
    emitter.advance("orders", 250_000)
    emitter.finish()

    output = stream.getvalue()
    assert "100% done" in output, output
    assert "FAILED" not in output, output


def test_a_corrupted_download_is_not_announced_as_complete():
    """Devin Review on this PR: the byte counter cannot see the real failure.

    A hash mismatch is the most common download failure, and on it every byte
    DOES arrive — the counter reaches the manifest size, the file is never
    promoted to disk, and the finalizer printed "100% done". That is the exact
    green-line-for-a-failure this change set removes, surviving on the path it
    matters most.
    """
    from io import StringIO

    from cli.lib.pull import _TextualProgress

    stream = StringIO()
    emitter = _TextualProgress(stream=stream, total_files=1, file_sizes={"orders": 1_000_000})
    emitter.advance("orders", 1_000_000)  # every byte arrived …
    emitter.fail("orders", "integrity check failed")  # … and it still did not land
    emitter.finish()

    output = stream.getvalue()
    assert "100% done" not in output, output
    assert "FAILED" in output, output
    assert "integrity check failed" in output, output


def test_the_caller_verdict_beats_a_complete_byte_count():
    """Ordering: the explicit failure must be checked before the count."""
    import inspect

    from cli.lib.pull import _TextualProgress

    src = inspect.getsource(_TextualProgress.finish)
    assert src.index("self._failed") < src.index("if not bytes_"), (
        "a fully-counted transfer would take the done branch before the failure is seen"
    )


def test_every_download_failure_path_tells_the_progress_printer():
    """A failure return that skips `fail_progress` prints a green line.

    Checked per return statement rather than by counting: each `return tid,
    None, ...` (the failure shape — no manifest entry) must have announced
    itself in the lines immediately before it.
    """
    from cli.lib import pull as mod

    src = open(mod.__file__).read()
    start = src.index("def _download_one")
    end = src.index("if workers <= 1:", start)
    body = src[start:end]

    chunks = body.split("return tid, None,")
    assert len(chunks) - 1 == 3, f"expected 3 failure returns, found {len(chunks) - 1}"
    for n, preceding in enumerate(chunks[:-1], start=1):
        assert "fail_progress(" in preceding[-400:], (
            f"failure return #{n} does not tell the progress printer - it will print a done line"
        )
