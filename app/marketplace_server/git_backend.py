"""Build & cache a bare git repo that mirrors the user's filtered plugin set.

Dulwich writes the on-disk repo; FastAPI's `git_router.py` serves it over
smart-HTTP through the WSGI bridge. The cache is keyed by the *content* ETag
(sha256 of the aggregated plugin files), so two users who resolve to the same
plugin set share one bare repo — and, because commit metadata is fixed, that
single commit hash is also stable across rebuilds.

Cache layout (per agnes-the-ai-analyst conventions):
    ${DATA_DIR}/marketplaces/git-cache/<etag>.git/   — bare repo
    ${DATA_DIR}/marketplaces/git-cache/.tmp-*.git/  — in-flight builds, atomically renamed

Stale entries are never pruned in this iteration — a different content ETag
just materializes a new directory next to the old one. First iteration of
prune logic is deferred; see plan "Out of scope".
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Dict

import duckdb
from dulwich.index import commit_tree
from dulwich.objects import Blob, Commit
from dulwich.repo import Repo

from app.marketplace_server.packager import (
    MARKETPLACE_DESCRIPTION,
    MARKETPLACE_NAME,
    MARKETPLACE_OWNER,
)
from app.utils import get_marketplaces_dir
from src import marketplace_filter

logger = logging.getLogger(__name__)

FIXED_AUTHOR = b"agnes-marketplace <noreply@agnes.local>"
FIXED_TIMESTAMP = 0
FIXED_TZ = 0
FIXED_MESSAGE = b"agnes marketplace snapshot"
FIXED_ENCODING = b"UTF-8"


def cache_dir() -> Path:
    return get_marketplaces_dir() / "git-cache"


def _merged_manifest_bytes(plugins: list[dict], etag: str) -> bytes:
    """Same manifest as the ZIP channel produces — kept inline to avoid
    importing packager internals into the hot path."""
    entries = []
    for plugin in plugins:
        entry = dict(plugin["raw"])
        entry["name"] = plugin["prefixed_name"]
        entry["source"] = f"./plugins/{plugin['prefixed_name']}"
        if plugin.get("version") and "version" not in entry:
            entry["version"] = plugin["version"]
        entries.append(entry)
    manifest = {
        "name": MARKETPLACE_NAME,
        "owner": MARKETPLACE_OWNER,
        "metadata": {
            "description": MARKETPLACE_DESCRIPTION,
            "version": etag,
        },
        "plugins": entries,
    }
    return json.dumps(manifest, indent=2, sort_keys=False).encode("utf-8")


def file_set_for_user(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    *,
    plugins: list[dict] | None = None,
    etag: str | None = None,
) -> Dict[str, bytes]:
    """Files that go into the bare repo tree, in the same layout as the ZIP
    but without `.agnes/version.json` (which contains `generated_at` and
    would force a different commit SHA on every rebuild).

    When *plugins* and *etag* are supplied the expensive
    ``resolve_allowed_plugins`` / ``compute_etag`` round-trip is skipped
    (callers that already resolved them — e.g. ``ensure_repo_for_user`` —
    pass them through to avoid doubling the DB + disk-hash work).
    """
    if plugins is None:
        plugins = marketplace_filter.resolve_allowed_plugins(conn, user)
    if etag is None:
        etag = marketplace_filter.compute_etag(plugins)

    files: Dict[str, bytes] = {}
    files[".claude-plugin/marketplace.json"] = _merged_manifest_bytes(plugins, etag)

    for plugin in plugins:
        plugin_dir: Path = plugin["plugin_dir"]
        if not plugin_dir.is_dir():
            continue
        for f in sorted(p for p in plugin_dir.rglob("*") if p.is_file()):
            rel = f.relative_to(plugin_dir).as_posix()
            arc = f"plugins/{plugin['prefixed_name']}/{rel}"
            files[arc] = f.read_bytes()
    return files


def build_bare_repo(files: Dict[str, bytes], target_path: Path) -> None:
    """Initialize a fresh bare repo at target_path with one deterministic commit.

    `target_path` MUST NOT exist — caller atomically renames a tmp dir into
    place so concurrent workers never observe a half-written repo.
    """
    target_path.mkdir(parents=True, exist_ok=False)
    repo = Repo.init_bare(str(target_path))
    try:
        blobs = []
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


def ensure_repo_for_user(conn: duckdb.DuckDBPyConnection, user: dict) -> Path:
    """Return the on-disk bare repo for this user's RBAC view, building it
    lazily if needed. Safe under concurrent identical-etag requests: each
    builder uses a unique tmp dir and atomic rename; loser deletes its tmp.
    """
    plugins = marketplace_filter.resolve_allowed_plugins(conn, user)
    etag = marketplace_filter.compute_etag(plugins)

    root = cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{etag}.git"
    if target.is_dir():
        return target

    files = file_set_for_user(conn, user, plugins=plugins, etag=etag)
    tmp = root / f".tmp-{etag}.{uuid.uuid4().hex}.git"
    try:
        build_bare_repo(files, tmp)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    try:
        os.rename(str(tmp), str(target))
    except FileExistsError:
        # Another worker won the atomic-rename race. That's fine — discard ours.
        if target.is_dir():
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            raise
    except OSError as e:
        # Windows: rename fails with WinError 183 if target exists. Same outcome.
        if target.is_dir():
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            raise RuntimeError(f"git-cache rename failed: {e}") from None
    return target
