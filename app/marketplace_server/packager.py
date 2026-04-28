"""Build a deterministic ZIP + per-request info for the aggregated marketplace.

The ZIP is the delivery artifact for the non-git channel. Its layout:

    .claude-plugin/marketplace.json   ← merged, prefixed-name manifest
    plugins/<prefixed_name>/...       ← copy of ${DATA_DIR}/marketplaces/<slug>/
                                         plugins/<plugin_name>/...
    .agnes/version.json               ← per-request diagnostics

Determinism requirements:
  - Members sorted by arcname
  - Fixed DOS timestamp (1980-01-01)
  - ZIP_DEFLATED
  - UNIX mode 0o644

Two users with the same allowed plugin set therefore produce byte-identical
ZIPs (modulo `.agnes/version.json`, which carries `generated_at`; this is why
the git channel strips that file — see git_backend).
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import duckdb

from src import marketplace_filter

MARKETPLACE_NAME = "agnes"
MARKETPLACE_OWNER = {"name": "Agnes AI Analyst"}
MARKETPLACE_DESCRIPTION = (
    "Aggregated per-user Claude Code marketplace — served by agnes-the-ai-analyst"
)
DETERMINISTIC_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _merged_manifest(plugins: List[dict], etag: str) -> Dict[str, Any]:
    """Synthesize .claude-plugin/marketplace.json over the filtered plugin set.

    Each entry copies the plugin's cached `raw` manifest, then overrides:
      - `name`   = prefixed_name
      - `source` = "./plugins/<prefixed_name>"  (flat relative path in the ZIP)
    All other fields (version, description, author, homepage, keywords, ...)
    are preserved so Claude Code UI looks the same as if the user pulled from
    the upstream marketplace directly.
    """
    entries: List[dict] = []
    for plugin in plugins:
        entry = dict(plugin["raw"])  # shallow copy — we only override two keys
        entry["name"] = plugin["prefixed_name"]
        entry["source"] = f"./plugins/{plugin['prefixed_name']}"
        # Always honor the cached version on the aggregated manifest — the
        # plugin_dir on disk might have drifted if sync fetched a new commit
        # after marketplace_plugins was written, but this is the authoritative
        # record.
        if plugin.get("version") and "version" not in entry:
            entry["version"] = plugin["version"]
        entries.append(entry)
    return {
        "name": MARKETPLACE_NAME,
        "owner": MARKETPLACE_OWNER,
        "metadata": {
            "description": MARKETPLACE_DESCRIPTION,
            "version": etag,
        },
        "plugins": entries,
    }


def build_info(conn: duckdb.DuckDBPyConnection, user: dict) -> Dict[str, Any]:
    """Return a JSON-serializable summary for diagnostic / admin endpoints.

    Mirrors the PoC's /marketplace/info contract.
    """
    plugins = marketplace_filter.resolve_allowed_plugins(conn, user)
    etag = marketplace_filter.compute_etag(plugins)
    return {
        "user_id": user.get("id"),
        "email": user.get("email"),
        "groups": marketplace_filter.resolve_user_groups(conn, user),
        "marketplace_name": MARKETPLACE_NAME,
        "etag": etag,
        "plugin_count": len(plugins),
        "plugins": [
            {
                "name": p["prefixed_name"],
                "original_name": p["original_name"],
                "marketplace_slug": p["marketplace_slug"],
                "version": p.get("version"),
                "description": p["raw"].get("description"),
            }
            for p in plugins
        ],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _collect_members(plugins: List[dict], etag: str) -> List[Tuple[str, bytes]]:
    """Collect (arcname, bytes) pairs for everything that goes into the ZIP.

    Intentionally returns unsorted — caller sorts for deterministic order.
    """
    members: List[Tuple[str, bytes]] = []
    manifest = _merged_manifest(plugins, etag)
    members.append(
        (
            ".claude-plugin/marketplace.json",
            json.dumps(manifest, indent=2, sort_keys=False).encode("utf-8"),
        )
    )

    for plugin in plugins:
        plugin_dir = plugin["plugin_dir"]
        if not plugin_dir.is_dir():
            continue
        for f in sorted(p for p in plugin_dir.rglob("*") if p.is_file()):
            rel = f.relative_to(plugin_dir).as_posix()
            arc = f"plugins/{plugin['prefixed_name']}/{rel}"
            members.append((arc, f.read_bytes()))

    return members


def _write_zip_entry(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    info = zipfile.ZipInfo(filename=arcname, date_time=DETERMINISTIC_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def compute_etag_for_user(
    conn: duckdb.DuckDBPyConnection, user: dict
) -> str:
    """Resolve plugins and compute the content-addressed ETag without building
    the ZIP.  Used by the router to short-circuit 304 responses before paying
    the cost of file collection + ZIP compression."""
    plugins = marketplace_filter.resolve_allowed_plugins(conn, user)
    return marketplace_filter.compute_etag(plugins)


def build_zip(conn: duckdb.DuckDBPyConnection, user: dict) -> Tuple[bytes, str]:
    """Build the deterministic ZIP for this user. Returns (bytes, etag).

    The `.agnes/version.json` entry carries `generated_at` for diagnostics and
    therefore makes the ZIP non-byte-identical on every request. That's fine
    for the ZIP channel (the ETag gate is computed from content hashes *before*
    that file is added). The git channel uses file_set_for_user() instead,
    which deliberately omits this diagnostic file.
    """
    plugins = marketplace_filter.resolve_allowed_plugins(conn, user)
    etag = marketplace_filter.compute_etag(plugins)

    members = _collect_members(plugins, etag)

    version_payload = {
        "user_id": user.get("id"),
        "email": user.get("email"),
        "groups": marketplace_filter.resolve_user_groups(conn, user),
        "marketplace_name": MARKETPLACE_NAME,
        "etag": etag,
        "plugin_count": len(plugins),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    members.append(
        (
            ".agnes/version.json",
            json.dumps(version_payload, indent=2, sort_keys=True).encode("utf-8"),
        )
    )

    members.sort(key=lambda m: m[0])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc, data in members:
            _write_zip_entry(zf, arc, data)
    return buf.getvalue(), etag
