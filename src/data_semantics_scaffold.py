"""Scaffolder for the workspace data-semantics pack (Gap 1 / #469).

An operator hand-authors the analyst workspace ``data/`` knowledge pack — per
data package a ``_brief.md``, an ``_overview.md``, ``tables/<id>.yml`` and
``metrics/<name>.yml``. Much of that is *derivable* from data Agnes already
holds: ``metric_definitions``, ``table_registry`` (+ ``column_metadata`` and
``bq_metadata_cache``), grouped into ``data_packages``. This module emits a
*starter* pack from that data so the operator only layers hand-authored
know-how (join contracts, gotchas, query recipes) on top.

The pack layout — one directory per data package:

    <package_slug>/
      _brief.md            AI-query instructions (prose + SQL + gotchas)
      _overview.md         short overview
      tables/<id>.yml      table spec  (grain, partition, columns)
      metrics/<name>.yml   metric      (list form: sql, grain, synonyms, …)

Field classification (per item):

  GEN    deterministically derived; regenerated each run.
           metric : name, category, type, unit, grain
           table  : id, fqn, partition_by, clustered_by, columns
  DRAFT  seeded from a source as a starting point; a human edit still wins
         (3-way merge via a content hash).
           metric : display_name, description, synonyms, dimensions,
                    required_filters, notes, sql, validation, coordinate*
           table  : display_name, grain, gotchas, columns[].note
  KEEP   anything a human adds to a generated item that we do not derive
         (e.g. ``approx_rows_per_day``, extra prose) — preserved untouched.

Provenance uses the pack's **native ``sync:`` block** rather than a bespoke
side-car (the example files already carry ``sync.{source,last_synced,method}``):

    sync:
      source: metric_definitions
      method: generated            # <- ownership switch
      last_synced: 2026-06-01T...Z
      generated_fields:            # field -> content-hash of what we wrote
        description: 6f1c…

Merge contract:

  item has no ``sync`` / ``method`` != "generated"  -> KEEP whole item (human owns it)
  item ``method`` == "generated":
    field absent                                    -> write derived   (fill)
    field present, hash == last generated           -> write derived   (regenerate)
    field present, hash != last generated           -> keep existing   (human edit wins)
    field present, never tracked by us              -> keep existing   (human-added)
    field was ours, no longer derived, untouched    -> remove          (source gone)

``_brief.md`` / ``_overview.md`` are **seed-if-absent**: written only when
missing, never overwritten (the prose is human-owned the moment it exists).

This module has no ``app.`` dependency so it stays importable and unit-testable
on its own (mirrors ``src/marketplace_metadata_scaffold.py``). Inputs are plain
Python data assembled by the CLI from the repositories; the engine never opens
a database.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import yaml

GENERATOR_TAG = "agnes-data-semantics-scaffold"
SCHEMA_VERSION = 1

# Statuses that mean "the generator owns this field" (its hash is recorded).
_OWNED_STATUSES = frozenset({"generated", "regenerated", "unchanged"})


class ScaffoldError(Exception):
    """Raised for unrecoverable input problems. The CLI turns this into a
    one-line error + exit 1."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def humanize(slug: str) -> str:
    """``"new_customers"`` -> ``"New Customers"``. Only the first letter of each
    token is upper-cased so internal caps survive (``"MRR"`` stays ``"MRR"``)."""
    parts = re.split(r"[-_\s]+", (slug or "").strip())
    return " ".join(p[:1].upper() + p[1:] for p in parts if p)


def _field_hash(value: Any) -> str:
    """Stable short content hash of a JSON-able value (provenance tracking)."""
    canon = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v is not None]
    return [value]


def _clean(value: Any) -> bool:
    """True if a derived value is worth writing (non-empty)."""
    if value is None:
        return False
    if isinstance(value, (str, list, dict)) and len(value) == 0:
        return False
    return True


def _maybe_json(value: Any) -> Any:
    """``validation``/``sql_variants`` round-trip from DuckDB as JSON strings."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


# --------------------------------------------------------------------------- #
# YAML rendering (literal block scalars for multi-line strings, key order kept)
# --------------------------------------------------------------------------- #


class _PackDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.Dumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_PackDumper.add_representer(str, _str_representer)


def _dump_yaml(obj: Any) -> str:
    return yaml.dump(
        obj,
        Dumper=_PackDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )


# --------------------------------------------------------------------------- #
# Derivation — metric_definitions row -> workspace metric item
# --------------------------------------------------------------------------- #


def _derive_metric(row: Dict[str, Any], package: Dict[str, Any]) -> Dict[str, Any]:
    """Build the derived (machine-owned) fields of one ``metrics/<name>.yml``
    item from a ``metric_definitions`` row. Order mirrors the example file."""
    name = (row.get("name") or "").strip()
    # Union of the metric's explicit ``tables`` and its single ``table_name``,
    # de-duplicated + order-preserving. This must match the set the CLI uses to
    # assign a metric to a package (``_metric_tables`` in admin_data_semantics)
    # so a metric's coordinate always lists every table that placed it here.
    tables = _as_list(row.get("tables"))
    tn = row.get("table_name")
    if tn and tn not in tables:
        tables = [*tables, tn]
    slug = package.get("slug") or ""

    out: Dict[str, Any] = {}
    out["name"] = name
    out["data_product"] = package.get("name") or humanize(slug)
    out["display_name"] = (row.get("display_name") or humanize(name)).strip()
    if _clean(row.get("description")):
        out["description"] = row["description"].strip()
    if _clean(row.get("category")):
        out["category"] = row["category"]
    if tables:
        out["coordinate"] = list(tables)
        out["coordinate_fqn"] = [f"{slug}.{t}" if slug else str(t) for t in tables]
    if _clean(row.get("type")):
        out["type"] = row["type"]
    if _clean(row.get("unit")):
        out["unit"] = row["unit"]
    if _clean(row.get("grain")):
        out["grain"] = row["grain"]
    if _clean(row.get("synonyms")):
        out["synonyms"] = list(row["synonyms"])
    if _clean(row.get("filters")):
        out["required_filters"] = list(row["filters"])
    if _clean(row.get("dimensions")):
        out["dimensions"] = list(row["dimensions"])
    if _clean(row.get("notes")):
        out["notes"] = list(row["notes"])
    if _clean(row.get("sql")):
        out["sql"] = row["sql"]
    variants = _maybe_json(row.get("sql_variants"))
    if isinstance(variants, dict):
        for vkey, vsql in variants.items():
            out[f"sql_{vkey}"] = vsql
    validation = _maybe_json(row.get("validation"))
    if _clean(validation):
        out["validation"] = validation
    return out


# --------------------------------------------------------------------------- #
# Derivation — table_registry (+ columns + bq cache) -> workspace table item
# --------------------------------------------------------------------------- #


def _derive_columns(columns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in columns:
        nm = (c.get("column_name") or c.get("name") or "").strip()
        if not nm:
            continue
        item: Dict[str, Any] = {"name": nm}
        typ = c.get("basetype") or c.get("type")
        if _clean(typ):
            item["type"] = typ
        note = c.get("description") or c.get("note")
        if _clean(note):
            item["note"] = note.strip() if isinstance(note, str) else note
        out.append(item)
    return out


def _derive_table(table: Dict[str, Any], package: Dict[str, Any]) -> Dict[str, Any]:
    """Build the derived fields of one ``tables/<id>.yml`` from a
    ``table_registry`` row plus its column_metadata + bq_metadata_cache."""
    bq = table.get("bq_cache") or {}
    columns = _derive_columns(table.get("columns") or [])
    slug = package.get("slug") or ""

    # fqn: explicit BigQuery fqn, else construct from bucket + source_table.
    fqn = table.get("bq_fqn")
    if not _clean(fqn) and _clean(table.get("bucket")) and _clean(table.get("source_table")):
        fqn = f"{table['bucket']}.{table['source_table']}"

    # partition: bq cache is freshest; fall back to the registry columns.
    partition = bq.get("partition_by") or table.get("partition_col") or table.get("partition_by")

    out: Dict[str, Any] = {}
    out["id"] = (table.get("id") or "").strip()
    out["display_name"] = (table.get("name") or humanize(out["id"])).strip()
    out["data_product"] = package.get("name") or humanize(slug)
    if _clean(fqn):
        out["fqn"] = fqn
    if _clean(table.get("grain")):
        out["grain"] = table["grain"]
    if _clean(partition):
        out["partition_by"] = partition
    if _clean(bq.get("clustered_by")):
        out["clustered_by"] = list(bq["clustered_by"])
    if columns:
        out["columns"] = columns
    gotchas = table.get("gotchas")
    if _clean(gotchas):
        out["gotchas"] = gotchas
    return out


# --------------------------------------------------------------------------- #
# sync block + 3-way merge
# --------------------------------------------------------------------------- #


def _build_sync(source: str, hashes: Dict[str, str], generated_at: str) -> Dict[str, Any]:
    return {
        "source": source,
        "method": "generated",
        "last_synced": generated_at,
        "generator": GENERATOR_TAG,
        "schema": SCHEMA_VERSION,
        "generated_fields": {k: hashes[k] for k in sorted(hashes)},
    }


def _merge_item(
    derived: Dict[str, Any],
    existing: Optional[Dict[str, Any]],
    *,
    source: str,
    generated_at: str,
    report: "ScaffoldReport",
    item_key: str,
) -> Optional[Dict[str, Any]]:
    """3-way merge one item (metric or table) using its ``sync`` block.

    Returns the merged item dict. A human-owned item (no ``sync`` or
    ``method`` != "generated") is returned unchanged."""
    if existing is not None:
        sync = existing.get("sync") if isinstance(existing, dict) else None
        method = sync.get("method") if isinstance(sync, dict) else None
        if method != "generated":
            report.actions.append((item_key, "*", "kept-human"))
            return copy.deepcopy(existing)

    prior_sync = (existing or {}).get("sync") if isinstance(existing, dict) else None
    prior_hashes = {}
    if isinstance(prior_sync, dict) and isinstance(prior_sync.get("generated_fields"), dict):
        prior_hashes = {
            k: v for k, v in prior_sync["generated_fields"].items()
            if isinstance(k, str) and isinstance(v, str)
        }

    merged: Dict[str, Any] = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    new_hashes: Dict[str, str] = {}

    for fname, dval in derived.items():
        ev = merged.get(fname)
        ph = prior_hashes.get(fname)
        if ev is None:
            merged[fname] = dval
            status = "generated"
        elif ph is None:
            status = "kept-human"          # human-added field on a generated item
        elif _field_hash(ev) == ph:
            if ev == dval:
                status = "unchanged"
            else:
                merged[fname] = dval
                status = "regenerated"
        else:
            status = "kept-edited"          # human edited since last generation
        report.actions.append((item_key, fname, status))
        if status in _OWNED_STATUSES:
            new_hashes[fname] = _field_hash(merged[fname])

    # Fields we generated last run but no longer derive (source removed):
    # drop if untouched, keep if hand-edited (mirrors the marketplace scaffolder).
    for fname, ph in prior_hashes.items():
        if fname in derived or fname not in merged:
            continue
        if _field_hash(merged[fname]) == ph:
            del merged[fname]
            report.actions.append((item_key, fname, "removed"))
        else:
            report.actions.append((item_key, fname, "kept-edited"))

    merged["sync"] = _build_sync(source, new_hashes, generated_at)
    return merged


# --------------------------------------------------------------------------- #
# _brief.md / _overview.md skeletons (seed-if-absent)
# --------------------------------------------------------------------------- #


def _overview_skeleton(package: Dict[str, Any]) -> str:
    name = package.get("name") or humanize(package.get("slug") or "")
    desc = (package.get("description") or "").strip()
    body = desc or "_TODO: one-paragraph overview of this data package._"
    return f"# {name} — Overview\n\n{body}\n"


def _brief_skeleton(
    package: Dict[str, Any],
    table_items: List[Dict[str, Any]],
    metric_items: List[Dict[str, Any]],
) -> str:
    name = package.get("name") or humanize(package.get("slug") or "")
    desc = (package.get("description") or "").strip()
    lines: List[str] = []
    lines.append(f"# {name} — AI Query Context")
    lines.append("")
    lines.append("## 1. Overview")
    lines.append("")
    lines.append(desc or "_TODO: what this package is for and when to start here._")
    lines.append("")
    lines.append("## 2. AI Query Instructions")
    lines.append("")
    lines.append("_TODO: non-negotiable filter rules, standard joins, and don'ts._")
    lines.append("")
    lines.append("## 3. Tables")
    lines.append("")
    if table_items:
        lines.append("| Table | Grain | Partition |")
        lines.append("|---|---|---|")
        for t in table_items:
            lines.append(
                f"| `{t.get('id', '?')}` | {t.get('grain', '—')} | "
                f"{t.get('partition_by', '—')} |"
            )
    else:
        lines.append("_No registered tables in this package yet._")
    lines.append("")
    lines.append("## 4. Metrics")
    lines.append("")
    if metric_items:
        lines.append("| Metric | Grain | Description |")
        lines.append("|---|---|---|")
        for m in metric_items:
            d = (m.get("description") or "").replace("\n", " ").strip()
            if len(d) > 80:
                d = d[:77] + "…"
            lines.append(f"| `{m.get('name', '?')}` | {m.get('grain', '—')} | {d or '—'} |")
    else:
        lines.append("_No registered metrics in this package yet._")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


@dataclass
class ScaffoldReport:
    packages: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    metrics: List[str] = field(default_factory=list)
    briefs_seeded: List[str] = field(default_factory=list)
    actions: List[Tuple[str, str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def status_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for _key, _field, status in self.actions:
            counts[status] = counts.get(status, 0) + 1
        return counts

    def wrote_changes(self) -> bool:
        return bool(self.briefs_seeded) or any(
            status in ("generated", "regenerated", "removed")
            for _key, _field, status in self.actions
        )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

_METRIC_SOURCE = "metric_definitions"
_TABLE_SOURCE = "table_registry+column_metadata+bq_metadata_cache"


def _existing_metric(existing: Dict[str, Any], relpath: str) -> Optional[Dict[str, Any]]:
    """A metrics file parses to a single-item list; return the item dict."""
    raw = existing.get(relpath)
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return raw[0]
    if isinstance(raw, dict):
        return raw
    return None


def scaffold_pack(
    inputs: Dict[str, Any],
    existing: Optional[Dict[str, Any]] = None,
    *,
    generated_at: Optional[str] = None,
) -> Tuple[Dict[str, str], ScaffoldReport]:
    """Generate / refresh a workspace data-semantics pack.

    ``inputs`` is pre-grouped, plain Python data (assembled by the CLI from the
    repositories)::

        {"packages": [
            {"slug": str, "name": str, "description": str | None,
             "tables":  [<registry row> + "columns":[...] + "bq_cache":{...}],
             "metrics": [<metric_definitions row>, ...]},
            ...]}

    ``existing`` maps each pack relpath to its parsed on-disk content (YAML →
    list/dict, markdown → str); missing files are simply absent.

    Returns ``(files, report)`` where ``files`` maps relpath -> rendered text
    for every file the generator (re)produced. Files it did not touch (e.g. an
    existing ``_brief.md``) are not returned, so the caller leaves them as-is.
    """
    existing = existing or {}
    ts = generated_at or _utcnow_iso()
    report = ScaffoldReport()
    files: Dict[str, str] = {}

    packages = inputs.get("packages")
    if not isinstance(packages, list):
        raise ScaffoldError("inputs['packages'] must be a list")

    for package in packages:
        if not isinstance(package, dict):
            continue
        slug = (package.get("slug") or "").strip()
        if not slug:
            report.warnings.append("package with no slug — skipped")
            continue
        report.packages.append(slug)

        # ---- tables ------------------------------------------------------ #
        table_items: List[Dict[str, Any]] = []
        for table in package.get("tables") or []:
            if not isinstance(table, dict) or not (table.get("id") or "").strip():
                continue
            tid = table["id"].strip()
            relpath = f"{slug}/tables/{tid}.yml"
            derived = _derive_table(table, package)
            existing_item = existing.get(relpath)
            if isinstance(existing_item, list):
                existing_item = existing_item[0] if existing_item else None
            merged = _merge_item(
                derived, existing_item if isinstance(existing_item, dict) else None,
                source=_TABLE_SOURCE, generated_at=ts, report=report, item_key=relpath,
            )
            files[relpath] = _dump_yaml(merged)
            table_items.append(merged)
            report.tables.append(relpath)

        # ---- metrics ----------------------------------------------------- #
        metric_items: List[Dict[str, Any]] = []
        for row in package.get("metrics") or []:
            if not isinstance(row, dict) or not (row.get("name") or "").strip():
                continue
            mname = row["name"].strip()
            relpath = f"{slug}/metrics/{mname}.yml"
            derived = _derive_metric(row, package)
            merged = _merge_item(
                derived, _existing_metric(existing, relpath),
                source=_METRIC_SOURCE, generated_at=ts, report=report, item_key=relpath,
            )
            files[relpath] = _dump_yaml([merged])
            metric_items.append(merged)
            report.metrics.append(relpath)

        # ---- _overview.md / _brief.md (seed-if-absent) ------------------- #
        ov_rel = f"{slug}/_overview.md"
        if ov_rel not in existing:
            files[ov_rel] = _overview_skeleton(package)
            report.briefs_seeded.append(ov_rel)
        br_rel = f"{slug}/_brief.md"
        if br_rel not in existing:
            files[br_rel] = _brief_skeleton(package, table_items, metric_items)
            report.briefs_seeded.append(br_rel)

    return files, report


# --------------------------------------------------------------------------- #
# --check support
# --------------------------------------------------------------------------- #


def _strip_volatile(obj: Any) -> Any:
    """Drop the volatile ``sync.last_synced`` timestamp so a no-op re-run
    compares equal."""
    if isinstance(obj, dict):
        out = {k: _strip_volatile(v) for k, v in obj.items() if k != "last_synced"}
        return out
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def comparable_view(files: Dict[str, str]) -> Dict[str, str]:
    """Canonical view of rendered files for ``--check`` — YAML re-parsed with
    the volatile ``sync.last_synced`` removed; markdown compared verbatim."""
    out: Dict[str, str] = {}
    for relpath, text in files.items():
        if relpath.endswith((".yml", ".yaml")):
            try:
                parsed = yaml.safe_load(text)
            except yaml.YAMLError:
                out[relpath] = text
                continue
            out[relpath] = json.dumps(
                _strip_volatile(parsed), sort_keys=True, ensure_ascii=False, default=str
            )
        else:
            out[relpath] = text
    return out


__all__ = [
    "GENERATOR_TAG",
    "SCHEMA_VERSION",
    "ScaffoldError",
    "ScaffoldReport",
    "humanize",
    "scaffold_pack",
    "comparable_view",
]
