"""B2-NEW — the bash fallback writer emits URLs as YAML double-quoted
scalars; values containing colons, hash, brackets, quotes, or
backslashes round-trip through yaml.safe_load without corrupting the
backend field.

The shim-subprocess round-trip approach was evaluated but is fragile in
this test environment for the same reasons as H4-NEW (see
test_applier_yaml_writer_no_pyyaml.py): the heredoc python path intercepts
`python3 -c 'import yaml'` before any PATH-shim takes effect.  Following
the established H2-NEW / H4-NEW pattern, the third test uses a static
structural assertion instead of a subprocess invocation.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path


APPLIER = Path("scripts/ops/agnes-state-applier.sh")


def _bash_fallback_body() -> str:
    """Extract the pure-bash fallback block from write_instance_yaml."""
    script = APPLIER.read_text()
    m = re.search(
        r"# Pure-bash fallback.*?chown agnes-applier:agnes-applier.*?\n",
        script,
        re.DOTALL,
    )
    assert m is not None, (
        "bash fallback block not found in write_instance_yaml — "
        "script may have been restructured"
    )
    return m.group(0)


def test_bash_fallback_quotes_url_with_query_string_colon() -> None:
    """A URL with `?application_name=a:b` (colon inside a query value)
    must round-trip — pre-fix the bare interpolation produced a
    malformed YAML that read_backend_state silently swallowed.

    B2-NEW: the fallback must NOT emit a bare `url: ${url}` line;
    it must emit a YAML double-quoted scalar.
    """
    body = _bash_fallback_body()
    # Reject the pre-fix shape (bare interpolation):
    assert 'echo "  url: ${url}"' not in body, (
        "B2-NEW regression: url is still interpolated bare; YAML-special "
        "chars in the URL produce malformed YAML that read_backend_state "
        "silently defaults to DuckDB."
    )


def test_bash_fallback_url_is_double_quoted_scalar() -> None:
    """The bash fallback must emit the URL as a YAML double-quoted
    scalar — i.e. the output line is `  url: "<escaped-url>"` — so
    that colons, hashes, brackets, and other YAML-special chars in the
    URL do not corrupt the document structure.

    B2-NEW: `\\` must be escaped before `"` to avoid double-escaping.
    Acceptable forms: printf with explicit quoting, or equivalent sed
    pipeline that escapes `\\` then `"`.
    """
    body = _bash_fallback_body()
    # Must have a printf emitting a double-quoted url scalar.
    has_printf_double_quoted = (
        'printf \'  url: "%s"\\n\'' in body
        or "printf '  url: \"%s\"\\n'" in body
        or 'printf "  url: \\"%s\\"\\n"' in body
    )
    assert has_printf_double_quoted, (
        "B2-NEW: bash fallback url line must use printf with double-quote "
        "wrapping (e.g. `printf '  url: \"%s\"\\n' \"$url_escaped\"`) "
        "rather than bare echo interpolation.\n"
        f"Actual body:\n{body}"
    )


def test_bash_fallback_escapes_backslash_before_quote() -> None:
    r"""The sed escape pipeline must process `\` before `"` to avoid
    double-escaping: escape `\\` → `\\\\` first, then `"` → `\\"`.
    Wrong order (`"` first) would turn `\"` into `\\"` — the `\\` that
    was just inserted would itself get escaped.

    B2-NEW: look for the sed commands in the correct order in the body.
    """
    body = _bash_fallback_body()
    # The sed pipeline must contain both escape substitutions.
    # Order: backslash first (s/\\/\\\\/g), then quote (s/"/\\"/g).
    has_escape_backslash = r's/\\/\\\\/g' in body or "s/\\\\/\\\\\\\\/g" in body
    has_escape_quote = r's/"/\\"/g' in body or 's/\\"/\\\\"/g' in body
    assert has_escape_backslash, (
        r"B2-NEW: bash fallback must escape backslashes via sed "
        r"(s/\\/\\\\/g) before escaping quotes (B2-NEW correct order)"
        f"\nActual body:\n{body}"
    )
    assert has_escape_quote, (
        r'B2-NEW: bash fallback must escape double-quotes via sed '
        r'(s/"/\\"/g) so they are valid inside a YAML double-quoted scalar'
        f"\nActual body:\n{body}"
    )
    # Verify ordering: backslash-escape sed appears before quote-escape sed.
    idx_bs = body.find(r's/\\/\\\\/g')
    idx_q = body.find(r's/"/\\"/g')
    assert idx_bs < idx_q, (
        "B2-NEW: backslash-escape sed must come BEFORE quote-escape sed; "
        "wrong order double-escapes the inserted backslash.\n"
        f"Actual body:\n{body}"
    )


def test_read_backend_state_logs_loudly_on_yaml_error(tmp_path, caplog) -> None:
    """When read_backend_state cannot parse instance.yaml, it must log
    at WARNING (was silent fallback to DUCKDB pre-fix). The fallback
    behaviour itself stays — refusing to boot the app is worse than a
    surfaced warning — but the operator must see the corruption.

    B2-NEW: silent catch → logged catch.

    Note: YAML's block scalar is tolerant of bare colons; only colon
    followed by a space (`: `) produces a YAMLError in block context.
    A URL containing `: ` (e.g. from an `options=-c replication: logical`
    style PG option) is the triggerable case.  We simulate the bash
    fallback's output directly — no subprocess needed.
    """
    from src import db_state_machine

    bad = tmp_path / "instance.yaml"
    # Simulate what the bash fallback emits without quoting when the URL
    # contains `: ` (colon-space = YAML key-value separator in block
    # context → YAMLError).
    bad.write_text(
        "database:\n  backend: cloud\n  url: postgresql://host/db?options=-c replication: logical\n"
    )
    original_path = db_state_machine._OVERLAY_PATH
    try:
        db_state_machine._OVERLAY_PATH = bad
        with caplog.at_level(logging.WARNING, logger="src.db_state_machine"):
            state, url = db_state_machine.read_backend_state()
        # Behaviour: still falls back to (DUCKDB, None) — failing safe.
        assert state.value == "duckdb", f"expected duckdb fallback, got {state!r}"
        assert url is None
        # Contract: the warning is emitted with text identifying the
        # parse failure and the file path.
        assert any(
            "yaml" in rec.message.lower() and "parse" in rec.message.lower()
            for rec in caplog.records
        ), (
            "B2-NEW: read_backend_state must log a WARNING mentioning "
            "'yaml' and 'parse' when instance.yaml is malformed.\n"
            f"Captured records: {[r.message for r in caplog.records]}"
        )
    finally:
        db_state_machine._OVERLAY_PATH = original_path
