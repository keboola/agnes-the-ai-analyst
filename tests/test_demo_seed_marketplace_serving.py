"""Serving-level check for the baked demo marketplace.

The baked fixture must produce real plugins when copied into the
marketplaces dir and read back through ``read_plugins`` — the same path
the instance uses to refresh its plugin cache. Guards against the manifest
(``.claude-plugin/marketplace.json``) regressing to missing/empty, which
would silently serve zero plugins.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from src import _demo_seed

BAKED = Path(_demo_seed.__file__).parent / "marketplace"


def test_baked_marketplace_serves_two_plugins(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    dest = tmp_path / "marketplaces" / "demo"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(BAKED, dest)

    from src.marketplace import read_plugins

    plugins = read_plugins("demo")
    names = sorted(p["name"] for p in plugins)
    assert names == ["cohort-helper", "revenue-explorer"]
    # Each plugin's source dir must exist on disk so the detail/serving
    # layer can read its SKILL.md.
    for p in plugins:
        src_rel = p["source"].lstrip("./")
        assert (dest / src_rel / "skills" / p["name"] / "SKILL.md").is_file()
