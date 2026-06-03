import json
from pathlib import Path
from src import _demo_seed

ROOT = Path(_demo_seed.__file__).parent


def test_memory_items_fixture_valid():
    data = json.loads((ROOT / "memory_items.json").read_text())
    assert len(data["domains"]) == 6
    assert len(data["items"]) >= 20
    slugs = {d["slug"] for d in data["domains"]}
    for it in data["items"]:
        assert it["id"] and it["content"] and it["title"]
        assert it["domain"] in slugs            # every item's domain is defined


def test_data_package_fixture_valid():
    pkg = json.loads((ROOT / "data_package.json").read_text())
    assert pkg["slug"] and pkg["name"] and isinstance(pkg["tables"], list) and pkg["tables"]


def test_marketplace_metadata_present():
    md = ROOT / "marketplace" / ".claude-plugin" / "marketplace-metadata.json"
    assert json.loads(md.read_text())["plugins"]


def test_marketplace_manifest_present_and_matches_enrichment():
    """The authoritative Claude Code manifest must exist with a non-empty
    ``plugins`` array, and its plugin names must match the enrichment file's
    plugin keys. ``read_plugins`` reads only this file — a missing/empty
    manifest serves zero plugins regardless of the enrichment metadata.
    """
    cp = ROOT / "marketplace" / ".claude-plugin"
    manifest = json.loads((cp / "marketplace.json").read_text())
    plugins = manifest["plugins"]
    assert isinstance(plugins, list) and plugins
    manifest_names = {p["name"] for p in plugins}

    enrichment = json.loads((cp / "marketplace-metadata.json").read_text())
    assert manifest_names == set(enrichment["plugins"].keys())

    # Every plugin's source dir must exist on disk so the served plugin
    # resolves to real content.
    for p in plugins:
        src_rel = p["source"].lstrip("./")
        assert (ROOT / "marketplace" / src_rel).is_dir()
