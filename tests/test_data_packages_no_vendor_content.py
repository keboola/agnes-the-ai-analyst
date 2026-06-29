"""Vendor-agnostic guard for v56 content fields.

The OSS repo provides the *surfaces* for extended Data Packages content
(schema, API, UI) but MUST NOT ship customer-specific content as default
seed data. Per CLAUDE.md "Vendor-agnostic OSS — no customer-specific
content" — Groupon-specific package descriptions, owner names, etc. live
in the private infra repo's admin-import flow, not in this repo's seeds
or fixtures.

This test fails if a future commit accidentally lands the example MD's
proprietary tokens in repository code paths.
"""

from __future__ import annotations

import pathlib

import pytest


# Tokens lifted from the colleague's extended-descriptions spec MD. If
# any of these appear in OSS source — code, comments, defaults, seed
# scripts, migrations — that's the vendor-specific leak this test
# catches. Tests + brainstorms/superpowers dirs are intentionally
# excluded; tests can mention generic strings for assertions, and
# brainstorms/ is gitignored anyway.
_GROUPON_SPECIFIC = (
    "Pavel Cernik",
    "Foundry Data team",
    "transactional_core",
    "s1_session_landings",
    "s4a_search_deal_detail",
    "ds1_session_deal_impressions",
    "xs1_session_experiments",
    "groupon_version",
    "user_brand_affiliation",
)

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_SCAN_DIRS = ("app", "src", "cli", "config", "scripts")
_EXCLUDED_NAMES = {
    "__pycache__", ".venv", "node_modules", ".git",
    "brainstorms", "superpowers", ".claude",
}


def _candidate_files():
    for top in _SCAN_DIRS:
        for path in (_REPO_ROOT / top).rglob("*"):
            if not path.is_file():
                continue
            if any(part in _EXCLUDED_NAMES for part in path.parts):
                continue
            if path.suffix in (".pyc", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                               ".woff", ".woff2", ".ttf", ".ico", ".duckdb"):
                continue
            yield path


def test_no_groupon_specific_strings_in_oss():
    """Scan app/ + src/ + cli/ + config/ + scripts/ for Groupon-specific
    tokens from the extended-descriptions spec. Any hit means a future
    commit accidentally leaked customer content into the OSS surfaces."""
    hits: dict[str, list[str]] = {}
    for path in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in _GROUPON_SPECIFIC:
            if token in text:
                hits.setdefault(str(path.relative_to(_REPO_ROOT)), []).append(token)

    if hits:
        msg_lines = ["Customer-specific tokens leaked into OSS:"]
        for f, tokens in sorted(hits.items()):
            msg_lines.append(f"  {f}: {', '.join(sorted(set(tokens)))}")
        msg_lines.append(
            "\nMove the offending content to the private infra repo's "
            "admin-import flow. OSS seeds + defaults stay generic."
        )
        pytest.fail("\n".join(msg_lines))
