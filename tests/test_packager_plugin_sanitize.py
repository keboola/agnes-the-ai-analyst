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

    out = json.loads(_sanitize_served_plugin_json(raw, pdir, "keboola-howto"))

    assert out["skills"] == "./skills"   # populated → kept
    assert "agents" not in out           # empty dir → dropped
    assert "commands" not in out         # absent dir → dropped
    assert out["name"] == "keboola-howto"  # other fields untouched


def test_noop_when_all_dirs_populated(tmp_path):
    manifest = {"name": "p", "version": "1", "description": "d", "skills": "./skills"}
    pdir = _plugin(tmp_path, manifest, empty_agents=False)
    raw = (pdir / ".claude-plugin" / "plugin.json").read_bytes()
    # No changes → returns the exact same bytes (determinism preserved).
    assert _sanitize_served_plugin_json(raw, pdir, "p") == raw


def test_leaves_non_string_component_untouched(tmp_path):
    manifest = {"name": "p", "version": "1", "description": "d", "agents": ["./a.md"]}
    pdir = _plugin(tmp_path, manifest, with_skill=False, empty_agents=False)
    raw = (pdir / ".claude-plugin" / "plugin.json").read_bytes()
    # Array form is a valid explicit list — not our concern; leave as-is.
    assert _sanitize_served_plugin_json(raw, pdir, "p") == raw


def test_bad_json_returned_unchanged(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    assert _sanitize_served_plugin_json(b"not json{", pdir, "p") == b"not json{"


# --- served identity matches the catalog entry ---------------------------
#
# Claude Code resolves a loaded plugin back to its catalog entry BY NAME, so
# the `name` in the served `plugins/<prefix>/.claude-plugin/plugin.json` must
# equal the `name` of that plugin's entry in the served `marketplace.json`.
# The two are produced by different code for the two entry kinds — a bundle
# gets a synthesized file (`_bundle_plugin_json_bytes`), a curated plugin gets
# the curator's real file copied through `_sanitize_served_plugin_json` — and
# they diverged exactly when `resolve_manifest_name` rejected a declared name
# and fell back to the upstream one, which surfaces as the
# "Plugin <X> not found in marketplace" error name resolution exists to
# prevent. Both channels serve the same file set, so both are checked.

import pytest

from src.marketplace_filter import resolve_manifest_name


def _curated_plugin(tmp_path, declared_name: str) -> dict:
    pdir = tmp_path / "vendor-plugin"
    (pdir / ".claude-plugin").mkdir(parents=True)
    (pdir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": declared_name, "version": "1.0", "description": "d", "skills": "./skills"})
    )
    (pdir / "skills" / "s").mkdir(parents=True)
    (pdir / "skills" / "s" / "SKILL.md").write_text("# s")
    return {
        "marketplace_id": "test",
        "marketplace_slug": "test",
        "original_name": "vendor-plugin",
        "prefixed_name": "test-vendor-plugin",
        # what the serving path computes for this dir
        "manifest_name": resolve_manifest_name(pdir, "vendor-plugin"),
        "version": "1.0",
        "plugin_dir": pdir,
        "raw": {"name": declared_name, "version": "1.0", "description": "d"},
        "source": "marketplace",
    }


def _zip_files(plugins):
    from app.marketplace_server import packager

    return {arc: data for arc, data in packager._collect_members(plugins, etag="e")}


def _git_files(plugins, monkeypatch):
    from app.marketplace_server import git_backend

    monkeypatch.setattr(git_backend.marketplace_filter, "resolve_user_marketplace", lambda *a, **k: plugins)
    monkeypatch.setattr(git_backend.marketplace_filter, "compute_etag", lambda *a, **k: "e")
    return git_backend.file_set_for_user(None, {"id": "u", "email": "u@example.com"})


@pytest.mark.parametrize("channel", ["zip", "git"])
@pytest.mark.parametrize(
    "declared_name",
    [
        "vendor/plugin",  # separator — rejected by is_safe_plugin_name
        "vendor\x00plugin",  # control character
        "v" * 100,  # over MAX_MANIFEST_NAME_LEN
        "vendor-plugin",  # conformant — the invariant must hold here too
    ],
)
def test_served_plugin_json_name_matches_its_catalog_entry(tmp_path, monkeypatch, channel, declared_name):
    plugin = _curated_plugin(tmp_path, declared_name)
    files = _zip_files([plugin]) if channel == "zip" else _git_files([plugin], monkeypatch)

    manifest = json.loads(files[".claude-plugin/marketplace.json"])
    entry_name = next(e["name"] for e in manifest["plugins"] if e["source"].endswith("test-vendor-plugin"))
    served = json.loads(files["plugins/test-vendor-plugin/.claude-plugin/plugin.json"])

    assert served["name"] == entry_name, "served plugin.json identity diverged from its catalog entry"


def test_a_rejected_name_is_not_what_gets_served(tmp_path):
    """Guards the direction: the bad name must be gone, not merely consistent."""
    plugin = _curated_plugin(tmp_path, "vendor/plugin")
    files = _zip_files([plugin])
    served = json.loads(files["plugins/test-vendor-plugin/.claude-plugin/plugin.json"])
    assert served["name"] == "vendor-plugin"
    assert served["skills"] == "./skills", "unrelated keys still pass through"
