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
import logging
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
        logger.error("plugin %r listed in marketplace.json but %s is missing", name, p)
        return None
    try:
        return _read_json(p)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("cannot read %s: %s", p, e)
        return None


def load_source_marketplace() -> dict[str, Any]:
    # plugin.json is the single source of truth for each plugin's version.
    # A stale listing version in marketplace.json caused Claude Code to see no
    # update available because it cross-checks the installed plugin's own manifest.
    # If a plugin.json is missing or unreadable we log and keep the listing version
    # so one broken plugin does not take the whole marketplace down.
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
            logger.warning(
                "marketplace.json lists %s=%r but plugins/%s/.claude-plugin/plugin.json says %r; serving %r",
                name, listed, name, authoritative, authoritative,
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
