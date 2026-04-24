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
            {"name": "alpha", "version": "0.1.0", "description": "alpha"},
            {"name": "beta",  "version": "0.1.0", "description": "beta"},
            {"name": "gamma", "version": "0.1.0", "description": "gamma"},
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
               monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Full marketplace test env + isolated DATA_DIR so each test sees a
    fresh system.duckdb (prevents cross-test user leakage via the cached
    get_system_db connection).
    """
    monkeypatch.setenv("MARKETPLACE_SOURCE_PATH", str(temp_source))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "notifications").mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    return {
        "source": temp_source,
        "config": temp_config,
        "cache": temp_cache,
        "data_dir": data_dir,
    }


@pytest.fixture
def seeded_admin(configured: dict) -> dict:
    """Seed a single `admin@test` user (id=mkt-admin-1) for tests that need
    a JWT to resolve through the DB-validating auth path. Returns a dict
    with the seeded user's fields.
    """
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        if not repo.get_by_email("admin@test"):
            repo.create(
                id="mkt-admin-1", email="admin@test", name="Admin", role="admin",
            )
        user = repo.get_by_email("admin@test")
    finally:
        conn.close()
    return {"id": user["id"], "email": user["email"], "role": user["role"]}
