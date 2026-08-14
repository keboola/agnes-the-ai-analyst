"""Storage-independent core of the semantic-layer query validator.

Design: docs/superpowers/specs/2026-08-14-semantic-layer-ui-and-agent-parity-design.md
(section 3, "Query validator"). Mirrors the upstream vendor assistant's
``validate_semantic_query`` tool contract — same input/output shape, same
honesty about its limits.

This module operates on a plain ``document`` dict — the parsed form of an
Ossie-style semantic-model document (docs/superpowers/specs/
2026-08-13-open-semantic-layer-contract-design.md), i.e. what will become a
``semantic_models.document_json`` row once the storage slice lands. It has
**no** DB access, no HTTP, and no import from ``app.`` or ``src.repositories``
— every surface (REST/CLI/MCP) wraps these functions rather than duplicating
the logic.

Expected document shape (fields this module reads; anything else is ignored)::

    {
        "name": str, "description": str,
        "datasets": [
            {"name": str, "source": str, "primary_key": [...],
             "ai_context": {...}, "fields": [{"name": str, ...}, ...]},
            ...
        ],
        "metrics": [
            {"name": str, "dataset": str,
             "expression": {"dialects": [{"dialect": str, "expression": str}, ...]}},
            ...
        ],
        "relationships": [
            {"name": str, "from": <dataset name>, "to": <dataset name>, ...},
            ...
        ],
        "glossary": [{"term": str, "definition": str, "seeAlso": [...]}, ...],
        "custom_extensions": [{"vendor_name": str, "data": "<json string>"}, ...],
    }

LIMITATIONS (carried into every surface that wraps this module):

- Detection (``detect_used_objects``) is best-effort, case-insensitive string
  matching of declared names against the raw SQL text — **not SQL parsing**.
  A dataset/metric/column whose name is a common word, or a query that
  references objects only through an unrelated alias or a view several joins
  removed, can produce false negatives; a name that happens to appear in a
  comment or a string literal can produce a false positive.
- Constraint checking (``evaluate_constraints``) can only verify rules whose
  ``type`` this module recognizes as checkable from the raw SQL text alone
  (currently just ``"required_filter"``, checked as text presence — see
  ``_STATICALLY_CHECKABLE_CONSTRAINT_TYPES``). Anything else — most business
  rules are about the query's *result*, not its text (e.g. a value-range rule
  like ``"value >= 0"``) — degrades to a ``post_execution_checks`` entry
  rather than a guess in either direction.
- ``locally_executable`` only inspects whether a *used* metric declares an
  expression for the target engine (or an ``ANSI_SQL`` fallback -- the
  universally accepted baseline, any target engine; see ``check_dialects``);
  it says nothing about whether the composed SQL is otherwise valid.

Constraint/expected-object payload shapes referenced above (``type``/``rule``
on a constraint, ``{type, name}`` on an expected object) are this module's own
provisional convention — the contract spec deliberately leaves constraints
homeless in the core schema (they ride ``custom_extensions``); see the
implementation report for the exact points still open for confirmation there.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Custom_extensions[].vendor_name under which Agnes-specific constraint data
# travels (contract spec, "Background: Apache Ossie" -> custom_extensions).
# The contract spec doesn't literally pin this string; "agnes" is this
# module's working choice, consistent with existing lowercase source/vendor
# tags elsewhere in the codebase (e.g. metric_definitions.source values).
AGNES_VENDOR_NAME = "agnes"

# Constraint ``type`` values this module can check directly against the raw
# SQL text (a "requires this text to appear somewhere" rule). Everything else
# needs the query's *result* to evaluate (e.g. a value-range rule) and always
# degrades to a post_execution_checks entry -- never a guessed violation.
_STATICALLY_CHECKABLE_CONSTRAINT_TYPES = frozenset({"required_filter"})

# ANSI_SQL is executable on EVERY target engine, not just DuckDB: the
# contract spec's dialect-handling rule states it for DuckDB ("Read the
# DuckDB expression when the document offers one; otherwise ANSI SQL, which
# DuckDB accepts"), and ``_mixed_dialect_warning`` already composes by the
# same universality -- executability and composability must agree on it
# (Devin Review on PR #1319). ``check_dialects`` unions it into every
# target's usable set.
_UNIVERSAL_DIALECT = "ansi_sql"


def _word_present(text: str, ident: str) -> bool:
    """Case-insensitive whole-identifier presence check.

    ``ident`` comes from imported document content (untrusted), so it is
    ``re.escape``-d before compiling -- the pattern is then a literal string
    with two fixed-width lookarounds, which is linear-time regardless of
    ``ident``'s content (no nested quantifiers, no backtracking blowup).
    """
    ident = (ident or "").strip()
    if not ident or not text:
        return False
    pattern = r"(?<![A-Za-z0-9_])" + re.escape(ident) + r"(?![A-Za-z0-9_])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def detect_used_objects(sql: str, document: dict[str, Any]) -> dict[str, list[str]]:
    """Heuristic, case-insensitive match of dataset names/table ids/column
    names/metric names declared in ``document`` against ``sql``'s text.

    Best-effort string matching by declared contract -- not SQL parsing (see
    the module LIMITATIONS section). Returns ``used_datasets`` (a dataset
    counts as used if its name, its physical ``source``, or any of its
    ``fields[]`` names is present), ``used_metrics`` (by metric name), and
    ``matched_relationships`` (a relationship matches when both its ``from``
    and ``to`` datasets are used).
    """
    text = sql or ""
    document = document if isinstance(document, dict) else {}

    used_datasets: list[str] = []
    for dataset in document.get("datasets") or []:
        if not isinstance(dataset, dict):
            continue
        name = dataset.get("name")
        if not name:
            continue
        candidates: list[str] = [str(name)]
        source = dataset.get("source")
        if source:
            source = str(source)
            candidates.append(source)
            last_segment = source.rsplit(".", 1)[-1]
            if last_segment:
                candidates.append(last_segment)

        matched = any(_word_present(text, c) for c in candidates)
        if not matched:
            for field in dataset.get("fields") or []:
                if isinstance(field, dict) and field.get("name") and _word_present(text, str(field["name"])):
                    matched = True
                    break
        if matched:
            used_datasets.append(str(name))

    used_metrics: list[str] = []
    for metric in document.get("metrics") or []:
        if isinstance(metric, dict) and metric.get("name") and _word_present(text, str(metric["name"])):
            used_metrics.append(str(metric["name"]))

    used_dataset_set = set(used_datasets)
    matched_relationships: list[str] = []
    for relationship in document.get("relationships") or []:
        if not isinstance(relationship, dict):
            continue
        name = relationship.get("name")
        if not name:
            continue
        if relationship.get("from") in used_dataset_set and relationship.get("to") in used_dataset_set:
            matched_relationships.append(str(name))

    return {
        "used_datasets": used_datasets,
        "used_metrics": used_metrics,
        "matched_relationships": matched_relationships,
    }


def extract_constraints(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Read constraints from ``document["custom_extensions"]`` under
    ``AGNES_VENDOR_NAME``. ``data`` may be a JSON string or already-parsed
    JSON (dict/list) -- storage layers legitimately hand back either shape.
    Tolerates an absent/malformed extension entirely -- returns ``[]`` rather
    than raising, for every shape of bad input (missing key, wrong type,
    non-JSON string ``data``, missing/wrong-typed ``constraints``).

    Each returned constraint is normalized to
    ``{"name", "type", "rule", "severity", "metrics"}`` -- ``severity``
    defaults to ``"warning"`` unless it is exactly ``"error"`` or
    ``"warning"``, so a malformed/absent severity can never accidentally
    drive ``valid=False`` downstream.
    """
    if not isinstance(document, dict):
        return []
    extensions = document.get("custom_extensions")
    if not isinstance(extensions, list):
        return []

    constraints: list[dict[str, Any]] = []
    for entry in extensions:
        if not isinstance(entry, dict) or entry.get("vendor_name") != AGNES_VENDOR_NAME:
            continue
        raw = entry.get("data")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                continue
        elif isinstance(raw, (dict, list)):
            # Already-parsed JSON: once storage keeps document_json as a
            # parsed tree, ``data`` arrives as an object, not a stringified
            # blob. Same payload, so read it directly rather than silently
            # dropping the constraints (Devin Review on PR #1319).
            parsed = raw
        else:
            continue

        if isinstance(parsed, dict):
            items = parsed.get("constraints")
        elif isinstance(parsed, list):
            items = parsed
        else:
            items = None
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            metrics = item.get("metrics")
            if isinstance(metrics, str):
                metrics = [metrics]
            elif not isinstance(metrics, list):
                metrics = []
            severity = item.get("severity")
            if severity not in ("error", "warning"):
                severity = "warning"
            constraints.append(
                {
                    "name": str(item["name"]),
                    "type": item.get("type"),
                    "rule": item.get("rule"),
                    "severity": severity,
                    "metrics": [str(m) for m in metrics if m],
                }
            )
    return constraints


def evaluate_constraints(
    constraints: list[dict[str, Any]],
    used_metrics: list[str],
    sql: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Evaluate ``constraints`` (as returned by ``extract_constraints``)
    against the metrics actually detected as used and the raw SQL text.

    A constraint whose ``metrics`` don't overlap ``used_metrics`` is not on a
    used metric and is skipped entirely -- it shows up in neither list. That
    includes a constraint with an empty/absent ``metrics`` list: the
    provisional constraint convention has no model-wide scope, so such an
    entry is never evaluated (deliberate, not an oversight). A
    constraint on a used metric whose ``type`` is not statically checkable
    (see ``_STATICALLY_CHECKABLE_CONSTRAINT_TYPES``) degrades to a
    ``post_execution_checks`` entry, never a guessed violation. Only a
    checkable rule that actually fails becomes a ``violations`` entry;
    ``severity == "error"`` there is what drives ``valid=False`` upstream.
    """
    used = set(used_metrics or [])
    text = sql or ""
    normalized_text = " ".join(text.split()).lower()

    violations: list[dict[str, Any]] = []
    post_execution_checks: list[dict[str, Any]] = []

    for constraint in constraints or []:
        if not isinstance(constraint, dict):
            continue
        applicable_metrics = [m for m in (constraint.get("metrics") or []) if m in used]
        if not applicable_metrics:
            continue

        entry = {
            "name": constraint.get("name"),
            "type": constraint.get("type"),
            "rule": constraint.get("rule"),
            "severity": constraint.get("severity") or "warning",
            "metrics": applicable_metrics,
        }

        rule = constraint.get("type")
        if rule not in _STATICALLY_CHECKABLE_CONSTRAINT_TYPES or not constraint.get("rule"):
            post_execution_checks.append({**entry, "reason": "rule cannot be checked before executing the query"})
            continue

        rule_text = " ".join(str(constraint["rule"]).split()).lower()
        if rule_text not in normalized_text:
            violations.append({**entry, "reason": f"required filter not found in the query: {constraint['rule']}"})

    return violations, post_execution_checks


def _declared_dialects(metric: dict[str, Any]) -> list[str]:
    # `expression` is imported, untrusted data: a plain-string expression (or
    # any non-dict shape) means "no declared dialects", never an error.
    expression = metric.get("expression")
    if not isinstance(expression, dict):
        return []
    dialects = expression.get("dialects") or []
    if not isinstance(dialects, list):
        return []
    return [str(d["dialect"]) for d in dialects if isinstance(d, dict) and d.get("dialect")]


def _canonical_dialect(dialect: str) -> str:
    return dialect.strip().lower()


def _used_metric_dialects(document: dict[str, Any], used_metrics: list[str]) -> list[list[str]]:
    """Per *used* metric in ``document``, the dialect labels it declares
    (raw display form; an entry may be empty for a metric with no
    expressions)."""
    used = set(used_metrics or [])
    per_metric: list[list[str]] = []
    for metric in document.get("metrics") or []:
        if not isinstance(metric, dict) or metric.get("name") not in used:
            continue
        per_metric.append(_declared_dialects(metric))
    return per_metric


def _mixed_dialect_warning(declared: list[str], metric_dialect_lists: list[list[str]]) -> str | None:
    """Warn only when the used metrics share no dialect to compose in.

    A metric's declared dialects are ALTERNATIVES for one expression, not
    requirements, so a metric constrains the composition only through the
    set it declares -- and a metric declaring nothing, or only ``ANSI_SQL``
    (universally composable per the contract spec's dialect-handling rule),
    constrains it not at all. The warning fires when the constraining
    metrics' dialect sets have no common member. Labels come from untrusted
    imported text and are compared case-insensitively -- ``"DUCKDB"`` and
    ``"duckdb"`` are one dialect (Devin Review on PR #1319, both points).
    """
    constraining = [
        {_canonical_dialect(d) for d in dialects} - {_UNIVERSAL_DIALECT} for dialects in metric_dialect_lists
    ]
    constraining = [s for s in constraining if s]
    if not constraining or set.intersection(*constraining):
        return None
    non_ansi = sorted((d for d in declared if _canonical_dialect(d) != _UNIVERSAL_DIALECT), key=str.lower)
    return (
        "Used metrics declare mixed SQL dialects (" + ", ".join(non_ansi) + "); "
        "composing their expressions into one query may not run as written."
    )


def check_dialects(
    document: dict[str, Any],
    used_metrics: list[str],
    target_engine: str = "duckdb",
) -> dict[str, Any]:
    """Dialects declared by the *used* metrics in ``document``, a mixed-dialect
    warning (only when those metrics share no composable dialect -- see
    ``_mixed_dialect_warning``), and whether every used metric is executable
    on ``target_engine``.

    ``sql_dialects`` is de-duplicated case-insensitively (first-seen display
    form wins). ``locally_executable`` is ``False`` when a used metric
    declares expressions but none of them target ``target_engine`` or
    ``ANSI_SQL`` (usable on any target -- see ``_UNIVERSAL_DIALECT``). A
    metric with no expressions at all is not flagged here -- nothing to
    conflict with.
    """
    document = document if isinstance(document, dict) else {}
    target = (target_engine or "duckdb").strip().lower()
    usable = frozenset({target, _UNIVERSAL_DIALECT})

    per_metric = _used_metric_dialects(document, used_metrics)

    declared: list[str] = []
    seen: set[str] = set()
    locally_executable = True
    for metric_dialects in per_metric:
        for dialect in metric_dialects:
            key = _canonical_dialect(dialect)
            if key not in seen:
                seen.add(key)
                declared.append(dialect)
        if metric_dialects and not any(_canonical_dialect(d) in usable for d in metric_dialects):
            locally_executable = False

    return {
        "sql_dialects": declared,
        "mixed_dialect_warning": _mixed_dialect_warning(declared, per_metric),
        "locally_executable": locally_executable,
    }


def _build_summary(
    *,
    used_datasets: list[str],
    used_metrics: list[str],
    violations: list[dict[str, Any]],
    post_execution_checks: list[dict[str, Any]],
    locally_executable: bool,
    mixed_dialect_warning: str | None,
) -> str:
    # Written from the violations list, not the pass/fail flag: a
    # warning-severity violation keeps valid=True but must still be named
    # here, never summarized as "no constraint violations detected"
    # (Devin Review on PR #1319).
    error_count = sum(1 for v in violations if v.get("severity") == "error")
    warning_count = len(violations) - error_count

    parts: list[str] = []
    if error_count:
        parts.append(f"{error_count} constraint violation(s) found -- see 'violations'.")
    else:
        base = (
            f"Query references {len(used_datasets)} dataset(s) and {len(used_metrics)} "
            "metric(s) from the semantic layer"
        )
        if warning_count:
            parts.append(f"{base}; {warning_count} advisory constraint violation(s) detected -- see 'violations'.")
        else:
            parts.append(f"{base}; no constraint violations detected.")
    if post_execution_checks:
        parts.append(
            f"{len(post_execution_checks)} constraint(s) cannot be checked before execution -- "
            "see 'post_execution_checks'."
        )
    if not locally_executable:
        parts.append("One or more used metrics have no expression for the local engine and are not locally executable.")
    if mixed_dialect_warning:
        parts.append(mixed_dialect_warning)
    return " ".join(parts)


def _diff_expected(
    expected: list[dict[str, Any]],
    used_datasets: list[str],
    used_metrics: list[str],
    matched_relationships: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    detected_lists: dict[str, list[str]] = {
        "dataset": used_datasets,
        "metric": used_metrics,
        "relationship": matched_relationships,
    }
    detected_sets = {etype: set(names) for etype, names in detected_lists.items()}
    expected_names: dict[str, set[str]] = {etype: set() for etype in detected_lists}

    matched: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for item in expected or []:
        if not isinstance(item, dict):
            continue
        etype, name = item.get("type"), item.get("name")
        if not etype or not name or etype not in detected_sets:
            continue
        expected_names[etype].add(name)
        entry = {"type": etype, "name": name}
        (matched if name in detected_sets[etype] else missing).append(entry)

    unexpected: list[dict[str, Any]] = []
    for etype, names in detected_lists.items():
        for name in names:
            if name not in expected_names[etype]:
                unexpected.append({"type": etype, "name": name})

    return matched, missing, unexpected


def validate_query(
    sql: str,
    documents: list[dict[str, Any]],
    expected: list[dict[str, Any]] | None = None,
    target_engine: str = "duckdb",
) -> dict[str, Any]:
    """Compose the functions above into the vendor-shape validation result.

    ``documents`` is a list of semantic-model documents (a query may span more
    than one model); every list below is the union across all of them,
    de-duplicated, in first-seen order. An empty ``documents`` list (no
    semantic model available) degrades gracefully to an all-clear result
    rather than raising -- the actual "only offer this when the instance has
    >=1 valid model" gate is a wiring-level concern for the surfaces that wrap
    this module, not this pure core.
    """
    documents = [d for d in (documents or []) if isinstance(d, dict)]

    used_datasets: list[str] = []
    used_metrics: list[str] = []
    matched_relationships: list[str] = []
    declared_dialects: list[str] = []
    seen_dialects: set[str] = set()
    metric_dialect_lists: list[list[str]] = []
    locally_executable = True
    violations: list[dict[str, Any]] = []
    post_execution_checks: list[dict[str, Any]] = []

    # Constraints and dialects are scoped to the document that declares them:
    # each document is evaluated against ITS OWN detected metrics, never a
    # name-keyed pool across models — a constraint (or a dialect declaration)
    # in model A must not fire on a same-named metric that only model B
    # defines (Devin Review on PR #1319). The result lists are still unions
    # across documents, de-duplicated in first-seen order (case-insensitively
    # for dialect labels); the mixed-dialect warning is computed over the
    # per-metric dialect sets of every used metric across all documents.
    for document in documents:
        detected = detect_used_objects(sql, document)
        for name in detected["used_datasets"]:
            if name not in used_datasets:
                used_datasets.append(name)
        for name in detected["used_metrics"]:
            if name not in used_metrics:
                used_metrics.append(name)
        for name in detected["matched_relationships"]:
            if name not in matched_relationships:
                matched_relationships.append(name)

        dialect_info = check_dialects(document, detected["used_metrics"], target_engine)
        for dialect in dialect_info["sql_dialects"]:
            key = _canonical_dialect(dialect)
            if key not in seen_dialects:
                seen_dialects.add(key)
                declared_dialects.append(dialect)
        if not dialect_info["locally_executable"]:
            locally_executable = False
        metric_dialect_lists.extend(_used_metric_dialects(document, detected["used_metrics"]))

        document_violations, document_checks = evaluate_constraints(
            extract_constraints(document), detected["used_metrics"], sql
        )
        violations.extend(document_violations)
        post_execution_checks.extend(document_checks)
    valid = not any(v.get("severity") == "error" for v in violations)
    mixed_dialect_warning = _mixed_dialect_warning(declared_dialects, metric_dialect_lists)

    summary = _build_summary(
        used_datasets=used_datasets,
        used_metrics=used_metrics,
        violations=violations,
        post_execution_checks=post_execution_checks,
        locally_executable=locally_executable,
        mixed_dialect_warning=mixed_dialect_warning,
    )

    result: dict[str, Any] = {
        "valid": valid,
        "used_datasets": used_datasets,
        "used_metrics": used_metrics,
        "matched_relationships": matched_relationships,
        "violations": violations,
        "post_execution_checks": post_execution_checks,
        "sql_dialects": declared_dialects,
        "locally_executable": locally_executable,
        "summary": summary,
    }

    if expected is not None:
        matched_expected, missing_expected, unexpected_detected = _diff_expected(
            expected, used_datasets, used_metrics, matched_relationships
        )
        result["matched_expected_objects"] = matched_expected
        result["missing_expected_objects"] = missing_expected
        result["unexpected_detected_objects"] = unexpected_detected

    return result


__all__ = [
    "AGNES_VENDOR_NAME",
    "check_dialects",
    "detect_used_objects",
    "evaluate_constraints",
    "extract_constraints",
    "validate_query",
]
