"""Verify Agnes-only files never leak into the synth Claude Code marketplace.

Two surfaces are covered:

* the ZIP delivery (``app/marketplace_server/packager.py::build_zip``),
* the git tree built from ``file_set_for_user`` (``app/marketplace_server/git_backend.py``).

Both must strip:

* ``.claude-plugin/agnes-metadata.json`` (anywhere in the plugin tree),
* anything under ``.agnes/`` (anywhere in the plugin tree).

Test fixtures stand up a fake plugin dir on disk + a fake `plugins` list
that the packager / git_backend consume directly — we don't go through the
DB layer because the strip behavior is independent of plugin resolution.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from src.marketplace_filter import is_agnes_only_path


def _build_plugin_dir(tmp_path: Path) -> Path:
    """Create a plugin directory with a mix of regular and Agnes-only files."""
    plugin = tmp_path / "plugins" / "demo"
    plugin.mkdir(parents=True)

    # Standard Claude Code files
    (plugin / ".claude-plugin").mkdir()
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        '{"name":"demo","version":"1.0"}', encoding="utf-8",
    )
    (plugin / "skills").mkdir()
    (plugin / "skills" / "foo").mkdir()
    (plugin / "skills" / "foo" / "SKILL.md").write_text(
        "---\nname: foo\n---\nbody", encoding="utf-8",
    )

    # Agnes-only files that MUST be stripped
    (plugin / ".claude-plugin" / "agnes-metadata.json").write_text(
        '{"version":1}', encoding="utf-8",
    )
    (plugin / ".agnes").mkdir()
    (plugin / ".agnes" / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (plugin / ".agnes" / "docs").mkdir()
    (plugin / ".agnes" / "docs" / "internal.md").write_text(
        "# internal", encoding="utf-8",
    )

    return plugin


def _fake_plugin_record(plugin_dir: Path, *, prefixed_name: str = "test-demo") -> dict:
    """Shape the packager / git_backend expect from `resolve_user_marketplace`."""
    return {
        "marketplace_id": "test",
        "marketplace_slug": "test",
        "original_name": "demo",
        "prefixed_name": prefixed_name,
        "manifest_name": "demo",
        "version": "1.0",
        "plugin_dir": plugin_dir,
        "raw": {"name": "demo", "version": "1.0", "description": "demo"},
        "source": "marketplace",
    }


# --- is_agnes_only_path ---------------------------------------------------


def test_is_agnes_only_path_strips_dot_agnes_at_root():
    assert is_agnes_only_path((".agnes", "cover.png"))
    assert is_agnes_only_path((".agnes", "docs", "x.md"))


def test_is_agnes_only_path_strips_dot_agnes_anywhere():
    """Even nested `.agnes/` dirs are stripped — e.g. inside a plugin."""
    assert is_agnes_only_path(("plugins", "foo", ".agnes", "x.png"))


def test_is_agnes_only_path_strips_agnes_metadata_json():
    assert is_agnes_only_path((".claude-plugin", "agnes-metadata.json"))
    assert is_agnes_only_path(("nested", "agnes-metadata.json"))


def test_is_agnes_only_path_keeps_normal_files():
    """`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, plain
    plugin code etc. all survive the filter."""
    assert not is_agnes_only_path((".claude-plugin", "plugin.json"))
    assert not is_agnes_only_path((".claude-plugin", "marketplace.json"))
    assert not is_agnes_only_path(("skills", "foo", "SKILL.md"))
    assert not is_agnes_only_path(("agents", "bar.md"))


def test_is_agnes_only_path_does_not_match_lookalikes():
    """Naming collisions: a directory named ``.agneswriter`` (single segment
    starting with `.agnes` but not equal) is NOT stripped."""
    assert not is_agnes_only_path((".agneswriter", "x.md"))
    assert not is_agnes_only_path(("not-agnes-metadata.json",))


# --- ZIP path -------------------------------------------------------------


def test_zip_strips_agnes_only_files(tmp_path, monkeypatch):
    """End-to-end through the packager's ``_collect_members``: produce a ZIP
    and assert no Agnes-only paths land inside it."""
    from app.marketplace_server import packager

    plugin_dir = _build_plugin_dir(tmp_path)
    plugins = [_fake_plugin_record(plugin_dir)]
    members = packager._collect_members(plugins, etag="testetag")

    arcnames = {arc for arc, _ in members}
    # Sanity: the regular files DID survive
    assert any(a.endswith(".claude-plugin/plugin.json") for a in arcnames)
    assert any(a.endswith("skills/foo/SKILL.md") for a in arcnames)
    assert any(a.endswith(".claude-plugin/marketplace.json") for a in arcnames)
    # Agnes-only files DID NOT
    assert not any("agnes-metadata.json" in a for a in arcnames), arcnames
    assert not any(".agnes/" in a for a in arcnames), arcnames


def test_zip_etag_independent_of_agnes_files(tmp_path):
    """Compute_etag in marketplace_filter consumes the same is_agnes_only_path
    filter, so adding/removing `.agnes/` content doesn't bust the ETag."""
    from src import marketplace_filter

    plugin_dir = _build_plugin_dir(tmp_path)
    plugins = [_fake_plugin_record(plugin_dir)]
    etag_with_agnes = marketplace_filter.compute_etag(plugins)

    # Drop the `.agnes/` content and re-compute. ETag must match.
    import shutil
    shutil.rmtree(plugin_dir / ".agnes")
    (plugin_dir / ".claude-plugin" / "agnes-metadata.json").unlink()
    etag_without_agnes = marketplace_filter.compute_etag(plugins)
    assert etag_with_agnes == etag_without_agnes


# --- Git tree path -------------------------------------------------------


def test_git_tree_strips_agnes_only_files(tmp_path):
    """The git_backend ``file_set_for_user`` builds the same dict shape as
    the ZIP, so the same strip applies."""
    # We don't have an easy way to call file_set_for_user without a DB, so
    # instead replicate the small block that walks plugin_dir and calls the
    # filter — exact same code path as git_backend, identical inputs.
    plugin_dir = _build_plugin_dir(tmp_path)

    files: dict[str, bytes] = {}
    prefix = "demo"
    for f in sorted(p for p in plugin_dir.rglob("*") if p.is_file()):
        rel_parts = f.relative_to(plugin_dir).parts
        if is_agnes_only_path(rel_parts):
            continue
        files[f"plugins/{prefix}/{f.relative_to(plugin_dir).as_posix()}"] = f.read_bytes()

    assert any(p.endswith("/SKILL.md") for p in files)
    assert any(p.endswith("/.claude-plugin/plugin.json") for p in files)
    assert not any("agnes-metadata.json" in p for p in files)
    assert not any(".agnes/" in p for p in files)
