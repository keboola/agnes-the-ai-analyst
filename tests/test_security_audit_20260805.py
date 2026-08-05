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


# ── F-1b: hostile symlinks inside a legitimately-named plugin dir ──


def test_f1b_escapes_base_flags_symlink_and_outside_paths(tmp_path):
    from src.marketplace_filter import escapes_base

    root = tmp_path / "marketplaces"
    base = root / "acme" / "plugins" / "legit"
    base.mkdir(parents=True)
    secret = tmp_path / "system_secret.txt"
    secret.write_text("SUPER-SECRET", encoding="utf-8")

    plain = base / "README.md"
    plain.write_text("hi", encoding="utf-8")
    link = base / "evil.txt"
    link.symlink_to(secret)

    assert escapes_base(plain, [root]) is False
    assert escapes_base(link, [root]) is True
    assert escapes_base(secret, [root]) is True


def test_f1b_escapes_base_accepts_any_of_several_bases(tmp_path):
    """Multi-base: cowork_packager merges several source roots into one zip."""
    from src.marketplace_filter import escapes_base

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    f = b / "SKILL.md"
    f.write_text("hi", encoding="utf-8")

    assert escapes_base(f, [a, b]) is False
    assert escapes_base(f, [a]) is True


def test_f1b_symlinked_plugin_dir_is_stopped_by_layer_a_not_layer_b(tmp_path):
    """The two layers divide the work; neither covers the other's case alone.

    Layer B (escapes_base) is anchored on the resolved plugin_dir, so it cannot
    see a plugin_dir that IS a symlink — resolving re-anchors it on the target.
    That case belongs to layer A (_contained_plugin_dir), which refuses to hand
    out such a plugin_dir at all. Asserting both halves here keeps a future
    refactor from dropping layer A on the assumption that layer B covers it.
    """
    from src.marketplace_filter import _contained_plugin_dir, escapes_base

    root = tmp_path / "marketplaces"
    (root / "acme" / "plugins").mkdir(parents=True)
    outside = tmp_path / "state"
    outside.mkdir()
    (outside / "system_secret.txt").write_text("SUPER-SECRET", encoding="utf-8")
    plugin_dir = root / "acme" / "plugins" / "sneaky"
    plugin_dir.symlink_to(outside)

    leaked = next(p for p in plugin_dir.rglob("*") if p.is_file())

    # Layer B, anchored on the resolved dir, does NOT catch this.
    assert escapes_base(leaked, [plugin_dir.resolve()]) is False
    # Layer A does — the plugin_dir is never produced in the first place.
    assert _contained_plugin_dir(root, "acme", "sneaky") is None


def test_f1b_zip_packager_skips_symlinked_files(tmp_path, monkeypatch):
    """A symlink in plugin content must not put out-of-tree bytes in the ZIP."""
    from app.marketplace_server import packager

    root = tmp_path / "marketplaces"
    plugin_dir = root / "acme" / "plugins" / "legit"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "README.md").write_text("hi", encoding="utf-8")
    secret = tmp_path / "system_secret.txt"
    secret.write_text("SUPER-SECRET", encoding="utf-8")
    (plugin_dir / "leak.txt").symlink_to(secret)

    members = packager._collect_members(
        [
            {
                "prefixed_name": "acme-legit",
                "manifest_name": "legit",
                "original_name": "legit",
                "marketplace_slug": "acme",
                "version": None,
                "raw": {},
                "plugin_dir": plugin_dir,
            }
        ],
        "deadbeef",
    )

    arcs = [arc for arc, _ in members]
    payloads = b"".join(data for _, data in members)
    assert not any(a.endswith("leak.txt") for a in arcs)
    assert b"SUPER-SECRET" not in payloads
    assert any(a.endswith("README.md") for a in arcs)


# ── F-2: the marketplace sync PAT must never hit argv or .git/config ──


def test_f2_no_authenticated_url_helper_remains():
    """The credential-in-URL builder is gone, not merely unused."""
    import src.marketplace as mp

    assert not hasattr(mp, "_authenticated_url")


def test_f2_git_env_carries_token_and_helper():
    from src.marketplace import _CREDENTIAL_HELPER, _git_env

    env = _git_env("SECRET123")

    assert env["AGNES_TOKEN"] == "SECRET123"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "AGNES_TOKEN" in _CREDENTIAL_HELPER
    assert "SECRET123" not in _CREDENTIAL_HELPER
    # No token → no AGNES_TOKEN leaked into the child env at all.
    assert "AGNES_TOKEN" not in _git_env(None)


def test_f2_scrub_strips_credentials_from_existing_config(tmp_path):
    """Instances that synced before the fix get their .git/config cleaned."""
    import subprocess

    from src.marketplace import _scrub_credentialed_remote

    repo = tmp_path / "acme"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://x-access-token:SECRET123@example.com/acme.git",
        ],
        check=True,
    )
    assert "SECRET123" in (repo / ".git" / "config").read_text(encoding="utf-8")

    _scrub_credentialed_remote(repo, "https://example.com/acme.git")

    config = (repo / ".git" / "config").read_text(encoding="utf-8")
    assert "SECRET123" not in config
    assert "https://example.com/acme.git" in config


def test_f2_scrub_runs_even_when_ref_validation_rejects_the_row(tmp_path, monkeypatch):
    """A row with a malformed ref still gets its pre-fix credential scrubbed.

    The ValueError is raised before the sync body, so a scrub placed inside that
    body would never run for such a row — leaving the PAT on disk until an admin
    noticed. Ordering matters here, hence the test.
    """
    import subprocess

    import src.marketplace as mp

    root = tmp_path / "marketplaces"
    repo = root / "acme"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://x-access-token:SECRET123@example.com/acme.git",
        ],
        check=True,
    )
    monkeypatch.setattr(mp, "get_marketplaces_dir", lambda: root)

    with pytest.raises(ValueError):
        mp._sync_spec({"id": "acme", "url": "https://example.com/acme.git", "ref": "bad..ref"})

    assert "SECRET123" not in (repo / ".git" / "config").read_text(encoding="utf-8")


# ── F-4: every quoted SQL identifier routes through quote_ident ──

# Statement positions where a bare `"{var}"` is an IDENTIFIER, not a literal.
#
# Two alternatives, and the second one is load-bearing:
#   1. a keyword, optionally followed by a qualifier prefix — `FROM "{t}"`,
#      `FROM lake."{t}"`
#   2. ANY dot immediately before the quote — `lake."{schema}"."{table}"`,
#      `{source_name}."{name}"`
#
# The first version of this guard had only alternative 1, so it could not see a
# qualified name whose quoted part follows a dot. That blind spot sat in exactly
# the multi-part-identifier class this sweep is about, and it made the guard's
# own "allowlist is empty" claim true only by not looking. Caught in review, not
# by the test — which is the point of writing it down here.
_IDENT_POSITION_RE = r'(?:(?:VIEW|TABLE|DESCRIBE|FROM|INTO|JOIN)\s+[\w.]*|\.)\\?"\{'


def test_f4_no_bare_quoted_identifier_interpolation():
    """Ratchet: the allowlist is empty and must stay empty.

    ``-P``, not ``-E``. git grep's ``-E`` is POSIX ERE, where ``\\s`` degrades to
    a literal ``s`` — the ``-E`` form of this pattern matches NOTHING and the
    test would pass vacuously forever. That is not hypothetical: it is how this
    guard was first written during the 2026-08-05 audit follow-up, and it was
    caught in review rather than by the test.
    """
    import subprocess

    proc = subprocess.run(
        ["git", "grep", "-nP", _IDENT_POSITION_RE, "--", "*.py"],
        capture_output=True,
        text=True,
    )
    # git grep emits repo-relative paths with NO leading slash, so a
    # `"/tests/" not in ln` filter would never fire.
    hits = [ln for ln in proc.stdout.splitlines() if ln and not ln.startswith("tests/")]

    assert hits == [], "bare-quoted SQL identifier(s) — route through quote_ident:\n" + "\n".join(hits)


def test_f4_quote_ident_reexported_from_profiler():
    """Existing `from src.profiler import quote_ident` importers keep working."""
    from src.profiler import quote_ident as from_profiler
    from src.sql_ident import quote_ident as canonical

    assert from_profiler is canonical
    assert canonical('a") AS x, (SELECT 1) AS "b') == '"a"") AS x, (SELECT 1) AS ""b"'


# ── F-4b: server-supplied manifest ids are path segments AND SQL identifiers ──


def test_f4b_safe_id_re_rejects_traversal_and_injection():
    from cli.lib.pull import _SAFE_ID_RE

    assert _SAFE_ID_RE.match("orders_v2")
    assert _SAFE_ID_RE.match("orders.v2")  # dots are legitimate — admin-derived ids carry them
    assert not _SAFE_ID_RE.match("../../.ssh/authorized_keys")
    assert not _SAFE_ID_RE.match('x" AS y, (SELECT 1) AS "z')
    assert not _SAFE_ID_RE.match("a/b")


def test_f4b_pull_skips_unsafe_manifest_table_ids():
    """An unsafe id is dropped, and the drop must NOT become a pull error.

    cli/commands/pull.py raises typer.Exit(1) on a non-empty result.errors —
    including from the --quiet SessionStart hook path — so collecting these
    would turn one odd id into a permanently red `agnes pull`.
    """
    from cli.lib.pull import _safe_manifest_tables

    kept, dropped = _safe_manifest_tables(
        {
            "orders": {"hash": "a"},
            "orders.v2": {"hash": "b"},
            "../../../etc/passwd": {"hash": "c"},
            "..": {"hash": "d"},
            'x" AS y': {"hash": "e"},
        }
    )

    assert sorted(kept) == ["orders", "orders.v2"]
    assert sorted(dropped) == sorted(["../../../etc/passwd", "..", 'x" AS y'])
