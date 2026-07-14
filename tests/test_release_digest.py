"""scripts/release_digest.py — pure-function coverage for the Slack release digest.

The IO shell (GH API fetch, webhook POST) is exercised only by the workflow's
dry-run mode; these tests pin the windowing, changelog parsing, aggregation
order, and Slack-payload caps.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "release_digest", Path(__file__).resolve().parents[1] / "scripts" / "release_digest.py"
)
rd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rd)


def _rel(tag, created, body="", **kw):
    return {"tag_name": tag, "created_at": created, "body": body, "draft": False, "prerelease": False, **kw}


BODY = """### Added
- Unified knowledge search: one query across [collections](https://x) and more (#797).

### Fixed
- **Security:** `scope='per_user'` MCP sources fail closed.
  Continuation line that must not become its own bullet.

### Internal
- Schema v89 bump (not a digest section).
"""


class TestFilterReleases:
    def test_window_is_strictly_after_since_and_sorted_oldest_first(self):
        rels = [
            _rel("v3", "2026-07-13T10:00:00Z"),
            _rel("v1", "2026-07-12T06:00:00Z"),
            _rel("v2", "2026-07-12T18:00:00Z"),
        ]
        out = rd.filter_releases(rels, "2026-07-12T06:00:00Z")
        assert [r["tag_name"] for r in out] == ["v2", "v3"]  # v1 == since → excluded

    def test_drafts_and_prereleases_excluded(self):
        rels = [
            _rel("v1", "2026-07-13T10:00:00Z", draft=True),
            _rel("v2", "2026-07-13T11:00:00Z", prerelease=True),
            _rel("v3", "2026-07-13T12:00:00Z"),
        ]
        assert [r["tag_name"] for r in rd.filter_releases(rels, "2026-07-12T00:00:00Z")] == ["v3"]


class TestExtractHighlights:
    def test_sections_parsed_links_and_emphasis_stripped(self):
        h = rd.extract_highlights(BODY)
        assert h["Added"] == ["Unified knowledge search: one query across collections and more (#797)."]
        # Wrapped continuation lines join the owning bullet (no mid-sentence cuts).
        assert h["Fixed"] == [
            "Security: scope='per_user' MCP sources fail closed. Continuation line that must not become its own bullet."
        ]
        assert "Internal" not in h  # only Added/Changed/Fixed/Removed surface

    def test_long_bullets_truncated(self):
        h = rd.extract_highlights("### Added\n- " + "x" * 500)
        assert len(h["Added"][0]) <= rd.MAX_BULLET_CHARS
        assert h["Added"][0].endswith("…")

    def test_empty_body(self):
        assert rd.extract_highlights("") == {}
        assert rd.extract_highlights(None) == {}


class TestBuildMessage:
    def test_single_release_header_and_links(self):
        msg = rd.build_message([_rel("v0.74.62", "2026-07-13T10:00:00Z", BODY)], "acme/agnes")
        assert "1 release (v0.74.62)" in msg["text"]
        ctx = msg["blocks"][2]["elements"][0]["text"]
        assert "releases/tag/v0.74.62" in ctx and "CHANGELOG.md" in ctx

    def test_multi_release_span_and_section_grouping(self):
        rels = [
            _rel("v1", "2026-07-13T01:00:00Z", "### Fixed\n- fix one\n"),
            _rel("v2", "2026-07-13T02:00:00Z", "### Added\n- feat two\n"),
        ]
        msg = rd.build_message(rels, "acme/agnes")
        assert "2 releases (v1 → v2)" in msg["text"]
        body = msg["blocks"][1]["text"]["text"]
        # Added group renders before Fixed regardless of release order.
        assert body.index("feat two") < body.index("fix one")

    def test_highlight_cap_adds_more_marker(self):
        body = "### Added\n" + "\n".join(f"- bullet {i}" for i in range(30))
        msg = rd.build_message([_rel("v1", "2026-07-13T01:00:00Z", body)], "acme/agnes")
        text = msg["blocks"][1]["text"]["text"]
        assert text.count("• ") == rd.MAX_HIGHLIGHTS
        assert "and more" in text

    def test_no_bullets_fallback_line(self):
        msg = rd.build_message([_rel("v1", "2026-07-13T01:00:00Z", "prose only")], "acme/agnes")
        assert "No changelog bullets" in msg["blocks"][1]["text"]["text"]
