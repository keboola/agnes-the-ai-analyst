"""Content-guardrail tests — the mechanical per-component description check.

Exercises every failure-mode code on a synthetic baked plugin tree:
empty, placeholder_text, too_short, low_word_count, body_too_short.
Also verifies the aggregate ``InlineResult.passed`` flips false when the
content tier fails even with manifest + security passing.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from src.store_guardrails import run_inline_checks
from src.store_guardrails.content_check import (
    check as content_check,
    check_submission_description,
    summarize_components,
    summarize_for_preview,
)


_OK_DESC = "Use when validating per-component description guardrails end to end"
_OK_BODY = "Body content explaining what this component does, when to use it, and the constraints. " * 4


@pytest.fixture
def plugin_dir():
    d = Path(tempfile.mkdtemp(prefix="agnes_content_test_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _write_skill(plugin_dir: Path, *, description: str = _OK_DESC, body: str = _OK_BODY) -> None:
    target = plugin_dir / "skills" / "test-skill"
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        f"---\nname: test-skill\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _write_agent(plugin_dir: Path, *, name: str = "reviewer", description: str = _OK_DESC, body: str = _OK_BODY) -> None:
    target = plugin_dir / "agents"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _write_plugin_json(plugin_dir: Path, *, description: str = _OK_DESC) -> None:
    target = plugin_dir / ".claude-plugin"
    target.mkdir(parents=True, exist_ok=True)
    (target / "plugin.json").write_text(
        json.dumps({"name": "test-plugin", "description": description, "version": "0.1.0"}),
        encoding="utf-8",
    )


def _write_command(plugin_dir: Path, *, name: str = "run", description: str = "Run the test suite and report failures") -> None:
    target = plugin_dir / "commands"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nrun it\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Component-level failure codes
# ---------------------------------------------------------------------------


class TestSkillDescriptions:
    def test_empty_description_fails(self, plugin_dir):
        _write_skill(plugin_dir, description="")
        out = content_check(plugin_dir)
        assert out["status"] == "fail"
        codes = {i["code"] for i in out["issues"]}
        assert "empty" in codes

    def test_todo_literal_fails_as_placeholder(self, plugin_dir):
        _write_skill(plugin_dir, description="TODO")
        out = content_check(plugin_dir)
        codes = {i["code"] for i in out["issues"]}
        assert "placeholder_text" in codes

    def test_todo_prefix_fails(self, plugin_dir):
        _write_skill(plugin_dir, description="TODO add the real description later")
        out = content_check(plugin_dir)
        codes = {i["code"] for i in out["issues"]}
        assert "placeholder_text" in codes

    def test_short_description_fails(self, plugin_dir):
        _write_skill(plugin_dir, description="too short here")  # 14 chars
        out = content_check(plugin_dir)
        codes = {i["code"] for i in out["issues"]}
        assert "too_short" in codes

    def test_unfilled_jinja_placeholder_fails(self, plugin_dir):
        _write_skill(plugin_dir, description="Use when {{my_skill}} fires")
        out = content_check(plugin_dir)
        codes = {i["code"] for i in out["issues"]}
        assert "placeholder_text" in codes

    def test_length_floor_takes_precedence_over_word_count(self, plugin_dir):
        # 4 words but well under 30 chars.
        _write_skill(plugin_dir, description="foo bar baz quux")
        out = content_check(plugin_dir)
        codes = {i["code"] for i in out["issues"]}
        assert "too_short" in codes

    def test_low_distinct_words_fails(self, plugin_dir):
        # 80 chars clears the length floor but only 1 distinct word
        # after stripping punctuation — low_word_count fires.
        _write_skill(plugin_dir, description=("foo " * 20).strip())
        out = content_check(plugin_dir)
        codes = {i["code"] for i in out["issues"]}
        assert "low_word_count" in codes

    def test_body_too_short_fails(self, plugin_dir):
        _write_skill(plugin_dir, body="short body")
        out = content_check(plugin_dir)
        codes = {(i["field"], i["code"]) for i in out["issues"]}
        assert ("body", "body_too_short") in codes

    def test_well_formed_skill_passes(self, plugin_dir):
        _write_skill(plugin_dir)
        out = content_check(plugin_dir)
        assert out["status"] == "pass"
        assert out["issues"] == []


class TestPluginAndAgentDescriptions:
    def test_plugin_description_empty_fails(self, plugin_dir):
        _write_plugin_json(plugin_dir, description="")
        out = content_check(plugin_dir)
        files = {i["file"] for i in out["issues"]}
        assert ".claude-plugin/plugin.json" in files

    def test_one_bad_agent_among_many_is_isolated(self, plugin_dir):
        _write_plugin_json(plugin_dir)
        _write_agent(plugin_dir, name="good_one", description=_OK_DESC)
        _write_agent(plugin_dir, name="bad_one", description="")
        out = content_check(plugin_dir)
        assert out["status"] == "fail"
        # Only the bad agent's file shows in issues.
        files = {i["file"] for i in out["issues"]}
        assert "agents/bad_one.md" in files
        assert "agents/good_one.md" not in files

    def test_plugin_passes_when_descriptions_all_strong(self, plugin_dir):
        _write_plugin_json(plugin_dir)
        _write_agent(plugin_dir)
        _write_skill(plugin_dir)
        _write_command(plugin_dir)
        out = content_check(plugin_dir)
        assert out["status"] == "pass"


class TestCommands:
    def test_command_short_description_fails(self, plugin_dir):
        _write_command(plugin_dir, description="run")  # 3 chars
        out = content_check(plugin_dir)
        codes = {i["code"] for i in out["issues"]}
        assert "too_short" in codes

    def test_command_lower_floor_still_enforced(self, plugin_dir):
        # 38 chars + 6 distinct words — clears the 25/5 command floor.
        _write_command(plugin_dir, description="Run tests, format output, report failures clearly")
        out = content_check(plugin_dir)
        assert out["status"] == "pass"


# ---------------------------------------------------------------------------
# Submission-level description
# ---------------------------------------------------------------------------


class TestSubmissionDescription:
    def test_empty_submission_description_fails(self):
        out = check_submission_description("")
        assert out["status"] == "fail"
        assert out["issues"][0]["code"] == "empty"
        assert out["issues"][0]["file"] == "<submission>"

    def test_placeholder_submission_description_fails(self):
        out = check_submission_description("TBD")
        codes = {i["code"] for i in out["issues"]}
        assert "placeholder_text" in codes

    def test_strong_submission_description_passes(self):
        out = check_submission_description(_OK_DESC)
        assert out["status"] == "pass"
        assert out["issues"] == []


# ---------------------------------------------------------------------------
# Aggregation — InlineResult.passed flips on content failure
# ---------------------------------------------------------------------------


class TestInlineAggregate:
    def test_content_failure_blocks_passed(self, plugin_dir):
        # Skill with frontmatter description = TODO. Manifest + security
        # pass; content fails; aggregate must be False.
        _write_skill(plugin_dir, description="TODO")
        r = run_inline_checks(plugin_dir, type_="skill", description=_OK_DESC)
        assert r.manifest["status"] == "pass"
        assert r.static_security["status"] == "pass"
        assert r.content["status"] == "fail"
        assert not r.passed

    def test_submission_desc_failure_merges_into_content(self, plugin_dir):
        _write_skill(plugin_dir)
        r = run_inline_checks(plugin_dir, type_="skill", description="")
        assert r.content["status"] == "fail"
        files = {i["file"] for i in r.content["issues"]}
        assert "<submission>" in files

    def test_clean_bundle_passes(self, plugin_dir):
        _write_skill(plugin_dir)
        r = run_inline_checks(plugin_dir, type_="skill", description=_OK_DESC)
        assert r.passed


# ---------------------------------------------------------------------------
# summarize_components + summarize_for_preview
# ---------------------------------------------------------------------------


class TestSummaries:
    def test_summarize_components_baked_plugin_tree(self, plugin_dir):
        _write_plugin_json(plugin_dir)
        _write_agent(plugin_dir)
        _write_skill(plugin_dir)
        rows = summarize_components(plugin_dir)
        types = {r["type"] for r in rows}
        assert types == {"plugin", "agent", "skill"}
        for r in rows:
            assert r["ok"] is True

    def test_summarize_for_preview_skill(self, plugin_dir):
        # Single SKILL.md at root — preview should locate it without the
        # `skills/<name>/` wrapper.
        (plugin_dir / "SKILL.md").write_text(
            f"---\nname: probe\ndescription: {_OK_DESC}\n---\n\n{_OK_BODY}\n",
            encoding="utf-8",
        )
        rows = summarize_for_preview(plugin_dir, "skill")
        assert len(rows) == 1
        assert rows[0]["type"] == "skill"
        assert rows[0]["ok"] is True

    def test_summarize_for_preview_marks_bad_descriptions(self, plugin_dir):
        (plugin_dir / "SKILL.md").write_text(
            "---\nname: probe\ndescription: TODO\n---\n\n" + _OK_BODY + "\n",
            encoding="utf-8",
        )
        rows = summarize_for_preview(plugin_dir, "skill")
        assert len(rows) == 1
        assert rows[0]["ok"] is False
        codes = {i["code"] for i in rows[0]["issues"]}
        assert "placeholder_text" in codes


class TestAgentsWalkerSkipsNonAgentFiles:
    """`agents/README.md` (and other helper files without frontmatter)
    must not be evaluated as a missing-description agent. Pre-fix the
    `_iter_components` walker greedily evaluated every `*.md` under
    `agents/`, which gave a green dot in the upload preview (preview
    walker correctly filtered) but a red rejection on submit (check
    walker did not). Pin the parity here so the two stay aligned."""

    def test_readme_under_agents_is_skipped(self, plugin_dir):
        # One real agent + one README (no frontmatter at all).
        _write_agent(plugin_dir, name="reviewer")
        (plugin_dir / "agents" / "README.md").write_text(
            "# How to author agents in this plugin\n\nA few notes for contributors.\n",
            encoding="utf-8",
        )
        result = content_check(plugin_dir)
        # README must NOT generate any issue. The lone real agent passes
        # the floor, so the whole plugin passes.
        assert result["status"] == "pass", result["issues"]

    def test_helper_md_without_frontmatter_is_skipped(self, plugin_dir):
        _write_agent(plugin_dir, name="reviewer")
        (plugin_dir / "agents" / "_NOTES.md").write_text(
            "Some helper notes — not an agent. No frontmatter, no agent shape.\n",
            encoding="utf-8",
        )
        rows = summarize_components(plugin_dir)
        types_files = {(r["type"], r["file"]) for r in rows}
        # Only the real agent should appear; _NOTES.md is filtered out.
        assert ("agent", "agents/reviewer.md") in types_files
        assert ("agent", "agents/_NOTES.md") not in types_files


class TestSkillsWalkerSkipsNonMd:
    """Skills walker should not visit assets / scripts / data files
    under skills/ — only SKILL.md. The pre-#277 walker used
    rglob("*") and stat()d every file just to filter by name; the
    fix uses rglob("*.md") to push the filter into the glob. Pin
    the contract here so a regression to rglob("*") is loud."""

    def test_assets_and_scripts_under_skill_dir_are_ignored(self, plugin_dir):
        # Real skill + a bunch of non-.md siblings
        _write_skill(plugin_dir)  # creates skills/test-skill/SKILL.md
        skill_dir = plugin_dir / "skills" / "test-skill"
        (skill_dir / "assets").mkdir()
        (skill_dir / "assets" / "cover.png").write_bytes(b"fake png")
        (skill_dir / "assets" / "data.json").write_text('{"k": "v"}')
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "run.sh").write_text("#!/bin/sh\necho ok\n")
        rows = summarize_components(plugin_dir)
        # Exactly one skill component, no false positives from siblings.
        skill_rows = [r for r in rows if r["type"] == "skill"]
        assert len(skill_rows) == 1, skill_rows
        # No row references a non-md file path.
        for r in rows:
            assert not r["file"].endswith(".png"), r
            assert not r["file"].endswith(".json"), r
            assert not r["file"].endswith(".sh"), r

    def test_skills_walker_uses_md_glob_not_star(self):
        """Pin the glob pattern: a regression to rglob('*') would walk
        every asset / script / data file just to filter by name.
        Source-level pinning works for this kind of "use this glob,
        not that glob" contract — the functional test above passes
        with either glob, so we also assert the literal pattern."""
        import inspect

        from src.store_guardrails import content_check as _cc

        src = inspect.getsource(_cc._iter_components)
        # The skills section must use the .md filter at the glob layer.
        assert 'rglob("*.md")' in src or "rglob('*.md')" in src, (
            "skills walker must filter at the glob layer "
            "(rglob('*.md')) — not stat() every asset under skills/"
        )
