"""Build a single-plugin ZIP that Claude Desktop's **Cowork** upload accepts.

Cowork (``Customize → upload a custom plugin file``) is NOT a Claude Code
marketplace consumer. Its server-side validator is stricter than
``claude plugin validate`` and expects a *single plugin* per zip, with the
plugin at the **zip root** (no wrapper dir, no ``marketplace.json``).

The transforms here are matched against a **known-good reference zip**
(``grpn-v1.15.28.zip``) that uploaded successfully — not the (more
aggressive) strip list first sketched in issue #464. The empirically-working
artifact **keeps all content** (``data/``, ``scripts/``, ``vendor/``,
``global-rules/``, ``CLAUDE.md``, ``settings.json``, agent ``tools:`` …) and
only:

  1. zips the plugin at the root (no ``marketplace.json`` wrapper),
  2. coerces ``plugin.json`` → semver version, required ``author``, no
     ``homepage``,
  3. whitelists ``SKILL.md`` frontmatter to ``name`` / ``description`` /
     ``compatibility`` (drops Claude-Code-only ``argument-hint`` /
     ``user-invocable``),
  4. concatenates the per-directory ``.md`` files **under ``data/``** into
     ``_all.md`` (the docs/Confluence dump explodes the file count — Cowork
     caps a zip at 5000 files; concatenation keeps every byte while collapsing
     thousands of catalog ``.md`` into a handful of searchable ``_all.md``),
  5. renames Next.js route path segments (``[x]``→``dyn-x``, ``(y)``→``grp-y``),
  6. strips ``.DS_Store`` + Agnes-only paths (``.agnes/**``,
     ``marketplace-metadata.json``).

Agent frontmatter and every non-``data`` file pass through untouched — the
reference kept them and uploaded fine.

Reuses ``packager``'s deterministic zip-entry writer so two builds of
unchanged content are byte-identical (ETag-stable).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.marketplace_server.packager import _write_zip_entry
from src import marketplace_filter

# Bump when the transform rules change so cached ETags bust and clients
# re-download the newer-shaped zip.
COWORK_FORMAT_VERSION = "3"

# SKILL.md frontmatter whitelist. Anything else is a Claude-Code-only field
# that gets the plugin rejected (``argument-hint``, ``user-invocable``, …).
# Agent frontmatter is NOT filtered — the reference zip kept ``tools:`` and
# uploaded fine.
_SKILL_FRONTMATTER_KEEP = ("name", "description", "compatibility")

# Top-level dir whose ``.md`` files get concatenated per-directory into
# ``_all.md`` to stay under Cowork's 5000-file cap. Scoped to ``data`` so the
# loadable surface (``skills/``, ``agents/``, ``commands/``) keeps its on-disk
# structure intact.
_CONCAT_ROOT = "data"

# Caps.
_MAX_FILES = 5000
_MAX_DEPTH = 10
_MAX_FILE_BYTES = 512 * 1024 * 1024

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
# A plain YAML scalar is unsafe if it embeds ``: ``/`` #`` or opens with an
# indicator char — then we fall back to a folded block scalar.
_YAML_HAZARD_RE = re.compile(r":\s|\s#")
_YAML_INDICATORS = set("[]{}>|#&*!%@`\"'?-,")


# ─────────────────────────── content transforms ────────────────────────────


def sanitize_description(desc: Any) -> str:
    """Strip the characters Cowork's YAML/sanitizer pipeline rejects.

    Angle brackets (treated as HTML/XML-like) are removed; double quotes
    (plain-scalar quoting hazard) are downgraded to single quotes. Newlines
    collapse to spaces so the value emits on one line.
    """
    if not isinstance(desc, str):
        desc = "" if desc is None else str(desc)
    desc = desc.replace("<", "").replace(">", "").replace('"', "'")
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc


def coerce_plugin_name(name: Any, fallback: str) -> str:
    """Coerce to kebab-case ``[a-z][a-z0-9-]*`` (Cowork plugin-name rule)."""
    candidate = name if isinstance(name, str) and name.strip() else fallback
    s = candidate.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"^[^a-z]+", "", s)  # must start with a letter
    s = s.strip("-")
    return s or "plugin"


def transform_plugin_json(data: dict, *, manifest_name: str, raw: dict) -> dict:
    """Semver version, required author, no homepage, kebab name.

    Returns a NEW dict (does not mutate the input). Preserves every other
    field so the plugin keeps its identity in Cowork.
    """
    out = dict(data) if isinstance(data, dict) else {}

    out["name"] = coerce_plugin_name(out.get("name") or manifest_name, manifest_name)

    version = out.get("version")
    if not (isinstance(version, str) and _SEMVER_RE.match(version)):
        out["version"] = "0.0.1"

    author = out.get("author")
    if isinstance(author, dict) and (author.get("name") or "").strip():
        pass  # already valid
    elif isinstance(author, str) and author.strip():
        out["author"] = {"name": author.strip()}
    else:
        fallback_author = raw.get("author")
        if isinstance(fallback_author, dict) and (fallback_author.get("name") or "").strip():
            out["author"] = fallback_author
        elif isinstance(fallback_author, str) and fallback_author.strip():
            out["author"] = {"name": fallback_author.strip()}
        else:
            out["author"] = {"name": "Unknown"}

    if "description" in out:
        out["description"] = sanitize_description(out.get("description"))

    out.pop("homepage", None)  # internal URLs trip the validator; optional anyway
    return out


def _parse_frontmatter_block(block: str) -> Optional[dict]:
    """Parse a frontmatter YAML block to a dict; tolerate malformed YAML.

    Falls back to a line-based parse (mirrors ``src.store_guardrails._frontmatter``)
    when the block isn't valid YAML — so a stray ``: `` in a plain scalar still
    yields usable keys rather than crashing the build.
    """
    try:
        loaded = yaml.safe_load(block)
        if isinstance(loaded, dict):
            return loaded
    except yaml.YAMLError:
        pass
    out: dict = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out or None


def _emit_description(text: str) -> List[str]:
    """Render a sanitized description as YAML lines.

    Plain one-line scalar (matching the reference's bundled skills) unless the
    text would trip a plain-scalar parse (``: ``, leading indicator char), in
    which case a folded block scalar (``>-``) is used."""
    if not text:
        return ["description: ''"]
    hazardous = bool(_YAML_HAZARD_RE.search(text)) or text[0] in _YAML_INDICATORS
    if hazardous:
        return ["description: >-", f"  {text}"]
    return [f"description: {text}"]


def _emit_frontmatter(fields: List[Tuple[str, Any]], body: str) -> str:
    """Emit a clean frontmatter block + body."""
    lines = ["---"]
    for key, value in fields:
        if value is None:
            continue
        if key == "description":
            lines.extend(_emit_description(sanitize_description(value)))
        elif key == "name":
            lines.append(f"name: {value}")
        else:
            dumped = yaml.safe_dump(
                {key: value}, default_flow_style=True, sort_keys=False
            ).strip()
            lines.append(dumped)
    lines.append("---")
    fm = "\n".join(lines)
    if body.startswith("\n"):
        return fm + body
    return fm + "\n" + body


def filter_skill_frontmatter(text: str, folder_name: str) -> str:
    """Keep name/description/compatibility; folder name wins for name."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        # No frontmatter — synthesize a minimal one so Cowork has name+description.
        return _emit_frontmatter(
            [("name", folder_name), ("description", folder_name)],
            "\n" + text.lstrip("\n"),
        )
    parsed = _parse_frontmatter_block(m.group(1)) or {}
    body = text[m.end():]

    fields: List[Tuple[str, Any]] = [("name", folder_name)]
    if "description" in parsed:
        fields.append(("description", parsed["description"]))
    else:
        fields.append(("description", parsed.get("name") or folder_name))
    if "compatibility" in parsed:
        fields.append(("compatibility", parsed["compatibility"]))
    return _emit_frontmatter(fields, body)


def sanitize_path_segment(seg: str) -> str:
    """Next.js route syntax breaks the validator. ``[X]``→``dyn-X``,
    ``(Y)``→``grp-Y``."""
    seg = re.sub(r"\[([^\]]*)\]", r"dyn-\1", seg)
    seg = re.sub(r"\(([^)]*)\)", r"grp-\1", seg)
    return seg


def _sanitize_arcname(rel_posix: str) -> str:
    return "/".join(sanitize_path_segment(p) for p in rel_posix.split("/"))


# ─────────────────────────── member collection ─────────────────────────────


def _is_stripped(rel_parts: tuple) -> bool:
    """Only ``.DS_Store`` + Agnes-only paths are dropped — the reference zip
    keeps everything else (``data/``, ``scripts/``, ``CLAUDE.md`` …)."""
    if not rel_parts:
        return True
    if any(p == ".DS_Store" for p in rel_parts):
        return True
    if marketplace_filter.is_agnes_only_path(rel_parts):
        return True
    return False


def _transform_file(rel_parts: tuple, raw_bytes: bytes, plugin: dict) -> bytes:
    """Per-file content transform for plugin.json + SKILL.md. Everything else
    (agents, commands, data, root files) passes through untouched."""
    rel = "/".join(rel_parts)

    if rel == ".claude-plugin/plugin.json":
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = {}
        transformed = transform_plugin_json(
            data, manifest_name=plugin["manifest_name"], raw=plugin.get("raw") or {}
        )
        return (json.dumps(transformed, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    if rel_parts[0] == "skills" and rel_parts[-1].lower() == "skill.md":
        folder = rel_parts[-2] if len(rel_parts) >= 2 else plugin["manifest_name"]
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return raw_bytes
        return filter_skill_frontmatter(text, folder).encode("utf-8")

    return raw_bytes


def _synth_plugin_json(plugin: dict) -> bytes:
    """Build a plugin.json from the resolver dict when the source has none."""
    transformed = transform_plugin_json(
        {
            "name": plugin["manifest_name"],
            "version": plugin.get("version") or "",
            "description": (plugin.get("raw") or {}).get("description") or "",
        },
        manifest_name=plugin["manifest_name"],
        raw=plugin.get("raw") or {},
    )
    return json.dumps(transformed, indent=2).encode("utf-8")


def _concat_md_under(
    members: List[Tuple[str, bytes]], root: str
) -> List[Tuple[str, bytes]]:
    """Concatenate per-directory ``.md`` files under ``<root>/`` into
    ``<dir>/_all.md`` — header ``# <dir> — combined docs`` then one
    ``## `<file>` `` section per original file. Matches the reference zip's
    layout. Non-``.md`` files (JSON catalogs) and everything outside ``root``
    pass through unchanged."""
    kept: List[Tuple[str, bytes]] = []
    by_dir: Dict[str, List[Tuple[str, bytes]]] = {}
    for arc, data in members:
        parts = arc.split("/")
        if parts[0] == root and arc.endswith(".md") and len(parts) >= 2:
            by_dir.setdefault("/".join(parts[:-1]), []).append((arc, data))
        else:
            kept.append((arc, data))
    for dir_path, md_files in sorted(by_dir.items()):
        md_files.sort(key=lambda m: m[0])
        dirname = dir_path.split("/")[-1]
        chunks = [f"# {dirname} — combined docs\n".encode("utf-8")]
        for arc, data in md_files:
            fname = arc.split("/")[-1]
            chunks.append(f"\n---\n\n## `{fname}`\n\n".encode("utf-8") + data)
        kept.append((f"{dir_path}/_all.md", b"\n".join(chunks)))
    return kept


def _fallback_file_cap(members: List[Tuple[str, bytes]]) -> List[Tuple[str, bytes]]:
    """Safety net — if still over the 5000-file cap after the ``data/`` concat,
    concatenate ``.md`` in any remaining directory (>1 ``.md``) so the zip
    uploads. Skill/agent/command dirs typically hold a single ``.md`` so are
    left intact."""
    if len(members) <= _MAX_FILES:
        return members
    by_dir: Dict[str, List[Tuple[str, bytes]]] = {}
    kept: List[Tuple[str, bytes]] = []
    for arc, data in members:
        if arc.endswith(".md") and "/" in arc:
            by_dir.setdefault(arc.rsplit("/", 1)[0], []).append((arc, data))
        else:
            kept.append((arc, data))
    for dir_path, md_files in by_dir.items():
        if len(md_files) <= 1:
            kept.extend(md_files)
            continue
        chunks = [f"# {dir_path.split('/')[-1]} — combined docs\n".encode("utf-8")]
        for arc, data in sorted(md_files):
            chunks.append(f"\n---\n\n## `{arc.rsplit('/', 1)[1]}`\n\n".encode("utf-8") + data)
        kept.append((f"{dir_path}/_all.md", b"\n".join(chunks)))
    return kept


def _finalize(members: List[Tuple[str, bytes]]) -> List[Tuple[str, bytes]]:
    members = _concat_md_under(members, _CONCAT_ROOT)
    return _fallback_file_cap(members)


def collect_members(plugin: dict) -> List[Tuple[str, bytes]]:
    """Collect transformed (arcname, bytes) pairs for the single-plugin zip.

    Arcnames are at the zip ROOT (no ``plugins/<prefixed>/`` wrapper) and are
    path-sanitized. Returns unsorted — caller sorts for determinism.
    """
    plugin_dir: Optional[Path] = plugin.get("plugin_dir")
    members: List[Tuple[str, bytes]] = []
    has_plugin_json = False

    # Store-bundle entries (e.g. "flea") have no single on-disk root — their
    # content is composed from several source dirs. Mirror packager's bundle
    # path: merge files (minus each source's .claude-plugin/) + one synth
    # plugin.json, then apply the same Cowork transforms.
    if plugin.get("bundle_dirs"):
        for rel, abs_path in marketplace_filter._bundle_files(plugin["bundle_dirs"]):
            rel_parts = tuple(rel.split("/"))
            if _is_stripped(rel_parts):
                continue
            data = _transform_file(rel_parts, abs_path.read_bytes(), plugin)
            arc = _sanitize_arcname(rel)
            if len(arc.split("/")) > _MAX_DEPTH:
                continue
            members.append((arc, data))
        members.append((".claude-plugin/plugin.json", _synth_plugin_json(plugin)))
        return _finalize(members)

    if plugin_dir is not None and plugin_dir.is_dir():
        for f in sorted(p for p in plugin_dir.rglob("*") if p.is_file()):
            rel_parts = f.relative_to(plugin_dir).parts
            if _is_stripped(rel_parts):
                continue
            try:
                if f.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            raw = f.read_bytes()
            data = _transform_file(rel_parts, raw, plugin)
            arc = _sanitize_arcname("/".join(rel_parts))
            if arc == ".claude-plugin/plugin.json":
                has_plugin_json = True
            # Depth cap — drop pathologically deep paths.
            if len(arc.split("/")) > _MAX_DEPTH:
                continue
            members.append((arc, data))

    if not has_plugin_json:
        members.append((".claude-plugin/plugin.json", _synth_plugin_json(plugin)))

    return _finalize(members)


# ─────────────────────────────── zip + etag ────────────────────────────────


def _etag_from_members(members: List[Tuple[str, bytes]]) -> str:
    """Content-addressed ETag over the TRANSFORMED members that actually ship.
    COWORK_FORMAT_VERSION is folded in so a transform-rule change busts caches.
    """
    h = hashlib.sha256()
    h.update(COWORK_FORMAT_VERSION.encode("utf-8"))
    h.update(b"\x00")
    for arc, data in sorted(members, key=lambda m: m[0]):
        h.update(arc.encode("utf-8"))
        h.update(b"\x00")
        h.update(hashlib.sha256(data).digest())
        h.update(b"\x00")
    return h.hexdigest()[:16]


def compute_cowork_etag(plugin: dict) -> str:
    """ETag for the served Cowork zip (hashes transformed members)."""
    return _etag_from_members(collect_members(plugin))


def build_cowork_zip(plugin: dict) -> Tuple[bytes, str]:
    """Build the deterministic single-plugin Cowork zip. Returns (bytes, etag)."""
    members = collect_members(plugin)
    members.sort(key=lambda m: m[0])
    etag = _etag_from_members(members)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc, data in members:
            _write_zip_entry(zf, arc, data)
    return buf.getvalue(), etag
