"""Unit tests for the Cowork plugin-zip transforms (issue #464).

Behaviour is matched against the known-good reference zip
(``grpn-v1.15.28.zip``): keep ALL content, concatenate per-directory ``.md``
under ``data/`` into ``_all.md``, keep agent ``tools:``, whitelist SKILL.md
frontmatter, fix plugin.json. Pure-function tests — no DB, no FastAPI.
"""

from __future__ import annotations

import io
import json
import zipfile

import yaml

from app.marketplace_server import cowork_packager as cp


# ─────────────────────────── description sanitizer ─────────────────────────


class TestSanitizeDescription:
    def test_strips_angle_brackets(self):
        assert cp.sanitize_description("Use <role> here") == "Use role here"

    def test_double_quotes_become_single(self):
        assert cp.sanitize_description('say "create agent"') == "say 'create agent'"

    def test_collapses_newlines_and_whitespace(self):
        assert cp.sanitize_description("a\n  b\tc") == "a b c"

    def test_non_string_is_safe(self):
        assert cp.sanitize_description(None) == ""


# ─────────────────────────────── plugin.json ───────────────────────────────


class TestTransformPluginJson:
    def _t(self, data, manifest_name="my-plugin", raw=None):
        return cp.transform_plugin_json(
            data, manifest_name=manifest_name, raw=raw or {}
        )

    def test_hex_version_coerced_to_semver(self):
        assert self._t({"name": "flea", "version": "f92bc28"})["version"] == "0.0.1"

    def test_valid_semver_preserved(self):
        assert self._t({"name": "x", "version": "1.4.2"})["version"] == "1.4.2"

    def test_missing_author_injected(self):
        assert self._t({"name": "x", "version": "1.0.0"})["author"] == {"name": "Unknown"}

    def test_string_author_wrapped(self):
        out = self._t({"name": "x", "version": "1.0.0", "author": "Jane"})
        assert out["author"] == {"name": "Jane"}

    def test_existing_dict_author_preserved(self):
        out = self._t({"name": "x", "version": "1.0.0", "author": {"name": "Team"}})
        assert out["author"] == {"name": "Team"}

    def test_author_falls_back_to_raw(self):
        out = self._t({"name": "x", "version": "1.0.0"}, raw={"author": "RawGuy"})
        assert out["author"] == {"name": "RawGuy"}

    def test_homepage_deleted(self):
        out = self._t({"name": "x", "version": "1.0.0", "homepage": "https://internal/x"})
        assert "homepage" not in out

    def test_name_coerced_to_kebab(self):
        assert self._t({"name": "My Cool Plugin!", "version": "1.0.0"})["name"] == "my-cool-plugin"

    def test_name_must_start_with_letter(self):
        assert cp.coerce_plugin_name("123-abc", "fallback") == "abc"

    def test_description_sanitized(self):
        out = self._t({"name": "x", "version": "1.0.0", "description": 'a <b> "c"'})
        assert out["description"] == "a b 'c'"


# ────────────────────────── SKILL.md frontmatter ───────────────────────────


SKILL_WITH_EXTRAS = """---
name: create
description: Create new agents and skills. Use "create agent <role>" now.
argument-hint: "[role]"
user-invocable: true
---

Body content stays.
"""


class TestSkillFrontmatter:
    def test_extras_dropped(self):
        out = cp.filter_skill_frontmatter(SKILL_WITH_EXTRAS, "create")
        assert "argument-hint" not in out
        assert "user-invocable" not in out

    def test_only_whitelisted_keys_kept(self):
        out = cp.filter_skill_frontmatter(SKILL_WITH_EXTRAS, "create")
        parsed = yaml.safe_load(out.split("---")[1])
        assert set(parsed.keys()) <= {"name", "description", "compatibility"}

    def test_folder_name_wins_for_name(self):
        out = cp.filter_skill_frontmatter(SKILL_WITH_EXTRAS, "renamed-folder")
        assert yaml.safe_load(out.split("---")[1])["name"] == "renamed-folder"

    def test_description_sanitized_and_parses(self):
        out = cp.filter_skill_frontmatter(SKILL_WITH_EXTRAS, "create")
        fm = yaml.safe_load(out.split("---")[1])
        assert "<" not in fm["description"] and '"' not in fm["description"]
        assert "create agent role" in fm["description"]

    def test_plain_scalar_for_clean_description(self):
        # Reference uses a plain one-line scalar (not folded) for clean text.
        out = cp.filter_skill_frontmatter(
            "---\nname: x\ndescription: A clean one liner\n---\nbody\n", "x"
        )
        assert "description: A clean one liner" in out
        assert ">-" not in out

    def test_folded_scalar_for_hazardous_description(self):
        # A colon-space would break a plain scalar → folded block scalar.
        out = cp.filter_skill_frontmatter(
            "---\nname: x\ndescription: Ratio is this: that\n---\nbody\n", "x"
        )
        assert "description: >-" in out
        assert yaml.safe_load(out.split("---")[1])["description"] == "Ratio is this: that"

    def test_body_preserved(self):
        assert "Body content stays." in cp.filter_skill_frontmatter(SKILL_WITH_EXTRAS, "create")

    def test_missing_frontmatter_synthesized(self):
        out = cp.filter_skill_frontmatter("# just a heading\n", "my-skill")
        assert yaml.safe_load(out.split("---")[1])["name"] == "my-skill"
        assert "# just a heading" in out


# ───────────────────────────── path sanitizer ──────────────────────────────


class TestPathSanitizer:
    def test_square_brackets(self):
        assert cp.sanitize_path_segment("[id]") == "dyn-id"

    def test_parens(self):
        assert cp.sanitize_path_segment("(group)") == "grp-group"

    def test_plain_segment_unchanged(self):
        assert cp.sanitize_path_segment("normal-dir") == "normal-dir"


# ─────────────────────────── zip assembly (reference parity) ────────────────


AGENT_WITH_TOOLS = """---
name: reviewer
description: Reviews things.
tools: Read, Grep, Glob, Bash
---

Agent body.
"""


def _make_plugin(tmp_path):
    """On-disk plugin dir exercising the reference behaviour, returned as the
    resolver-shaped dict ``build_cowork_zip`` expects."""
    d = tmp_path / "marketplaces" / "mkt" / "plugins" / "grpn"
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "grpn", "version": "deadbeef",
                    "homepage": "https://internal.example/grpn"}),
        encoding="utf-8",
    )
    (d / "skills" / "create").mkdir(parents=True)
    (d / "skills" / "create" / "SKILL.md").write_text(SKILL_WITH_EXTRAS, encoding="utf-8")
    (d / "agents").mkdir()
    (d / "agents" / "reviewer.md").write_text(AGENT_WITH_TOOLS, encoding="utf-8")
    # Content the reference KEEPS.
    (d / "CLAUDE.md").write_text("root context", encoding="utf-8")
    (d / "settings.json").write_text("{}", encoding="utf-8")
    (d / "scripts").mkdir()
    (d / "scripts" / "query.mjs").write_text("console.log(1)", encoding="utf-8")
    (d / "vendor").mkdir()
    (d / "vendor" / "lib.js").write_text("// vendored", encoding="utf-8")
    # data/: a JSON catalog kept as-is + a docs dir of many .md → concatenated.
    (d / "data").mkdir()
    (d / "data" / "catalog.json").write_text('{"k": 1}', encoding="utf-8")
    (d / "data" / "confluence" / "AMR").mkdir(parents=True)
    for i in range(3):
        (d / "data" / "confluence" / "AMR" / f"page{i}.md").write_text(
            f"# page {i}\ncontent {i}\n", encoding="utf-8"
        )
    # Stripped.
    (d / ".DS_Store").write_text("junk", encoding="utf-8")
    # Kept root docs.
    (d / "README.md").write_text("# grpn", encoding="utf-8")
    (d / ".mcp.json").write_text("{}", encoding="utf-8")
    return {
        "manifest_name": "grpn",
        "prefixed_name": "mkt-grpn",
        "version": "deadbeef",
        "raw": {"name": "grpn", "description": "Groupon plugin"},
        "plugin_dir": d,
    }


def _read_zip(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


class TestBuildCoworkZip:
    def test_no_wrapper_plugin_json_at_root(self, tmp_path):
        names = set(_read_zip(cp.build_cowork_zip(_make_plugin(tmp_path))[0]))
        assert ".claude-plugin/plugin.json" in names
        assert not any(n.startswith("plugins/") for n in names)

    def test_no_marketplace_json(self, tmp_path):
        assert ".claude-plugin/marketplace.json" not in _read_zip(
            cp.build_cowork_zip(_make_plugin(tmp_path))[0]
        )

    def test_content_kept(self, tmp_path):
        names = set(_read_zip(cp.build_cowork_zip(_make_plugin(tmp_path))[0]))
        # The reference keeps these — they must survive.
        for kept in ("CLAUDE.md", "settings.json", "scripts/query.mjs",
                     "vendor/lib.js", "data/catalog.json", "README.md",
                     ".mcp.json", "skills/create/SKILL.md", "agents/reviewer.md"):
            assert kept in names, kept

    def test_dsstore_stripped(self, tmp_path):
        assert ".DS_Store" not in _read_zip(cp.build_cowork_zip(_make_plugin(tmp_path))[0])

    def test_data_md_concatenated(self, tmp_path):
        files = _read_zip(cp.build_cowork_zip(_make_plugin(tmp_path))[0])
        names = set(files)
        # The 3 confluence .md collapse into one _all.md; originals gone.
        assert "data/confluence/AMR/_all.md" in names
        assert "data/confluence/AMR/page0.md" not in names
        allmd = files["data/confluence/AMR/_all.md"].decode()
        assert "# AMR — combined docs" in allmd
        assert "## `page0.md`" in allmd and "content 2" in allmd

    def test_plugin_json_transformed(self, tmp_path):
        files = _read_zip(cp.build_cowork_zip(_make_plugin(tmp_path))[0])
        pj = json.loads(files[".claude-plugin/plugin.json"])
        assert pj["version"] == "0.0.1"
        assert pj["author"] == {"name": "Unknown"}
        assert "homepage" not in pj

    def test_skill_whitelisted_agent_tools_kept(self, tmp_path):
        files = _read_zip(cp.build_cowork_zip(_make_plugin(tmp_path))[0])
        skill = files["skills/create/SKILL.md"].decode()
        assert "argument-hint" not in skill and "user-invocable" not in skill
        # Agents are NOT filtered — tools: survives (reference parity).
        agent = files["agents/reviewer.md"].decode()
        assert "tools: Read, Grep, Glob, Bash" in agent

    def test_deterministic(self, tmp_path):
        plugin = _make_plugin(tmp_path)
        a, ea = cp.build_cowork_zip(plugin)
        b, eb = cp.build_cowork_zip(plugin)
        assert a == b and ea == eb
