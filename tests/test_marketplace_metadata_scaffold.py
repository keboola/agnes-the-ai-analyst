"""Unit tests for the marketplace-metadata.json scaffolder (Gap 2 / #469).

Covers the three things that matter:

1. Derivation — display_name / tagline / invocation / when_to_use are computed
   from marketplace.json + plugin.json + SKILL.md / agent frontmatter.
2. The 3-way merge contract — a human edit always wins, an untouched machine
   field regenerates when the source changes, and KEEP fields are never
   touched.
3. Round-trip — the emitted document is consumed cleanly by the real runtime
   parser (src.marketplace_metadata) and the inner names line up with
   src.marketplace_listing.
"""

from __future__ import annotations

import json

from src.marketplace_listing import list_inner_agents, list_inner_skills
from src.marketplace_metadata import (
    resolve_inner_metadata,
    resolve_plugin_metadata,
)
from src.marketplace_metadata_scaffold import (
    ScaffoldError,
    comparable_view,
    humanize,
    render_document,
    scaffold_metadata,
)

FIXED_TS = "2026-05-29T00:00:00Z"


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #


def _fm(fields: dict) -> str:
    body = "".join(f"{k}: {v}\n" for k, v in fields.items())
    return f"---\n{body}---\n\n# Body\n\nSome content.\n"


def _write_skill(plugin_dir, dir_name: str, frontmatter: dict) -> None:
    d = plugin_dir / "skills" / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_fm(frontmatter), encoding="utf-8")


def _write_agent(plugin_dir, file_stem: str, frontmatter: dict) -> None:
    d = plugin_dir / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{file_stem}.md").write_text(_fm(frontmatter), encoding="utf-8")


def _write_plugin_json(plugin_dir, name: str, description: str) -> None:
    cp = plugin_dir / ".claude-plugin"
    cp.mkdir(parents=True, exist_ok=True)
    (cp / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "description": description}),
        encoding="utf-8",
    )


def _write_manifest(root, plugins: list) -> None:
    cp = root / ".claude-plugin"
    cp.mkdir(parents=True, exist_ok=True)
    entries = [{"name": p, "source": f"./plugins/{p}"} for p in plugins]
    (cp / "marketplace.json").write_text(
        json.dumps({"name": "dept-marketplace", "plugins": entries}),
        encoding="utf-8",
    )


def _make_dept_howto(root):
    """Re-create the example-template `dept-howto` plugin shape: 2 skills (one
    with an argument-hint, one knowledge-only) + 1 agent."""
    _write_manifest(root, ["dept-howto"])
    pdir = root / "plugins" / "dept-howto"
    _write_plugin_json(
        pdir,
        "dept-howto",
        "How-to guides and onboarding skills for the team. Covers tools and getting started.",
    )
    _write_skill(
        pdir,
        "gws-onboarding",
        {
            "name": "gws-onboarding",
            "description": "Guides a new team member through Google Workspace setup.",
            "argument-hint": "<role>",
            "user-invocable": "true",
        },
    )
    _write_skill(
        pdir,
        "asana-setup",
        {
            "name": "asana-setup",
            "description": "Step-by-step guide for setting up Asana as a new hire.",
        },
    )
    _write_agent(
        pdir,
        "onboarding-guide",
        {
            "name": "onboarding-guide",
            "description": "Onboarding assistant. Answers questions about tools and processes.",
        },
    )
    return pdir


def _section(doc, plugin):
    return doc["plugins"][plugin]


# --------------------------------------------------------------------------- #
# humanize
# --------------------------------------------------------------------------- #


def test_humanize_basic():
    assert humanize("dept-howto") == "Dept Howto"
    assert humanize("gws-onboarding") == "Gws Onboarding"
    assert humanize("asana_setup") == "Asana Setup"
    assert humanize("query") == "Query"


def test_humanize_preserves_internal_caps():
    # Only the first char of each token is forced upper — internal caps stay.
    assert humanize("APIClient-helper") == "APIClient Helper"


# --------------------------------------------------------------------------- #
# Fresh derivation
# --------------------------------------------------------------------------- #


def test_fresh_scaffold_derives_all_fields(tmp_path):
    _make_dept_howto(tmp_path)
    doc, report = scaffold_metadata(tmp_path, existing={}, generated_at=FIXED_TS)

    plugin = _section(doc, "dept-howto")
    assert plugin["display_name"] == "Dept Howto"
    assert plugin["tagline"].startswith("How-to guides and onboarding skills")

    skills = plugin["skills"]
    # argument-hint flows into the invocation string
    assert skills["gws-onboarding"]["invocation"] == "/dept-howto:gws-onboarding <role>"
    assert skills["gws-onboarding"]["display_name"] == "Gws Onboarding"
    assert skills["gws-onboarding"]["when_to_use"].startswith("Guides a new team member")
    # no argument-hint → bare invocation
    assert skills["asana-setup"]["invocation"] == "/dept-howto:asana-setup"

    agent = plugin["agents"]["onboarding-guide"]
    assert agent["invocation"] == "@dept-howto:onboarding-guide"
    assert agent["display_name"] == "Onboarding Guide"

    assert report.plugins == ["dept-howto"]
    assert len(report.skills) == 2
    assert len(report.agents) == 1
    # Fresh run: every derived field is freshly generated.
    assert report.status_counts().get("generated", 0) >= 6


def test_generated_block_records_owned_fields(tmp_path):
    _make_dept_howto(tmp_path)
    doc, _ = scaffold_metadata(tmp_path, existing={}, generated_at=FIXED_TS)
    gen = doc["_generated"]
    assert gen["by"] == "agnes-marketplace-scaffold"
    assert gen["at"] == FIXED_TS
    fields = gen["fields"]
    assert "display_name" in fields["plugins/dept-howto"]
    assert "tagline" in fields["plugins/dept-howto"]
    assert "invocation" in fields["plugins/dept-howto/skills/gws-onboarding"]
    assert "when_to_use" in fields["plugins/dept-howto/skills/asana-setup"]
    assert "invocation" in fields["plugins/dept-howto/agents/onboarding-guide"]


# --------------------------------------------------------------------------- #
# Merge contract
# --------------------------------------------------------------------------- #


def test_keep_fields_and_human_authored_values_preserved(tmp_path):
    """A field present in the file that the scaffolder never generated (here a
    hand-written display_name + a cover_photo KEEP field) survives a run."""
    _make_dept_howto(tmp_path)
    existing = {
        "plugins": {
            "dept-howto": {
                "cover_photo": ".agnes/dept-howto.png",
                "category": "Knowledge",
                "display_name": "Department How-To",  # human, no provenance
            }
        }
    }
    doc, report = scaffold_metadata(tmp_path, existing=existing, generated_at=FIXED_TS)
    plugin = _section(doc, "dept-howto")
    # KEEP fields untouched
    assert plugin["cover_photo"] == ".agnes/dept-howto.png"
    assert plugin["category"] == "Knowledge"
    # Human display_name kept (no prior provenance hash → kept-human)
    assert plugin["display_name"] == "Department How-To"
    assert ("plugins/dept-howto", "display_name", "kept-human") in report.actions
    # And the kept-human field is NOT recorded as machine-owned.
    assert "display_name" not in doc["_generated"]["fields"].get("plugins/dept-howto", {})


def test_human_edit_of_generated_field_wins_on_rerun(tmp_path):
    _make_dept_howto(tmp_path)
    # First run generates everything (and records provenance hashes).
    doc1, _ = scaffold_metadata(tmp_path, existing={}, generated_at=FIXED_TS)
    # A curator polishes the generated tagline.
    doc1["plugins"]["dept-howto"]["tagline"] = "Onboarding, the easy way."
    # Second run feeds the edited doc back in.
    doc2, report = scaffold_metadata(tmp_path, existing=doc1, generated_at=FIXED_TS)
    assert doc2["plugins"]["dept-howto"]["tagline"] == "Onboarding, the easy way."
    assert ("plugins/dept-howto", "tagline", "kept-edited") in report.actions
    # Ownership released — the edited field is no longer tracked as generated.
    assert "tagline" not in doc2["_generated"]["fields"].get("plugins/dept-howto", {})


def test_untouched_generated_field_regenerates_on_source_change(tmp_path):
    pdir = _make_dept_howto(tmp_path)
    doc1, _ = scaffold_metadata(tmp_path, existing={}, generated_at=FIXED_TS)
    # The plugin description changes upstream; the curator never touched the tagline.
    _write_plugin_json(pdir, "dept-howto", "A brand new description for the plugin.")
    doc2, report = scaffold_metadata(tmp_path, existing=doc1, generated_at=FIXED_TS)
    assert doc2["plugins"]["dept-howto"]["tagline"] == "A brand new description for the plugin."
    assert ("plugins/dept-howto", "tagline", "regenerated") in report.actions


def test_rename_skill_regenerates_invocation(tmp_path):
    pdir = _make_dept_howto(tmp_path)
    doc1, _ = scaffold_metadata(tmp_path, existing={}, generated_at=FIXED_TS)
    # Curator renames the skill via its frontmatter `name`.
    _write_skill(
        pdir,
        "gws-onboarding",
        {
            "name": "google-workspace",
            "description": "Guides a new team member through Google Workspace setup.",
            "argument-hint": "<role>",
        },
    )
    doc2, _ = scaffold_metadata(tmp_path, existing=doc1, generated_at=FIXED_TS)
    skills = doc2["plugins"]["dept-howto"]["skills"]
    assert skills["google-workspace"]["invocation"] == "/dept-howto:google-workspace <role>"
    # Old name kept as an orphan (never deleted), new name added.
    assert "gws-onboarding" in skills


def test_idempotent_no_op_second_run(tmp_path):
    _make_dept_howto(tmp_path)
    doc1, _ = scaffold_metadata(tmp_path, existing={}, generated_at=FIXED_TS)
    doc2, report = scaffold_metadata(tmp_path, existing=doc1, generated_at=FIXED_TS)
    assert comparable_view(doc1) == comparable_view(doc2)
    # Nothing changed the second time.
    assert report.status_counts().get("generated", 0) == 0
    assert report.status_counts().get("regenerated", 0) == 0
    assert not report.wrote_changes()


# --------------------------------------------------------------------------- #
# Orphans / odd inputs
# --------------------------------------------------------------------------- #


def test_orphan_plugin_in_file_is_kept_and_reported(tmp_path):
    _make_dept_howto(tmp_path)
    existing = {"plugins": {"removed-plugin": {"display_name": "Gone"}}}
    doc, report = scaffold_metadata(tmp_path, existing=existing, generated_at=FIXED_TS)
    assert doc["plugins"]["removed-plugin"]["display_name"] == "Gone"
    assert "plugins/removed-plugin" in report.orphans


def test_remote_source_skips_enumeration_but_keeps_plugin_fields(tmp_path):
    cp = tmp_path / ".claude-plugin"
    cp.mkdir(parents=True)
    (cp / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "m",
                "plugins": [
                    {
                        "name": "remote-plugin",
                        "description": "A plugin sourced from a git URL.",
                        "source": "https://github.com/x/y",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    doc, report = scaffold_metadata(tmp_path, existing={}, generated_at=FIXED_TS)
    plugin = doc["plugins"]["remote-plugin"]
    assert plugin["display_name"] == "Remote Plugin"
    assert plugin["tagline"] == "A plugin sourced from a git URL."
    assert "skills" not in plugin and "agents" not in plugin
    assert any("remote source" in w for w in report.warnings)


def test_missing_manifest_raises(tmp_path):
    try:
        scaffold_metadata(tmp_path, existing={})
    except ScaffoldError as e:
        assert "manifest" in str(e)
    else:
        raise AssertionError("expected ScaffoldError for missing marketplace.json")


# --------------------------------------------------------------------------- #
# Parity + round-trip with the rest of Agnes
# --------------------------------------------------------------------------- #


def test_inner_names_match_marketplace_listing(tmp_path):
    """The scaffolder's skill/agent keys must equal what Agnes resolves at
    request time, or the enrichment would never attach."""
    pdir = _make_dept_howto(tmp_path)
    doc, _ = scaffold_metadata(tmp_path, existing={}, generated_at=FIXED_TS)
    assert sorted(doc["plugins"]["dept-howto"]["skills"]) == sorted(
        list_inner_skills(pdir)
    )
    assert sorted(doc["plugins"]["dept-howto"]["agents"]) == sorted(
        list_inner_agents(pdir)
    )


def test_output_round_trips_through_runtime_parser(tmp_path):
    """The emitted document is consumed by the real parser without loss."""
    _make_dept_howto(tmp_path)
    doc, _ = scaffold_metadata(tmp_path, existing={}, generated_at=FIXED_TS)

    plugin_resolved = resolve_plugin_metadata(doc, "dept-howto")
    assert plugin_resolved["display_name"] == "Dept Howto"
    assert plugin_resolved["tagline"].startswith("How-to guides")

    skill_resolved = resolve_inner_metadata(doc, "dept-howto", "skills", "gws-onboarding")
    assert skill_resolved["invocation"] == "/dept-howto:gws-onboarding <role>"
    assert skill_resolved["display_name"] == "Gws Onboarding"
    assert skill_resolved["when_to_use"].startswith("Guides a new team member")

    agent_resolved = resolve_inner_metadata(
        doc, "dept-howto", "agents", "onboarding-guide"
    )
    assert agent_resolved["invocation"] == "@dept-howto:onboarding-guide"


def test_generated_block_is_ignored_by_runtime_parser(tmp_path):
    """The provenance block must not leak into any resolved plugin/skill."""
    _make_dept_howto(tmp_path)
    doc, _ = scaffold_metadata(tmp_path, existing={}, generated_at=FIXED_TS)
    # `_generated` is a top-level sibling of `plugins`; the parser only reads
    # `plugins`, so resolving a plugin named "_generated" yields nothing.
    assert resolve_plugin_metadata(doc, "_generated") == {}


def test_render_document_is_valid_json_with_trailing_newline(tmp_path):
    _make_dept_howto(tmp_path)
    doc, _ = scaffold_metadata(tmp_path, existing={}, generated_at=FIXED_TS)
    rendered = render_document(doc)
    assert rendered.endswith("\n")
    assert json.loads(rendered)["plugins"]["dept-howto"]["display_name"] == "Dept Howto"


# --------------------------------------------------------------------------- #
# --check view
# --------------------------------------------------------------------------- #


def test_comparable_view_detects_out_of_sync_then_in_sync(tmp_path):
    _make_dept_howto(tmp_path)
    # Empty file vs scaffolded → out of sync.
    doc, _ = scaffold_metadata(tmp_path, existing={}, generated_at=FIXED_TS)
    assert comparable_view({}) != comparable_view(doc)
    # Scaffolded file re-scaffolds to the same view → in sync (timestamp ignored).
    doc2, _ = scaffold_metadata(tmp_path, existing=doc, generated_at="2099-01-01T00:00:00Z")
    assert comparable_view(doc) == comparable_view(doc2)
