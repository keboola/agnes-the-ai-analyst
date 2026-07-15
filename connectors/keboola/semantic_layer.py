"""Keboola semantic layer (Metastore) -> Agnes metric_definitions importer.

Design: docs/superpowers/specs/2026-07-15-keboola-semantic-layer-importer-design.md

Maps a Keboola project's semantic-layer metrics (bound to Storage tables via
`semantic-dataset`, annotated with `semantic-constraint` rules) into Agnes's
`metric_definitions` table. Runs on a schedule (see
app/api/keboola_semantic_layer_refresh.py); this module has no HTTP-layer
concerns of its own.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MasterTokenRequiredError(RuntimeError):
    """The configured Keboola token is not a master (owner) Storage API token.

    Verified live (2026-07-15): the Metastore API rejects non-master tokens
    with an opaque ``401 {"exception": "Failed to create project scope"}``
    regardless of the token's bucket permissions. This check turns that into
    an actionable error before any Metastore call is made.
    """


def require_master_token(storage_client) -> None:
    """Raise MasterTokenRequiredError unless the client's token is a master token.

    `storage_client` is a `connectors.keboola.storage_api.KeboolaStorageClient`
    (or any object exposing a compatible `verify_token() -> dict` method).
    """
    info = storage_client.verify_token()
    if not info.get("isMasterToken"):
        raise MasterTokenRequiredError(
            "Keboola semantic layer sync requires a master (owner) Storage "
            "API token; the configured token is not a master token. The "
            "Metastore API rejects non-master tokens with an opaque "
            "'Failed to create project scope' error regardless of bucket "
            "permissions — use the project's owner token instead."
        )


def table_lookup_from_registry(rows: list[dict]) -> dict[tuple[str, str], str]:
    """Build {(bucket, source_table): agnes_view_name} from table_registry
    rows (from `table_registry_repo().list_by_source("keboola")`)."""
    lookup: dict[tuple[str, str], str] = {}
    for row in rows:
        bucket = row.get("bucket")
        source_table = row.get("source_table")
        name = row.get("name")
        if bucket and source_table and name:
            lookup[(bucket, source_table)] = name
    return lookup


def resolve_table_name(table_id: str, lookup: dict[tuple[str, str], str]) -> str | None:
    """Resolve a Keboola tableId ('bucket.table') to its Agnes
    table_registry view name, or None if that table isn't registered.

    Bucket ids themselves contain dots (e.g. `in.c-example_source`), so the
    tableId must be split on the LAST dot to isolate the table name —
    splitting on the first dot would misparse the bucket.
    """
    if "." not in table_id:
        return None
    bucket, _, source_table = table_id.rpartition(".")
    return lookup.get((bucket, source_table))


def dataset_lookup_by_table_id(dataset_items: list[dict]) -> dict[str, dict]:
    """Build {tableId: attributes} from semantic-dataset items, for
    enriching a metric row with grain/dimensions/synonyms/notes."""
    result: dict[str, dict] = {}
    for d in dataset_items:
        attrs = d.get("attributes") or {}
        table_id = attrs.get("tableId")
        if table_id:
            result[table_id] = attrs
    return result


# Matches `<alias>."column"` or `<alias>.column` — qualified-column shapes
# observed in live Keboola semantic-metric.sql fragments. Verified live
# (2026-07-15): single-dataset expressions are always bare column references
# (`SUM("amount")`); an alias-qualified reference only appears when the
# expression crosses into a JOINed dataset via semantic-relationship data
# this importer does not have (relationship support is out of scope for
# v1) — so any match here means "skip, cannot safely compose."
_ALIAS_QUALIFIER_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_"])')
# Single-quoted SQL string literal (handles '' escapes), masked out before the
# alias scan so dotted enum values like 'in.progress' don't read as an alias.
_SQL_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def references_foreign_alias(expression: str) -> bool:
    """True if `expression` qualifies any column with an `<alias>.` prefix.

    See _ALIAS_QUALIFIER_RE docstring for why this indicates a multi-dataset
    JOIN this importer cannot safely compose in v1.

    String literals are masked first: a dotted value inside a single-quoted
    literal (e.g. `WHEN "status" = 'in.progress'`) is data, not an alias
    reference, and must not cause a valid single-table metric to be skipped.
    """
    masked = _SQL_STRING_LITERAL_RE.sub("''", expression)
    return bool(_ALIAS_QUALIFIER_RE.search(masked))


def compose_sql(expression: str, table_name: str) -> str:
    """Compose a full, runnable metric_definitions.sql from a Keboola
    semantic-metric.sql fragment (a bare aggregation expression, verified
    live to never be a full query) and the resolved Agnes table_registry
    view name.

    Callers MUST check `references_foreign_alias(expression)` first and
    skip the metric if True — this function does not itself guard against
    that case.
    """
    return f'SELECT {expression} FROM "{table_name}" AS t'


def merge_constraints(metric_name: str, constraints: list[dict]) -> dict | None:
    """Build the `validation` JSON for one metric from semantic-constraint
    items whose `metrics[]` list includes it, or None if none match.

    Constraint attribute shape (`name`, `constraintType`, `rule` — a single
    SQL-ish string like `'value >= 0'`, `metrics: [...]`, `severity`) per
    `keboola/cli`'s documented live-verified contract.
    """
    matching = [c for c in constraints if metric_name in ((c.get("attributes") or {}).get("metrics") or [])]
    if not matching:
        return None
    return {
        "rules": [
            {
                "name": (c.get("attributes") or {}).get("name"),
                "constraint_type": (c.get("attributes") or {}).get("constraintType"),
                "rule": (c.get("attributes") or {}).get("rule"),
                "severity": (c.get("attributes") or {}).get("severity"),
            }
            for c in matching
        ]
    }


def build_metric_row(
    metric_item: dict,
    table_lookup: dict[tuple[str, str], str],
    dataset_lookup: dict[str, dict],
    constraints: list[dict],
    model_uuid: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Map one semantic-metric item to a metric_definitions row dict.

    Returns (row, None) on success, or (None, skip_reason) where
    skip_reason is "unresolved_table" (the metric's dataset isn't
    registered in Agnes's table_registry) or "foreign_alias_reference"
    (the expression needs a JOIN this importer can't safely compose — see
    references_foreign_alias).
    """
    attrs = metric_item.get("attributes") or {}
    name = attrs.get("name")
    expression = attrs.get("sql") or ""
    dataset_table_id = attrs.get("dataset") or ""

    if references_foreign_alias(expression):
        return None, "foreign_alias_reference"

    table_name = resolve_table_name(dataset_table_id, table_lookup)
    if table_name is None:
        return None, "unresolved_table"

    row: dict[str, Any] = {
        "id": f"keboola/{model_uuid}/{name}",
        "name": name,
        "display_name": name,
        "category": "keboola",
        "description": attrs.get("description") or "",
        "expression": expression,
        "table_name": table_name,
        "sql": compose_sql(expression, table_name),
        "source": "keboola_semantic_layer",
    }

    dataset_attrs = dataset_lookup.get(dataset_table_id) or {}
    grain = dataset_attrs.get("grain")
    if grain:
        row["grain"] = grain
    primary_key = dataset_attrs.get("primaryKey") or []
    if primary_key:
        row["dimensions"] = list(primary_key)
    ai_block = dataset_attrs.get("ai") or {}
    synonyms = ai_block.get("synonyms") or []
    if synonyms:
        row["synonyms"] = list(synonyms)
    notes = list(ai_block.get("hints") or []) + list(ai_block.get("warnings") or [])
    if notes:
        row["notes"] = notes

    validation = merge_constraints(name, constraints)
    if validation is not None:
        row["validation"] = validation

    return row, None


def sync_semantic_layer(
    keboola_url: Optional[str] = None,
    keboola_token: Optional[str] = None,
) -> dict:
    """Fetch a Keboola project's semantic layer (Metastore) and upsert it
    into Agnes's metric_definitions table, pruning stale
    'keboola_semantic_layer'-sourced rows that no longer exist upstream.

    Credentials default to the standard Keboola env-var/vault resolution
    (KEBOOLA_STACK_URL + KEBOOLA_STORAGE_TOKEN via datasource_secret) — same
    hierarchy connectors/keboola/metadata.py uses.

    Raises MasterTokenRequiredError if the configured token is not a master
    token (see require_master_token) — this is a configuration error the
    caller should surface loudly, not swallow into the returned dict.
    """

    import requests

    from app.datasource_secrets import datasource_secret
    from connectors.keboola.storage_api import KeboolaStorageClient, StorageApiError
    from connectors.keboola.metastore_client import MetastoreApiError, MetastoreClient
    from src.repositories import table_registry_repo, metric_repo

    url = keboola_url or os.environ.get("KEBOOLA_STACK_URL", "")
    token = keboola_token or datasource_secret("KEBOOLA_STORAGE_TOKEN") or ""
    if not url or not token:
        return {"status": "error", "error": "Keboola credentials not configured"}

    storage_client = KeboolaStorageClient(url=url, token=token)
    # A Storage API outage during the master-token preflight must abort with a
    # structured error, not an unhandled 500 — same defense the Metastore
    # fetch below gets. MasterTokenRequiredError is intentionally NOT caught:
    # it is a configuration error the endpoint surfaces as a 400.
    try:
        require_master_token(storage_client)
    except (StorageApiError, requests.RequestException) as e:
        logger.error("Keboola Storage API preflight (verify_token) failed: %s", e)
        return {"status": "error", "error": f"Storage API preflight failed: {e}"}

    metastore = MetastoreClient(url=url, token=token)

    # A Metastore outage / 401 / 5xx must abort the whole run with a logged,
    # structured error — never propagate as an unhandled 500 and never reach
    # the prune loop (which would delete against an empty seen_ids). Mirrors
    # the defensive fetch-wrapping in app/api/bq_metadata_refresh.py.
    try:
        models = metastore.list_items("semantic-model")
    except (MetastoreApiError, requests.RequestException) as e:
        logger.error("Keboola Metastore fetch failed (semantic-model): %s", e)
        return {"status": "error", "error": f"Metastore fetch failed: {e}"}

    empty_result = {
        "status": "ok",
        "created_or_updated": 0,
        "pruned": 0,
        "skipped_unresolved_table": 0,
        "skipped_foreign_alias": 0,
    }
    if not models:
        return empty_result
    if len(models) > 1:
        logger.warning(
            "Keboola project has %d semantic models; using the first (%s)",
            len(models),
            (models[0].get("attributes") or {}).get("name"),
        )
    model_uuid = models[0]["id"]

    try:
        datasets = metastore.list_items("semantic-dataset", model_uuid)
        metrics = metastore.list_items("semantic-metric", model_uuid)
        constraints = metastore.list_items("semantic-constraint", model_uuid)
    except (MetastoreApiError, requests.RequestException) as e:
        logger.error("Keboola Metastore fetch failed (model %s): %s", model_uuid, e)
        return {"status": "error", "error": f"Metastore fetch failed: {e}"}

    table_lookup = table_lookup_from_registry(table_registry_repo().list_by_source("keboola"))
    dataset_lookup = dataset_lookup_by_table_id(datasets)

    repo = metric_repo()
    seen_ids: set[str] = set()
    skipped_unresolved_table = 0
    skipped_foreign_alias = 0

    for item in metrics:
        row, skip_reason = build_metric_row(item, table_lookup, dataset_lookup, constraints, model_uuid)
        if row is None:
            if skip_reason == "unresolved_table":
                skipped_unresolved_table += 1
            else:
                skipped_foreign_alias += 1
            continue
        repo.create(**row)
        seen_ids.add(row["id"])

    existing = [m for m in repo.list() if m.get("source") == "keboola_semantic_layer"]
    pruned = 0
    if not seen_ids and existing:
        # Safety valve: the fetch succeeded (HTTP 200) but produced zero
        # usable metrics while we already hold keboola_semantic_layer rows.
        # Pruning here would wipe *every* imported business-metric
        # definition in one pass. A successful-but-empty/wrong-shaped
        # Metastore response (e.g. the client-side modelUUID filter drifting
        # on an upstream schema change) is the likely cause, not a genuine
        # "all metrics deleted upstream". Mirror the `if not models` guard —
        # skip the prune and log loudly rather than silently delete.
        logger.warning(
            "Keboola semantic layer: upstream returned zero usable metrics "
            "while %d existing rows are present; skipping prune to avoid a "
            "full wipe. Existing rows retained.",
            len(existing),
        )
    else:
        for m in existing:
            if m["id"] not in seen_ids:
                repo.delete(m["id"])
                pruned += 1

    return {
        "status": "ok",
        "created_or_updated": len(seen_ids),
        "pruned": pruned,
        "skipped_unresolved_table": skipped_unresolved_table,
        "skipped_foreign_alias": skipped_foreign_alias,
    }
