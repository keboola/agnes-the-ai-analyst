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

from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)


class MasterTokenRequiredError(RuntimeError):
    """The configured Keboola token is not a master (owner) Storage API token.

    Verified live (2026-07-15): the Metastore API rejects non-master tokens
    with an opaque ``401 {"exception": "Failed to create project scope"}``
    regardless of the token's bucket permissions. This check turns that into
    an actionable error before any Metastore call is made.
    """


def check_master_token(info: dict) -> None:
    """Raise MasterTokenRequiredError unless ``info`` (a ``verify_token()``
    payload) describes a master (owner) Storage API token.

    Split out of ``require_master_token`` so a caller that already holds the
    ``verify_token()`` response (the sync loop needs it for the project
    identity anyway) can run the same check without a second API round-trip.
    """
    if not info.get("isMasterToken"):
        raise MasterTokenRequiredError(
            "Keboola semantic layer sync requires a master (owner) Storage "
            "API token; the configured token is not a master token. The "
            "Metastore API rejects non-master tokens with an opaque "
            "'Failed to create project scope' error regardless of bucket "
            "permissions — use the project's owner token instead."
        )


def require_master_token(storage_client) -> None:
    """Raise MasterTokenRequiredError unless the client's token is a master token.

    `storage_client` is a `connectors.keboola.storage_api.KeboolaStorageClient`
    (or any object exposing a compatible `verify_token() -> dict` method).
    """
    check_master_token(storage_client.verify_token())


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
# Single-quoted SQL string literal (handles '' escapes).
_SQL_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
# Double-quoted SQL identifier (handles "" escapes).
_SQL_IDENTIFIER_RE = re.compile(r'"(?:[^"]|"")*"')


def _mask_quoted_regions(expression: str) -> str:
    """Blank the CONTENT of double-quoted identifiers and single-quoted string
    literals, keeping the surrounding SQL structure intact, so a dotted enum
    value (`'in.progress'`), a dotted column name (`"total.amount"`), or a
    double-hyphen inside a quoted region is not mistaken for an alias qualifier
    or a line comment.

    Identifiers are masked FIRST: a single quote inside an identifier
    (e.g. `"col'name"`) would otherwise start a spurious string-literal match
    that swallows a following real literal and re-exposes its contents (a
    dotted value or a `--`), causing a valid single-table metric to be skipped.
    """
    masked = _SQL_IDENTIFIER_RE.sub('""', expression)
    masked = _SQL_STRING_LITERAL_RE.sub("''", masked)
    return masked


def references_foreign_alias(expression: str) -> bool:
    """True if `expression` qualifies any column with an `<alias>.` prefix.

    See _ALIAS_QUALIFIER_RE docstring for why this indicates a multi-dataset
    JOIN this importer cannot safely compose in v1.

    Quoted regions are masked first: a dotted value inside a single-quoted
    literal (`WHEN "status" = 'in.progress'`) or a dotted column name inside a
    quoted identifier (`"total.amount"`) is data, not an alias reference, and
    must not cause a valid single-table metric to be skipped.
    """
    return bool(_ALIAS_QUALIFIER_RE.search(_mask_quoted_regions(expression)))


def has_embedded_sql_comment(expression: str) -> bool:
    """True if `expression` contains a `--` SQL line-comment marker outside
    any quoted literal or identifier.

    Verified live (2026-07-15) against a real project: some real Keboola
    metric expressions carry a trailing `--` comment as a free-text author
    note (e.g. flagging a table that doesn't exist in this project, or a
    WHERE condition the author never actually applied to the formula).
    Composing a `SELECT <expression> FROM <table> AS t` statement naively
    appends the FROM clause AFTER such an expression — SQL then treats
    everything from
    `--` onward (including the appended FROM clause) as part of the
    comment, silently dropping it and breaking the query. Confirmed live:
    DuckDB raises a binder error ("FROM clause is missing") rather than
    returning a wrong number, but the query is still unusable — and the
    comment text itself often signals the metric is genuinely incomplete
    for this table. Per this importer's "skip rather than guess" contract,
    any embedded comment means skip, never strip-and-compose.
    """
    return "--" in _mask_quoted_regions(expression)


_QUOTED_REGION_RE = re.compile(f"({_SQL_IDENTIFIER_RE.pattern}|{_SQL_STRING_LITERAL_RE.pattern})")


def _sub_outside_quotes(pattern: str, repl: str, expression: str) -> str:
    """Like re.sub(pattern, repl, expression), but never touches text inside
    a single-quoted string literal or a double-quoted identifier.

    Splits on quoted regions (kept verbatim as a capture group, so they land
    at odd indices) and only substitutes in the unquoted segments — unlike
    _mask_quoted_regions, this preserves the exact original text, so it's
    safe to use for a rewrite (not just detection).
    """
    parts = _QUOTED_REGION_RE.split(expression)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(pattern, repl, parts[i])
    return "".join(parts)


def compose_sql(expression: str, table_name: str) -> str:
    """Compose a full, runnable metric_definitions.sql from a Keboola
    semantic-metric.sql fragment (a bare aggregation expression, verified
    live to never be a full query) and the resolved Agnes table_registry
    view name.

    Callers MUST check BOTH `references_foreign_alias(expression)` and
    `has_embedded_sql_comment(expression)` first and skip the metric if
    either is True — this function does not itself guard against those
    cases. A foreign-alias reference needs a JOIN this importer can't
    compose; a trailing `--` comment would swallow the appended FROM clause.
    `build_metric_row` performs both checks before calling this.
    """
    return f"SELECT {expression} FROM {quote_ident(table_name)} AS t"


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


def try_join_composition(
    expression: str,
    dataset_table_id: str,
    table_lookup: dict[tuple[str, str], str],
    relationship_lookup: dict[str, list[dict]],
    column_lookup: dict[str, set[str]],
) -> tuple[Optional[dict], Optional[str]]:
    """Attempt to resolve a foreign-alias-referencing metric expression into
    a JOIN. Returns (fields, None) with 'table_name' / 'tables' / 'sql'
    keys set on success, or (None, skip_reason) — never raises, every
    failure mode is a specific skip_reason (docs/superpowers/specs/
    2026-07-17-keboola-relationship-metrics-design.md — "skip and count,
    never guess"). Any failure not covered by a resolve_relationship()
    skip_reason falls back to "foreign_alias_reference", the pre-existing
    generic reason — so this function never introduces a regression for a
    metric it can't fully resolve.
    """
    relationship, skip_reason = resolve_relationship(dataset_table_id, relationship_lookup)
    if relationship is None:
        return None, skip_reason

    table_name = resolve_table_name(dataset_table_id, table_lookup)
    joined_table_name = resolve_table_name(relationship["from"], table_lookup)
    if table_name is None or joined_table_name is None:
        return None, "foreign_alias_reference"

    to_columns = column_lookup.get(table_name)
    from_columns = column_lookup.get(joined_table_name)
    if not to_columns or not from_columns:
        return None, "foreign_alias_reference"

    alias_sides = resolve_join_aliases(relationship["on"], from_columns, to_columns)
    if alias_sides is None:
        return None, "foreign_alias_reference"
    to_alias, from_alias = alias_sides

    sql = compose_join_sql(expression, table_name, joined_table_name, relationship["on"], to_alias, from_alias)
    return {"table_name": table_name, "tables": [table_name, joined_table_name], "sql": sql}, None


def build_metric_row(
    metric_item: dict,
    table_lookup: dict[tuple[str, str], str],
    dataset_lookup: dict[str, dict],
    constraints: list[dict],
    model_uuid: str,
    relationship_lookup: Optional[dict[str, list[dict]]] = None,
    column_lookup: Optional[dict[str, set[str]]] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """Map one semantic-metric item to a metric_definitions row dict.

    Returns (row, None) on success, or (None, skip_reason) where
    skip_reason is "missing_name", "unresolved_table", "embedded_sql_comment",
    "foreign_alias_reference" (generic fallback — see try_join_composition
    for the more specific relationship-resolution skip reasons this
    function also propagates: "ambiguous_relationship",
    "unsupported_relationship_type", "unverified_relationship_direction").

    `relationship_lookup` / `column_lookup` are optional — omitting them
    (the pre-relationship-feature call shape) preserves the exact
    pre-existing behavior: every foreign-alias expression skips as
    "foreign_alias_reference", unconditionally.
    """
    attrs = metric_item.get("attributes") or {}
    name = attrs.get("name")
    expression = attrs.get("sql") or ""
    dataset_table_id = attrs.get("dataset") or ""

    if not name:
        return None, "missing_name"

    tables: list[str] = []
    if references_foreign_alias(expression):
        if has_embedded_sql_comment(expression):
            return None, "embedded_sql_comment"
        join_fields: Optional[dict] = None
        join_skip_reason: Optional[str] = "foreign_alias_reference"
        if relationship_lookup is not None and column_lookup is not None:
            join_fields, join_skip_reason = try_join_composition(
                expression,
                dataset_table_id,
                table_lookup,
                relationship_lookup,
                column_lookup,
            )
        if join_fields is None:
            return None, join_skip_reason
        table_name = join_fields["table_name"]
        tables = join_fields["tables"]
        sql = join_fields["sql"]
    else:
        if has_embedded_sql_comment(expression):
            return None, "embedded_sql_comment"
        table_name = resolve_table_name(dataset_table_id, table_lookup)
        if table_name is None:
            return None, "unresolved_table"
        sql = compose_sql(expression, table_name)

    row: dict[str, Any] = {
        "id": f"keboola/{model_uuid}/{name}",
        "name": name,
        "display_name": name,
        "category": "keboola",
        "description": attrs.get("description") or "",
        "expression": expression,
        "table_name": table_name,
        "sql": sql,
        "source": "keboola_semantic_layer",
    }
    if tables:
        row["tables"] = tables

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


def _default_keboola_connection() -> Optional[dict]:
    """The Keboola ``source_connections`` row that acts as "the default one":
    the row flagged ``is_default``, or — when none is flagged — the first
    Keboola connection.

    Shared by credential resolution, the multi-source ordering (default
    connection syncs first) and NULL-``source_ref`` adoption (only the default
    connection adopts legacy, unstamped rows), so all three agree on which
    connection is "the default".
    """
    from src.repositories import source_connections_repo

    conns_repo = source_connections_repo()
    conn = conns_repo.get_default("keboola")
    if conn is not None:
        return conn
    candidates = conns_repo.list(source_type="keboola")
    return candidates[0] if candidates else None


def _resolve_keboola_credentials(
    explicit_url: Optional[str],
    explicit_token: Optional[str],
) -> tuple[str, str]:
    """Resolve (stack_url, token) for the semantic-layer sync — see
    ``_resolve_keboola_credentials_slot`` for the full precedence; this wrapper
    drops the slot label for callers that only need the credentials."""
    url, token, _slot = _resolve_keboola_credentials_slot(explicit_url, explicit_token)
    return url, token


def _resolve_keboola_credentials_slot(
    explicit_url: Optional[str],
    explicit_token: Optional[str],
) -> tuple[str, str, str]:
    """Resolve (stack_url, token, slot) for the semantic-layer sync.

    ``slot`` names WHERE the credentials came from — ``"explicit"``,
    ``"legacy_env"``, ``"connection"`` or ``"none"``. Only the
    ``"connection"`` slot has a connection identity, so only it may stamp
    rows with that connection's ``source_ref`` (spec §3, fallback bullet).

    Precedence: explicit args > legacy ``KEBOOLA_STACK_URL``/
    ``KEBOOLA_STORAGE_TOKEN`` (env or system-secrets vault) > the default
    (or first, if none is marked default) named Keboola ``source_connections``
    row — the same connection the "Add Keboola project" admin wizard
    (``/admin/data-sources``) manages.

    Verified live (2026-07-22, agnes-dev): before this fallback existed, an
    instance that connected a Keboola project only through that wizard —
    never setting the legacy env var — silently failed every semantic-layer
    sync with "credentials not configured", even though the same
    connection's regular table syncs and its own ``/test`` endpoint both
    resolve their token off it fine. Mirrors
    ``app/api/admin_source_connections.py::_resolve_token``'s own
    vault-secret-then-token_env precedence for a single connection.

    Returns ("", "") if nothing resolves — callers treat that as the
    existing "not configured" error.
    """
    if explicit_url and explicit_token:
        return explicit_url, explicit_token, "explicit"

    from app.datasource_secrets import datasource_secret

    url = explicit_url or os.environ.get("KEBOOLA_STACK_URL", "")
    token = explicit_token or datasource_secret("KEBOOLA_STORAGE_TOKEN") or ""
    if url and token:
        return url, token, "legacy_env"

    from src.repositories import connection_secrets_repo

    conn = _default_keboola_connection()
    if conn is None:
        return url, token, "none"

    conn_url = (conn.get("config") or {}).get("stack_url", "")
    conn_token = ""
    try:
        secrets = connection_secrets_repo()
        if secrets.has(conn["id"]):
            conn_token = secrets.get(conn["id"]) or ""
    except Exception:
        conn_token = ""
    if not conn_token:
        token_env = conn.get("token_env") or ""
        if token_env:
            conn_token = os.environ.get(token_env, "")

    # url/token here are at most a PARTIAL legacy pair (the full-pair case
    # returned above) — mixing that partial half with the named connection's
    # other half could pair one project's token with a different project's
    # stack URL. Only a full legacy pair counts as "legacy resolved"; a
    # partial one falls through to the named connection as a coherent pair.
    return conn_url, conn_token, "connection"


# Every per-source counter the sync reports. The orchestrator sums these
# across sources for the top-level aggregate, so a new counter only has to be
# added here to appear in both the per-source dict and the total.
_COUNTER_KEYS = (
    "created_or_updated",
    "pruned",
    "skipped_unresolved_table",
    "skipped_foreign_alias",
    "skipped_embedded_comment",
    "skipped_ambiguous_relationship",
    "skipped_unsupported_relationship_type",
    "skipped_unverified_relationship_direction",
    "skipped_conflict",
    "glossary_created_or_updated",
    "glossary_pruned",
    "skipped_missing_term",
    "skipped_duplicate_project",
)


def _empty_counters() -> dict[str, int]:
    return {key: 0 for key in _COUNTER_KEYS}


def _in_scope(row: dict, scope_refs: set, adopt_null: bool) -> bool:
    """True if an existing metric/glossary row belongs to the source currently
    syncing — i.e. is inside that source's prune scope.

    ``scope_refs`` is usually just the source's own ``source_ref``; the legacy
    fallback path widens it to "NULL or the default connection id" so a
    downgrade (last master token removed) still prunes the rows it used to own
    even when the credentials it resolved have no connection identity to stamp.

    Only imported rows are ever in scope (manual/yaml rows are never touched).
    A row stamped with a DIFFERENT connection's ``source_ref`` is out of scope
    and is therefore left intact — including when that connection no longer
    exists (orphaned-but-intact beats silently deleted).
    """
    if row.get("source") != "keboola_semantic_layer":
        return False
    ref = row.get("source_ref")
    return ref in scope_refs or (ref is None and adopt_null)


def _is_owned_by_source(
    existing: Optional[dict],
    incoming_id: str,
    scope_refs: set,
    adopt_null: bool,
) -> bool:
    """True if this source may write a row under a name/term that ``existing``
    (the row currently holding that name/term, or None) already occupies.

    Ownership tracks the same scope as the prune (``scope_refs``): the rows a
    source may delete are exactly the rows it may overwrite. Names must stay
    unique for catalog UX, and ownership is sticky — whichever source's ref is
    already on the row keeps it, so discovery order does not decide outcomes
    beyond the deterministic first claim. A non-imported row (manual /
    yaml_import) always keeps its name.
    """
    if existing is None:
        return True
    if existing.get("id") == incoming_id:
        # Same row id ≠ same source: ids are built from (model_uuid, name)
        # with no connection component, and a cloned/restored Keboola project
        # can carry the SAME Metastore model UUID under a different
        # connection. An unconditional id match would let that second source
        # silently re-stamp source_ref on the first source's row (the writer
        # is ON CONFLICT(id) DO UPDATE), putting the row outside the first
        # source's prune scope — the exact cross-wipe this gate exists to
        # prevent. Allow the id shortcut only for rows this source already
        # owns or un-stamped legacy rows (pre-v107 NULL source_ref, which any
        # colliding source may claim — first writer wins, then stickiness
        # applies).
        ref = existing.get("source_ref")
        return ref is None or ref in scope_refs
    return _in_scope(existing, scope_refs, adopt_null)


def _project_key(url: str, token_info: dict) -> Optional[tuple[str, Any]]:
    """Identity of the upstream Keboola project behind (stack URL, token):
    (stack host, token owner id).

    Nothing stops an admin from registering the same project under two
    connections (the uniqueness constraint is on connection *name*). Two
    connections syncing one project would flip-flop ``source_ref`` ownership
    and cross-wipe each other every run, so the orchestrator dedupes on this
    key and skips the later connection.

    Returns ``None`` when the token's ``owner.id`` is missing — a
    ``verify_token`` response without an owner id gives no reliable identity
    to dedupe on, and treating a missing id as an identity (``(host, None)``)
    would collide any two such projects on the same stack, silently skipping
    the second one as a spurious "duplicate". Callers must treat ``None`` as
    "not dedupable" and sync the source normally rather than folding it into
    the seen-keys set.
    """
    from urllib.parse import urlparse

    host = urlparse(url).netloc or url
    owner = token_info.get("owner") or {}
    owner_id = owner.get("id")
    if owner_id is None:
        return None
    return host, owner_id


def _enumerate_master_sources() -> list[dict]:
    """Every Keboola connection that has a master (owner) token in the vault,
    as ``[{"connection_id", "name", "stack_url", "token"}]``.

    Ordered default connection first, then by connection id — a deterministic
    order so first-claim outcomes for same-run name conflicts are stable
    across runs. Connections without a ``stack_url`` are unusable and skipped.
    """
    from app.api.admin_source_connections import master_secret_key
    from src.repositories import connection_secrets_repo, source_connections_repo

    secrets = connection_secrets_repo()
    sources: list[dict] = []
    for conn in source_connections_repo().list(source_type="keboola"):
        try:
            token = secrets.get(master_secret_key(conn["id"])) or ""
        except Exception:
            logger.warning("Could not read master token for connection %s", conn["id"])
            continue
        if not token:
            continue
        stack_url = (conn.get("config") or {}).get("stack_url") or ""
        if not stack_url:
            logger.warning(
                "Keboola connection %s has a master token but no stack_url; skipping",
                conn["id"],
            )
            continue
        sources.append(
            {
                "connection_id": conn["id"],
                "name": conn.get("name") or conn["id"],
                "stack_url": stack_url,
                "token": token,
            }
        )

    default_conn = _default_keboola_connection()
    default_id = default_conn["id"] if default_conn else None
    sources.sort(key=lambda s: (s["connection_id"] != default_id, s["connection_id"]))
    return sources


def _sync_one_source(
    url: str,
    token: str,
    source_ref: Optional[str],
    *,
    adopt_null: bool,
    prune_scope_refs: Optional[set] = None,
    seen_project_keys: Optional[set[tuple[str, Any]]] = None,
) -> dict:
    """Fetch ONE Keboola project's semantic layer (Metastore) and upsert it
    into Agnes's metric_definitions / glossary_terms tables, pruning stale
    rows *of this source only* that no longer exist upstream.

    ``source_ref`` is the originating ``source_connections.id`` stamped onto
    every written row (None when the credentials have no connection identity).
    ``adopt_null`` lets the default connection claim legacy rows written
    before provenance existed (``source_ref IS NULL``).
    ``prune_scope_refs`` widens the owned/prunable set beyond ``source_ref``
    (the legacy fallback owns "NULL or the default connection id" while
    stamping only what the resolved credentials justify); it defaults to
    ``{source_ref}``.

    ``seen_project_keys``, when given, is the set of project identities
    already synced in this run: if this source resolves to one of them it
    short-circuits as ``skipped_duplicate_project`` before touching any row;
    otherwise its own identity is added to the set.

    Raises MasterTokenRequiredError if the token is not a master token (see
    require_master_token) — a configuration error the caller surfaces loudly
    (the endpoint maps it to a 400) rather than swallowing into the result.
    """

    import requests

    from connectors.keboola.metastore_client import MetastoreApiError, MetastoreClient
    from connectors.keboola.storage_api import KeboolaStorageClient, StorageApiError
    from src.repositories import column_metadata_repo, glossary_repo, metric_repo, table_registry_repo

    scope_refs: set = set(prune_scope_refs) if prune_scope_refs is not None else {source_ref}

    storage_client = KeboolaStorageClient(url=url, token=token)
    # A Storage API outage during the master-token preflight must abort with a
    # structured error, not an unhandled 500 — same defense the Metastore
    # fetch below gets. MasterTokenRequiredError is intentionally NOT caught:
    # it is a configuration error the endpoint surfaces as a 400.
    try:
        token_info = storage_client.verify_token()
    except (StorageApiError, requests.RequestException) as e:
        logger.error("Keboola Storage API preflight (verify_token) failed: %s", e)
        return {"status": "error", "error": f"Storage API preflight failed: {e}", "source_ref": source_ref}
    check_master_token(token_info)

    project_key = _project_key(url, token_info)
    # A missing owner id (project_key is None) gives no reliable identity to
    # dedupe on — two distinct projects lacking an owner id must NOT collide
    # on a shared "no identity" bucket and silently skip the second one.
    # Only a real, comparable identity participates in the seen-keys set.
    if seen_project_keys is not None and project_key is not None:
        if project_key in seen_project_keys:
            logger.warning(
                "Keboola semantic layer: connection %s resolves to a project already "
                "synced in this run; skipping to avoid cross-wiping its rows",
                source_ref,
            )
            return {
                "status": "skipped",
                **_empty_counters(),
                "skipped_duplicate_project": 1,
                "source_ref": source_ref,
                "project_key": project_key,
            }
        seen_project_keys.add(project_key)

    metastore = MetastoreClient(url=url, token=token)

    # A Metastore outage / 401 / 5xx must abort the whole run with a logged,
    # structured error — never propagate as an unhandled 500 and never reach
    # the prune loop (which would delete against an empty seen_ids). Mirrors
    # the defensive fetch-wrapping in app/api/bq_metadata_refresh.py.
    try:
        models = metastore.list_items("semantic-model")
    except (MetastoreApiError, requests.RequestException) as e:
        logger.error("Keboola Metastore fetch failed (semantic-model): %s", e)
        return {"status": "error", "error": f"Metastore fetch failed: {e}", "source_ref": source_ref}

    empty_result = {"status": "ok", **_empty_counters(), "source_ref": source_ref, "project_key": project_key}
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
        relationships = metastore.list_items("semantic-relationship", model_uuid)
        glossary_items = metastore.list_items("semantic-glossary", model_uuid)
    except (MetastoreApiError, requests.RequestException) as e:
        logger.error("Keboola Metastore fetch failed (model %s): %s", model_uuid, e)
        return {"status": "error", "error": f"Metastore fetch failed: {e}", "source_ref": source_ref}

    table_lookup = table_lookup_from_registry(table_registry_repo().list_by_source("keboola"))
    dataset_lookup = dataset_lookup_by_table_id(datasets)
    relationship_lookup = relationship_lookup_by_dataset(relationships)
    column_metadata = column_metadata_repo()
    column_lookup = {
        name: {c["column_name"] for c in column_metadata.list_for_table(name)} for name in set(table_lookup.values())
    }

    repo = metric_repo()
    seen_ids: set[str] = set()
    # Ids this run refused to write because another owner holds the name. They
    # are NOT "gone upstream" — upstream still publishes them — so they must be
    # held back from the prune loop, which would otherwise delete this source's
    # own previously-written row the first time a conflict appears.
    retained_ids: set[str] = set()
    skipped_unresolved_table = 0
    skipped_foreign_alias = 0
    skipped_embedded_comment = 0
    skipped_ambiguous_relationship = 0
    skipped_unsupported_relationship_type = 0
    skipped_unverified_relationship_direction = 0
    skipped_conflict = 0

    for item in metrics:
        row, skip_reason = build_metric_row(
            item,
            table_lookup,
            dataset_lookup,
            constraints,
            model_uuid,
            relationship_lookup=relationship_lookup,
            column_lookup=column_lookup,
        )
        if row is None:
            if skip_reason == "unresolved_table":
                skipped_unresolved_table += 1
            elif skip_reason == "foreign_alias_reference":
                skipped_foreign_alias += 1
            elif skip_reason == "embedded_sql_comment":
                skipped_embedded_comment += 1
            elif skip_reason == "ambiguous_relationship":
                skipped_ambiguous_relationship += 1
            elif skip_reason == "unsupported_relationship_type":
                skipped_unsupported_relationship_type += 1
            elif skip_reason == "unverified_relationship_direction":
                skipped_unverified_relationship_direction += 1
            else:
                logger.warning(
                    "Keboola semantic metric skipped (%s): %r",
                    skip_reason,
                    (item.get("attributes") or {}).get("name"),
                )
            continue
        # Name-uniqueness gate: another source (or a hand-authored row) may
        # already hold this metric name. Ownership is sticky — skip and count
        # rather than shadowing the incumbent with a second same-named row.
        if not _is_owned_by_source(repo.find_by_name(row["name"]), row["id"], scope_refs, adopt_null):
            logger.warning(
                "Keboola semantic metric %r already exists under a different owner; skipping",
                row["name"],
            )
            skipped_conflict += 1
            retained_ids.add(row["id"])
            continue
        repo.create(**row, source_ref=source_ref)
        seen_ids.add(row["id"])

    existing = [m for m in repo.list() if _in_scope(m, scope_refs, adopt_null)]
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
            "while %d existing rows are present for source %s; skipping prune "
            "to avoid a full wipe. Existing rows retained.",
            len(existing),
            source_ref,
        )
    else:
        for m in existing:
            if m["id"] not in seen_ids and m["id"] not in retained_ids:
                repo.delete(m["id"])
                pruned += 1

    glossary_repository = glossary_repo()
    used_glossary_ids: set[str] = set()
    seen_glossary_ids: set[str] = set()
    retained_glossary_ids: set[str] = set()
    skipped_missing_term = 0

    for item in glossary_items:
        row, skip_reason = build_glossary_row(item, model_uuid, used_glossary_ids)
        if row is None:
            if skip_reason == "missing_term":
                skipped_missing_term += 1
            else:
                logger.warning(
                    "Keboola glossary item skipped (%s): %r",
                    skip_reason,
                    (item.get("attributes") or {}).get("term"),
                )
            continue
        # Same sticky-ownership gate as the metric loop, keyed on the term.
        if not _is_owned_by_source(glossary_repository.find_by_term(row["term"]), row["id"], scope_refs, adopt_null):
            logger.warning(
                "Keboola glossary term %r already exists under a different owner; skipping",
                row["term"],
            )
            skipped_conflict += 1
            retained_glossary_ids.add(row["id"])
            continue
        # refresh_fts=False: rebuilding the BM25 index is O(N) per call, so
        # doing it once per imported term is O(N^2) over a sync. Refresh once
        # after the full create+prune loop below instead.
        glossary_repository.create(**row, source_ref=source_ref, refresh_fts=False)
        seen_glossary_ids.add(row["id"])

    existing_glossary = [g for g in glossary_repository.list(limit=100000) if _in_scope(g, scope_refs, adopt_null)]
    glossary_pruned = 0
    if not seen_glossary_ids and existing_glossary:
        # Same safety valve as the metric prune above: a successful-but-empty
        # glossary response must not wipe every previously-imported term.
        logger.warning(
            "Keboola glossary: upstream returned zero usable terms while %d "
            "existing rows are present for source %s; skipping prune to avoid a full wipe.",
            len(existing_glossary),
            source_ref,
        )
    else:
        for g in existing_glossary:
            if g["id"] not in seen_glossary_ids and g["id"] not in retained_glossary_ids:
                glossary_repository.delete(g["id"])
                glossary_pruned += 1

    if seen_glossary_ids:
        # Single rebuild for the whole batch (see the refresh_fts=False note
        # in the create loop above).
        glossary_repository.refresh_search_index()

    return {
        "status": "ok",
        "created_or_updated": len(seen_ids),
        "pruned": pruned,
        "skipped_unresolved_table": skipped_unresolved_table,
        "skipped_foreign_alias": skipped_foreign_alias,
        "skipped_embedded_comment": skipped_embedded_comment,
        "skipped_ambiguous_relationship": skipped_ambiguous_relationship,
        "skipped_unsupported_relationship_type": skipped_unsupported_relationship_type,
        "skipped_unverified_relationship_direction": skipped_unverified_relationship_direction,
        "skipped_conflict": skipped_conflict,
        "glossary_created_or_updated": len(seen_glossary_ids),
        "glossary_pruned": glossary_pruned,
        "skipped_missing_term": skipped_missing_term,
        "skipped_duplicate_project": 0,
        "source_ref": source_ref,
        "project_key": project_key,
    }


def _as_source_entry(connection_id: Optional[str], name: Optional[str], result: dict) -> dict:
    """Normalize one ``_sync_one_source`` result into the per-source entry the
    endpoint publishes: identity first, every counter present (so consumers
    never have to guard on a missing key), no internal ``project_key``.

    ``connection_id`` is the one public identity key; ``result``'s own
    ``source_ref`` (always equal to ``connection_id`` for this call site) is
    dropped rather than published redundantly alongside it.
    """
    entry = {
        "connection_id": connection_id,
        "name": name,
        **_empty_counters(),
        **result,
    }
    entry.pop("project_key", None)
    entry.pop("source_ref", None)
    return entry


def _aggregate_sources(sources: list[dict]) -> dict:
    """Sum per-source counters into the historical top-level result shape and
    attach the per-source breakdown.

    Top-level ``status`` is "ok" when at least one source synced; otherwise
    "error" carrying the first source's error message — which keeps the
    endpoint's 502-on-returned-error contract intact for the single-source
    (legacy) shape it has always seen.
    """
    totals = {key: sum(int(s.get(key) or 0) for s in sources) for key in _COUNTER_KEYS}
    any_ok = any(s.get("status") == "ok" for s in sources)
    result: dict[str, Any] = {"status": "ok" if any_ok else "error", **totals, "sources": sources}
    if not any_ok:
        result["error"] = next(
            (s.get("error") for s in sources if s.get("error")),
            "Keboola semantic layer sync failed",
        )
    return result


def sync_semantic_layer(
    keboola_url: Optional[str] = None,
    keboola_token: Optional[str] = None,
) -> dict:
    """Sync every configured Keboola project's semantic layer (Metastore) into
    Agnes's metric_definitions / glossary_terms tables.

    Source selection, in order:

    1. Explicit ``keboola_url`` + ``keboola_token`` — one unstamped source
       (``source_ref=None``), today's single-project behavior.
    2. Every Keboola connection holding a master token in the vault
       (``_enumerate_master_sources``) — each synced under its own
       ``source_ref``, prunes scoped to its own rows, errors isolated per
       source. When at least one master token exists these are the ONLY
       sources: the legacy env pair has no connection identity to scope a
       prune to, and mixing it in would recreate the cross-wipe problem.
    3. Otherwise the legacy single-source resolution
       (``_resolve_keboola_credentials_slot``): rows are stamped with the
       default connection's id only when the credentials came from that
       connection, else left NULL, while the prune scope covers "NULL or the
       default connection id" either way. Rows belonging to other connections
       are left orphaned-but-intact rather than pruned.

    MasterTokenRequiredError propagates in modes 1 and 3 (a configuration
    error the endpoint surfaces as a 400); in mode 2 it is captured into the
    offending source's entry so one misconfigured connection cannot abort the
    whole run.
    """
    if keboola_url and keboola_token:
        result = _sync_one_source(keboola_url, keboola_token, None, adopt_null=True)
        return _aggregate_sources([_as_source_entry(None, None, result)])

    master_sources = _enumerate_master_sources()
    if master_sources:
        default_conn = _default_keboola_connection()
        default_id = default_conn["id"] if default_conn else None
        seen_project_keys: set[tuple[str, Any]] = set()
        entries: list[dict] = []
        for source in master_sources:
            connection_id = source["connection_id"]
            try:
                result = _sync_one_source(
                    source["stack_url"],
                    source["token"],
                    connection_id,
                    # Legacy rows predate provenance and belong to whichever
                    # project was synced before this feature — only the default
                    # connection may claim them.
                    adopt_null=connection_id == default_id,
                    seen_project_keys=seen_project_keys,
                )
            except MasterTokenRequiredError as e:
                logger.error("Keboola semantic layer: connection %s rejected: %s", connection_id, e)
                result = {"status": "error", "error": str(e)}
            except Exception as e:
                # Per-source isolation is the whole point of the loop: one
                # connection blowing up in an unforeseen way (a client bug, an
                # unexpected upstream shape) must not abort the sources after
                # it. Broad on purpose — the error is recorded, not swallowed.
                logger.exception("Keboola semantic layer: connection %s failed", connection_id)
                result = {"status": "error", "error": str(e)}
            entries.append(_as_source_entry(connection_id, source["name"], result))
        return _aggregate_sources(entries)

    url, token, slot = _resolve_keboola_credentials_slot(keboola_url, keboola_token)
    if not url or not token:
        return {
            "status": "error",
            "error": "Keboola credentials not configured",
            **_empty_counters(),
            "sources": [],
        }
    default_conn = _default_keboola_connection()
    default_id = default_conn["id"] if default_conn else None
    # Stamp the default connection's id only when the credentials actually came
    # from that connection — a legacy env pair may well point at a different
    # project, and mislabeling it would hand those rows to the connection on a
    # later master-token sync. The prune scope stays "NULL or default id"
    # either way, so a downgrade still cleans up what it previously owned.
    stamp = default_id if slot == "connection" else None
    result = _sync_one_source(
        url,
        token,
        stamp,
        adopt_null=True,
        prune_scope_refs={None, default_id},
    )
    return _aggregate_sources([_as_source_entry(default_id, default_conn["name"] if default_conn else None, result)])


def relationship_lookup_by_dataset(relationship_items: list[dict]) -> dict[str, list[dict]]:
    """Index semantic-relationship attributes by every tableId that appears
    on either side (from or to), so a metric's dataset can be looked up
    against every relationship touching it in O(1).

    A relationship's attributes dict is stored under BOTH its from and to
    tableId keys — resolve_relationship() below determines which side
    (verified vs. unverified direction) the caller's dataset sits on.
    """
    lookup: dict[str, list[dict]] = {}
    for item in relationship_items:
        attrs = item.get("attributes") or {}
        from_id = attrs.get("from")
        to_id = attrs.get("to")
        if from_id:
            lookup.setdefault(from_id, []).append(attrs)
        if to_id:
            lookup.setdefault(to_id, []).append(attrs)
    return lookup


def resolve_relationship(
    dataset_table_id: str,
    relationship_lookup: dict[str, list[dict]],
) -> tuple[Optional[dict], Optional[str]]:
    """Resolve exactly one semantic-relationship for a metric's dataset,
    restricted to the ONE live-verified-safe case (docs/superpowers/specs/
    2026-07-17-keboola-relationship-metrics-design.md):

    - exactly one relationship touches this dataset (from OR to side) —
      zero or multiple candidates return "ambiguous_relationship";
    - that relationship's type == "left" — the only value observed live;
      anything else returns "unsupported_relationship_type";
    - the dataset is on the relationship's "to" side — the only direction
      verified live to compose FROM t LEFT JOIN joined correctly; a
      dataset on the "from" side returns "unverified_relationship_direction"
      rather than assuming the reverse direction behaves the same way.

    Returns (relationship_attrs, None) on success, (None, skip_reason)
    otherwise. Never raises, never guesses.
    """
    candidates = relationship_lookup.get(dataset_table_id, [])
    if len(candidates) != 1:
        return None, "ambiguous_relationship"

    relationship = candidates[0]
    if relationship.get("type") != "left":
        return None, "unsupported_relationship_type"
    if relationship.get("to") != dataset_table_id:
        return None, "unverified_relationship_direction"

    return relationship, None


# Matches the live-verified semantic-relationship.on shape exactly:
# `<alias>."<column>" = <alias>."<column>"`. Verified live (2026-07-17):
# 29/29 sampled relationships matched this pattern with no variation.
_ON_CLAUSE_RE = re.compile(r'^\s*(\w+)\s*\.\s*"([^"]+)"\s*=\s*(\w+)\s*\.\s*"([^"]+)"\s*$')


def parse_on_clause(on: str) -> Optional[tuple[str, str, str, str]]:
    """Parse a semantic-relationship.on string into (alias1, col1, alias2, col2).

    Returns None if `on` doesn't match the live-verified shape — callers
    must treat that as "can't resolve, skip" rather than a hard error.
    """
    m = _ON_CLAUSE_RE.match(on)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def resolve_join_aliases(
    on: str,
    from_columns: set[str],
    to_columns: set[str],
) -> Optional[tuple[str, str]]:
    """Determine which of the two aliases in `on` belongs to the `to`
    (metric's own) table vs. the `from` (joined) table, by matching each
    side's column name against the real, already-known column sets of
    both tables (column_metadata_repo(), populated by the profiler).

    Returns (to_alias, from_alias) when EXACTLY ONE of the two possible
    pairings is consistent with both tables' real schemas. Returns None —
    "can't resolve with confidence" — when the on-clause doesn't parse,
    when BOTH pairings are consistent (genuinely ambiguous, e.g. both
    tables share a column name used in the join), or when NEITHER pairing
    is consistent (e.g. column metadata is missing or stale).
    """
    parsed = parse_on_clause(on)
    if parsed is None:
        return None
    alias1, col1, alias2, col2 = parsed

    # Candidate A: alias1 is the `to` side, alias2 is the `from` side.
    candidate_a = col1 in to_columns and col2 in from_columns
    # Candidate B: alias1 is the `from` side, alias2 is the `to` side.
    candidate_b = col1 in from_columns and col2 in to_columns

    if candidate_a and not candidate_b:
        return alias1, alias2
    if candidate_b and not candidate_a:
        return alias2, alias1
    return None


def extract_foreign_aliases(expression: str) -> set[str]:
    """Return every distinct alias (excluding `t`) that qualifies a column
    in `expression`, masking quoted regions first (same rationale as
    references_foreign_alias / has_embedded_sql_comment).

    A metric may use more than one local alias spelling for what resolves
    to the SAME single relationship (live-verified real case) — all of
    them get rewritten to the canonical join alias in compose_join_sql.
    """
    masked = _mask_quoted_regions(expression)
    aliases = {m.group(1) for m in _ALIAS_QUALIFIER_RE.finditer(masked)}
    aliases.discard("t")
    return aliases


def compose_join_sql(
    expression: str,
    primary_table: str,
    joined_table: str,
    on: str,
    to_alias: str,
    from_alias: str,
) -> str:
    """Compose a two-table LEFT JOIN metric_definitions.sql.

    `to_alias`/`from_alias` are the on-clause's alias tokens as resolved by
    resolve_join_aliases — `to_alias` corresponds to `primary_table`
    (rewritten to the canonical `t`), `from_alias` to `joined_table`
    (rewritten to the canonical `j`). Every foreign-alias-qualified column
    in `expression` (there may be multiple distinct alias spellings for the
    same joined table — see extract_foreign_aliases) is rewritten to `j.`.

    Callers MUST have already checked references_foreign_alias(expression)
    and has_embedded_sql_comment(expression) — this function does not
    itself guard against those cases (mirrors compose_sql's contract).
    """
    rewritten_expression = expression
    for alias in extract_foreign_aliases(expression):
        # Devin Review, PR #944: rewrite outside quoted regions only, so an
        # alias-qualified-looking substring inside a string literal or a
        # quoted identifier (e.g. `'o.pending'`) is never corrupted.
        rewritten_expression = _sub_outside_quotes(rf"\b{re.escape(alias)}\s*\.", "j.", rewritten_expression)

    on_alias1, on_col1, on_alias2, on_col2 = parse_on_clause(on)  # type: ignore[misc]
    remapped_alias1 = "t" if on_alias1 == to_alias else "j"
    remapped_alias2 = "t" if on_alias2 == to_alias else "j"
    remapped_on = f'{remapped_alias1}."{on_col1}" = {remapped_alias2}."{on_col2}"'

    return f"SELECT {rewritten_expression} FROM {quote_ident(primary_table)} AS t LEFT JOIN {quote_ident(joined_table)} AS j ON {remapped_on}"


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def slugify_term(term: str) -> str:
    """Lowercase, replace runs of non-alphanumeric characters with a single
    underscore, strip leading/trailing underscores.

    Keboola glossary terms are natural-language phrases ("Monthly Recurring
    Revenue") — unlike semantic-metric.name, which is already a slug — so a
    stable primary key requires this normalization step (verified live,
    2026-07-17: terms contain spaces/uppercase/punctuation).
    """
    slug = _NON_ALNUM_RE.sub("_", term.lower()).strip("_")
    return slug


def assign_glossary_id(term: str, model_uuid: str, used_ids: set[str]) -> str:
    """Build a stable glossary_terms.id from (model_uuid, slugified term),
    resolving a slug collision within the same model with a numeric
    ``-2``, ``-3``, ... suffix on first-seen order.

    Mutates ``used_ids`` by adding the returned id — callers processing a
    list of glossary items must reuse the same set across the whole run so
    collisions are detected against everything assigned so far.
    """
    base = f"keboola/{model_uuid}/{slugify_term(term)}"
    candidate = base
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def build_glossary_row(
    item: dict,
    model_uuid: str,
    used_ids: set[str],
) -> tuple[Optional[dict], Optional[str]]:
    """Map one semantic-glossary item to a glossary_terms row dict.

    Returns (row, None) on success, or (None, skip_reason) where
    skip_reason is "missing_term" or "missing_definition" — both fields
    are NOT NULL on glossary_terms, so a missing value is skipped
    defensively rather than written as an empty string.
    """
    attrs = item.get("attributes") or {}
    term = attrs.get("term")
    definition = attrs.get("definition")

    if not term:
        return None, "missing_term"
    if not definition:
        return None, "missing_definition"

    return {
        "id": assign_glossary_id(term, model_uuid, used_ids),
        "term": term,
        "definition": definition,
        "see_also": list(attrs.get("seeAlso") or []),
        "model_uuid": model_uuid,
        "source": "keboola_semantic_layer",
    }, None
