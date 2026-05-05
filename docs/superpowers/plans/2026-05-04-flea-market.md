# Flea Market (Blesí Trh) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-serve community skill marketplace where any user can submit a `SKILL.md` directly via Agnes UI — skill is immediately available via the marketplace feed, and pushed to a backing GitHub repo in the background.

**Architecture:** Three layers — `src/github_app.py` (GitHub App auth + file API), `src/flea_market.py` (domain logic: slug/version/disk/LLM review), `app/api/flea_market.py` (FastAPI router). A minimal Jinja2 page at `/flea-market` provides the submission form. Background `BackgroundTasks` push to GitHub; `_refresh_plugin_cache` + `invalidate_etag_cache` make the skill visible immediately after disk write. The flea-market plugin lives in its own marketplace repo (registered as a normal Agnes marketplace). An LLM call warns on duplicates; credentials/MCP warnings are issued but never block submission.

**Tech Stack:** FastAPI, Pydantic v2, PyJWT (GitHub App token), Python `requests` or `httpx` for GitHub API, `pathlib` for disk ops, existing `src.marketplace._refresh_plugin_cache` + `app.marketplace_server.packager.invalidate_etag_cache`, Jinja2 for the submission page.

---

### Task 1: GitHub App auth and file-push helper (`src/github_app.py`)

**Files:**
- Create: `src/github_app.py`
- Create: `tests/unit/test_github_app.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_github_app.py`:

```python
"""Tests for src/github_app.py GitHub App helpers."""
import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from src.github_app import GitHubAppConfig, _get_file_sha, push_file


@pytest.fixture
def config():
    return GitHubAppConfig(
        app_id="123",
        private_key_pem="fake-pem",
        installation_id="456",
        repo="org/repo",
    )


def test_get_file_sha_returns_none_on_404(config):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    with patch("src.github_app.requests.get", return_value=mock_resp):
        sha = _get_file_sha("tok", "org/repo", "plugins/x/SKILL.md")
    assert sha is None


def test_get_file_sha_returns_sha_on_200(config):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"sha": "abc123"}
    with patch("src.github_app.requests.get", return_value=mock_resp):
        sha = _get_file_sha("tok", "org/repo", "plugins/x/SKILL.md")
    assert sha == "abc123"


def test_push_file_creates_new_file():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    with (
        patch("src.github_app._get_file_sha", return_value=None),
        patch("src.github_app.requests.put", return_value=mock_resp) as mock_put,
    ):
        push_file("tok", "org/repo", "plugins/x/SKILL.md", "content", "add skill")

    call_body = json.loads(mock_put.call_args[1]["data"])
    assert call_body["message"] == "add skill"
    assert call_body["content"] == base64.b64encode(b"content").decode()
    assert "sha" not in call_body


def test_push_file_updates_existing_file():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    with (
        patch("src.github_app._get_file_sha", return_value="existing-sha"),
        patch("src.github_app.requests.put", return_value=mock_resp) as mock_put,
    ):
        push_file("tok", "org/repo", "plugins/x/SKILL.md", "content", "update skill")

    call_body = json.loads(mock_put.call_args[1]["data"])
    assert call_body["sha"] == "existing-sha"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
python -m pytest tests/unit/test_github_app.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'src.github_app'`

- [ ] **Step 3: Implement `src/github_app.py`**

```python
"""GitHub App authentication and file-push helpers for the flea-market feature."""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"


@dataclass
class GitHubAppConfig:
    app_id: str
    private_key_pem: str
    installation_id: str
    repo: str  # "owner/repo"


def _get_installation_token(config: GitHubAppConfig) -> str:
    """Exchange GitHub App JWT for a short-lived installation access token."""
    import jwt  # PyJWT

    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": config.app_id}
    app_jwt = jwt.encode(payload, config.private_key_pem, algorithm="RS256")

    resp = requests.post(
        f"{_GITHUB_API}/app/installations/{config.installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
        },
    )
    resp.raise_for_status()
    return resp.json()["token"]


def _get_file_sha(token: str, repo: str, path: str) -> Optional[str]:
    """Return the blob SHA of an existing file, or None if it doesn't exist."""
    resp = requests.get(
        f"{_GITHUB_API}/repos/{repo}/contents/{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["sha"]


def push_file(
    token: str,
    repo: str,
    path: str,
    content: str,
    message: str,
) -> None:
    """Create or update a file in the repo via the GitHub Contents API."""
    sha = _get_file_sha(token, repo, path)
    body: dict = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
    }
    if sha:
        body["sha"] = sha
    resp = requests.put(
        f"{_GITHUB_API}/repos/{repo}/contents/{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        data=json.dumps(body),
    )
    resp.raise_for_status()


def push_skill_files(
    config: GitHubAppConfig,
    plugin_name: str,
    skill_name: str,
    skill_md: str,
    plugin_json: str,
    marketplace_json: str,
) -> None:
    """Push SKILL.md + updated plugin.json + marketplace.json to GitHub."""
    token = _get_installation_token(config)
    push_file(
        token, config.repo,
        f"plugins/{plugin_name}/skills/{skill_name}/SKILL.md",
        skill_md,
        f"feat: add community skill {skill_name}",
    )
    push_file(
        token, config.repo,
        f"plugins/{plugin_name}/.claude-plugin/plugin.json",
        plugin_json,
        f"chore: bump plugin version for {skill_name}",
    )
    push_file(
        token, config.repo,
        ".claude-plugin/marketplace.json",
        marketplace_json,
        f"chore: bump marketplace version for {skill_name}",
    )
```

- [ ] **Step 4: Create the tests/unit/ directory and `__init__.py` if needed**

```bash
mkdir -p C:/ai/agnes/agnes-the-ai-analyst/tests/unit
touch C:/ai/agnes/agnes-the-ai-analyst/tests/__init__.py
touch C:/ai/agnes/agnes-the-ai-analyst/tests/unit/__init__.py
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
python -m pytest tests/unit/test_github_app.py -v
```

Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
git add src/github_app.py tests/unit/test_github_app.py tests/__init__.py tests/unit/__init__.py
git commit -m "feat: add GitHub App auth and file-push helper"
```

---

### Task 2: Flea market domain logic (`src/flea_market.py`)

**Files:**
- Create: `src/flea_market.py`
- Create: `tests/unit/test_flea_market_core.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_flea_market_core.py`:

```python
"""Tests for src/flea_market.py domain logic."""
import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.flea_market import (
    FleaMarketConfig,
    SkillReview,
    _bump_patch,
    review_skill,
    skill_exists,
    slugify,
    write_skill_and_bump_version,
)


@pytest.fixture
def config(tmp_path):
    plugin_dir = tmp_path / "plugins" / "flea-market"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / "skills").mkdir()
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "flea-market", "version": "1.0.0", "description": "Community skills"})
    )
    mp_dir = tmp_path / ".claude-plugin"
    mp_dir.mkdir()
    (mp_dir / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "flea-market", "version": "1.0.0", "path": "plugins/flea-market"}]})
    )
    return FleaMarketConfig(
        marketplace_slug="flea-market",
        plugin_name="flea-market",
        github_repo="org/repo",
        github_app_id="1",
        github_app_private_key="pem",
        github_app_installation_id="2",
        _root=tmp_path,
    )


def test_slugify_lowercases_and_replaces_spaces():
    assert slugify("My Cool Skill") == "my-cool-skill"


def test_slugify_strips_leading_trailing_hyphens():
    assert slugify("--my-skill--") == "my-skill"


def test_slugify_collapses_multiple_hyphens():
    assert slugify("my--cool--skill") == "my-cool-skill"


def test_bump_patch():
    assert _bump_patch("1.0.0") == "1.0.1"
    assert _bump_patch("2.3.14") == "2.3.15"


def test_skill_exists_false_when_missing(config):
    assert skill_exists(config, "nonexistent") is False


def test_skill_exists_true_when_present(config):
    skill_dir = config._root / "plugins" / "flea-market" / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n# hi")
    assert skill_exists(config, "my-skill") is True


def test_write_skill_creates_files_and_bumps_version(config):
    skill_md, plugin_json, marketplace_json = write_skill_and_bump_version(
        config, "my-skill", "Does X", "# Body"
    )
    skill_path = config._root / "plugins" / "flea-market" / "skills" / "my-skill" / "SKILL.md"
    assert skill_path.exists()
    assert "name: my-skill" in skill_path.read_text()
    pj = json.loads(plugin_json)
    assert pj["version"] == "1.0.1"
    mj = json.loads(marketplace_json)
    assert mj["plugins"][0]["version"] == "1.0.1"


def test_review_skill_flags_duplicate():
    extractor = MagicMock()
    extractor.extract_json.return_value = {
        "is_duplicate": True,
        "duplicate_of": "existing-skill",
        "duplicate_reason": "Same purpose",
        "requires_setup": False,
        "setup_description": None,
    }
    result = review_skill(extractor, "new-skill", "Does X", "# body", [{"name": "existing-skill", "description": "Does X"}])
    assert result.is_duplicate is True
    assert result.duplicate_of == "existing-skill"


def test_review_skill_flags_requires_setup():
    extractor = MagicMock()
    extractor.extract_json.return_value = {
        "is_duplicate": False,
        "duplicate_of": None,
        "duplicate_reason": None,
        "requires_setup": True,
        "setup_description": "Needs MCP server configured",
    }
    result = review_skill(extractor, "mcp-skill", "Uses MCP", "# body", [])
    assert result.requires_setup is True
    assert "MCP" in result.setup_description
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
python -m pytest tests/unit/test_flea_market_core.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'src.flea_market'`

- [ ] **Step 3: Implement `src/flea_market.py`**

```python
"""Domain logic for the flea-market community skill marketplace."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


@dataclass
class FleaMarketConfig:
    marketplace_slug: str
    plugin_name: str
    github_repo: str
    github_app_id: str
    github_app_private_key: str
    github_app_installation_id: str
    # _root is injected by tests; production code resolves from DATA_DIR
    _root: Optional[Path] = field(default=None, repr=False)

    def plugin_root(self) -> Path:
        if self._root is not None:
            return self._root
        import os
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        return data_dir / "marketplaces" / self.marketplace_slug

    def plugin_dir(self) -> Path:
        return self.plugin_root() / "plugins" / self.plugin_name

    def skills_dir(self) -> Path:
        return self.plugin_dir() / "skills"

    def plugin_json_path(self) -> Path:
        return self.plugin_dir() / ".claude-plugin" / "plugin.json"

    def marketplace_json_path(self) -> Path:
        return self.plugin_root() / ".claude-plugin" / "marketplace.json"


@dataclass
class SkillReview:
    is_duplicate: bool
    duplicate_of: Optional[str]
    duplicate_reason: Optional[str]
    requires_setup: bool
    setup_description: Optional[str]


def slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s


def _bump_patch(version: str) -> str:
    parts = version.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def skill_exists(config: FleaMarketConfig, skill_name: str) -> bool:
    return (config.skills_dir() / skill_name / "SKILL.md").exists()


def list_skills(config: FleaMarketConfig) -> List[Dict[str, str]]:
    skills_dir = config.skills_dir()
    if not skills_dir.exists():
        return []
    result = []
    for skill_dir in sorted(skills_dir.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        name = skill_dir.name
        description = ""
        for line in text.splitlines():
            if line.startswith("description:"):
                description = line.split(":", 1)[1].strip()
                break
        result.append({"name": name, "description": description})
    return result


_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "is_duplicate": {"type": "boolean"},
        "duplicate_of": {"type": ["string", "null"]},
        "duplicate_reason": {"type": ["string", "null"]},
        "requires_setup": {"type": "boolean"},
        "setup_description": {"type": ["string", "null"]},
    },
    "required": ["is_duplicate", "duplicate_of", "duplicate_reason", "requires_setup", "setup_description"],
}


def review_skill(
    extractor: Any,
    skill_name: str,
    description: str,
    body: str,
    existing_skills: List[Dict[str, str]],
) -> SkillReview:
    existing_list = "\n".join(
        f"- {s['name']}: {s['description']}" for s in existing_skills
    ) or "(none)"
    prompt = f"""You are reviewing a new community skill submission for Claude Code.

New skill name: {skill_name}
New skill description: {description}
New skill body (first 500 chars):
{body[:500]}

Existing skills in this marketplace:
{existing_list}

Evaluate and return JSON with these fields:
- is_duplicate (bool): true if this skill substantially overlaps an existing one
- duplicate_of (string|null): name of the existing skill it duplicates, if any
- duplicate_reason (string|null): brief reason if duplicate
- requires_setup (bool): true if the skill requires credentials, MCP server installation, or external tools
- setup_description (string|null): what setup is needed, if any
"""
    raw = extractor.extract_json(
        prompt=prompt,
        max_tokens=300,
        json_schema=_REVIEW_SCHEMA,
        schema_name="SkillReview",
    )
    return SkillReview(
        is_duplicate=bool(raw.get("is_duplicate")),
        duplicate_of=raw.get("duplicate_of"),
        duplicate_reason=raw.get("duplicate_reason"),
        requires_setup=bool(raw.get("requires_setup")),
        setup_description=raw.get("setup_description"),
    )


def write_skill_and_bump_version(
    config: FleaMarketConfig,
    skill_name: str,
    description: str,
    body: str,
) -> tuple[str, str, str]:
    """Write SKILL.md to disk and bump version in plugin.json + marketplace.json.

    Returns (skill_md, plugin_json_str, marketplace_json_str) — the content
    strings are used by the background GitHub push.
    """
    skill_md = f"---\nname: {skill_name}\ndescription: {description}\nuser-invocable: true\n---\n\n{body}\n"

    skill_dir = config.skills_dir() / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

    pj_path = config.plugin_json_path()
    pj = json.loads(pj_path.read_text(encoding="utf-8"))
    pj["version"] = _bump_patch(pj["version"])
    plugin_json_str = json.dumps(pj, indent=2)
    pj_path.write_text(plugin_json_str, encoding="utf-8")

    mj_path = config.marketplace_json_path()
    mj = json.loads(mj_path.read_text(encoding="utf-8"))
    for p in mj.get("plugins", []):
        if p.get("name") == config.plugin_name:
            p["version"] = pj["version"]
    marketplace_json_str = json.dumps(mj, indent=2)
    mj_path.write_text(marketplace_json_str, encoding="utf-8")

    return skill_md, plugin_json_str, marketplace_json_str


def refresh_serving(marketplace_slug: str) -> None:
    """Refresh the in-memory plugin cache and invalidate ZIP etag cache."""
    try:
        from src.marketplace import _refresh_plugin_cache
        _refresh_plugin_cache(marketplace_slug)
    except Exception:
        logger.exception("flea_market: plugin cache refresh failed for %s", marketplace_slug)
        return
    try:
        from app.marketplace_server.packager import invalidate_etag_cache
        invalidate_etag_cache()
    except Exception:
        logger.warning("flea_market: etag cache invalidation failed (non-fatal)")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
python -m pytest tests/unit/test_flea_market_core.py -v
```

Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
git add src/flea_market.py tests/unit/test_flea_market_core.py
git commit -m "feat: add flea market domain logic (slugify, write skill, LLM review)"
```

---

### Task 3: FastAPI router (`app/api/flea_market.py`)

**Files:**
- Create: `app/api/flea_market.py`
- Create: `tests/unit/test_flea_market_api.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_flea_market_api.py`:

```python
"""Tests for app/api/flea_market.py endpoints."""
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.flea_market import router


@pytest.fixture
def app():
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.marketplace_slug = "flea-market"
    cfg.plugin_name = "flea-market"
    cfg.github_repo = "org/repo"
    cfg.github_app_id = "1"
    cfg.github_app_private_key = "pem"
    cfg.github_app_installation_id = "2"
    return cfg


def test_get_skills_returns_empty_list(client, mock_config):
    with (
        patch("app.api.flea_market._get_config", return_value=mock_config),
        patch("app.api.flea_market.list_skills", return_value=[]),
    ):
        resp = client.get("/api/flea-market/skills")
    assert resp.status_code == 200
    assert resp.json() == {"skills": []}


def test_submit_skill_success(client, mock_config):
    review = MagicMock(is_duplicate=False, duplicate_of=None, duplicate_reason=None,
                        requires_setup=False, setup_description=None)
    with (
        patch("app.api.flea_market._get_config", return_value=mock_config),
        patch("app.api.flea_market.skill_exists", return_value=False),
        patch("app.api.flea_market.list_skills", return_value=[]),
        patch("app.api.flea_market.review_skill", return_value=review),
        patch("app.api.flea_market.write_skill_and_bump_version", return_value=("md", "pj", "mj")),
        patch("app.api.flea_market.refresh_serving"),
        patch("app.api.flea_market._get_extractor", return_value=MagicMock()),
    ):
        resp = client.post("/api/flea-market/submit", json={
            "name": "my-skill",
            "description": "Does something useful",
            "body": "# Title\nThis is the skill body with enough content here.",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "submitted"
    assert data["skill_name"] == "my-skill"


def test_submit_skill_name_conflict_returns_409(client, mock_config):
    with (
        patch("app.api.flea_market._get_config", return_value=mock_config),
        patch("app.api.flea_market.skill_exists", return_value=True),
    ):
        resp = client.post("/api/flea-market/submit", json={
            "name": "existing-skill",
            "description": "Already there",
            "body": "# Already exists with enough content here.",
        })
    assert resp.status_code == 409


def test_submit_skill_duplicate_returns_warning(client, mock_config):
    review = MagicMock(is_duplicate=True, duplicate_of="other-skill",
                        duplicate_reason="Same purpose", requires_setup=False, setup_description=None)
    with (
        patch("app.api.flea_market._get_config", return_value=mock_config),
        patch("app.api.flea_market.skill_exists", return_value=False),
        patch("app.api.flea_market.list_skills", return_value=[{"name": "other-skill", "description": "Same"}]),
        patch("app.api.flea_market.review_skill", return_value=review),
        patch("app.api.flea_market.write_skill_and_bump_version", return_value=("md", "pj", "mj")),
        patch("app.api.flea_market.refresh_serving"),
        patch("app.api.flea_market._get_extractor", return_value=MagicMock()),
    ):
        resp = client.post("/api/flea-market/submit", json={
            "name": "new-skill",
            "description": "Does something useful",
            "body": "# Title\nThis is the skill body with enough content here.",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "submitted"
    assert data["warning"] is not None
    assert "other-skill" in data["warning"]


def test_submit_skill_with_setup_warning(client, mock_config):
    review = MagicMock(is_duplicate=False, duplicate_of=None, duplicate_reason=None,
                        requires_setup=True, setup_description="Needs MCP server")
    with (
        patch("app.api.flea_market._get_config", return_value=mock_config),
        patch("app.api.flea_market.skill_exists", return_value=False),
        patch("app.api.flea_market.list_skills", return_value=[]),
        patch("app.api.flea_market.review_skill", return_value=review),
        patch("app.api.flea_market.write_skill_and_bump_version", return_value=("md", "pj", "mj")),
        patch("app.api.flea_market.refresh_serving"),
        patch("app.api.flea_market._get_extractor", return_value=MagicMock()),
    ):
        resp = client.post("/api/flea-market/submit", json={
            "name": "mcp-skill",
            "description": "Uses an MCP server for data access",
            "body": "# Title\nThis skill requires an MCP server to be running.",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "submitted"
    assert "setup" in data["warning"].lower() or "MCP" in data["warning"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
python -m pytest tests/unit/test_flea_market_api.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'app.api.flea_market'`

- [ ] **Step 3: Implement `app/api/flea_market.py`**

```python
"""FastAPI router for the flea-market community skill marketplace."""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.flea_market import (
    FleaMarketConfig,
    list_skills,
    refresh_serving,
    review_skill,
    skill_exists,
    slugify,
    write_skill_and_bump_version,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/flea-market", tags=["flea-market"])


class SubmitRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=64)
    description: str = Field(..., min_length=10, max_length=200)
    body: str = Field(..., min_length=20, max_length=20_000)

    @field_validator("name")
    @classmethod
    def name_must_be_valid_slug(cls, v: str) -> str:
        s = slugify(v)
        import re
        from src.flea_market import SKILL_SLUG_RE
        if not SKILL_SLUG_RE.match(s):
            raise ValueError("Name must produce a valid slug (letters, digits, hyphens)")
        return s


class SubmitResponse(BaseModel):
    status: str
    skill_name: str
    warning: Optional[str] = None
    duplicate_reason: Optional[str] = None


def _get_config() -> FleaMarketConfig:
    """Build FleaMarketConfig from instance config. Raises HTTPException 503 when not configured."""
    from app.instance_config import get_value
    cfg = get_value("flea_market", default=None)
    if not cfg:
        raise HTTPException(status_code=503, detail="Flea market is not configured on this instance.")
    import os
    def _resolve(val: str) -> str:
        if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
            return os.environ.get(val[2:-1], "")
        return val or ""
    return FleaMarketConfig(
        marketplace_slug=cfg.get("marketplace_slug", "flea-market"),
        plugin_name=cfg.get("plugin_name", "flea-market"),
        github_repo=cfg.get("github_repo", ""),
        github_app_id=_resolve(cfg.get("github_app_id", "")),
        github_app_private_key=_resolve(cfg.get("github_app_private_key", "")),
        github_app_installation_id=_resolve(cfg.get("github_app_installation_id", "")),
    )


def _get_extractor() -> Any:
    """Return an LLM extractor, or a no-op stub when no LLM is configured."""
    try:
        from connectors.llm import get_extractor
        return get_extractor()
    except Exception:
        return _NoOpExtractor()


class _NoOpExtractor:
    """Stub used when no LLM connector is available — skips duplicate detection."""
    def extract_json(self, **_kwargs):
        return {
            "is_duplicate": False, "duplicate_of": None, "duplicate_reason": None,
            "requires_setup": False, "setup_description": None,
        }


def _push_to_github(
    config: FleaMarketConfig,
    skill_name: str,
    skill_md: str,
    plugin_json: str,
    marketplace_json: str,
) -> None:
    """Background task — push skill files to GitHub. Logs errors; never raises."""
    try:
        from src.github_app import GitHubAppConfig, push_skill_files
        gh_config = GitHubAppConfig(
            app_id=config.github_app_id,
            private_key_pem=config.github_app_private_key,
            installation_id=config.github_app_installation_id,
            repo=config.github_repo,
        )
        push_skill_files(gh_config, config.plugin_name, skill_name, skill_md, plugin_json, marketplace_json)
        logger.info("flea_market: pushed skill %s to GitHub", skill_name)
    except Exception:
        logger.exception("flea_market: GitHub push failed for skill %s (non-fatal)", skill_name)


@router.get("/skills")
def get_skills(config: FleaMarketConfig = None):
    if config is None:
        config = _get_config()
    return {"skills": list_skills(config)}


@router.post("/submit", response_model=SubmitResponse)
def submit_skill(req: SubmitRequest, background_tasks: BackgroundTasks):
    config = _get_config()
    skill_name = req.name  # already slugified by validator

    if skill_exists(config, skill_name):
        raise HTTPException(
            status_code=409,
            detail=f"A skill named '{skill_name}' already exists. Choose a different name.",
        )

    extractor = _get_extractor()
    existing = list_skills(config)
    review = review_skill(extractor, skill_name, req.description, req.body, existing)

    skill_md, plugin_json, marketplace_json = write_skill_and_bump_version(
        config, skill_name, req.description, req.body
    )
    refresh_serving(config.marketplace_slug)

    background_tasks.add_task(
        _push_to_github, config, skill_name, skill_md, plugin_json, marketplace_json
    )

    warning: Optional[str] = None
    if review.is_duplicate:
        warning = (
            f"This skill may overlap with '{review.duplicate_of}': {review.duplicate_reason}. "
            "It was submitted anyway — consider merging with the existing skill."
        )
    elif review.requires_setup:
        warning = (
            f"This skill requires additional setup: {review.setup_description}. "
            "Make sure users know what to install before using it."
        )

    return SubmitResponse(
        status="submitted",
        skill_name=skill_name,
        warning=warning,
        duplicate_reason=review.duplicate_reason,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
python -m pytest tests/unit/test_flea_market_api.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
git add app/api/flea_market.py tests/unit/test_flea_market_api.py
git commit -m "feat: add flea market FastAPI router with submit and list endpoints"
```

---

### Task 4: Wire into main.py + web router + Jinja2 template

**Files:**
- Modify: `app/main.py` (add flea_market router import + include_router)
- Modify: `app/web/router.py` (add `/flea-market` GET route)
- Create: `app/web/templates/flea_market.html`
- Modify: `config/instance.yaml.example` (add flea_market section)

- [ ] **Step 1: Add flea market router to `app/main.py`**

In `app/main.py`, add after the `marketplaces_router` import (line ~119):

```python
from app.api.flea_market import router as flea_market_router
```

And add after `app.include_router(marketplaces_router)` (line ~519):

```python
    app.include_router(flea_market_router)
```

- [ ] **Step 2: Add `/flea-market` route to `app/web/router.py`**

Add this route after the existing `/admin/marketplaces` route (around line 902):

```python
@router.get("/flea-market", response_class=HTMLResponse)
async def flea_market_page(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Community skill marketplace — submit and browse shared skills."""
    ctx = _build_context(request, user=user)
    return templates.TemplateResponse(request, "flea_market.html", ctx)
```

- [ ] **Step 3: Create `app/web/templates/flea_market.html`**

This template uses DOM methods for server-provided data (no `innerHTML` with untrusted content):

```html
{% extends "base.html" %}

{% block title %}Community Skills — {{ config.INSTANCE_NAME }}{% endblock %}

{% block content %}
<div class="container" style="max-width:760px;margin:2rem auto;padding:0 1rem">
  <h1>Community Skills</h1>
  <p>Share a skill with your team. Skills are available immediately after submission.
     No pull request required — use responsibly.</p>

  <form id="submit-form" style="background:#f8f9fa;padding:1.5rem;border-radius:8px;margin-bottom:2rem">
    <div style="margin-bottom:1rem">
      <label for="skill-name" style="display:block;font-weight:600;margin-bottom:.25rem">Skill name</label>
      <input id="skill-name" type="text" placeholder="e.g. summarise-jira-ticket"
             style="width:100%;padding:.5rem;border:1px solid #ccc;border-radius:4px;box-sizing:border-box" required>
      <small style="color:#666">Lowercase letters, digits, and hyphens only.</small>
    </div>
    <div style="margin-bottom:1rem">
      <label for="skill-desc" style="display:block;font-weight:600;margin-bottom:.25rem">One-line description</label>
      <input id="skill-desc" type="text" placeholder="What does this skill do?"
             style="width:100%;padding:.5rem;border:1px solid #ccc;border-radius:4px;box-sizing:border-box" required>
    </div>
    <div style="margin-bottom:1rem">
      <label for="skill-body" style="display:block;font-weight:600;margin-bottom:.25rem">Skill body (Markdown)</label>
      <textarea id="skill-body" rows="10" placeholder="# Title&#10;&#10;Describe what Claude should do..."
                style="width:100%;padding:.5rem;border:1px solid #ccc;border-radius:4px;font-family:monospace;box-sizing:border-box" required></textarea>
    </div>
    <button type="submit" style="background:#0d6efd;color:#fff;padding:.5rem 1.5rem;border:none;border-radius:4px;cursor:pointer">
      Submit skill
    </button>
    <div id="result" style="margin-top:1rem"></div>
  </form>

  <h2>Available community skills</h2>
  <div id="skills-list"><em>Loading…</em></div>
</div>

<script>
(function () {
  "use strict";

  function loadSkills() {
    fetch("/api/flea-market/skills")
      .then(function (r) { return r.json(); })
      .then(function (data) { renderSkills(data.skills || []); })
      .catch(function () { renderSkills([]); });
  }

  function renderSkills(skills) {
    var container = document.getElementById("skills-list");
    container.innerHTML = "";
    if (!skills.length) {
      container.textContent = "No community skills yet — be the first!";
      return;
    }
    skills.forEach(function (s) {
      var div = document.createElement("div");
      div.style.cssText = "padding:.6rem 1rem;border:1px solid #dee2e6;border-radius:4px;margin-bottom:.5rem;display:flex;justify-content:space-between;align-items:center";

      var code = document.createElement("code");
      code.textContent = "/flea-market:" + s.name;

      var sep = document.createTextNode(" — ");

      var desc = document.createElement("span");
      desc.style.color = "#666";
      desc.textContent = s.description;

      div.appendChild(code);
      div.appendChild(sep);
      div.appendChild(desc);
      container.appendChild(div);
    });
  }

  document.getElementById("submit-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var resultDiv = document.getElementById("result");
    resultDiv.innerHTML = "";

    var payload = {
      name: document.getElementById("skill-name").value,
      description: document.getElementById("skill-desc").value,
      body: document.getElementById("skill-body").value
    };

    fetch("/api/flea-market/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, status: r.status, data: d }; }); })
      .then(function (result) {
        if (!result.ok) {
          showMessage(resultDiv, "error", result.data.detail || "Submission failed.");
          return;
        }
        showSuccess(resultDiv, result.data);
        document.getElementById("submit-form").reset();
        loadSkills();
      })
      .catch(function () {
        showMessage(resultDiv, "error", "Network error — please try again.");
      });
  });

  function showMessage(container, type, text) {
    var colors = { error: "#842029", warning: "#664d03", success: "#0f5132" };
    var bgColors = { error: "#f8d7da", warning: "#fff3cd", success: "#d1e7dd" };
    var div = document.createElement("div");
    div.style.cssText = "padding:.75rem 1rem;border-radius:4px;margin-top:.5rem";
    div.style.color = colors[type] || "#000";
    div.style.backgroundColor = bgColors[type] || "#eee";
    div.textContent = text;
    container.appendChild(div);
  }

  function showSuccess(container, data) {
    var successDiv = document.createElement("div");
    successDiv.style.cssText = "padding:.75rem 1rem;border-radius:4px;margin-top:.5rem;background:#d1e7dd;color:#0f5132";

    var bold = document.createElement("b");
    bold.textContent = "Submitted: ";
    var code = document.createElement("code");
    code.textContent = "/flea-market:" + data.skill_name;
    successDiv.appendChild(bold);
    successDiv.appendChild(code);
    successDiv.appendChild(document.createTextNode(" is now available."));
    container.appendChild(successDiv);

    if (data.warning) {
      showMessage(container, "warning", "⚠️ " + data.warning);
    }
  }

  loadSkills();
}());
</script>
{% endblock %}
```

- [ ] **Step 4: Add flea_market section to `config/instance.yaml.example`**

Add after the last existing section:

```yaml
# --- Flea Market (community skill marketplace) ---
# Optional. When configured, users can submit skills via the Agnes web UI.
# Skills are served immediately and synced to the backing GitHub repo in the background.
# flea_market:
#   marketplace_slug: "flea-market"      # Slug of the registered Agnes marketplace
#   plugin_name: "flea-market"           # Plugin name inside that marketplace repo
#   github_repo: "your-org/flea-market"  # GitHub repo to push skills to
#   github_app_id: "${FLEA_MARKET_GITHUB_APP_ID}"
#   github_app_private_key: "${FLEA_MARKET_GITHUB_APP_PRIVATE_KEY}"
#   github_app_installation_id: "${FLEA_MARKET_GITHUB_APP_INSTALLATION_ID}"
```

- [ ] **Step 5: Run existing tests and verify nothing broke**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all previously-passing tests still pass.

- [ ] **Step 6: Commit**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
git add app/main.py app/web/router.py app/web/templates/flea_market.html config/instance.yaml.example
git commit -m "feat: wire flea market into web UI and app routing"
```

---

### Task 5: Expose `refresh_plugin_cache` and add CHANGELOG entry

**Files:**
- Modify: `src/marketplace.py` (add public `refresh_plugin_cache` wrapper)
- Modify: `CHANGELOG.md` (add entry under [Unreleased])

- [ ] **Step 1: Write a test for the public wrapper**

Add to `tests/unit/test_flea_market_core.py`:

```python
def test_refresh_plugin_cache_calls_internal_and_invalidates_etag():
    with (
        patch("src.marketplace._refresh_plugin_cache", return_value=2) as mock_refresh,
        patch("app.marketplace_server.packager.invalidate_etag_cache") as mock_inv,
    ):
        from src.marketplace import refresh_plugin_cache
        count = refresh_plugin_cache("test-slug")
    mock_refresh.assert_called_once_with("test-slug")
    mock_inv.assert_called_once()
    assert count == 2
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
python -m pytest tests/unit/test_flea_market_core.py::test_refresh_plugin_cache_calls_internal_and_invalidates_etag -v 2>&1 | head -20
```

Expected: `ImportError` or `AttributeError` — `refresh_plugin_cache` not yet public.

- [ ] **Step 3: Add public wrapper to `src/marketplace.py`**

After the closing `finally` of `_refresh_plugin_cache` (after line 182), add:

```python

def refresh_plugin_cache(slug: str) -> int:
    """Public wrapper: reload plugins from disk and invalidate the ZIP etag cache.

    Called by the flea-market feature after writing a new skill to disk so the
    change is visible in the next marketplace.zip / git-channel response without
    waiting for a nightly sync.
    """
    count = _refresh_plugin_cache(slug)
    try:
        from app.marketplace_server.packager import invalidate_etag_cache
        invalidate_etag_cache()
    except ImportError:
        pass
    return count
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
python -m pytest tests/unit/test_flea_market_core.py -v
```

Expected: all 10 tests pass.

- [ ] **Step 5: Update CHANGELOG.md**

Add under the topmost `## [Unreleased]` heading:

```markdown
### Added
- **Flea market (Blesí Trh)**: community skill marketplace at `/flea-market`. Any signed-in user can submit a `SKILL.md` via the web UI. Skills are available immediately (no nightly sync wait); files are pushed to a backing GitHub repo in the background via a GitHub App. An LLM review warns on potential duplicates or skills requiring credentials/MCP setup but never blocks submission. Configure under `flea_market:` in `instance.yaml` (disabled by default). See `config/instance.yaml.example` for the schema.
```

- [ ] **Step 6: Run full test suite**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 7: Final commit**

```bash
cd C:/ai/agnes/agnes-the-ai-analyst
git add src/marketplace.py CHANGELOG.md tests/unit/test_flea_market_core.py
git commit -m "feat: expose public refresh_plugin_cache; add flea market CHANGELOG entry"
```

---

## Self-Review

### Spec coverage

| Requirement | Task |
|-------------|------|
| Submit skill via UI (no PR) | Task 3+4 |
| Immediately available after submit | Task 2 (`refresh_serving`) + Task 5 (`refresh_plugin_cache`) |
| Background GitHub push | Task 3 (`_push_to_github` BackgroundTask) |
| LLM duplicate detection | Task 2 (`review_skill`) + Task 3 |
| Warning (not block) for credentials/MCP | Task 2 (`requires_setup`) + Task 3 |
| GitHub App auth (not PAT) | Task 1 (`src/github_app.py`) |
| Config in `instance.yaml` | Task 4 |
| CHANGELOG entry | Task 5 |

### No placeholders — all code is complete.

### Type consistency — `FleaMarketConfig` with `_root` injected by tests used consistently across Tasks 2–3.
