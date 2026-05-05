"""Tests for src/flea_market.py domain logic."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.flea_market import (
    FleaMarketConfig,
    SkillReview,
    _bump_patch,
    clear_pending_marker,
    list_pending_skills,
    review_skill,
    skill_exists,
    slugify,
    write_pending_marker,
    write_skill_and_bump_version,
)


@pytest.fixture
def config(tmp_path):
    plugin_dir = tmp_path / "plugins" / "flea-market"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / "skills").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "flea-market", "version": "1.0.0", "description": "Community skills"})
    )
    mp_dir = tmp_path / ".claude-plugin"
    mp_dir.mkdir()
    (mp_dir / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "flea-market", "version": "1.0.0", "path": "plugins/flea-market"}]})
    )
    return FleaMarketConfig(
        marketplace_slug="flea-market",
        plugin_name="flea-market",
        github_repo="org/repo",
        github_app_id="1",
        github_app_private_key="pem",
        github_app_installation_id="2",
        _root=tmp_path,
    )


def test_slugify_lowercases_and_replaces_spaces():
    assert slugify("My Cool Skill") == "my-cool-skill"


def test_slugify_strips_leading_trailing_hyphens():
    assert slugify("--my-skill--") == "my-skill"


def test_slugify_collapses_multiple_hyphens():
    assert slugify("my--cool--skill") == "my-cool-skill"


def test_bump_patch():
    assert _bump_patch("1.0.0") == "1.0.1"
    assert _bump_patch("2.3.14") == "2.3.15"


def test_skill_exists_false_when_missing(config):
    assert skill_exists(config, "nonexistent") is False


def test_skill_exists_true_when_present(config):
    skill_dir = config._root / "plugins" / "flea-market" / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n# hi")
    assert skill_exists(config, "my-skill") is True


def test_write_skill_creates_files_and_bumps_version(config):
    skill_md = write_skill_and_bump_version(config, "my-skill", "Does X", "# Body")
    skill_path = config._root / "plugins" / "flea-market" / "skills" / "my-skill" / "SKILL.md"
    assert skill_path.exists()
    assert "name: my-skill" in skill_path.read_text()
    pj = json.loads((config._root / "plugins" / "flea-market" / ".claude-plugin" / "plugin.json").read_text())
    assert pj["version"] == "1.0.1"
    mj = json.loads((config._root / ".claude-plugin" / "marketplace.json").read_text())
    assert mj["plugins"][0]["version"] == "1.0.1"
    assert "name: my-skill" in skill_md


def test_pending_marker_lifecycle(config):
    write_skill_and_bump_version(config, "my-skill", "Does X", "# Body")
    assert list_pending_skills(config) == []

    write_pending_marker(config, "my-skill")
    assert list_pending_skills(config) == ["my-skill"]

    clear_pending_marker(config, "my-skill")
    assert list_pending_skills(config) == []


def test_list_pending_skills_ignores_directories_without_skill_md(config):
    orphan_dir = config.skills_dir() / "orphan"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / ".pending").touch()
    assert list_pending_skills(config) == []


def test_clear_pending_marker_is_idempotent(config):
    write_skill_and_bump_version(config, "my-skill", "Does X", "# Body")
    clear_pending_marker(config, "my-skill")  # no marker exists — must not raise
    clear_pending_marker(config, "my-skill")


def test_review_skill_flags_duplicate():
    extractor = MagicMock()
    extractor.extract_json.return_value = {
        "is_duplicate": True,
        "duplicate_of": "existing-skill",
        "duplicate_reason": "Same purpose",
        "requires_setup": False,
        "setup_description": None,
    }
    result = review_skill(extractor, "new-skill", "Does X", "# body", [{"name": "existing-skill", "description": "Does X"}])
    assert result.is_duplicate is True
    assert result.duplicate_of == "existing-skill"


def test_review_skill_flags_requires_setup():
    extractor = MagicMock()
    extractor.extract_json.return_value = {
        "is_duplicate": False,
        "duplicate_of": None,
        "duplicate_reason": None,
        "requires_setup": True,
        "setup_description": "Needs MCP server configured",
    }
    result = review_skill(extractor, "mcp-skill", "Uses MCP", "# body", [])
    assert result.requires_setup is True
    assert "MCP" in result.setup_description


def test_refresh_plugin_cache_calls_internal_and_invalidates_etag():
    with (
        patch("src.marketplace._refresh_plugin_cache", return_value=2) as mock_refresh,
        patch("app.marketplace_server.packager.invalidate_etag_cache") as mock_inv,
    ):
        from src.marketplace import refresh_plugin_cache
        count = refresh_plugin_cache("test-slug")
    mock_refresh.assert_called_once_with("test-slug")
    mock_inv.assert_called_once()
    assert count == 2
