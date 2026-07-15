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
import re

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


def references_foreign_alias(expression: str) -> bool:
    """True if `expression` qualifies any column with an `<alias>.` prefix.

    See _ALIAS_QUALIFIER_RE docstring for why this indicates a multi-dataset
    JOIN this importer cannot safely compose in v1.
    """
    return bool(_ALIAS_QUALIFIER_RE.search(expression))


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
