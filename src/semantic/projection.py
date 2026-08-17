"""Project a validated Ossie document into the flat tables queries actually
read (``metric_definitions``, ``glossary_terms``, ``column_metadata``).

The document itself is stored whole elsewhere (``semantic_models``, Task 3);
this module only derives the flat, query-shaped rows from it — stamped with
the document's own ``(source, source_ref)`` provenance and pruned only within
that scope, so two sources (or two refs of the same source) can never delete
each other's rows.

See ``docs/superpowers/specs/2026-08-13-open-semantic-layer-contract-design.md``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from src.repositories import column_metadata_repo, glossary_repo, metric_repo
from src.semantic.dialect import resolve_expression

logger = logging.getLogger(__name__)

# Vendor name under which Agnes rides its own concepts (glossary entries,
# keywords, constraints — see Task 13) through Ossie's generic
# `custom_extensions` escape hatch. Matches the convention the Keboola
# metastore adapter uses for the same purpose.
_GLOSSARY_VENDOR = "AGNES"


def _agnes_payload(obj: dict) -> dict:
    """The AGNES `custom_extensions` payload of one object, merged.

    Ossie pins `additionalProperties: false` on every object and gives Metric
    no dataset link at all, so a metric's table binding, its dataset's grain
    and the model's constraints have nowhere to live but this escape hatch.
    `data` is a JSON-ENCODED STRING per the schema; an entry that fails to
    parse is skipped rather than raised, like `_glossary_entries`.
    """
    merged: dict = {}
    for ext in obj.get("custom_extensions") or []:
        if ext.get("vendor_name") != _GLOSSARY_VENDOR:
            continue
        try:
            data = json.loads(ext.get("data") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged


def _table_binder():
    """Return ``resolve(table_id) -> view_name | None`` over the registered
    Keboola tables, or ``None`` when nothing is registered.

    The binding path is Keboola-shaped today by construction: the metastore
    adapter is the only one that writes a `dataset` key, and its value is a
    Keboola tableId. Imported lazily and behind this one seam so the core
    projector keeps no import-time dependency on a connector, and so a second
    adapter can be given its own resolver here rather than at every callsite.
    Never raises: an instance with no Keboola tables simply binds nothing.
    """
    try:
        from connectors.keboola.semantic_layer import resolve_table_name, table_lookup_from_registry
        from src.repositories import table_registry_repo

        lookup = table_lookup_from_registry(table_registry_repo().list_by_source("keboola"))
    except Exception:  # pragma: no cover - a registry read failure must not lose metrics
        return None
    if not lookup:
        return None
    return lambda table_id: resolve_table_name(table_id, lookup)


def _keboola_lookups():
    """``(table_lookup, column_lookup)`` for JOIN composition, or ``None`` when
    no Keboola tables are registered. Same seam as :func:`_table_binder`:
    Keboola-specific, imported lazily, and the finished JOIN needs Agnes's own
    registry + column metadata — data the adapter deliberately never had, which
    is why the JOIN is composed here rather than in the document."""
    try:
        from connectors.keboola.semantic_layer import table_lookup_from_registry
        from src.repositories import column_metadata_repo, table_registry_repo

        table_lookup = table_lookup_from_registry(table_registry_repo().list_by_source("keboola"))
    except Exception:  # pragma: no cover - a registry read failure must not lose metrics
        return None
    if not table_lookup:
        return None
    col_repo = column_metadata_repo()
    column_lookup = {
        view: {c["column_name"] for c in col_repo.list_for_table(view)} for view in set(table_lookup.values())
    }
    return table_lookup, column_lookup


def _relationship_lookup_from_model(model: dict) -> dict:
    """``tableId -> [relationship attrs]`` for one model, rebuilt from each
    relationship's ``AGNES`` extension (which carries the raw tableIds, the
    on-clause and the type). Mirrors the legacy
    ``relationship_lookup_by_dataset``: a relationship is filed under BOTH its
    ``from`` and ``to`` tableId, and ``resolve_relationship`` decides which side
    the metric's dataset sits on."""
    lookup: dict = {}
    for rel in model.get("relationships") or []:
        ext = _agnes_payload(rel)
        from_id, to_id = ext.get("from_table"), ext.get("to_table")
        if not (from_id and to_id):
            continue
        attrs = {"from": from_id, "to": to_id, "on": ext.get("on") or "", "type": ext.get("type") or ""}
        lookup.setdefault(from_id, []).append(attrs)
        lookup.setdefault(to_id, []).append(attrs)
    return lookup


def _bind_metric(
    fragment: str, table_id: str, binder, kb_lookups, rel_lookup: dict
) -> Optional[tuple[str, Optional[str], Optional[list]]]:
    """Resolve one metric's SQL against its declared table binding.

    Returns ``(sql, table_name, tables)`` or ``None`` (caller SKIPS). Four
    outcomes, matching the legacy Keboola composer exactly:

    - **No binding declared** (``table_id`` empty) → ``(fragment, None, None)``.
      A plain upstream Ossie metric — e.g. from a git source — kept verbatim.
    - **Simple binding honored** → ``(SELECT … FROM "view" AS t, view, None)``.
    - **Foreign-alias binding resolvable via a relationship** →
      ``(join_sql, primary_view, [primary_view, joined_view])``, composed by the
      legacy ``try_join_composition`` (the one live-verified LEFT-JOIN case).
    - **Binding declared but cannot be honored** (unregistered table, embedded
      ``--`` comment, unresolvable foreign alias) → ``None``. The composer skips
      these; keeping them as bare fragments would make the flat-table cutover
      start surfacing unrunnable metrics.
    """
    if not table_id:
        return fragment, None, None
    if binder is None:
        return None
    from connectors.keboola.semantic_layer import (
        compose_sql,
        has_embedded_sql_comment,
        references_foreign_alias,
        try_join_composition,
    )

    if has_embedded_sql_comment(fragment):
        return None
    if references_foreign_alias(fragment):
        if kb_lookups is None:
            return None
        table_lookup, column_lookup = kb_lookups
        fields, _reason = try_join_composition(fragment, table_id, table_lookup, rel_lookup, column_lookup)
        if fields is None:
            return None
        return fields["sql"], fields["table_name"], fields.get("tables")
    table_name = binder(table_id)
    if not table_name:
        return None
    return compose_sql(fragment, table_name), table_name, None


def _constraints_for(metric_name: str, constraints: list) -> Optional[dict]:
    """The `validation` payload for one metric — the constraints whose
    `metrics[]` names it. Mirrors the legacy importer's `merge_constraints`
    output shape, which `agnes catalog --metrics --show` already renders."""
    rules = [
        {
            "name": c.get("name"),
            "constraint_type": c.get("constraint_type"),
            "rule": c.get("rule"),
            "severity": c.get("severity"),
        }
        for c in constraints
        if isinstance(c, dict) and metric_name in (c.get("metrics") or [])
    ]
    return {"rules": rules} if rules else None


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    """Lowercase, collapse non-alphanumerics to a single underscore, strip
    leading/trailing underscores — glossary terms are natural-language
    phrases, unlike metric/field names which are already slugs."""
    return _NON_ALNUM_RE.sub("_", text.lower()).strip("_")


def _scoped_id(source: str, source_ref: Optional[str], *parts: str) -> str:
    """A stable id unique per (source, source_ref, *parts) — re-projecting the
    same document produces the same ids (upsert, not duplicate); two
    source_refs of the same source never collide even when their models or
    metrics share a name."""
    return "/".join([source, source_ref or "_", *parts])


@dataclass
class ProjectionReport:
    metrics_written: int = 0
    glossary_written: int = 0
    columns_written: int = 0
    metrics_pruned: int = 0
    glossary_pruned: int = 0
    skipped: list[dict] = field(default_factory=list)


def _synonyms_of(ai_context: Any) -> list[str]:
    """`ai_context.synonyms` — only the object form of `ai_context` carries
    them; the schema also allows a bare freeform string, which has none."""
    if isinstance(ai_context, dict):
        synonyms = ai_context.get("synonyms")
        if isinstance(synonyms, list):
            return [s for s in synonyms if isinstance(s, str)]
    return []


def _model_synonyms(model: dict) -> Optional[list[str]]:
    """Model-level and dataset-level `ai_context.synonyms`, combined onto
    every metric of the model.

    Ossie metrics are declared at model scope and may span multiple datasets
    (the spec: "Quantifiable measures spanning datasets") — there is no
    per-dataset metric link to key a narrower cascade off. So a dataset's
    synonym context enriches the whole model's metrics, the same as the
    model's own `ai_context.synonyms` — this is "today's behavior" the
    Keboola importer already has for its (single-dataset-per-metric) shape,
    generalized to Ossie's cross-dataset one.
    """
    combined: list[str] = []
    seen: set[str] = set()
    for syn in _synonyms_of(model.get("ai_context")):
        if syn not in seen:
            seen.add(syn)
            combined.append(syn)
    for dataset in model.get("datasets") or []:
        for syn in _synonyms_of(dataset.get("ai_context")):
            if syn not in seen:
                seen.add(syn)
                combined.append(syn)
    return combined or None


def _glossary_entries(model: dict) -> list[dict]:
    """Glossary terms riding `custom_extensions` under the Agnes vendor name.

    Core Ossie has no glossary object, so a document without this extension
    projects zero glossary rows — correct, not a bug. `data` is a
    JSON-ENCODED STRING per the schema (never a nested mapping); an entry
    that fails to parse is skipped rather than raised, consistent with the
    rest of this module reporting rather than crashing on unusable input.
    """
    entries: list[dict] = []
    for ext in model.get("custom_extensions") or []:
        if ext.get("vendor_name") != _GLOSSARY_VENDOR:
            continue
        raw = ext.get("data")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        glossary = data.get("glossary") if isinstance(data, dict) else None
        if isinstance(glossary, list):
            entries.extend(g for g in glossary if isinstance(g, dict) and g.get("term"))
    return entries


def project_document(
    document_json: dict,
    *,
    source: str,
    source_ref: Optional[str],
    safe_prune: bool = False,
    prune: bool = True,
) -> ProjectionReport:
    """Project one Ossie document, then prune rows this (source, source_ref)
    previously wrote that the document no longer mentions.

    Never a global delete: pruning always reads and deletes within this
    document's own (source, source_ref) scope, so re-projecting a shrunk
    document cannot touch another source's — or another ref's — rows.

    ``safe_prune`` adds a full-wipe guard on top of that scoping: when the
    projection wrote ZERO metrics (resp. glossary terms) while in-scope rows
    already exist, the prune is skipped and logged rather than deleting every
    row. A document that legitimately shrinks to zero is indistinguishable
    from a transient upstream that returned an empty-but-valid document, and
    the second must not wipe an installation's whole metric registry in one
    pass. Off by default (a git source emptying a model is a real delete
    signal); the Keboola sync — whose upstream can 200 with nothing usable —
    opts in. Mirrors the legacy ``_sync_one_source`` valve the cutover retired.

    ``prune`` gates the whole delete-what's-missing pass (metrics, glossary,
    columns), independent of ``safe_prune``. Off when the caller knows this
    call is projecting a PARTIAL merged model list — e.g. one of several
    composed documents failed validation and was dropped before reaching
    here — because pruning against an incomplete list would delete the
    dropped model's own previously-written rows, which upstream never asked
    to have removed. Upserts still run normally; only the prune is skipped.
    Defaults to True, the ordinary complete-document case.
    """
    report = ProjectionReport()

    written_metric_ids: set[str] = set()
    written_glossary_ids: set[str] = set()
    # table_id -> field names written for it by *this* call. Used to prune
    # dropped fields within a table this document still mentions (see
    # `_prune_columns` for why this is narrower than the metric/glossary
    # prune).
    written_columns_by_table: dict[str, set[str]] = {}

    # Resolved once per call, not per metric: a registry read per metric would
    # turn a routine sync of a few hundred metrics into a few hundred queries.
    binder = _table_binder()
    kb_lookups = _keboola_lookups()

    # A model name shared by two models in the same document (a real upstream
    # possibility — nothing prevents two Keboola semantic models being named
    # the same) would otherwise upsert both onto the same `_scoped_id`s and
    # silently overwrite one with the other; `report.metrics_written` would
    # still count both, hiding the loss entirely. Skip the collision and
    # report it instead. The durable fix is keying `_scoped_id` on the
    # adapter's own `metastore_id` rather than the model name; this is the
    # minimal guard until that lands.
    seen_model_names: set[str] = set()

    for model in document_json.get("semantic_model") or []:
        model_name = model.get("name") or ""
        if model_name in seen_model_names:
            report.skipped.append({"kind": "model", "name": model_name, "reason": "duplicate_model_name"})
            continue
        seen_model_names.add(model_name)

        synonyms = _model_synonyms(model)
        constraints = _agnes_payload(model).get("constraints") or []
        # Per model: its own relationships resolve its metrics' JOINs, never a
        # merged pool (a relationship in one model must not satisfy another's).
        rel_lookup = _relationship_lookup_from_model(model)
        # A dataset's grain describes the DATASET. It rides along as a note on
        # the metrics bound to it, never as `metric_definitions.grain`, which
        # would restate it as a fact about the metric's own time dimension.
        grain_by_table = {
            (d.get("source") or d.get("name") or ""): _agnes_payload(d).get("grain")
            for d in model.get("datasets") or []
        }

        for metric in model.get("metrics") or []:
            metric_name = metric.get("name")
            if not metric_name:
                continue
            sql, reason = resolve_expression(metric.get("expression") or {})
            if sql is None:
                report.skipped.append({"kind": "metric", "name": metric_name, "reason": reason})
                continue
            # The bare aggregation fragment, before `_bind_metric` composes it
            # into a runnable `SELECT ... FROM ...` (or a JOIN). The legacy
            # composer stored this alongside the composed `sql` as
            # `metric_definitions.expression`; kept here so the "Expression"
            # block in catalog_semantics.html still has something to render.
            fragment = sql
            table_id = _agnes_payload(metric).get("dataset") or ""
            bound = _bind_metric(sql, table_id, binder, kb_lookups, rel_lookup)
            if bound is None:
                # A binding was declared but cannot be honored — skip, as the
                # legacy composer does, rather than write an unrunnable row.
                report.skipped.append({"kind": "metric", "name": metric_name, "reason": "unresolved_binding"})
                continue
            sql, table_name, tables = bound
            grain = grain_by_table.get(table_id)
            notes = [f"dataset grain: {grain}"] if grain else None
            metric_id = _scoped_id(source, source_ref, model_name, metric_name)
            metric_repo().create(
                id=metric_id,
                name=metric_name,
                display_name=metric_name,
                category=model_name or "semantic_model",
                sql=sql,
                expression=fragment,
                description=metric.get("description"),
                synonyms=synonyms,
                table_name=table_name,
                tables=tables,
                notes=notes,
                validation=_constraints_for(metric_name, constraints),
                source=source,
                source_ref=source_ref,
            )
            written_metric_ids.add(metric_id)
            report.metrics_written += 1

        for dataset in model.get("datasets") or []:
            # Deliberately NOT resolved through the table binder to the Agnes
            # view name (unlike the metric leg above). `column_metadata` is
            # keyed `(table_id, column_name)` with a single `source` column —
            # no source dimension — so writing under the view name collides
            # with rows the profiler / import_proposal / admin already own
            # there: Keboola fields frequently have `description=None`, so
            # every sync would blank a previously-authored description and
            # re-stamp `source='keboola_metastore'`, and `_prune_columns` then
            # deletes it outright. Surfacing Keboola per-column descriptions
            # under the view name is deferred pending an ownership-aware
            # design for that key; for now this write is inert for Keboola
            # (nothing reads the raw tableId) but harmless.
            table_id = dataset.get("source") or dataset.get("name") or ""
            field_names = written_columns_by_table.setdefault(table_id, set())
            for column in dataset.get("fields") or []:
                column_name = column.get("name")
                if not column_name:
                    continue
                column_metadata_repo().save(
                    table_id=table_id,
                    column_name=column_name,
                    basetype=column.get("datatype"),
                    description=column.get("description"),
                    source=source,
                )
                field_names.add(column_name)
                report.columns_written += 1

        for entry in _glossary_entries(model):
            term = entry.get("term")
            if not term:
                continue
            base_glossary_id = _scoped_id(source, source_ref, model_name, _slugify(term))
            # Two distinct terms can slugify identically ("Revenue (net)" and
            # "Revenue net" both -> "revenue_net"); without a dedup, the
            # second silently overwrites the first while `glossary_written`
            # counts both. Numeric-suffix on collision, first-seen order,
            # scoped to this call — mirrors the deleted `assign_glossary_id`.
            glossary_id = base_glossary_id
            suffix = 2
            while glossary_id in written_glossary_ids:
                glossary_id = f"{base_glossary_id}-{suffix}"
                suffix += 1
            # refresh_fts=False: rebuilding the BM25 index once per glossary
            # term makes a routine sync of N terms an O(N^2) full-index
            # rebuild (DuckDB's `PRAGMA create_fts_index` is a full rebuild,
            # not incremental). One rebuild after every write AND prune below
            # instead. `GlossaryPgRepository.create` accepts the same kwarg as
            # a no-op (Postgres computes `ts_rank` on the fly, no index to
            # rebuild), so this is safe on either backend.
            glossary_repo().create(
                id=glossary_id,
                term=term,
                definition=entry.get("definition") or "",
                see_also=entry.get("see_also"),
                source=source,
                source_ref=source_ref,
                refresh_fts=False,
            )
            written_glossary_ids.add(glossary_id)
            report.glossary_written += 1

    if prune:
        report.metrics_pruned = _prune_metrics(source, source_ref, written_metric_ids, safe_prune=safe_prune)
        report.glossary_pruned = _prune_glossary(source, source_ref, written_glossary_ids, safe_prune=safe_prune)
        _prune_columns(source, written_columns_by_table)

    if report.glossary_written or report.glossary_pruned:
        glossary_repo().refresh_search_index()

    return report


def _prune_metrics(source: str, source_ref: Optional[str], written: set[str], *, safe_prune: bool = False) -> int:
    repo = metric_repo()
    in_scope = {
        m["id"]
        for m in repo.list()
        if (m.get("source") or "") == source and (m.get("source_ref") or "") == (source_ref or "")
    }
    if safe_prune and not written and in_scope:
        # Full-wipe guard: wrote nothing this pass while rows exist — a likely
        # empty-but-valid upstream, not a genuine "all metrics deleted". Skip
        # rather than delete every in-scope row (see project_document's
        # ``safe_prune``).
        logger.warning(
            "Semantic projection (%s/%s): wrote zero metrics while %d in-scope rows exist; "
            "skipping prune to avoid a full wipe. Existing rows retained.",
            source,
            source_ref,
            len(in_scope),
        )
        return 0
    pruned = 0
    for metric_id in in_scope - written:
        repo.delete(metric_id)
        pruned += 1
    return pruned


def _prune_glossary(source: str, source_ref: Optional[str], written: set[str], *, safe_prune: bool = False) -> int:
    repo = glossary_repo()
    # No list_all(): list(limit=...) with a high ceiling is the established
    # pattern for "give me every row" reads elsewhere (app/web/router.py).
    in_scope = {
        g["id"]
        for g in repo.list(limit=100_000)
        if (g.get("source") or "") == source and (g.get("source_ref") or "") == (source_ref or "")
    }
    if safe_prune and not written and in_scope:
        logger.warning(
            "Semantic projection (%s/%s): wrote zero glossary terms while %d in-scope rows exist; "
            "skipping prune to avoid a full wipe. Existing rows retained.",
            source,
            source_ref,
            len(in_scope),
        )
        return 0
    pruned = 0
    for glossary_id in in_scope - written:
        repo.delete(glossary_id)
        pruned += 1
    return pruned


def _prune_columns(source: str, written_by_table: dict[str, set[str]]) -> None:
    """Prune fields dropped from a table this document still mentions.

    ``column_metadata`` has no ``source_ref`` column (schema predates this
    task and Task 7 does not migrate it), so this can only scope on
    ``(table_id, source)`` — the finest boundary the current schema
    supports. Two source_refs of the same ``source`` describing the exact
    same ``table_id`` can still prune each other's fields here; that gap
    pre-dates this task and needs a schema change to close, not a projector
    change. It also only prunes tables the document still lists — a dataset
    dropped from the document entirely (not just emptied of fields) leaves
    its old columns in place, since there is no ``column_metadata`` read
    that enumerates "every table a given source has ever written to".
    """
    repo = column_metadata_repo()
    for table_id, field_names in written_by_table.items():
        for existing in repo.list_for_table(table_id):
            if (existing.get("source") or "") != source:
                continue
            if existing["column_name"] not in field_names:
                repo.delete(table_id, existing["column_name"])
