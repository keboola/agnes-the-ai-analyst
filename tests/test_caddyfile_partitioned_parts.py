"""Contract: the Caddy static fast path must never answer a `?part=` request.

`path_regexp` matches the path only, so without an explicit `not query part=*`
a per-part fetch claims the whole-table rule — whose every candidate is the
single-file `<table_id>.parquet` — and the analyst gets the entire table served
as one month. Full rationale sits next to the matcher in the Caddyfile.

Text assertions, no Caddy binary needed — same approach as
`test_caddyfile_sse_flush.py`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CADDYFILE = _ROOT / "Caddyfile"

# Anchored at line start so a `#` comment mentioning @download cannot match —
# the same hazard `_is_directive_start` guards in test_caddyfile_sse_flush.py.
# `[^{}]*` deliberately refuses to match a nested block: the matcher is flat
# today, and a future nested form should fail loudly here rather than be
# silently mis-parsed.
_DOWNLOAD_MATCHER = re.compile(r"^\s*@download\s*\{(?P<body>[^{}]*)\}", re.MULTILINE)


def _download_matcher_body(text: str) -> str:
    m = _DOWNLOAD_MATCHER.search(text)
    assert m is not None, (
        "could not find an `@download { ... }` matcher block in the Caddyfile. "
        "It was either removed, reverted to the one-line `@download path_regexp …` "
        "form (which cannot carry the `not query part=*` exclusion), or nested."
    )
    return " ".join(m.group("body").split())


def test_download_fast_path_excludes_part_requests():
    """A `?part=` request must not match the file_server fast path."""
    body = _download_matcher_body(_CADDYFILE.read_text())
    assert "not query part=*" in body, (
        "the @download matcher must exclude `?part=` requests "
        "(`not query part=*`); without it the static path serves the whole "
        f"table for a single part. Got: {body!r}"
    )


def test_download_fast_path_still_matches_whole_table_downloads():
    """The exclusion must not cost the fast path its actual job — whole-table
    downloads still have to hit file_server, or every multi-GB pull goes back
    through uvicorn and starves the app workers."""
    body = _download_matcher_body(_CADDYFILE.read_text())
    assert "path_regexp tid ^/api/data/([^/]+)/download$" in body, (
        "the @download matcher must still match whole-table download paths "
        "with the named `tid` capture the try_files candidates depend on"
    )


def test_try_files_candidates_are_all_single_file_parquets():
    """Pins the premise the exclusion rests on: every static candidate is
    `<table_id>.parquet`. Adding a new connector's dir keeps this green; only
    a SHAPE change (partition-aware static serving) fails it, which is exactly
    when the exclusion should be reconsidered rather than silently kept."""
    text = _CADDYFILE.read_text()
    line = next((ln for ln in text.splitlines() if ln.strip().startswith("try_files")), None)
    assert line is not None, "the @download handler no longer has a try_files directive"
    static = [c for c in line.split()[1:] if not c.startswith("/api/")]
    assert static, "expected at least one static parquet candidate"
    for c in static:
        assert c.endswith("/{re.tid.1}.parquet"), (
            f"try_files candidate {c!r} is not a single-file <table_id>.parquet; "
            "if partitioned parts are now served statically, revisit the "
            "`not query part=*` exclusion on @download"
        )


def _caddy_configs() -> list[Path]:
    return sorted({*_ROOT.glob("Caddyfile*"), *(_ROOT / "deploy" / "caddy").glob("Caddyfile*")})


def test_every_download_fast_path_anywhere_excludes_parts():
    """Invariant, not a point fix: today only the root Caddyfile has a static
    download fast path (`deploy/caddy/Caddyfile.mtier` proxies everything to
    the app). If another deployment config grows one, it inherits the same
    query-string blindness — so require the exclusion wherever the rule
    appears."""
    checked = 0
    for cfg in _caddy_configs():
        text = cfg.read_text()
        if "file_server" not in text:
            continue  # no static serving here — nothing to bypass the app
        for m in re.finditer(r"^\s*@\w+\s*\{(?P<body>[^{}]*)\}", text, re.MULTILINE):
            body = " ".join(m.group("body").split())
            if "/api/data/" in body and "/download" in body:
                checked += 1
                assert "not query part=*" in body, (
                    f"{cfg.name}: a matcher claiming /api/data/…/download feeds a "
                    "static file_server path but does not exclude `?part=` — "
                    "partitioned parts would be served the whole table"
                )
    assert checked, "expected at least one download fast-path matcher (the root Caddyfile)"


@pytest.mark.skipif(shutil.which("caddy") is None, reason="caddy binary not installed")
def test_caddyfile_is_syntactically_valid():
    """The matcher moved from a one-line form to a block. A malformed block
    takes the whole edge down on reload, not just downloads — worth checking
    wherever a caddy binary happens to exist."""
    proc = subprocess.run(
        ["caddy", "validate", "--adapter", "caddyfile", "--config", str(_CADDYFILE)],
        capture_output=True,
        text=True,
        check=False,  # assert on returncode below so the report shows caddy's own message
        env={
            **os.environ,
            "DOMAIN": "example.com",
            # `tls internal` on purpose: the Caddyfile's default is
            # `tls /certs/fullchain.pem …`, and `validate` provisions the TLS
            # app, so without this it fails on the missing cert file rather
            # than on anything to do with the config's syntax.
            "CADDY_TLS": "tls internal",
        },
    )
    assert proc.returncode == 0, f"caddy validate failed:\n{proc.stdout}\n{proc.stderr}"
