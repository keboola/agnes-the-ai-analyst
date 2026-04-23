"""Nightly sync of marketplace git repos onto the data volume.

Each row in the `marketplace_registry` DuckDB table is cloned (first run)
or fast-forwarded (subsequent runs) into ${DATA_DIR}/marketplaces/<slug>/.
FastAPI reads the working copies via the filesystem — this module has no
HTTP surface.

Callable from:
  - the scheduler (in-process, daily 03:00 UTC) via sync_marketplaces()
  - the admin API (POST /api/marketplaces/{id}/sync) via sync_one()
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

from app.utils import get_marketplaces_dir

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SEC = 300
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_lock = threading.Lock()

PLUGIN_MANIFEST_REL = Path(".claude-plugin") / "marketplace.json"


class MarketplaceNotFound(Exception):
    """Raised when a marketplace id is not present in the registry."""


def is_valid_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug or ""))


def _authenticated_url(repo_url: str, token: str) -> str:
    """Inject a PAT into an HTTPS URL as the x-access-token user.

    Non-HTTPS URLs (file://, ssh://, http://) and empty tokens pass through
    unchanged. Result is only ever used as a git remote — never logged.
    """
    if not token:
        return repo_url
    parts = urlparse(repo_url)
    if parts.scheme != "https" or not parts.hostname:
        return repo_url
    netloc = f"x-access-token:{token}@{parts.hostname}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunparse(
        (parts.scheme, netloc, parts.path, parts.params, parts.query, parts.fragment)
    )


def _redact(s: str, token: str) -> str:
    return s.replace(token, "***") if token and s else s


def _run_git(
    args: List[str], cwd: Optional[Path] = None
) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SEC,
        check=True,
    )


def _sync_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Perform the clone/update for a single marketplace spec.

    Raises RuntimeError on git failure (with token-redacted message).
    Raises ValueError on invalid slug.
    """
    slug = (spec.get("id") or "").strip()
    name = spec.get("name") or slug
    url = (spec.get("url") or "").strip()
    branch = (spec.get("branch") or "").strip() or None
    token_env = (spec.get("token_env") or "").strip()
    token = os.environ.get(token_env, "") if token_env else ""

    if not is_valid_slug(slug):
        raise ValueError(
            f"marketplace id {slug!r} invalid (must match [a-z0-9][a-z0-9_-]{{0,63}})"
        )
    if not url:
        raise ValueError(f"marketplace {slug!r}: url is required")

    target = get_marketplaces_dir() / slug
    auth_url = _authenticated_url(url, token)
    is_git = (target / ".git").is_dir()
    action = "update" if is_git else "clone"

    try:
        if not is_git:
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            clone_args = ["clone", "--depth", "1"]
            if branch:
                clone_args += ["--branch", branch]
            clone_args += [auth_url, str(target)]
            _run_git(clone_args)
        else:
            _run_git(["remote", "set-url", "origin", auth_url], cwd=target)
            ref = branch or "HEAD"
            _run_git(["fetch", "--depth", "1", "origin", ref], cwd=target)
            _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=target)
        sha = _run_git(["rev-parse", "HEAD"], cwd=target).stdout.strip()
    except subprocess.CalledProcessError as e:
        stderr = _redact(e.stderr or "", token).strip()
        raise RuntimeError(f"git {action} failed: {stderr}") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {action} timed out after {GIT_TIMEOUT_SEC}s") from None

    logger.info("marketplace %s %s -> %s", slug, action, sha)
    return {"id": slug, "name": name, "action": action, "commit": sha, "path": str(target)}


def read_plugins(slug: str) -> List[Dict[str, Any]]:
    """Read the plugin list from a cloned marketplace's manifest.

    Returns the `plugins` array from `.claude-plugin/marketplace.json` at
    the root of the working copy. Returns an empty list if the manifest
    is missing, unreadable, or has no plugins. Malformed JSON is logged
    and treated as empty — a broken manifest must not take the sync
    operation down.
    """
    if not is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    manifest = get_marketplaces_dir() / slug / PLUGIN_MANIFEST_REL
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("marketplace %s: unreadable manifest %s: %s", slug, manifest, e)
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, list):
        return []
    return [p for p in plugins if isinstance(p, dict) and p.get("name")]


def _refresh_plugin_cache(slug: str) -> int:
    """Reload plugins from disk into marketplace_plugins. Returns plugin count.

    Failures here are logged but never re-raised: the primary sync result
    (git commit) has already succeeded at this point and must still be
    reported.
    """
    from src.repositories.marketplace_plugins import MarketplacePluginsRepository

    try:
        plugins = read_plugins(slug)
    except Exception as e:  # noqa: BLE001
        logger.warning("marketplace %s: plugin read failed: %s", slug, e)
        return 0

    conn = _get_conn()
    try:
        return MarketplacePluginsRepository(conn).replace_for_marketplace(slug, plugins)
    except Exception as e:  # noqa: BLE001
        logger.warning("marketplace %s: plugin cache write failed: %s", slug, e)
        return 0
    finally:
        conn.close()


def _get_conn():
    """Lazy import to avoid circular deps with src.db at module load."""
    from src.db import get_system_db

    return get_system_db()


def sync_one(marketplace_id: str) -> Dict[str, Any]:
    """Sync a single marketplace by id. Updates registry row with result.

    Raises:
        MarketplaceNotFound: if the id isn't registered.
        RuntimeError: if the git operation failed (token-redacted).
    """
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = _get_conn()
    try:
        repo = MarketplaceRegistryRepository(conn)
        spec = repo.get(marketplace_id)
        if not spec:
            raise MarketplaceNotFound(marketplace_id)

        with _lock:
            try:
                result = _sync_spec(spec)
                repo.update_sync_status(
                    marketplace_id,
                    commit_sha=result["commit"],
                    synced_at=datetime.now(timezone.utc),
                )
                result["plugin_count"] = _refresh_plugin_cache(marketplace_id)
                return result
            except (RuntimeError, ValueError) as e:
                repo.update_sync_status(
                    marketplace_id,
                    synced_at=datetime.now(timezone.utc),
                    error=str(e),
                )
                raise
    finally:
        conn.close()


def sync_marketplaces() -> Dict[str, Any]:
    """Sync every registered marketplace. Empty registry = no-op.

    One failure does not abort the rest; errors are collected per entry.
    """
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = _get_conn()
    try:
        repo = MarketplaceRegistryRepository(conn)
        specs = repo.list_all()
    finally:
        conn.close()

    if not specs:
        logger.info("No marketplaces registered; nothing to sync.")
        return {"synced": [], "errors": []}

    synced: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    with _lock:
        for spec in specs:
            slug = spec.get("id", "")
            try:
                result = _sync_spec(spec)
                # Persist success per entry on its own connection (short-lived).
                conn = _get_conn()
                try:
                    MarketplaceRegistryRepository(conn).update_sync_status(
                        slug,
                        commit_sha=result["commit"],
                        synced_at=datetime.now(timezone.utc),
                    )
                finally:
                    conn.close()
                result["plugin_count"] = _refresh_plugin_cache(slug)
                synced.append(result)
            except (RuntimeError, ValueError) as e:
                err = {"id": slug, "error": str(e)}
                errors.append(err)
                logger.error("marketplace %s sync failed: %s", slug, e)
                conn = _get_conn()
                try:
                    MarketplaceRegistryRepository(conn).update_sync_status(
                        slug,
                        synced_at=datetime.now(timezone.utc),
                        error=str(e),
                    )
                finally:
                    conn.close()

    return {"synced": synced, "errors": errors}


def delete_marketplace_dir(slug: str) -> bool:
    """Remove the on-disk working copy for a marketplace slug. Returns True if removed."""
    if not is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    path = get_marketplaces_dir() / slug
    if path.exists():
        shutil.rmtree(path)
        return True
    return False
