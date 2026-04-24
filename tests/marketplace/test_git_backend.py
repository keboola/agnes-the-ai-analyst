from __future__ import annotations

import base64
from pathlib import Path

from dulwich.repo import Repo

from app.api.marketplace import _git_backend as git_backend, _packager as packager


def test_cache_key_stable(configured):
    k1 = git_backend.cache_key_for_email("admin@test")
    k2 = git_backend.cache_key_for_email("admin@test")
    assert k1 == k2
    assert len(k1) == 16
    assert all(c in "0123456789abcdef" for c in k1)


def test_cache_key_differs_by_group(configured):
    admin_key = git_backend.cache_key_for_email("admin@test")
    finance_key = git_backend.cache_key_for_email("finance@test")
    assert admin_key != finance_key


def test_file_set_for_admin_contains_all(configured):
    allowed = packager.resolve_allowed_plugin_names(
        packager.load_user_groups("admin@test")
    )
    files = git_backend.file_set_for_allowed(allowed)
    assert ".claude-plugin/marketplace.json" in files
    assert "plugins/alpha/README.md" in files
    assert "plugins/beta/README.md" in files
    assert "plugins/gamma/README.md" in files
    assert "global-rules/rules.md" in files
    # .agnes/version.json must be absent — runtime artifact that defeats
    # deterministic commit hashing.
    assert ".agnes/version.json" not in files
    assert all(isinstance(v, bytes) for v in files.values())


def test_file_set_for_finance_excludes_others(configured):
    allowed = packager.resolve_allowed_plugin_names(
        packager.load_user_groups("finance@test")
    )
    files = git_backend.file_set_for_allowed(allowed)
    assert "plugins/alpha/README.md" in files
    assert not any(p.startswith("plugins/beta/") for p in files)
    assert not any(p.startswith("plugins/gamma/") for p in files)
    import json
    mkt = json.loads(files[".claude-plugin/marketplace.json"])
    assert [p["name"] for p in mkt["plugins"]] == ["alpha"]


def test_build_bare_repo_creates_valid_repo(configured, tmp_path):
    target = tmp_path / "repo.git"
    allowed = {"alpha", "beta"}
    git_backend.build_bare_repo(allowed, target)

    assert (target / "HEAD").is_file()
    repo = Repo(str(target))
    try:
        head = repo.refs[b"HEAD"]
        commit = repo[head]
        assert commit.message == b"agnes marketplace snapshot"
        assert commit.author == b"agnes-marketplace <noreply@agnes.local>"
        assert commit.commit_time == 0
    finally:
        repo.close()


def test_build_bare_repo_tree_contains_expected_paths(configured, tmp_path):
    target = tmp_path / "repo.git"
    git_backend.build_bare_repo({"alpha"}, target)
    repo = Repo(str(target))
    try:
        head = repo.refs[b"HEAD"]
        commit = repo[head]
        paths = set()

        def walk(tree_sha: bytes, prefix: str = "") -> None:
            tree = repo[tree_sha]
            for entry in tree.items():
                full = f"{prefix}{entry.path.decode()}"
                obj = repo[entry.sha]
                if obj.type_name == b"tree":
                    walk(entry.sha, full + "/")
                else:
                    paths.add(full)

        walk(commit.tree)
        assert ".claude-plugin/marketplace.json" in paths
        assert "plugins/alpha/README.md" in paths
        assert "plugins/alpha/.claude-plugin/plugin.json" in paths
        assert "global-rules/rules.md" in paths
        assert not any(p.startswith("plugins/beta/") for p in paths)
    finally:
        repo.close()


def test_build_bare_repo_is_deterministic(configured, tmp_path):
    a = tmp_path / "a.git"
    b = tmp_path / "b.git"
    git_backend.build_bare_repo({"alpha", "beta"}, a)
    git_backend.build_bare_repo({"alpha", "beta"}, b)
    ra, rb = Repo(str(a)), Repo(str(b))
    try:
        assert ra.refs[b"HEAD"] == rb.refs[b"HEAD"]
    finally:
        ra.close()
        rb.close()


def test_ensure_repo_creates_on_miss(configured):
    path = git_backend.ensure_repo_for_email("admin@test")
    assert path.is_dir()
    assert path.name.endswith(".git")
    assert (path / "HEAD").is_file()


def test_ensure_repo_reuses_on_hit(configured):
    p1 = git_backend.ensure_repo_for_email("admin@test")
    mtime1 = (p1 / "HEAD").stat().st_mtime_ns
    p2 = git_backend.ensure_repo_for_email("admin@test")
    assert p1 == p2
    mtime2 = (p2 / "HEAD").stat().st_mtime_ns
    assert mtime1 == mtime2


def test_ensure_repo_different_users_different_paths(configured):
    admin_path = git_backend.ensure_repo_for_email("admin@test")
    finance_path = git_backend.ensure_repo_for_email("finance@test")
    assert admin_path != finance_path
    assert admin_path.parent == finance_path.parent


def _basic(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def test_email_from_basic_auth_valid():
    assert git_backend.email_from_basic_auth(_basic("x", "admin@test")) == "admin@test"


def test_email_from_basic_auth_missing():
    assert git_backend.email_from_basic_auth(None) is None
    assert git_backend.email_from_basic_auth("") is None


def test_email_from_basic_auth_wrong_scheme():
    assert git_backend.email_from_basic_auth("Bearer abc") is None


def test_email_from_basic_auth_malformed_base64():
    assert git_backend.email_from_basic_auth("Basic !!!notbase64!!!") is None


def test_email_from_basic_auth_missing_colon():
    import base64 as b64
    bad = "Basic " + b64.b64encode(b"nocolon").decode()
    assert git_backend.email_from_basic_auth(bad) is None


def test_email_from_basic_auth_empty_password():
    empty_pw = "Basic " + base64.b64encode(b"x:").decode()
    assert git_backend.email_from_basic_auth(empty_pw) is None


def test_is_known_email(configured):
    assert git_backend.is_known_email("admin@test") is True
    assert git_backend.is_known_email("finance@test") is True
    assert git_backend.is_known_email("stranger@test") is False
    assert git_backend.is_known_email("") is False


def test_email_from_basic_auth_case_insensitive_scheme():
    encoded = base64.b64encode(b"x:admin@test").decode()
    assert git_backend.email_from_basic_auth(f"basic {encoded}") == "admin@test"
    assert git_backend.email_from_basic_auth(f"BASIC {encoded}") == "admin@test"
    assert git_backend.email_from_basic_auth(f"Basic {encoded}") == "admin@test"


def test_is_known_email_rejects_non_dict_config(configured, tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("[\"not\", \"a\", \"dict\"]")
    monkeypatch.setattr("app.api.marketplace._packager.USER_GROUPS_PATH", bad)
    assert git_backend.is_known_email("admin@test") is False


def test_ensure_repo_uses_single_config_snapshot(configured, monkeypatch):
    """Regression for a TOCTOU bug: the cache key and the repo contents
    must be derived from the same snapshot of user_groups / allowed plugins.
    """
    admin_path = git_backend.ensure_repo_for_email("admin@test")
    admin_key = admin_path.name.removesuffix(".git")

    again = git_backend.ensure_repo_for_email("admin@test")
    assert again == admin_path
    assert admin_key == git_backend.cache_key_for_email("admin@test")
