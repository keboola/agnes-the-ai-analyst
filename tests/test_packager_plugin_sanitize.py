"""Served plugin.json sanitization — drop component keys pointing at empty dirs.

A scaffolded plugin often ships an unused `agents/` (or `commands/`) dir holding
only a `.gitkeep`. Claude Code's `plugin install` rejects such a plugin
("agents: Invalid input"), which broke the keboola-howto install in the
cloud-chat sandbox. The marketplace packager now drops those keys when serving.
"""
import json

from app.marketplace_server.packager import _sanitize_served_plugin_json


def _plugin(tmp_path, manifest, *, with_skill=True, empty_agents=True):
    pdir = tmp_path / "keboola-howto"
    (pdir / ".claude-plugin").mkdir(parents=True)
    (pdir / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    if with_skill:
        (pdir / "skills" / "howto").mkdir(parents=True)
        (pdir / "skills" / "howto" / "SKILL.md").write_text("# skill")
    if empty_agents:
        (pdir / "agents").mkdir()
        (pdir / "agents" / ".gitkeep").write_text("")
    return pdir


def test_drops_empty_component_dir_keeps_populated(tmp_path):
    manifest = {
        "name": "keboola-howto", "version": "0.1.0", "description": "d",
        "skills": "./skills", "agents": "./agents", "commands": "./commands",
    }
    pdir = _plugin(tmp_path, manifest)  # skills populated, agents empty, commands absent
    raw = (pdir / ".claude-plugin" / "plugin.json").read_bytes()

    out = json.loads(_sanitize_served_plugin_json(raw, pdir))

    assert out["skills"] == "./skills"   # populated → kept
    assert "agents" not in out           # empty dir → dropped
    assert "commands" not in out         # absent dir → dropped
    assert out["name"] == "keboola-howto"  # other fields untouched


def test_noop_when_all_dirs_populated(tmp_path):
    manifest = {"name": "p", "version": "1", "description": "d", "skills": "./skills"}
    pdir = _plugin(tmp_path, manifest, empty_agents=False)
    raw = (pdir / ".claude-plugin" / "plugin.json").read_bytes()
    # No changes → returns the exact same bytes (determinism preserved).
    assert _sanitize_served_plugin_json(raw, pdir) == raw


def test_leaves_non_string_component_untouched(tmp_path):
    manifest = {"name": "p", "version": "1", "description": "d", "agents": ["./a.md"]}
    pdir = _plugin(tmp_path, manifest, with_skill=False, empty_agents=False)
    raw = (pdir / ".claude-plugin" / "plugin.json").read_bytes()
    # Array form is a valid explicit list — not our concern; leave as-is.
    assert _sanitize_served_plugin_json(raw, pdir) == raw


def test_bad_json_returned_unchanged(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    assert _sanitize_served_plugin_json(b"not json{", pdir) == b"not json{"
