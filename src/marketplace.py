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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from app.utils import get_marketplace_cache_dir, get_marketplaces_dir

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

    Two-channel read:

    * ``.claude-plugin/marketplace.json`` (the Claude Code spec) is the
      authoritative source for plugin existence, source spec, and the bare
      Claude Code-shaped metadata.
    * ``.claude-plugin/agnes-metadata.json`` (Agnes-only) supplies cover
      photo, video URL, doc links, and category overrides per plugin. Missing
      file → no enrichment, plugins still cached at the bare shape.

    External URLs referenced from agnes-metadata are fed through the asset
    mirror (`src.marketplace_asset_mirror.sync_assets`) before the DB write
    so the persisted ``cover_photo_url`` / ``doc_links`` already point at the
    final served URL. Mirror failures degrade gracefully — failed external
    URLs surface as plain external links in the served data, never as 404s.
    """
    from src.marketplace_asset_mirror import sync_assets
    from src.marketplace_metadata import (
        collect_all_external_urls,
        read_agnes_metadata,
        resolve_plugin_metadata,
    )
    from src.marketplace_urls import (
        internal_asset_url,
        internal_doc_url,
        mirrored_url,
    )
    from src.repositories.marketplace_plugins import MarketplacePluginsRepository

    try:
        plugins = read_plugins(slug)
    except Exception as e:  # noqa: BLE001
        logger.warning("marketplace %s: plugin read failed: %s", slug, e)
        return 0

    repo_root = get_marketplaces_dir() / slug
    metadata = read_agnes_metadata(repo_root)

    # Resolve per-plugin enrichment + collect every external URL the mirror
    # needs to fetch this round. Internal references skip the mirror.
    resolved_per_plugin: Dict[str, Dict[str, Any]] = {}
    fetch_requests: List[tuple] = []
    for p in plugins:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        resolved = resolve_plugin_metadata(metadata, name)
        resolved_per_plugin[name] = resolved
        # collect_all_external_urls walks plugin + skills + agents so the
        # mirror caches every external URL, not just plugin-level. Inner-
        # level skill/agent detail enrichment then looks up entries in the
        # same manifest at request time.
        for kind, url in collect_all_external_urls(metadata, name):
            fetch_requests.append((name, kind, url))

    # Mirror external URLs (best-effort — see _refresh_asset_mirror docstring
    # for the failure-mode contract). Keyed by ``(plugin_name, url)`` so two
    # plugins referencing the same external URL each get their own served
    # path under their own plugin subdir — RBAC-safe (a user with grant on
    # plugin B never receives a URL pointing under plugin A's tree).
    served_url_for: Dict[Tuple[str, str], Optional[str]] = {}
    mirror_status: Dict[Tuple[str, str], str] = {}
    if fetch_requests:
        cache_dir = get_marketplace_cache_dir() / slug
        try:
            report = sync_assets(cache_dir=cache_dir, requests=fetch_requests)
            for (plugin_name, url), entry in report.entries.items():
                mirror_status[(plugin_name, url)] = entry.status
                if entry.status == "ok" and entry.local:
                    # /mirrored/{key} where key encodes plugin + kind + filename.
                    # The local relpath is already in the right shape.
                    served_url_for[(plugin_name, url)] = mirrored_url(
                        slug, entry.plugin_name, entry.local.split("/", 1)[1],
                    ) if "/" in entry.local else mirrored_url(
                        slug, entry.plugin_name, entry.local,
                    )
                else:
                    # Failed / rejected → fall back to the original URL so the
                    # frontend can still link out (b1).
                    served_url_for[(plugin_name, url)] = url
            logger.info(
                "marketplace %s: mirror summary fetched=%d not_modified=%d "
                "failed=%d rejected=%d removed=%d",
                slug, report.fetched, report.not_modified, report.failed,
                report.rejected, report.removed,
            )
        except Exception as e:  # noqa: BLE001 — never abort the sync
            logger.warning("marketplace %s: asset mirror crashed: %s", slug, e)
            # On total mirror crash, every (plugin, url) pair falls back to
            # the original URL so the strict-drop logic downstream marks it
            # as un-served and removes it from the rendered metadata.
            for plugin_name, _, url in fetch_requests:
                served_url_for.setdefault((plugin_name, url), url)
                mirror_status.setdefault((plugin_name, url), "failed_recent")

    # Compose the enriched plugin dicts and write to DB.
    enriched: List[Dict[str, Any]] = []
    for p in plugins:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        merged = dict(p)
        resolved = resolved_per_plugin.get(name) or {}

        # Direct serialization to avoid mutating the frozen DocLinkRef.
        # External docs that mirroring rejected (e.g. HTML page, oversized,
        # SSRF-blocked) or failed to fetch (404, timeout, never seen before)
        # are DROPPED from the served list entirely. Internal links whose
        # path doesn't exist on disk at sync time are dropped too. This
        # matches the operator contract: any doc_link Agnes can't deliver
        # as a real downloadable PDF / Markdown / plain text is treated as
        # if it weren't in agnes-metadata.json at all.
        serialized_links: List[Dict[str, str]] = []
        for link in resolved.get("doc_links") or []:
            if not hasattr(link, "kind"):
                continue
            if link.kind == "internal":
                local_path = repo_root / link.path
                if not local_path.is_file():
                    logger.info(
                        "marketplace %s plugin=%s: dropping internal doc_link "
                        "%r (file not found in working tree)",
                        slug, name, link.path,
                    )
                    continue
                serialized_links.append({
                    "name": link.name,
                    "url": internal_doc_url(slug, name, link.path),
                })
                continue
            # external — keep ONLY when the mirror succeeded for THIS plugin.
            status = mirror_status.get((name, link.url), "")
            served = served_url_for.get((name, link.url))
            if status != "ok" or not served or served == link.url:
                logger.info(
                    "marketplace %s plugin=%s: dropping external doc_link "
                    "%r (mirror status=%s)",
                    slug, name, link.url, status or "no_attempt",
                )
                continue
            serialized_links.append({
                "name": link.name,
                "url": served,
            })

        # Build the column-shape payload inline — strict-drop semantics
        # need access to mirror status + on-disk existence per reference,
        # which is decided here rather than in a generic translator.
        # Internal covers are dropped when the file doesn't exist on disk;
        # external covers are dropped when mirroring rejected/failed (no
        # successful mirror means the served URL is the original external
        # URL, which we don't trust to render — better to fall through to
        # the gradient placeholder).
        if isinstance(resolved.get("cover_photo_ref"), tuple):
            kind, target = resolved["cover_photo_ref"]
            if kind == "internal":
                local_path = repo_root / target
                if local_path.is_file():
                    merged["cover_photo_url"] = internal_asset_url(
                        slug, name, target,
                    )
                else:
                    logger.info(
                        "marketplace %s plugin=%s: dropping internal "
                        "cover_photo %r (file not found in working tree)",
                        slug, name, target,
                    )
            elif kind == "external":
                status = mirror_status.get((name, target), "")
                served = served_url_for.get((name, target))
                if status == "ok" and served and served != target:
                    merged["cover_photo_url"] = served
                else:
                    logger.info(
                        "marketplace %s plugin=%s: dropping external "
                        "cover_photo %r (mirror status=%s)",
                        slug, name, target, status or "no_attempt",
                    )
        if "video_url" in resolved:
            merged["video_url"] = resolved["video_url"]
        if "category" in resolved:
            # Override marketplace.json category when agnes-metadata supplies one.
            merged["category"] = resolved["category"]
        if serialized_links:
            merged["doc_links"] = serialized_links

        enriched.append(merged)

    conn = _get_conn()
    try:
        return MarketplacePluginsRepository(conn).replace_for_marketplace(slug, enriched)
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

    # Drop cached etags so the next /marketplace.zip request re-hashes against
    # the freshly-synced content rather than waiting for TTL expiry. Late
    # import: keeps src.marketplace decoupled from the FastAPI app surface.
    if synced:
        try:
            from app.marketplace_server import packager as _packager
            _packager.invalidate_etag_cache()
        except ImportError:
            pass

    return {"synced": synced, "errors": errors}


def delete_marketplace_dir(slug: str) -> bool:
    """Remove on-disk working copy + asset-mirror cache for a marketplace.

    Two directories are scoped per marketplace slug:
    * ``${DATA_DIR}/marketplaces/<slug>/``       — git working copy
    * ``${DATA_DIR}/marketplace-cache/<slug>/``  — external-asset mirror

    Removed together so a re-registered slug starts from a clean cache.
    Returns True iff at least one of the directories existed and was removed.
    """
    if not is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    removed = False
    work_path = get_marketplaces_dir() / slug
    if work_path.exists():
        shutil.rmtree(work_path)
        removed = True
    cache_path = get_marketplace_cache_dir() / slug
    if cache_path.exists():
        shutil.rmtree(cache_path, ignore_errors=True)
        removed = True
    return removed
