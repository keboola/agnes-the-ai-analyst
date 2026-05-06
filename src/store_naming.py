"""Naming helpers for Store entities.

The marketplace served to Claude Code is flat: skill / agent / plugin names
must be globally unique within a user's view, otherwise Claude Code resolves
the second-loaded entity over the first. To prevent collisions across
different Store owners uploading entities with the same display name, every
Store-derived plugin is suffixed with the owner's sanitized email-local-part
(``-by-<username>``) at upload time.

The username is **snapshotted on the entity row** at upload — it does not
auto-update if the owner's email changes later. Per product spec, emails are
stable in this deployment; we don't refactor on email rename.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")
_DASH_COLLAPSE_RE = re.compile(r"-+")


def sanitize_username(email: str) -> str:
    """Convert an email to a Claude-Code-safe username slug.

    Takes the local-part (everything before the first ``@``), lowercases it,
    replaces every run of non-``[a-z0-9-]`` characters with a single ``-``,
    collapses repeats, and trims leading/trailing dashes.

        sanitize_username("alice_smith@example.com")      -> "alice-smith"
        sanitize_username("john.doe+claude@acme.com")     -> "john-doe-claude"
        sanitize_username("USER@example.com")             -> "user"

    Raises ``ValueError`` if the local-part sanitizes to an empty string —
    callers (the upload endpoint) translate that to a 400.

    Note: this mapping is **many-to-one** — ``alice.smith@x`` and
    ``alice_smith@x`` both yield ``alice-smith``. The Store namespace is
    flat in Claude Code, so two such users uploading entities with the
    same display name would produce identical ``<name>-by-<username>``
    suffixes and collide in the served marketplace + bundle. The upload
    endpoint enforces global uniqueness on the suffixed value via
    ``app.api.store._suffixed_already_taken`` and rejects the second one
    with 409 ``conflict_global_suffix``; the per-owner UNIQUE on
    ``store_entities(owner_user_id, name)`` alone does not catch this.
    """
    local = email.split("@", 1)[0].lower()
    s = _SANITIZE_RE.sub("-", local)
    s = _DASH_COLLAPSE_RE.sub("-", s).strip("-")
    if not s:
        raise ValueError(f"email local-part sanitizes to empty: {email!r}")
    return s


def suffixed_name(original_name: str, username: str) -> str:
    """``<original-name>-by-<username>`` — the display+invocation name baked
    into Store-derived plugin/skill/agent files at upload time.
    """
    return f"{original_name}-by-{username}"


def compute_entity_version(plugin_dir: Path) -> str:
    """Content-addressed version for a Store entity's plugin tree.

    Hashes every regular file under ``plugin_dir`` in sorted-relative-path
    order, including each file's relative path in the digest so that a rename
    counts as a content change. Returns the first 16 hex chars of the SHA-256
    — short enough for human-readable use in plugin.json ``version`` and
    audit messages, long enough to be collision-free in practice.
    """
    h = hashlib.sha256()
    for f in _iter_files(plugin_dir):
        rel = f.relative_to(plugin_dir).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(f.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())
