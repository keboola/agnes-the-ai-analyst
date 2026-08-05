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
