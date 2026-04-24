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

    Loads groups/allowed once so the cache key and repo contents come from
    the same config snapshot (no TOCTOU). Atomic rename into place means
    concurrent requests can't observe a half-written repo. Unique per-call
    temp-dir names handle concurrent same-email requests.
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
        # Another worker won the atomic-rename race. Discard ours.
        if target.is_dir():
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            raise
    return target


def email_from_basic_auth(auth_header: str | None) -> str | None:
    """Extract the password field from an HTTP Basic header. Username ignored
    (git CLI typically sends 'x'). Returns None for missing/malformed/non-Basic.
    """
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
