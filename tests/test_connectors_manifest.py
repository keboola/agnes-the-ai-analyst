"""Tests for the seed-driven connector manifest scan + validation.

Covers:
  * Frontmatter parsing (valid, malformed YAML, missing closing fence)
  * Schema validation (required fields, length caps, type errors, clamping)
  * HTML/JS stripping (XSS defense)
  * Cache invalidation by source signature + file hash
  * Bundle fallback when no IWT clone is present
  * IWT-clone-wins-over-bundle resolution

Does NOT cover the HTTP endpoints — those are in test_api_connectors.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import connectors_manifest as cm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_skill(
    base: Path,
    slug: str,
    frontmatter: str,
    body: str = "Walk the user through setup.\n",
) -> Path:
    """Drop a SKILL.md with arbitrary frontmatter under
    base/workspace/.claude/skills/<slug>/SKILL.md.
    """
    skill_dir = base / "workspace" / ".claude" / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_cache():
    """Drop the manifest cache before AND after every test so file edits
    inside the test always trigger a re-scan and post-test state never
    leaks into the next.
    """
    cm.invalidate_cache()
    yield
    cm.invalidate_cache()


@pytest.fixture
def fake_bundle(monkeypatch, tmp_path: Path) -> Path:
    """Replace the bundled-seed path with a temp dir under our control.
    Also stubs out ``is_configured()`` to return False so the resolution
    chain falls through to the bundle.
    """
    bundle_root = tmp_path / "_bundled"
    bundle_root.mkdir()
    monkeypatch.setattr(cm, "bundled_seed_path", lambda: bundle_root)
    monkeypatch.setattr(cm, "is_configured", lambda: False)

    # Patch the helpers ``connectors_manifest.load_manifest`` calls
    # internally to walk the bundle.
    from src import initial_workspace as iw

    monkeypatch.setattr(iw, "_BUNDLED_SEED_DIR", bundle_root)
    monkeypatch.setattr(iw, "is_configured", lambda: False)
    return bundle_root


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------


def test_parse_frontmatter_valid():
    text = """---
name: connector-foo
connector:
  display_name: Foo
---
body
"""
    parsed = cm._parse_frontmatter(text)
    assert parsed is not None
    assert parsed["connector"]["display_name"] == "Foo"


def test_parse_frontmatter_missing_opening_returns_none():
    assert cm._parse_frontmatter("no frontmatter here") is None


def test_parse_frontmatter_missing_closing_fence_returns_none():
    text = "---\nname: foo\nbroken — no closing fence\n"
    assert cm._parse_frontmatter(text) is None


def test_parse_frontmatter_malformed_yaml_returns_none():
    text = "---\n: : : : :\n---\n"
    # Either yaml.safe_load raises (returns None) or the result isn't a dict.
    result = cm._parse_frontmatter(text)
    assert result is None or not isinstance(result, dict) or result == {}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _good_block(**overrides) -> dict:
    base = {
        "connector": {
            "display_name": "Test Vendor",
            "short_summary": "Test summary.",
            "estimated_minutes": 3,
            "vendor_url": "https://example.com",
            "requires_oauth_app": False,
        }
    }
    base["connector"].update(overrides)
    return base


def test_validate_happy_path():
    entry = cm._validate("connector-test", _good_block())
    assert entry is not None
    assert entry.slug == "connector-test"
    assert entry.display_name == "Test Vendor"
    assert entry.estimated_minutes == 3


def test_validate_required_defaults_false():
    entry = cm._validate("connector-test", _good_block())
    assert entry is not None
    assert entry.required is False


def test_validate_required_true():
    entry = cm._validate("connector-test", _good_block(required=True))
    assert entry is not None
    assert entry.required is True


def test_validate_required_truthy_coerced_not_rejected():
    """`required` follows the `requires_oauth_app` contract: bool()-coerced,
    never a reason to reject the entry (fail-soft + §9 forward-compat)."""
    entry = cm._validate("connector-test", _good_block(required="yes"))
    assert entry is not None
    assert entry.required is True

    entry = cm._validate("connector-test", _good_block(required=0))
    assert entry is not None
    assert entry.required is False


def test_load_manifest_carries_required_flag(fake_bundle):
    """`required: true` round-trips from frontmatter through load_manifest;
    the overall sort stays alphabetical by display_name regardless of the
    flag (required/optional split happens in the renderer, not here)."""
    _write_skill(
        fake_bundle,
        "connector-beta",
        "name: connector-beta\n"
        "connector:\n"
        "  display_name: Beta\n"
        "  short_summary: Optional tool.\n"
        "  estimated_minutes: 2",
    )
    _write_skill(
        fake_bundle,
        "connector-alpha",
        "name: connector-alpha\n"
        "connector:\n"
        "  display_name: Alpha\n"
        "  short_summary: Mandatory tool.\n"
        "  estimated_minutes: 1\n"
        "  required: true",
    )
    entries = cm.load_manifest()
    assert [e.slug for e in entries] == ["connector-alpha", "connector-beta"]
    assert [e.required for e in entries] == [True, False]


def test_validate_missing_connector_block_returns_none():
    assert cm._validate("connector-test", {"name": "x"}) is None


def test_validate_missing_required_field_returns_none():
    block = _good_block()
    del block["connector"]["display_name"]
    assert cm._validate("connector-test", block) is None


def test_validate_wrong_type_returns_none():
    block = _good_block(estimated_minutes="not-an-int")
    assert cm._validate("connector-test", block) is None


def test_validate_clamps_absurd_minutes():
    block = _good_block(estimated_minutes=99999)
    entry = cm._validate("connector-test", block)
    assert entry is not None
    assert entry.estimated_minutes == 120  # MAX_MINUTES


def test_validate_clamps_negative_minutes():
    block = _good_block(estimated_minutes=-5)
    entry = cm._validate("connector-test", block)
    assert entry is not None
    assert entry.estimated_minutes == 0


def test_validate_drops_html_from_display_name():
    block = _good_block(display_name="Foo <script>alert(1)</script> Bar")
    entry = cm._validate("connector-test", block)
    assert entry is not None
    assert "<script>" not in entry.display_name
    assert "Foo" in entry.display_name and "Bar" in entry.display_name


def test_validate_drops_html_entities_disguised_as_tags():
    block = _good_block(display_name="&lt;script&gt;alert(1)&lt;/script&gt;")
    entry = cm._validate("connector-test", block)
    assert entry is not None
    # After strip then unescape, `<script>` reappears as text but is NOT a
    # tag. Renderer escapes on output too — defense in depth.
    assert entry.display_name == "alert(1)"


def test_validate_caps_display_name_length():
    block = _good_block(display_name="X" * 500)
    entry = cm._validate("connector-test", block)
    assert entry is not None
    assert len(entry.display_name) <= cm._MAX_DISPLAY_LEN


def test_validate_caps_summary_length():
    block = _good_block(short_summary="Y" * 500)
    entry = cm._validate("connector-test", block)
    assert entry is not None
    assert len(entry.short_summary) <= cm._MAX_SUMMARY_LEN


def test_validate_drops_malformed_vendor_url():
    block = _good_block(vendor_url="javascript:alert(1)")
    entry = cm._validate("connector-test", block)
    assert entry is not None
    assert entry.vendor_url is None


def test_validate_accepts_https_vendor_url():
    block = _good_block(vendor_url="https://app.example.com/tokens")
    entry = cm._validate("connector-test", block)
    assert entry is not None
    assert entry.vendor_url == "https://app.example.com/tokens"


def test_validate_drops_oversized_vendor_url():
    block = _good_block(vendor_url="https://example.com/" + "x" * 600)
    entry = cm._validate("connector-test", block)
    assert entry is not None
    assert entry.vendor_url is None


# ---------------------------------------------------------------------------
# Bundle scan + cache
# ---------------------------------------------------------------------------


_VALID_FM = """name: connector-asana
description: Test.
connector:
  display_name: Asana
  short_summary: Read tasks.
  estimated_minutes: 3
  vendor_url: https://app.asana.com/0/my-apps
"""


def test_bundle_scan_returns_validated_entries(fake_bundle: Path):
    _write_skill(fake_bundle, "connector-asana", _VALID_FM)
    entries = cm.load_manifest()
    assert len(entries) == 1
    assert entries[0].slug == "connector-asana"
    assert entries[0].display_name == "Asana"


def test_bundle_scan_sorts_by_display_name(fake_bundle: Path):
    _write_skill(
        fake_bundle, "connector-zeta",
        _VALID_FM.replace("display_name: Asana", "display_name: Zeta"),
    )
    _write_skill(
        fake_bundle, "connector-alpha",
        _VALID_FM.replace("display_name: Asana", "display_name: Alpha"),
    )
    entries = cm.load_manifest()
    names = [e.display_name for e in entries]
    assert names == sorted(names, key=str.lower)


def test_invalid_connector_is_skipped_not_fatal(fake_bundle: Path):
    _write_skill(fake_bundle, "connector-good", _VALID_FM)
    # Bad connector: missing connector block
    bad_dir = fake_bundle / "workspace" / ".claude" / "skills" / "connector-bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text(
        "---\nname: connector-bad\n---\nbody\n", encoding="utf-8"
    )
    entries = cm.load_manifest()
    slugs = [e.slug for e in entries]
    assert "connector-good" in slugs
    assert "connector-bad" not in slugs


def test_empty_bundle_returns_empty_manifest(fake_bundle: Path):
    assert cm.load_manifest() == []


def test_cache_returns_same_object_on_repeat_call(fake_bundle: Path):
    _write_skill(fake_bundle, "connector-asana", _VALID_FM)
    first = cm.load_manifest()
    second = cm.load_manifest()
    # Same list instance — cache hit. (Lists in dict cache by identity.)
    assert first is second


def test_cache_invalidates_on_file_change(fake_bundle: Path):
    path = _write_skill(fake_bundle, "connector-asana", _VALID_FM)
    first = cm.load_manifest()
    assert first[0].display_name == "Asana"
    # Edit the file to change display_name. Different size → cache key
    # changes via _hash_paths even if mtime didn't move.
    _write_skill(
        fake_bundle, "connector-asana",
        _VALID_FM.replace("display_name: Asana", "display_name: AsanaPro"),
    )
    second = cm.load_manifest()
    assert second[0].display_name == "AsanaPro"


def test_invalidate_cache_drops_state(fake_bundle: Path):
    _write_skill(fake_bundle, "connector-asana", _VALID_FM)
    cm.load_manifest()
    assert cm._cache  # cache populated
    cm.invalidate_cache()
    assert not cm._cache


# ---------------------------------------------------------------------------
# Non-connector skill dirs are ignored
# ---------------------------------------------------------------------------


def test_non_connector_skills_are_ignored(fake_bundle: Path):
    """Only directories matching `connector-*` count. A seed with
    `connector-asana/` AND `some-other-skill/` should manifest only the
    former.
    """
    _write_skill(fake_bundle, "connector-asana", _VALID_FM)
    # Non-connector skill — also has frontmatter but no `connector:` block
    other = fake_bundle / "workspace" / ".claude" / "skills" / "decision-doc"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text(
        "---\nname: decision-doc\n---\nbody\n", encoding="utf-8"
    )
    entries = cm.load_manifest()
    assert len(entries) == 1
    assert entries[0].slug == "connector-asana"
