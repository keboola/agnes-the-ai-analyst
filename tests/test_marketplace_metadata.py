"""Unit tests for the agnes-metadata.json parser.

Covers the lenient-parse contract (missing file / malformed JSON / wrong-type
top level all degrade to empty) plus the per-plugin / per-skill resolution
that produces dicts ready for the DB write layer.
"""

from __future__ import annotations

import json

from src.marketplace_assets import DocLinkRef, parse_doc_link
from src.marketplace_metadata import (
    collect_all_external_urls,
    collect_external_urls,
    get_inner_section,
    get_plugin_section,
    read_agnes_metadata,
    resolve_inner_metadata,
    resolve_plugin_metadata,
)


def _write_metadata(repo_root, payload):
    """Write payload as `.claude-plugin/agnes-metadata.json` under repo_root."""
    target = repo_root / ".claude-plugin" / "agnes-metadata.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


# --- read_agnes_metadata --------------------------------------------------


def test_read_agnes_metadata_missing_file_returns_empty(tmp_path):
    """No metadata file → empty dict, no warning crash."""
    assert read_agnes_metadata(tmp_path) == {}


def test_read_agnes_metadata_malformed_json(tmp_path):
    """Malformed JSON degrades to empty dict (sync should not abort)."""
    target = tmp_path / ".claude-plugin" / "agnes-metadata.json"
    target.parent.mkdir(parents=True)
    target.write_text("{not json at all", encoding="utf-8")
    assert read_agnes_metadata(tmp_path) == {}


def test_read_agnes_metadata_top_level_array_rejected(tmp_path):
    """Top-level must be a JSON object — array is logged + ignored."""
    target = tmp_path / ".claude-plugin" / "agnes-metadata.json"
    target.parent.mkdir(parents=True)
    target.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_agnes_metadata(tmp_path) == {}


def test_read_agnes_metadata_happy_path(tmp_path):
    payload = {"version": 1, "plugins": {"x": {"category": "Tools"}}}
    _write_metadata(tmp_path, payload)
    assert read_agnes_metadata(tmp_path) == payload


# --- get_plugin_section / get_inner_section -------------------------------


def test_get_plugin_section_missing_returns_empty():
    assert get_plugin_section({}, "x") == {}
    assert get_plugin_section({"plugins": "wrong-type"}, "x") == {}
    assert get_plugin_section({"plugins": {"y": {}}}, "x") == {}


def test_get_inner_section_only_skills_or_agents():
    md = {
        "plugins": {
            "x": {
                "skills": {"foo": {"video_url": "https://y.com/v"}},
                "agents": {"bar": {"video_url": "https://y.com/a"}},
            },
        },
    }
    assert get_inner_section(md, "x", "skills", "foo") == {
        "video_url": "https://y.com/v"
    }
    assert get_inner_section(md, "x", "agents", "bar") == {
        "video_url": "https://y.com/a"
    }
    # Wrong kind / missing inner / wrong type all degrade to empty.
    assert get_inner_section(md, "x", "commands", "foo") == {}
    assert get_inner_section(md, "x", "skills", "missing") == {}
    assert get_inner_section({"plugins": {"x": "scalar"}}, "x", "skills", "f") == {}


# --- resolve_plugin_metadata ---------------------------------------------


def test_resolve_plugin_metadata_full_shape():
    md = {
        "plugins": {
            "p": {
                "cover_photo": ".agnes/cover.png",
                "video_url": "https://www.youtube.com/watch?v=abc",
                "category": "Productivity",
                "doc_links": [
                    {"name": "Setup", "path": "docs/setup.md"},
                    {"name": "API",   "url": "https://example.com/api.pdf"},
                ],
            }
        }
    }
    out = resolve_plugin_metadata(md, "p")
    assert out["cover_photo_ref"] == ("internal", ".agnes/cover.png")
    assert out["video_url"] == "https://www.youtube.com/watch?v=abc"
    assert out["category"] == "Productivity"
    assert len(out["doc_links"]) == 2
    assert out["doc_links"][0] == DocLinkRef(
        name="Setup", kind="internal", path="docs/setup.md"
    )
    assert out["doc_links"][1] == DocLinkRef(
        name="API", kind="external", url="https://example.com/api.pdf"
    )


def test_resolve_plugin_metadata_external_cover():
    md = {"plugins": {"p": {"cover_photo": "https://cdn.example.com/x.png"}}}
    out = resolve_plugin_metadata(md, "p")
    assert out["cover_photo_ref"] == ("external", "https://cdn.example.com/x.png")


def test_resolve_plugin_metadata_traversal_rejected():
    """Internal cover_photo with `..` is dropped at parse time."""
    md = {"plugins": {"p": {"cover_photo": "../etc/passwd"}}}
    out = resolve_plugin_metadata(md, "p")
    assert "cover_photo_ref" not in out


def test_resolve_plugin_metadata_video_must_be_http():
    """Non-http video_url is dropped silently with a warning."""
    md = {"plugins": {"p": {"video_url": "ftp://x/y"}}}
    out = resolve_plugin_metadata(md, "p")
    assert "video_url" not in out


def test_resolve_plugin_metadata_invalid_doc_links_dropped():
    """Each invalid doc_link entry is dropped; valid siblings survive."""
    md = {
        "plugins": {
            "p": {
                "doc_links": [
                    {"name": "ok", "url": "https://x.com/a.pdf"},
                    {"name": "missing-target"},
                    {"name": "both-set", "path": "x", "url": "https://y.com/y"},
                    "scalar-not-object",
                ]
            }
        }
    }
    out = resolve_plugin_metadata(md, "p")
    assert len(out["doc_links"]) == 1
    assert out["doc_links"][0].name == "ok"


def test_resolve_plugin_metadata_missing_plugin_returns_empty():
    assert resolve_plugin_metadata({"plugins": {}}, "p") == {}


# --- resolve_inner_metadata ----------------------------------------------


def test_resolve_inner_metadata_skill():
    md = {
        "plugins": {
            "p": {
                "skills": {
                    "s": {
                        "cover_photo": ".agnes/skills/s.png",
                        "video_url": "https://vimeo.com/123",
                        "doc_links": [{"name": "Howto", "path": "docs/s.md"}],
                    }
                }
            }
        }
    }
    out = resolve_inner_metadata(md, "p", "skills", "s")
    assert out["cover_photo_ref"] == ("internal", ".agnes/skills/s.png")
    assert out["video_url"] == "https://vimeo.com/123"
    assert len(out["doc_links"]) == 1


def test_resolve_inner_metadata_missing_returns_empty():
    assert resolve_inner_metadata({}, "p", "skills", "x") == {}


# --- collect_external_urls ------------------------------------------------


def test_collect_external_urls_includes_cover_and_external_docs():
    """Internal references skip the mirror; external cover + external doc URLs
    both get queued. Tagging by kind lets the mirror cache them in different
    sub-directories (cover vs docs)."""
    resolved = {
        "cover_photo_ref": ("external", "https://cdn.example.com/cover.png"),
        "doc_links": [
            DocLinkRef(name="internal", kind="internal", path="docs/x.md"),
            DocLinkRef(name="external", kind="external", url="https://e.com/d.pdf"),
        ],
    }
    urls = collect_external_urls(resolved)
    assert ("cover", "https://cdn.example.com/cover.png") in urls
    assert ("doc", "https://e.com/d.pdf") in urls
    assert len(urls) == 2


def test_collect_external_urls_internal_cover_only():
    """Internal cover and zero external doc_links → empty fetch list."""
    resolved = {
        "cover_photo_ref": ("internal", ".agnes/cover.png"),
        "doc_links": [],
    }
    assert collect_external_urls(resolved) == []


# --- collect_all_external_urls (walks plugin + skills + agents) -----------


def test_collect_all_external_urls_walks_full_tree():
    """v32 inner-level mirror: plugin + every skill + every agent's external
    URLs all end up in the fetch list, so the asset-mirror cache covers
    inner-detail look-ups too. Internal references stay out of the list."""
    md = {
        "plugins": {
            "demo": {
                "cover_photo": "https://cdn.example.com/plugin-cover.png",
                "doc_links": [
                    {"name": "external", "url": "https://e.com/d.pdf"},
                    {"name": "internal", "path": "docs/setup.md"},
                ],
                "skills": {
                    "skill-a": {
                        "cover_photo": "https://cdn.example.com/skill-a-cover.png",
                        "doc_links": [
                            {"name": "ref", "url": "https://e.com/skill-a.md"},
                        ],
                    },
                    "skill-b": {
                        # internal-only — should NOT contribute external URLs
                        "cover_photo": ".agnes/skills/b.png",
                        "doc_links": [
                            {"name": "internal", "path": "docs/b.md"},
                        ],
                    },
                },
                "agents": {
                    "agent-x": {
                        "video_url": "https://www.youtube.com/watch?v=abc",
                        "doc_links": [
                            {"name": "agent-doc", "url": "https://e.com/x.pdf"},
                        ],
                    },
                },
            }
        }
    }
    urls = collect_all_external_urls(md, "demo")
    # Convert to set of urls only — ordering doesn't matter for the assertion
    found = {url for _kind, url in urls}
    assert "https://cdn.example.com/plugin-cover.png" in found     # plugin cover
    assert "https://e.com/d.pdf" in found                          # plugin doc
    assert "https://cdn.example.com/skill-a-cover.png" in found    # skill cover
    assert "https://e.com/skill-a.md" in found                     # skill doc
    assert "https://e.com/x.pdf" in found                          # agent doc
    # Internal references and `video_url` (never mirrored) are absent
    assert "docs/setup.md" not in found
    assert ".agnes/skills/b.png" not in found
    assert "https://www.youtube.com/watch?v=abc" not in found


def test_collect_all_external_urls_no_metadata_returns_empty():
    """When the plugin has no entry, no URLs collected (no crash)."""
    assert collect_all_external_urls({"plugins": {}}, "demo") == []
    assert collect_all_external_urls({}, "demo") == []


# --- parse_doc_link extension allowlist (added in post-walkthrough fix) ---


def test_parse_doc_link_internal_path_must_be_allowlist_extension():
    """Internal `path` ending in something other than .pdf/.md/.markdown/.txt
    is rejected at parse time so the entry never reaches the served list."""
    ok, reason = parse_doc_link({"name": "Doc", "path": "docs/x.docx"})
    assert ok is False
    assert "unsupported_extension" in reason

    # .md still accepted
    ok, value = parse_doc_link({"name": "Doc", "path": "docs/x.md"})
    assert ok is True
    assert value.kind == "internal"


def test_parse_doc_link_external_url_with_explicit_bad_extension():
    """External URL that explicitly ends in .docx etc. is rejected at parse
    time — saves the mirror an HTTP HEAD on something we'd never accept."""
    ok, reason = parse_doc_link({"name": "Doc", "url": "https://x.com/y.docx"})
    assert ok is False
    assert "unsupported_extension" in reason


def test_parse_doc_link_external_url_without_extension_passes_parse():
    """URL without a clear extension (CDN pretty paths) survives parse — the
    Content-Type check at fetch time is the deciding gate."""
    ok, value = parse_doc_link({"name": "Doc", "url": "https://x.com/api/getting-started"})
    assert ok is True
    assert value.kind == "external"
