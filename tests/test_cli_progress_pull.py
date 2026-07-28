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
