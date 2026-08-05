"""Regression guards for the 2026-08-05 security audit.

Mirrors tests/test_security_audit_20260724.py: one test per finding, named after
it, so a refactor that reintroduces the hole fails with the finding id visible.
"""

import json

import pytest

# ── F-1: plugin name is a path segment; traversal must not survive ingest ──


@pytest.mark.parametrize(
    "name",
    [
        "..",
        ".",
        "../../../state",
        "analytics-tools/../../../state",
        "a/b",
        "a\\b",
        "\x00evil",
        " padded ",
        "trailing\n",
    ],
)
def test_f1_is_safe_plugin_name_rejects(name):
    from src.marketplace import is_safe_plugin_name

    assert is_safe_plugin_name(name) is False


@pytest.mark.parametrize("name", ["legit", "analytics-tools", "a.b_c-1"])
def test_f1_is_safe_plugin_name_accepts_plain_segments(name):
    from src.marketplace import is_safe_plugin_name

    assert is_safe_plugin_name(name) is True


def test_f1_is_safe_plugin_name_rejects_non_strings():
    from src.marketplace import is_safe_plugin_name

    assert is_safe_plugin_name(None) is False
    assert is_safe_plugin_name(42) is False
    assert is_safe_plugin_name({"name": "x"}) is False


def test_f1_read_plugins_drops_unsafe_names(tmp_path, monkeypatch):
    """A hostile marketplace.json must not put a traversing name into the DB."""
    import src.marketplace as mp

    root = tmp_path / "marketplaces"
    (root / "acme" / ".claude-plugin").mkdir(parents=True)
    (root / "acme" / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "plugins": [
                    {"name": "good-plugin"},
                    {"name": " padded-ok "},  # stripped form is safe → kept
                    {"name": "../../../state"},
                    {"name": "nested/plugin"},
                    {"name": ".."},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mp, "get_marketplaces_dir", lambda: root)

    names = [p["name"] for p in mp.read_plugins("acme")]

    assert names == ["good-plugin", " padded-ok "]


# ── F-1 layer 2: containment at the path-construction site ──


def test_f1_contained_plugin_dir_rejects_escape(tmp_path):
    """A row that bypassed ingest (older Agnes, hand-edited DB) still can't escape."""
    from src.marketplace_filter import _contained_plugin_dir

    root = tmp_path / "marketplaces"
    (root / "acme" / "plugins").mkdir(parents=True)
    (tmp_path / "state").mkdir()

    assert _contained_plugin_dir(root, "acme", "../../../state") is None
    assert _contained_plugin_dir(root, "acme", "..") is None
    assert _contained_plugin_dir(root, "acme", "a/b") is None


def test_f1_contained_plugin_dir_accepts_plain_name(tmp_path):
    from src.marketplace_filter import _contained_plugin_dir

    root = tmp_path / "marketplaces"
    (root / "acme" / "plugins" / "legit").mkdir(parents=True)

    assert _contained_plugin_dir(root, "acme", "legit") == root / "acme" / "plugins" / "legit"


def test_f1_contained_plugin_dir_rejects_symlinked_segment(tmp_path):
    """A `plugins/<name>` that is itself a symlink out of the root is contained."""
    from src.marketplace_filter import _contained_plugin_dir

    root = tmp_path / "marketplaces"
    (root / "acme" / "plugins").mkdir(parents=True)
    outside = tmp_path / "state"
    outside.mkdir()
    (root / "acme" / "plugins" / "sneaky").symlink_to(outside)

    assert _contained_plugin_dir(root, "acme", "sneaky") is None


# ── F-1: third path — the v2 skills endpoint ──


def test_f1_v2_skills_path_is_contained(tmp_path, monkeypatch):
    """_skills_for_plugin must not read SKILL.md from outside the marketplaces root.

    This is the third construction of `<root>/<slug>/plugins/<name>` in the
    codebase; the audit found the two in marketplace_filter and missed this one.
    Its output is returned in an HTTP response body, so an escape here discloses
    file contents directly.
    """
    import app.api.v2_marketplace as v2

    root = tmp_path / "marketplaces"
    (root / "acme" / "plugins").mkdir(parents=True)
    # A SKILL.md that lives OUTSIDE the marketplaces root.
    outside = tmp_path / "elsewhere" / "skills" / "leak"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("---\nname: leak\n---\nSECRET-BODY\n", encoding="utf-8")

    monkeypatch.setattr(v2, "get_marketplaces_dir", lambda: root)

    # Three `..` to climb plugins -> acme -> marketplaces -> tmp_path. Two would
    # land on <root>/elsewhere, which does not exist, and the test would pass
    # for the wrong reason.
    entries = v2._skills_for_plugin("acme", "../../../elsewhere")

    assert entries == [], f"escaped the marketplaces root: {entries!r}"
