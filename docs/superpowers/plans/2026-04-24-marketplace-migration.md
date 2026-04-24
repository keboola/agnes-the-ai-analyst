# Marketplace migration implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **User instruction (highest priority, overrides default commit steps):** Do NOT `git commit` during execution. All work is prepared for manual review and commit afterwards. Each task's "commit" step is replaced with a "stage for review" marker (list the changed files).

**Goal:** Migrate marketplace-server (ZIP + git-smart-HTTP plugin distribution) into agnes-the-ai-analyst as a self-contained `app/api/marketplace/` sub-package, backed by agnes's PAT auth.

**Architecture:** Copy-edit marketplace-server's well-factored modules (`packager`, `git_backend`, `git_router`) into a new `app/api/marketplace/` sub-package. Replace the "email-as-credential" PoC auth with agnes's existing PAT validation; keep email-as-credential as an env-gated fallback (`MARKETPLACE_ALLOW_EMAIL_AUTH=1`) during migration. Source marketplace moves from bind-mount to `/data/marketplace/source/` (operator-populated). All three endpoints live under `/api/marketplace/*`.

**Tech Stack:** FastAPI, dulwich (pure-Python git), a2wsgi (ASGI↔WSGI bridge), PyJWT (reuse agnes's existing), pytest, DuckDB (only for reading agnes's users/PAT tables).

**Spec:** `docs/superpowers/specs/2026-04-24-marketplace-migration-design.md`

**Source being migrated:** `C:/ai/agnes/marketplace-server/`

---

## Pre-work

Before starting, verify:

- `C:/ai/agnes/marketplace-server/` exists and is unchanged (this is the source we're porting from).
- `C:/ai/agnes/agnes-the-ai-analyst/` is the agnes repo where work happens.

Throughout the plan, "source file" = the file in `marketplace-server/`, "target file" = the file in `agnes-the-ai-analyst/`.

---

### Task 1: Add dulwich + a2wsgi dependencies

**Files:**
- Modify: `agnes-the-ai-analyst/pyproject.toml`

- [ ] **Step 1: Add two lines to `[project.dependencies]`**

Insert after the existing `httpx>=0.27.0` line (or near other web-stack deps):

```toml
    # Marketplace server (git smart-HTTP + WSGI bridge)
    "dulwich>=0.22",
    "a2wsgi>=1.10",
```

- [ ] **Step 2: Stage for review**

Changed: `pyproject.toml`.

---

### Task 2: Copy config files

**Files:**
- Create: `agnes-the-ai-analyst/config/marketplace/user_groups.json`
- Create: `agnes-the-ai-analyst/config/marketplace/group_plugins.json`

- [ ] **Step 1: Create `config/marketplace/` directory**

- [ ] **Step 2: Copy `marketplace-server/config/user_groups.json` → `config/marketplace/user_groups.json`** (byte-identical)

- [ ] **Step 3: Copy `marketplace-server/config/group_plugins.json` → `config/marketplace/group_plugins.json`** (byte-identical)

- [ ] **Step 4: Stage for review**

Changed: two new files.

---

### Task 3: Create test fixtures

**Files:**
- Create: `agnes-the-ai-analyst/tests/marketplace/__init__.py` (empty)
- Create: `agnes-the-ai-analyst/tests/marketplace/conftest.py`

- [ ] **Step 1: Write `tests/marketplace/__init__.py`** (empty file)

- [ ] **Step 2: Write `tests/marketplace/conftest.py`** (adapted from `marketplace-server/tests/conftest.py`):

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def temp_source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "agnes",
        "metadata": {"version": "1.0.0"},
        "plugins": [
            {"name": "alpha", "version": "0.1.0", "description": "α"},
            {"name": "beta",  "version": "0.1.0", "description": "β"},
            {"name": "gamma", "version": "0.1.0", "description": "γ"},
        ],
    }))
    for name in ("alpha", "beta", "gamma"):
        pdir = root / "plugins" / name / ".claude-plugin"
        pdir.mkdir(parents=True)
        (pdir / "plugin.json").write_text(json.dumps({"name": name, "version": "0.1.0"}))
        (root / "plugins" / name / "README.md").write_text(f"# {name}\n")
    (root / "global-rules").mkdir()
    (root / "global-rules" / "rules.md").write_text("# rules\n")
    return root


@pytest.fixture
def temp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "user_groups.json").write_text(json.dumps({
        "admin@test":   ["grp_admin"],
        "finance@test": ["grp_finance"],
    }))
    (cfg / "group_plugins.json").write_text(json.dumps({
        "grp_admin":   {"plugins": "*"},
        "grp_finance": {"plugins": ["alpha"]},
    }))
    monkeypatch.setattr(
        "app.api.marketplace._packager.USER_GROUPS_PATH",
        cfg / "user_groups.json",
    )
    monkeypatch.setattr(
        "app.api.marketplace._packager.GROUP_PLUGINS_PATH",
        cfg / "group_plugins.json",
    )
    return cfg


@pytest.fixture
def temp_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("MARKETPLACE_CACHE_DIR", str(cache))
    return cache


@pytest.fixture
def configured(temp_source: Path, temp_config: Path, temp_cache: Path,
               monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("MARKETPLACE_SOURCE_PATH", str(temp_source))
    return {"source": temp_source, "config": temp_config, "cache": temp_cache}
```

- [ ] **Step 3: Stage for review**

Changed: two new files.

---

### Task 4: Port `_packager.py` + tests

**Files:**
- Create: `agnes-the-ai-analyst/app/api/marketplace/__init__.py` (placeholder for now)
- Create: `agnes-the-ai-analyst/app/api/marketplace/_packager.py`
- Create: `agnes-the-ai-analyst/tests/marketplace/test_packager.py`

- [ ] **Step 1: Create `app/api/marketplace/__init__.py`** (empty, filled out in Task 9):

```python
```

- [ ] **Step 2: Write `tests/marketplace/test_packager.py`** (ported from `marketplace-server/tests/test_smoke.py` with import path updated):

```python
from app.api.marketplace import _packager as packager


def test_packager_reads_source(configured):
    data = packager.load_source_marketplace()
    names = {p["name"] for p in data["plugins"]}
    assert names == {"alpha", "beta", "gamma"}


def test_groups_resolve(configured):
    groups = packager.load_user_groups("finance@test")
    assert groups == ["grp_finance"]
    allowed = packager.resolve_allowed_plugin_names(groups)
    assert allowed == {"alpha"}
```

- [ ] **Step 3: Run the tests; confirm they fail with `ModuleNotFoundError` for `_packager`.**

```
pytest tests/marketplace/test_packager.py -v
```

Expected: collection error (module not yet created).

- [ ] **Step 4: Port `_packager.py`** from `marketplace-server/app/packager.py`, with two edits:

1. Change path defaults: `SOURCE_MARKETPLACE_PATH` → defaults to `/data/marketplace/source` (falls back through `MARKETPLACE_SOURCE_PATH` env var).
2. Change config lookup: `USER_GROUPS_PATH` and `GROUP_PLUGINS_PATH` point at `config/marketplace/` (via new `MARKETPLACE_USER_GROUPS_PATH` / `MARKETPLACE_GROUP_PLUGINS_PATH` env vars with sane defaults). Keep the monkeypatch-ability (the tests use `monkeypatch.setattr` on the module-level paths).

Full content of `app/api/marketplace/_packager.py`:

```python
"""Marketplace ZIP + metadata builder. Ported from marketplace-server.

Pure functions that read source marketplace files + JSON config to produce:
- per-email plugin info (build_info)
- deterministic filtered ZIP (build_zip)
- content-hash ETag (compute_etag)

Paths are resolved from env vars at call time so tests can monkeypatch them.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DETERMINISTIC_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

# Repo-relative default for `config/marketplace/*.json`, resolved at import time.
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "marketplace"

USER_GROUPS_PATH = Path(
    os.environ.get("MARKETPLACE_USER_GROUPS_PATH")
    or (_DEFAULT_CONFIG_DIR / "user_groups.json")
)
GROUP_PLUGINS_PATH = Path(
    os.environ.get("MARKETPLACE_GROUP_PLUGINS_PATH")
    or (_DEFAULT_CONFIG_DIR / "group_plugins.json")
)

DEFAULT_GROUPS = ["grp_foundryai_everyone"]


def source_path() -> Path:
    return Path(os.environ.get("MARKETPLACE_SOURCE_PATH", "/data/marketplace/source"))


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_user_groups(email: str) -> list[str]:
    data = _read_json(USER_GROUPS_PATH)
    groups = data.get(email)
    if groups is None:
        return list(DEFAULT_GROUPS)
    return list(groups)


def load_group_plugins() -> dict[str, Any]:
    return _read_json(GROUP_PLUGINS_PATH)


def _load_plugin_manifest(name: str) -> dict[str, Any] | None:
    p = source_path() / "plugins" / name / ".claude-plugin" / "plugin.json"
    if not p.is_file():
        print(f"ERROR: plugin {name!r} listed in marketplace.json but {p} is missing")
        return None
    try:
        return _read_json(p)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: cannot read {p}: {e}")
        return None


def load_source_marketplace() -> dict[str, Any]:
    data = _read_json(source_path() / ".claude-plugin" / "marketplace.json")
    for entry in data.get("plugins", []):
        name = entry.get("name")
        if not name:
            continue
        manifest = _load_plugin_manifest(name)
        if not manifest or "version" not in manifest:
            continue
        listed = entry.get("version")
        authoritative = manifest["version"]
        if listed != authoritative:
            print(
                f"WARN: marketplace.json lists {name}={listed!r} but "
                f"plugins/{name}/.claude-plugin/plugin.json says {authoritative!r}; "
                f"serving {authoritative!r}"
            )
            entry["version"] = authoritative
    return data


def resolve_allowed_plugin_names(groups: list[str]) -> set[str]:
    group_plugins = load_group_plugins()
    source = load_source_marketplace()
    source_names = {p["name"] for p in source.get("plugins", [])}

    allowed: set[str] = set()
    for group in groups:
        spec = group_plugins.get(group)
        if spec is None:
            continue
        plugins = spec.get("plugins")
        if plugins == "*":
            allowed |= source_names
        elif isinstance(plugins, list):
            allowed |= set(plugins)

    return allowed & source_names


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _plugin_entries(source: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in source.get("plugins", [])}


def compute_etag(allowed: set[str]) -> str:
    source = load_source_marketplace()
    entries = _plugin_entries(source)
    src = source_path()

    plugin_tokens: list[dict[str, Any]] = []
    for name in sorted(allowed):
        entry = entries[name]
        plugin_dir = src / "plugins" / name
        files: list[list[str]] = []
        if plugin_dir.is_dir():
            for f in _iter_files(plugin_dir):
                rel = f.relative_to(plugin_dir).as_posix()
                files.append([rel, _sha256_file(f)])
        plugin_tokens.append({
            "name": entry.get("name"),
            "version": entry.get("version"),
            "files": files,
        })

    global_rules_tokens: list[list[str]] = []
    rules_dir = src / "global-rules"
    if rules_dir.is_dir():
        for f in _iter_files(rules_dir):
            rel = f.relative_to(rules_dir).as_posix()
            global_rules_tokens.append([rel, _sha256_file(f)])

    canonical = {
        "marketplace_version": source.get("metadata", {}).get("version"),
        "plugins": plugin_tokens,
        "global_rules": global_rules_tokens,
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def filtered_marketplace_json(allowed: set[str]) -> dict[str, Any]:
    source = load_source_marketplace()
    filtered = dict(source)
    filtered["plugins"] = [p for p in source.get("plugins", []) if p["name"] in allowed]
    return filtered


def build_info(email: str) -> dict[str, Any]:
    groups = load_user_groups(email)
    allowed = resolve_allowed_plugin_names(groups)
    source = load_source_marketplace()
    entries = _plugin_entries(source)
    etag = compute_etag(allowed)

    plugins_out = []
    for name in sorted(allowed):
        e = entries[name]
        plugins_out.append({
            "name": e.get("name"),
            "version": e.get("version"),
            "description": e.get("description"),
        })

    return {
        "email": email,
        "groups": groups,
        "marketplace_name": source.get("name"),
        "marketplace_version": source.get("metadata", {}).get("version"),
        "etag": etag,
        "plugins": plugins_out,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _write_zip_entry(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    info = zipfile.ZipInfo(filename=arcname, date_time=DETERMINISTIC_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def build_zip(email: str) -> tuple[bytes, str, dict[str, Any]]:
    info = build_info(email)
    allowed = {p["name"] for p in info["plugins"]}
    src = source_path()

    filtered = filtered_marketplace_json(allowed)

    version_payload = {
        "email": info["email"],
        "groups": info["groups"],
        "plugins": info["plugins"],
        "etag": info["etag"],
        "marketplace_version": info["marketplace_version"],
        "generated_at": info["generated_at"],
    }

    members: list[tuple[str, bytes]] = []
    members.append((
        ".claude-plugin/marketplace.json",
        json.dumps(filtered, indent=2, sort_keys=False).encode("utf-8"),
    ))

    for name in sorted(allowed):
        plugin_dir = src / "plugins" / name
        if not plugin_dir.is_dir():
            continue
        for f in _iter_files(plugin_dir):
            rel = f.relative_to(plugin_dir).as_posix()
            arc = f"plugins/{name}/{rel}"
            members.append((arc, f.read_bytes()))

    rules_dir = src / "global-rules"
    if rules_dir.is_dir():
        for f in _iter_files(rules_dir):
            rel = f.relative_to(rules_dir).as_posix()
            members.append((f"global-rules/{rel}", f.read_bytes()))

    members.append((
        ".agnes/version.json",
        json.dumps(version_payload, indent=2, sort_keys=True).encode("utf-8"),
    ))

    members.sort(key=lambda m: m[0])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc, data in members:
            _write_zip_entry(zf, arc, data)

    return buf.getvalue(), info["etag"], info
```

- [ ] **Step 5: Run tests; confirm they pass.**

```
pytest tests/marketplace/test_packager.py -v
```

Expected: 2 passed.

- [ ] **Step 6: Stage for review**

Changed: three new files (`__init__.py` empty, `_packager.py`, `test_packager.py`).

---

### Task 5: Port `_git_backend.py` + tests

**Files:**
- Create: `agnes-the-ai-analyst/app/api/marketplace/_git_backend.py`
- Create: `agnes-the-ai-analyst/tests/marketplace/test_git_backend.py`

- [ ] **Step 1: Write `tests/marketplace/test_git_backend.py`** (port `marketplace-server/tests/test_git_backend.py`, update imports to `app.api.marketplace._git_backend` / `_packager` and the monkeypatch targets):

```python
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
    admin_path = git_backend.ensure_repo_for_email("admin@test")
    admin_key = admin_path.name.removesuffix(".git")

    again = git_backend.ensure_repo_for_email("admin@test")
    assert again == admin_path
    assert admin_key == git_backend.cache_key_for_email("admin@test")
```

- [ ] **Step 2: Run tests; confirm they fail on import.**

```
pytest tests/marketplace/test_git_backend.py -v
```

Expected: collection error.

- [ ] **Step 3: Port `_git_backend.py`** from `marketplace-server/app/git_backend.py` with two edits:

1. Change imports: `from app import packager` → `from app.api.marketplace import _packager as packager`.
2. Change cache dir env: `GIT_CACHE_DIR` → `MARKETPLACE_CACHE_DIR` with default `/data/marketplace/cache`.

Full content:

```python
"""Per-group bare-repo cache for the git smart-HTTP endpoint. Ported from
marketplace-server.

Materializes a deterministic bare git repo whose commit SHA is a pure function
of the file bytes that the caller would receive. Writes go through a temp dir
+ atomic rename so concurrent requests can't observe a half-written repo.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import uuid
from pathlib import Path

from dulwich.index import commit_tree
from dulwich.objects import Blob, Commit
from dulwich.repo import Repo

from app.api.marketplace import _packager as packager


def cache_dir() -> Path:
    return Path(os.environ.get("MARKETPLACE_CACHE_DIR", "/data/marketplace/cache"))


def cache_key_for_email(email: str) -> str:
    groups = packager.load_user_groups(email)
    allowed = packager.resolve_allowed_plugin_names(groups)
    return packager.compute_etag(allowed)


def _iter_source_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def file_set_for_allowed(allowed: set[str]) -> dict[str, bytes]:
    """Return a dict of arcname -> file bytes matching the zip layout,
    minus the `.agnes/version.json` runtime artifact (which would change
    on every build and defeat deterministic commit hashing).
    """
    source = packager.source_path()
    filtered = packager.filtered_marketplace_json(allowed)

    files: dict[str, bytes] = {}
    files[".claude-plugin/marketplace.json"] = json.dumps(
        filtered, indent=2, sort_keys=False
    ).encode("utf-8")

    for name in sorted(allowed):
        plugin_dir = source / "plugins" / name
        if not plugin_dir.is_dir():
            continue
        for f in _iter_source_files(plugin_dir):
            rel = f.relative_to(plugin_dir).as_posix()
            files[f"plugins/{name}/{rel}"] = f.read_bytes()

    rules_dir = source / "global-rules"
    if rules_dir.is_dir():
        for f in _iter_source_files(rules_dir):
            rel = f.relative_to(rules_dir).as_posix()
            files[f"global-rules/{rel}"] = f.read_bytes()

    return files


FIXED_AUTHOR = b"agnes-marketplace <noreply@agnes.local>"
FIXED_TIMESTAMP = 0
FIXED_TZ = 0
FIXED_MESSAGE = b"agnes marketplace snapshot"
FIXED_ENCODING = b"UTF-8"


def build_bare_repo(allowed: set[str], target_path: Path) -> None:
    """Create a bare git repo at target_path with one deterministic commit."""
    target_path.mkdir(parents=True, exist_ok=False)
    repo = Repo.init_bare(str(target_path))
    try:
        files = file_set_for_allowed(allowed)
        blobs: list[tuple[bytes, bytes, int]] = []
        for path, content in sorted(files.items()):
            blob = Blob.from_string(content)
            repo.object_store.add_object(blob)
            blobs.append((path.encode("utf-8"), blob.id, 0o100644))

        tree_sha = commit_tree(repo.object_store, blobs)

        commit = Commit()
        commit.tree = tree_sha
        commit.parents = []
        commit.author = commit.committer = FIXED_AUTHOR
        commit.author_time = commit.commit_time = FIXED_TIMESTAMP
        commit.author_timezone = commit.commit_timezone = FIXED_TZ
        commit.encoding = FIXED_ENCODING
        commit.message = FIXED_MESSAGE
        repo.object_store.add_object(commit)

        repo.refs[b"refs/heads/main"] = commit.id
        repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    finally:
        repo.close()


def ensure_repo_for_email(email: str) -> Path:
    """Return the path to the bare repo matching this email's RBAC view.
    Atomic rename into place means concurrent requests can't observe a
    half-written repo.
    """
    groups = packager.load_user_groups(email)
    allowed = packager.resolve_allowed_plugin_names(groups)
    key = packager.compute_etag(allowed)

    root = cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{key}.git"
    if target.is_dir():
        return target

    tmp = root / f".tmp-{key}.{uuid.uuid4().hex}.git"
    try:
        build_bare_repo(allowed, tmp)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    try:
        os.rename(str(tmp), str(target))
    except FileExistsError:
        if target.is_dir():
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            raise
    return target


def email_from_basic_auth(auth_header: str | None) -> str | None:
    """Extract the password field from an HTTP Basic header. Username ignored."""
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    encoded = parts[1]
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    _, _, password = decoded.partition(":")
    return password or None


def is_known_email(email: str) -> bool:
    """True iff this email has an entry in user_groups.json."""
    if not email:
        return False
    try:
        data = packager._read_json(packager.USER_GROUPS_PATH)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return email in data
```

- [ ] **Step 4: Run tests; confirm they pass.**

```
pytest tests/marketplace/test_git_backend.py -v
```

Expected: 18 passed.

- [ ] **Step 5: Stage for review**

Changed: two new files.

---

### Task 6: Create `_auth.py` + tests

**Files:**
- Create: `agnes-the-ai-analyst/app/api/marketplace/_auth.py`
- Create: `agnes-the-ai-analyst/tests/marketplace/test_auth.py`

- [ ] **Step 1: Write `tests/marketplace/test_auth.py`** (new tests for the auth resolver):

```python
"""Tests for the marketplace auth resolver.

Covers:
- PAT (JWT) password → email extracted from token payload.
- Email-as-password when MARKETPLACE_ALLOW_EMAIL_AUTH=1.
- Email-as-password rejected when the env flag is unset.
- LOCAL_DEV_MODE bypass returns the dev email.
- Malformed / unknown credentials → None.
"""
from __future__ import annotations

import base64

import pytest

from app.api.marketplace import _auth
from app.auth.jwt import create_access_token


def _basic(password: str) -> str:
    raw = f"x:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


@pytest.fixture
def jwt_env(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    yield


def test_resolve_from_basic_pat(configured, jwt_env):
    token = create_access_token(
        user_id="u1", email="admin@test", role="admin", typ="pat", omit_exp=True
    )
    assert _auth.resolve_email_from_basic(_basic(token)) == "admin@test"


def test_resolve_from_basic_email_fallback_enabled(configured, monkeypatch, jwt_env):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    assert _auth.resolve_email_from_basic(_basic("admin@test")) == "admin@test"


def test_resolve_from_basic_email_fallback_disabled(configured, monkeypatch, jwt_env):
    monkeypatch.delenv("MARKETPLACE_ALLOW_EMAIL_AUTH", raising=False)
    assert _auth.resolve_email_from_basic(_basic("admin@test")) is None


def test_resolve_from_basic_unknown_email_rejected(configured, monkeypatch, jwt_env):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    # Unknown email (not in user_groups.json) → fail-closed for the git path.
    assert _auth.resolve_email_from_basic(_basic("stranger@test")) is None


def test_resolve_from_basic_missing(configured, jwt_env):
    assert _auth.resolve_email_from_basic(None) is None
    assert _auth.resolve_email_from_basic("") is None


def test_resolve_from_basic_garbage_password(configured, monkeypatch, jwt_env):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    assert _auth.resolve_email_from_basic(_basic("not.a.jwt.nor.email")) is None


def test_resolve_from_basic_local_dev_mode(configured, monkeypatch, jwt_env):
    # No credentials at all + LOCAL_DEV_MODE=1 → dev email
    monkeypatch.setenv("LOCAL_DEV_MODE", "1")
    assert _auth.resolve_email_from_basic(None) == "dev@localhost"


def test_resolve_from_basic_invalid_jwt(configured, jwt_env):
    # JWT-shaped but bad signature
    bogus = "eyJ0eXAiOiJKV1QifQ.eyJlbWFpbCI6ImFAYi5jIn0.badsignature"
    assert _auth.resolve_email_from_basic(_basic(bogus)) is None
```

- [ ] **Step 2: Run tests; confirm they fail on import.**

```
pytest tests/marketplace/test_auth.py -v
```

Expected: collection error.

- [ ] **Step 3: Write `app/api/marketplace/_auth.py`**:

```python
"""Credential resolver shared by the marketplace endpoints.

Three inputs get mapped to an email (or None → 401):

1. PAT (JWT) — primary path. For WSGI, the token comes in the HTTP Basic
   password field (`git clone https://x:<PAT>@host/api/marketplace/git`).
   We verify the JWT signature and read `email` from the payload.
2. Email — temporary fallback, gated by MARKETPLACE_ALLOW_EMAIL_AUTH=1.
3. LOCAL_DEV_MODE=1 — no credentials → dev email, matches agnes's
   existing dev bypass in app/auth/dependencies.py.

Note: this module verifies JWT *signature* only for the WSGI git path —
it does not re-check PAT revocation/expiry in the DB. That would require
opening a DuckDB connection from sync WSGI code; deferred as follow-up.
The FastAPI endpoints (info, zip) go through the full Depends(get_current_user)
chain which does hit the DB.
"""
from __future__ import annotations

import os

from app.api.marketplace import _git_backend as git_backend
from app.auth.jwt import verify_token


def _allow_email_auth() -> bool:
    return os.environ.get("MARKETPLACE_ALLOW_EMAIL_AUTH", "").lower() in ("1", "true", "yes")


def _is_local_dev_mode() -> bool:
    return os.environ.get("LOCAL_DEV_MODE", "").lower() in ("1", "true", "yes")


def _local_dev_email() -> str:
    return os.environ.get("LOCAL_DEV_USER_EMAIL", "dev@localhost")


def resolve_email_from_credential(credential: str | None) -> str | None:
    """Given a raw credential (PAT or email), return the caller's email or None.

    Detection:
    - JWT-shaped (two `.`s, parses + verifies) → read email from payload.
    - Email-shaped (`@` present) + fallback enabled + known in user_groups → use.
    - Otherwise → None.
    """
    if not credential:
        return None

    if "@" in credential and "." in credential and credential.count(".") < 2:
        # Looks like an email (not a JWT, which has exactly two `.`s).
        if not _allow_email_auth():
            return None
        if not git_backend.is_known_email(credential):
            return None
        return credential

    # Try as JWT.
    payload = verify_token(credential)
    if payload:
        email = payload.get("email")
        if email:
            return email

    # Edge case: email containing a dot after @ with 2+ dots (e.g. user@sub.co.uk)
    # — JWT verify would have failed above, fall through to email treatment.
    if "@" in credential:
        if not _allow_email_auth():
            return None
        if not git_backend.is_known_email(credential):
            return None
        return credential

    return None


def resolve_email_from_basic(auth_header: str | None) -> str | None:
    """WSGI entrypoint: parse HTTP Basic, resolve password → email.

    LOCAL_DEV_MODE overrides everything and returns the dev email even with
    no credentials, matching agnes's FastAPI dependency behavior.
    """
    if _is_local_dev_mode():
        return _local_dev_email()
    password = git_backend.email_from_basic_auth(auth_header)
    return resolve_email_from_credential(password)
```

- [ ] **Step 4: Run tests; confirm they pass.**

```
pytest tests/marketplace/test_auth.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Stage for review**

Changed: two new files.

---

### Task 7: Create `git.py` WSGI endpoint + tests

**Files:**
- Create: `agnes-the-ai-analyst/app/api/marketplace/git.py`
- Create: `agnes-the-ai-analyst/tests/marketplace/test_git_router.py`

- [ ] **Step 1: Write `tests/marketplace/test_git_router.py`** (port + adapt):

```python
from __future__ import annotations

import base64
from io import BytesIO
from typing import Callable

import pytest

from app.auth.jwt import create_access_token


@pytest.fixture
def jwt_env(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    yield


def _basic(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


class _Capture:
    def __init__(self) -> None:
        self.status: str | None = None
        self.headers: list[tuple[str, str]] = []
        self._written: list[bytes] = []

    def __call__(self, status: str, headers, exc_info=None) -> Callable[[bytes], None]:
        self.status = status
        self.headers = headers
        return self._written.append

    @property
    def body(self) -> bytes:
        return b"".join(self._written)


def _base_environ(path: str, method: str = "GET",
                  auth: str | None = None,
                  query: str = "") -> dict:
    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8000",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.input": BytesIO(b""),
        "wsgi.errors": BytesIO(),
        "wsgi.url_scheme": "http",
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if auth is not None:
        env["HTTP_AUTHORIZATION"] = auth
    return env


def _consume(resp) -> None:
    try:
        for _ in resp:
            pass
    finally:
        close = getattr(resp, "close", None)
        if close is not None:
            close()


def test_git_wsgi_401_without_auth(configured, jwt_env):
    from app.api.marketplace.git import make_git_wsgi_app
    app = make_git_wsgi_app()
    cap = _Capture()
    _consume(app(_base_environ("/info/refs", query="service=git-upload-pack"), cap))
    assert cap.status and cap.status.startswith("401")
    header_names = {h[0].lower() for h in cap.headers}
    assert "www-authenticate" in header_names


def test_git_wsgi_401_with_unknown_email_fallback_disabled(configured, jwt_env):
    from app.api.marketplace.git import make_git_wsgi_app
    app = make_git_wsgi_app()
    cap = _Capture()
    _consume(app(
        _base_environ("/info/refs", query="service=git-upload-pack",
                      auth=_basic("x", "stranger@test")),
        cap,
    ))
    assert cap.status and cap.status.startswith("401")


def test_git_wsgi_200_info_refs_with_email_fallback(configured, jwt_env, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    from app.api.marketplace.git import make_git_wsgi_app
    app = make_git_wsgi_app()
    cap = _Capture()
    _consume(app(
        _base_environ("/info/refs", query="service=git-upload-pack",
                      auth=_basic("x", "admin@test")),
        cap,
    ))
    assert cap.status and cap.status.startswith("200"), cap.status
    body = cap.body
    assert b"# service=git-upload-pack" in body, f"body was: {body[:200]!r}"
    assert b"refs/heads/main" in body


def test_git_wsgi_200_info_refs_with_pat(configured, jwt_env):
    token = create_access_token(
        user_id="u1", email="admin@test", role="admin", typ="pat", omit_exp=True
    )
    from app.api.marketplace.git import make_git_wsgi_app
    app = make_git_wsgi_app()
    cap = _Capture()
    _consume(app(
        _base_environ("/info/refs", query="service=git-upload-pack",
                      auth=_basic("x", token)),
        cap,
    ))
    assert cap.status and cap.status.startswith("200"), cap.status
    assert b"refs/heads/main" in cap.body


def test_git_wsgi_local_dev_mode_bypass(configured, jwt_env, monkeypatch):
    """LOCAL_DEV_MODE + no creds → serves repo under dev email (if known).

    dev@localhost isn't in the fixture's user_groups.json, so it falls back
    to the default group (which maps to no plugins in the fixture).
    The request still succeeds — default-group users get the empty-filter repo.
    """
    monkeypatch.setenv("LOCAL_DEV_MODE", "1")
    from app.api.marketplace.git import make_git_wsgi_app
    app = make_git_wsgi_app()
    cap = _Capture()
    _consume(app(
        _base_environ("/info/refs", query="service=git-upload-pack"),
        cap,
    ))
    assert cap.status and cap.status.startswith("200"), cap.status
```

- [ ] **Step 2: Run tests; confirm they fail on import.**

```
pytest tests/marketplace/test_git_router.py -v
```

- [ ] **Step 3: Write `app/api/marketplace/git.py`** (port + adapt):

```python
"""WSGI app that authenticates, loads the caller's bare repo, and hands
off to dulwich's smart-HTTP implementation.

Ported from marketplace-server/app/git_router.py with the auth path
replaced by `_auth.resolve_email_from_basic` (PAT primary, email fallback
env-gated, LOCAL_DEV_MODE bypass).
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable

from dulwich.repo import Repo
from dulwich.server import DictBackend
from dulwich.web import HTTPGitApplication

from app.api.marketplace import _auth, _git_backend as git_backend

logger = logging.getLogger(__name__)


def make_git_wsgi_app() -> Callable[[dict, Callable], Iterable[bytes]]:
    """Return a WSGI app scoped to the mount point it's installed at.

    Auth → email → cached bare repo → dulwich HTTPGitApplication. The repo
    is closed deterministically after the response body drains (dulwich
    writes via the WSGI write() callable, not by yielding from the iterable).
    """
    def app(environ: dict, start_response: Callable) -> Iterable[bytes]:
        auth = environ.get("HTTP_AUTHORIZATION", "")
        email = _auth.resolve_email_from_basic(auth)
        if not email:
            start_response(
                "401 Unauthorized",
                [
                    ("Content-Type", "text/plain; charset=utf-8"),
                    ("WWW-Authenticate", 'Basic realm="agnes-marketplace"'),
                ],
            )
            return [b"authentication required\n"]

        try:
            repo_path = git_backend.ensure_repo_for_email(email)
            repo = Repo(str(repo_path))
        except Exception:
            logger.exception("Failed to open repo for email %r", email)
            start_response(
                "500 Internal Server Error",
                [("Content-Type", "text/plain; charset=utf-8")],
            )
            return [b"internal server error\n"]

        try:
            backend = DictBackend({"/": repo})
            git_app = HTTPGitApplication(backend)
            inner = git_app(environ, start_response)
        except Exception:
            repo.close()
            logger.exception("dulwich failed for email %r", email)
            start_response(
                "500 Internal Server Error",
                [("Content-Type", "text/plain; charset=utf-8")],
            )
            return [b"internal server error\n"]

        return _CloseOnExhaust(inner, repo)

    return app


class _CloseOnExhaust:
    """Wraps a WSGI response iterable, calling repo.close() when done.

    dulwich writes response bytes through start_response's write() callable,
    so the iterable is typically empty. We still need to close the repo
    after the WSGI server finishes, and forward close() for early disconnect.
    """
    def __init__(self, inner: Iterable[bytes], repo: Repo) -> None:
        self._inner = inner
        self._repo = repo

    def __iter__(self):
        try:
            yield from self._inner
        finally:
            self._repo.close()

    def close(self) -> None:
        try:
            inner_close = getattr(self._inner, "close", None)
            if inner_close is not None:
                inner_close()
        finally:
            self._repo.close()
```

- [ ] **Step 4: Run tests; confirm they pass.**

```
pytest tests/marketplace/test_git_router.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Stage for review**

Changed: two new files.

---

### Task 8: Create `info.py` + `zip.py` endpoints + integration tests

**Files:**
- Create: `agnes-the-ai-analyst/app/api/marketplace/info.py`
- Create: `agnes-the-ai-analyst/app/api/marketplace/zip.py`
- Create: `agnes-the-ai-analyst/tests/marketplace/test_integration.py`

- [ ] **Step 1: Write `app/api/marketplace/info.py`**:

```python
"""GET /api/marketplace/info — JSON describing the caller's allowed plugins."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.api.marketplace import _packager as packager
from app.auth.dependencies import get_optional_user

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


@router.get("/info")
async def marketplace_info(
    email: str | None = Query(None),
    user: dict | None = Depends(get_optional_user),
) -> JSONResponse:
    """Resolve the caller's email (from auth or `?email=` fallback) and return
    info about the plugins they're allowed to download.
    """
    resolved_email = _resolve_email(user, email)
    info = packager.build_info(resolved_email)
    print(f"marketplace.info email={resolved_email} etag={info['etag']} plugins={len(info['plugins'])}")
    return JSONResponse(info)


def _resolve_email(user: dict | None, email_param: str | None) -> str:
    import os
    if user and user.get("email"):
        return user["email"]
    if email_param:
        if os.environ.get("MARKETPLACE_ALLOW_EMAIL_AUTH", "").lower() not in ("1", "true", "yes"):
            raise HTTPException(
                status_code=401,
                detail="email query parameter requires MARKETPLACE_ALLOW_EMAIL_AUTH=1",
            )
        return email_param
    raise HTTPException(status_code=401, detail="authentication required")
```

- [ ] **Step 2: Write `app/api/marketplace/zip.py`**:

```python
"""GET /api/marketplace/zip — filtered marketplace as a deterministic ZIP."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from app.api.marketplace import _packager as packager
from app.api.marketplace.info import _resolve_email
from app.auth.dependencies import get_optional_user

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


@router.get("/zip")
async def marketplace_zip(
    request: Request,
    email: str | None = Query(None),
    user: dict | None = Depends(get_optional_user),
) -> Response:
    resolved_email = _resolve_email(user, email)

    if_none_match = request.headers.get("if-none-match", "").strip().strip('"')
    data, etag, _info = packager.build_zip(resolved_email)

    if if_none_match and if_none_match == etag:
        print(f"marketplace.zip 304 email={resolved_email} etag={etag}")
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})

    headers = {
        "ETag": f'"{etag}"',
        "Content-Disposition": 'attachment; filename="agnes-marketplace.zip"',
    }
    print(f"marketplace.zip 200 email={resolved_email} etag={etag} bytes={len(data)}")
    return Response(content=data, media_type="application/zip", headers=headers)
```

- [ ] **Step 3: Write `tests/marketplace/test_integration.py`**:

```python
"""End-to-end tests via the FastAPI TestClient.

Covers:
- /api/marketplace/info with email fallback and PAT.
- /api/marketplace/zip 200 + 304 flow.
- /api/marketplace/git mount reachable (smart-HTTP info/refs).
- Email fallback rejected without the env flag.
- Equivalence: PAT path and email-fallback path return byte-identical ZIP + same ETag.
"""
from __future__ import annotations

import base64
import os
import pytest


def _basic(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


@pytest.fixture
def test_env(monkeypatch):
    # Ensure JWT secret is consistent across FastAPI + token creation.
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    yield


@pytest.fixture
def client(configured, test_env):
    """Fresh TestClient per test; avoids caching app-level JWT secret."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app())


def test_info_email_fallback(client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    r = client.get("/api/marketplace/info?email=admin@test")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "admin@test"
    assert {p["name"] for p in body["plugins"]} == {"alpha", "beta", "gamma"}


def test_info_email_fallback_disabled(client, monkeypatch):
    monkeypatch.delenv("MARKETPLACE_ALLOW_EMAIL_AUTH", raising=False)
    r = client.get("/api/marketplace/info?email=admin@test")
    assert r.status_code == 401


def test_info_no_auth(client, monkeypatch):
    monkeypatch.delenv("MARKETPLACE_ALLOW_EMAIL_AUTH", raising=False)
    r = client.get("/api/marketplace/info")
    assert r.status_code == 401


def test_zip_email_fallback_200(client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    r = client.get("/api/marketplace/zip?email=admin@test")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    etag = r.headers["etag"].strip('"')
    assert len(etag) == 16


def test_zip_304_conditional(client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    r1 = client.get("/api/marketplace/zip?email=admin@test")
    etag = r1.headers["etag"]
    r2 = client.get(
        "/api/marketplace/zip?email=admin@test",
        headers={"If-None-Match": etag},
    )
    assert r2.status_code == 304


def test_git_info_refs_401_without_auth(client):
    r = client.get("/api/marketplace/git/info/refs?service=git-upload-pack")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_git_info_refs_200_with_email_fallback(client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    r = client.get(
        "/api/marketplace/git/info/refs?service=git-upload-pack",
        headers={"Authorization": _basic("x", "admin@test")},
    )
    assert r.status_code == 200, r.text
    assert b"# service=git-upload-pack" in r.content
    assert b"refs/heads/main" in r.content


def test_pat_and_email_fallback_return_identical_zip(client, configured, monkeypatch):
    """Equivalence: same user via PAT or email fallback → identical bytes + ETag."""
    from app.auth.jwt import create_access_token
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    token = create_access_token(
        user_id="u1", email="admin@test", role="admin", typ="pat", omit_exp=True
    )

    r_email = client.get("/api/marketplace/zip?email=admin@test")
    r_pat = client.get(
        "/api/marketplace/zip",
        headers={"Authorization": f"Bearer {token}"},
    )
    # Both succeed.
    assert r_email.status_code == 200, r_email.text
    assert r_pat.status_code == 200, r_pat.text
    # Same ETag (same filtered set, deterministic zip).
    assert r_email.headers["etag"] == r_pat.headers["etag"]
    # Byte-identical payload.
    assert r_email.content == r_pat.content
```

- [ ] **Step 4: Run tests — they'll fail until Task 9 wires the routers.**

```
pytest tests/marketplace/test_integration.py -v
```

Expected: failures — the TestClient loads the app but the marketplace router isn't registered yet.

- [ ] **Step 5: Stage for review**

Changed: three new files (tests + two endpoint modules).

---

### Task 9: Wire marketplace into `app.main`

**Files:**
- Modify: `agnes-the-ai-analyst/app/api/marketplace/__init__.py`
- Modify: `agnes-the-ai-analyst/app/main.py`

- [ ] **Step 1: Fill in `app/api/marketplace/__init__.py`**:

```python
"""Marketplace distribution endpoints.

Three endpoints plus a WSGI mount:
- GET /api/marketplace/info         — JSON
- GET /api/marketplace/zip          — deterministic ZIP
- /api/marketplace/git/*            — git smart-HTTP (WSGI)

Use `router` for the FastAPI routers, `make_git_wsgi_app()` for the WSGI mount.
"""
from fastapi import APIRouter

from app.api.marketplace.info import router as _info_router
from app.api.marketplace.zip import router as _zip_router
from app.api.marketplace.git import make_git_wsgi_app

router = APIRouter()
router.include_router(_info_router)
router.include_router(_zip_router)

__all__ = ["router", "make_git_wsgi_app"]
```

- [ ] **Step 2: Register router + WSGI mount in `app/main.py`**

Add import (group with the other `from app.api.*` imports, around line 78):

```python
from app.api import marketplace as _marketplace
```

Inside `create_app()`, after the last `app.include_router(...)` call for API routers and before the web router (around line 229):

```python
    app.include_router(_marketplace.router)
    # Git smart-HTTP is WSGI (dulwich) — mount via a2wsgi bridge.
    from a2wsgi import WSGIMiddleware
    app.mount("/api/marketplace/git", WSGIMiddleware(_marketplace.make_git_wsgi_app()))
```

- [ ] **Step 3: Install dulwich + a2wsgi for the running env**

The user is working out of an existing agnes install. If they use uv:

```
uv pip install "dulwich>=0.22" "a2wsgi>=1.10"
```

Or the equivalent in their virtualenv.

- [ ] **Step 4: Run the full marketplace test suite**

```
pytest tests/marketplace/ -v
```

Expected: all tests pass (33 total: 2 packager + 18 git_backend + 8 auth + 5 git_router + 8 integration).

- [ ] **Step 5: Stage for review**

Changed: `app/api/marketplace/__init__.py` (filled in), `app/main.py` (router + mount).

---

### Task 10: Wire env vars into docker-compose

**Files:**
- Modify: `agnes-the-ai-analyst/docker-compose.yml`

- [ ] **Step 1: Add env vars to the `app` service** (under `environment:`):

```yaml
      - MARKETPLACE_SOURCE_PATH=/data/marketplace/source
      - MARKETPLACE_CACHE_DIR=/data/marketplace/cache
      # Temporary: set to 1 during migration to accept legacy email credentials.
      # Remove once all marketplace clients have migrated to PATs.
      - MARKETPLACE_ALLOW_EMAIL_AUTH=${MARKETPLACE_ALLOW_EMAIL_AUTH:-0}
```

- [ ] **Step 2: Stage for review**

Changed: `docker-compose.yml`.

---

### Task 11: Full regression check

- [ ] **Step 1: Run the full agnes test suite.**

```
pytest tests/ -v
```

Expected: all existing tests still pass + 41 new marketplace tests pass. No regressions.

- [ ] **Step 2: Summarize changed files for user review.**

List every file created or modified (grouped by category) so the user can review before committing.

---

## Self-review

**Spec coverage:**
- Module layout (spec §Architecture) → Tasks 3–9.
- PAT primary + email fallback (spec §Authentication) → Task 6 (auth), Task 7 (WSGI), Task 8 (FastAPI).
- Paths + config (spec §Paths) → Tasks 2 (config), 4 (packager reads env), 5 (git backend reads env), 10 (compose env vars).
- URL paths `/api/marketplace/*` (spec §URL paths) → Task 9 (mount points), verified by Task 8 integration tests.
- LOCAL_DEV_MODE (spec §Auth) → Task 6 test, Task 7 test.
- Error handling table (spec §Error handling) → covered by ported tests + new ones (unknown email, no auth, fallback off).
- Tests (spec §Testing) → Tasks 4 (packager), 5 (git_backend), 6 (auth), 7 (git_router), 8 (integration).
- Dependencies (spec §Dependencies) → Task 1.
- Deployment (spec §Deployment) → Task 10.

**Placeholder scan:** no TBDs, no "appropriate error handling", no "similar to Task N"; every code block is complete.

**Type consistency:**
- `resolve_email_from_basic` and `resolve_email_from_credential` names consistent between `_auth.py` and its tests.
- `make_git_wsgi_app` consistent across `git.py`, `__init__.py`, `main.py`, tests.
- `router` importable from `app.api.marketplace` consistent with `main.py` import.

**Scope check:** single focused migration — not multiple subsystems.

**Ambiguity check:** `_resolve_email` helper in `info.py` is imported by `zip.py`; this is a deliberate minimal shared helper and clearly named with an underscore prefix. Env-var names are explicit. Auth detection rule (`@` + low dot count ⇒ email else JWT) is explicit.
