"""The access-policy resolver — the single junction every enforcement point
binds against (table access policies design doc §5, §6, §12).

``policied_relation(table_id, principal, *, dialect="duckdb")`` turns a
registered table and the calling principal into a :class:`PoliciedRelation`:
an *unexecuted*, parenthesizable ``SELECT`` a caller can read from, plus the
bind parameters for it. This module never runs that SQL — each enforcement
surface (Task 6's AST rewrite for SQL surfaces, Task 8's ``FROM``-builder for
``table_id`` surfaces, Task 10's BigQuery jobs-API path) does, binding
``params`` through the engine's own named-parameter mechanism, never string
interpolation (§6.2).

Two outcomes:

- **Passthrough** (``policied=False``) — the table has no
  ``access_policy_sql``, or it does but the caller is an unrestricted admin
  (§12: admin bypass follows the credential *surface*, not merely group
  membership). ``relation_sql`` is a bare ``SELECT * FROM <base view>``.
- **Policied** (``policied=True``) — a policy is attached and the caller has
  a resolvable identity. On ``dialect="duckdb"`` (the default) ``relation_sql``
  is the policy body *verbatim* — its ``$name`` placeholders left as bind
  markers, never rewritten, because DuckDB binds named parameters natively
  (§6.2). On ``dialect="bigquery"`` it is the SAME body transpiled to
  BigQuery Standard SQL (§7.2) — ``$name`` survives the transpile as
  BigQuery's own ``@name`` named-parameter syntax, so the binding guarantee
  holds on both engines from one authored policy. Either way ``params``
  carries only the ``user_email`` / ``user_id`` / ``user_groups`` keys the
  policy text actually references — identical Python values on both
  dialects; converting them to a BigQuery ``QueryParameter`` is the
  enforcement site's job, not this resolver's.

Identity resolution (§12): a plain user dict binds itself; an
``AgentPrincipal`` binds its *owner*'s identity (an agent's declared scope
narrows which tables it reaches, never who it reaches them as — handing it
``$user_groups`` for anyone but the owner would be a privilege escalation);
a ``SessionPrincipal`` (co-drive, several live participants, no single
identity) has nothing to bind and is refused outright rather than guessed.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from src.sql_ident import quote_ident

# The three identity values a policy body may bind (§6.2). Duplicated here
# rather than imported from ``src.access_policy_validate`` because the two
# modules ask different questions: the validator asks "is every ``$name`` in
# this SQL text one of these, in a safe position" (save time, on arbitrary
# untrusted-until-proven text); this module asks "which of these does this
# ALREADY-VALIDATED policy actually reference, so only those get looked up
# and bound" (every request, on text that passed the validator once).
_KNOWN_VARIABLES = frozenset({"user_email", "user_id", "user_groups"})

# SQL LIKE/ILIKE/SIMILAR-TO wildcard metacharacters. Group names are not
# validated against any character class elsewhere in the system (§6.3) — a
# Workspace-synced or admin-created group literally named ``%`` would
# silently widen every ``list_contains($user_groups, ...)`` policy for every
# member of it. The save-time validator (Task 3) already refuses to let an
# identity variable stand in LIKE/ILIKE/SIMILAR TO pattern position for one
# specific policy body; this is a second, independent check on the VALUE
# about to be bound — defense in depth that does not depend on knowing every
# way a policy (present or future) might turn out to depend on group-name
# shape.
_PATTERN_METACHARACTERS = ("%", "_")


@dataclass(frozen=True)
class PoliciedRelation:
    relation_sql: str  # a parenthesizable SELECT yielding the rows to read.
    # policied=False → "SELECT * FROM <base_view_name>"
    # policied=True  → the policy body verbatim ($vars kept as bind markers)
    params: dict  # bind values for the $vars actually referenced (subset of user_email/user_id/user_groups)
    policied: bool
    table_id: str


class PolicyIdentityUnresolvable(Exception):
    """The principal has no single identity to bind a policy against — a
    co-drive ``SessionPrincipal`` (§12), or any other principal shape this
    resolver does not recognize. Fails closed: an unrecognized principal is
    refused, never silently treated as passthrough or bound to a guess.
    """


class PolicyMappingEmpty(Exception):
    """A ``policy_mapping`` table (§15) a policy joins against has zero
    rows — indistinguishable, from the analyst's side, from "you legitimately
    have no data" unless surfaced explicitly (§15.1). Declared here as the
    shared reason code; raised by the surfaces that actually execute a
    policy's relation (Tasks 7/8), not by this module, which never runs SQL.
    """

    def __init__(self, mapping_table: str, last_sync: Any) -> None:
        self.mapping_table = mapping_table
        self.last_sync = last_sync
        super().__init__(f"access mapping {mapping_table!r} is empty (last_sync={last_sync!r})")


class PolicyError(Exception):
    """A policy failed to resolve or execute for ``table_id`` (§16, §17).

    Deliberately carries no engine-level detail: the whole point of this
    reason code is that a raw DuckDB/BigQuery error for a failing policy can
    quote literal values out of the policy body (§16's closing paragraph),
    so callers render a table-scoped message instead of
    ``str(underlying_exception)``.
    """

    def __init__(self, table_id: str) -> None:
        self.table_id = table_id
        super().__init__(f"access policy for table {table_id!r} failed to resolve or execute")


def policied_relation(table_id: str, principal, *, dialect: str = "duckdb") -> PoliciedRelation:
    """Resolve ``(table_id, principal)`` to a :class:`PoliciedRelation` (§5).

    ``table_id`` accepts either the registry ``id`` or its ``name`` (§5.3) —
    every caller who only knows one of the two can call this directly; the
    returned ``.table_id`` is always the normalized registry ``id``.

    ``dialect='bigquery'`` (Task 10, §7.2) shares every step below with the
    default ``'duckdb'`` arm — table resolution, no-policy passthrough,
    admin bypass (§12), identity resolution, and which of the three
    variables get bound — and differs ONLY in the final ``relation_sql``:
    the policy body is transpiled to BigQuery Standard SQL via
    ``sqlglot.transpile(..., read="duckdb", write="bigquery")`` instead of
    returned verbatim. ``$name`` placeholders survive the transpile as
    BigQuery's own ``@name`` named-parameter syntax (verified on sqlglot
    30.6.0), so ``params`` carries the SAME identity values on both
    dialects — the enforcement site (Task 10's BQ jobs-API path) converts
    them to ``bigquery.QueryParameter`` objects, never string-interpolates
    them.

    Never executes ``relation_sql`` — that is each enforcement surface's job.
    """
    if dialect not in ("duckdb", "bigquery"):
        raise ValueError(f"unknown dialect: {dialect!r}")

    row = _resolve_table_row(table_id)
    resolved_id = row["id"]
    base_view_sql = f"SELECT * FROM {quote_ident(row['name'])}"
    policy_sql = row.get("access_policy_sql")

    if not policy_sql:
        return PoliciedRelation(relation_sql=base_view_sql, params={}, policied=False, table_id=resolved_id)

    if _is_admin_bypass(principal):
        return PoliciedRelation(relation_sql=base_view_sql, params={}, policied=False, table_id=resolved_id)

    user_id, user_email, live_groups = _resolve_identity(principal, table_id=resolved_id)
    referenced = _referenced_variables(policy_sql, table_id=resolved_id)

    params: dict[str, Any] = {}
    if "user_email" in referenced:
        params["user_email"] = user_email
    if "user_id" in referenced:
        params["user_id"] = user_id
    if "user_groups" in referenced:
        groups = live_groups()
        for name in groups:
            if any(ch in name for ch in _PATTERN_METACHARACTERS):
                raise PolicyError(resolved_id)
        params["user_groups"] = groups

    relation_sql = (
        _transpile_policy_to_bigquery(policy_sql, table_id=resolved_id) if dialect == "bigquery" else policy_sql
    )

    return PoliciedRelation(relation_sql=relation_sql, params=params, policied=True, table_id=resolved_id)


def _transpile_policy_to_bigquery(policy_sql: str, *, table_id: str) -> str:
    """§7.2 — transpile an admin-authored, DuckDB-dialect policy body to
    BigQuery Standard SQL via sqlglot.

    Verified end to end on sqlglot 30.6.0: ``EXCLUDE`` → ``EXCEPT``,
    ``md5(x)`` → ``TO_HEX(MD5(x))``, ``list_contains($g, col)`` →
    ``EXISTS(SELECT 1 FROM UNNEST(@g) AS _col WHERE _col = col)`` — and,
    the part that makes the whole feature work on this engine, every
    ``$name`` placeholder → BigQuery's own ``@name`` named-parameter
    syntax, so the binding guarantee (§6.2) holds on both engines from one
    authored policy. The transpiled body's own ``FROM <name>`` stays a
    bare registry name here, exactly like the DuckDB arm's verbatim
    ``relation_sql`` — resolving it to the table's physical
    ``bq.<dataset>.<table>`` path is the enforcement site's job (§7.3),
    not this resolver's; ``policied_relation`` only ever answers "what
    should a caller read", never "where does that physically live".

    A transpile failure is a ``PolicyError`` — the admin never writes BQ
    SQL directly (§7.2: only the DuckDB-dialect body is authored, and the
    save-time preview shows the transpiled form, §13), so a failure here
    means the policy body uses a construct sqlglot cannot carry across
    dialects. Raising the SAME reason code every other resolution failure
    uses (rather than leaking sqlglot's own exception text) keeps §16's
    contract — no engine detail in a policy failure — true for this new
    failure mode too.
    """
    try:
        statements = sqlglot.transpile(policy_sql, read="duckdb", write="bigquery")
    except Exception as exc:
        raise PolicyError(table_id) from exc
    if not statements:
        raise PolicyError(table_id)
    return statements[0]


def _resolve_table_row(table_id: str) -> dict:
    """id-or-name lookup (§5.3) — ``id`` checked first (registry PK, exact
    match), then ``name`` (what master views and SQL ``FROM`` clauses name).
    Neither resolving is treated the same as any other resolution failure:
    ``PolicyError`` ("failed to resolve"), never a silent passthrough.
    """
    from src.repositories import table_registry_repo

    repo = table_registry_repo()
    row = repo.get(table_id)
    if row is None:
        row = repo.get_by_name(table_id)
    if row is None:
        raise PolicyError(table_id)
    return row


def _is_admin_bypass(principal) -> bool:
    """§12 — admin bypass follows the credential *surface*, not merely group
    membership: a ``surface='stack'`` PAT (the ``agnes init`` default) is
    filtered like any analyst even when its holder is an Admin. Only a plain
    user dict can be admin; a restricted ``Principal`` (agent/session) is
    "never admin" — the same rule ``can_access_table`` applies.
    """
    if not isinstance(principal, dict):
        return False
    user_id = principal.get("id")
    if not user_id:
        return False

    from app.auth.access import is_user_admin
    from src.rbac import _credential_surface

    return is_user_admin(user_id) and _credential_surface(principal) == "all"


def _resolve_identity(principal, *, table_id: str):
    """Resolve ``principal`` to ``(user_id, user_email, live_groups)`` (§12).

    ``live_groups`` is a zero-arg callable rather than an already-fetched
    list so a policy that never references ``$user_groups`` doesn't pay for
    the live group-membership read at all — ``policied_relation`` only
    calls it when the policy text needs it.
    """
    from app.auth.session_principal import AgentPrincipal, SessionPrincipal

    if isinstance(principal, SessionPrincipal):
        raise PolicyIdentityUnresolvable(
            f"table {table_id!r} has a per-user access policy; this session has "
            "multiple participants and no single identity to bind it against -- "
            "open the table in a solo session"
        )
    if isinstance(principal, AgentPrincipal):
        owner_id, owner_email = principal.owner_user_id, principal.owner_email
        return owner_id, owner_email, lambda: _live_groups(owner_id)
    if isinstance(principal, dict):
        user_id, user_email = principal.get("id"), principal.get("email")
        return user_id, user_email, lambda: _live_groups(user_id)

    # Not a shape this resolver recognizes -- fail closed rather than guess.
    raise PolicyIdentityUnresolvable(
        f"table {table_id!r} has a per-user access policy; the caller has no "
        f"identity this resolver recognizes ({type(principal).__name__})"
    )


def _live_groups(user_id: str | None) -> list[str]:
    """§6.4 — read through the SAME live path ``get_accessible_tables`` /
    ``StackResolver`` already use for table-grain authorization, so
    ``$user_groups`` never diverges from what that check just decided.
    """
    if not user_id:
        return []

    from src.repositories import user_group_members_repo

    return user_group_members_repo().list_group_names_for_user(user_id)


def _referenced_variables(policy_sql: str, *, table_id: str) -> set[str]:
    """Which of the three known variables ``policy_sql`` actually
    references, so ``params`` only carries the keys the policy text uses.

    The save-time validator (Task 3) already proved every ``$name`` in a
    saved policy is one of ``_KNOWN_VARIABLES`` in value position —
    re-parsing here (rather than a substring search over the raw text) is
    what stays correct if that ever stops holding for a given row (a
    hand-edited DB value, a future authoring path), and never mistakes a
    variable *name* appearing inside a string literal or comment for a
    reference.
    """
    try:
        statement = sqlglot.parse_one(policy_sql, read="duckdb")
    except Exception as exc:
        raise PolicyError(table_id) from exc
    return {p.name for p in statement.find_all(exp.Placeholder) if p.name in _KNOWN_VARIABLES}


# ---------------------------------------------------------------------------
# Task 6 -- AST substitution for SQL read surfaces (§5.2). The other
# consumer of `policied_relation` (Task 8's `table_id`-shaped FROM builder)
# needs none of this: it never has a caller SQL tree to rewrite.
# ---------------------------------------------------------------------------

# A bare "word" -- the widest a SQL identifier or keyword can be. Used only
# by the last-resort scan over SQL sqlglot could not parse at all (rule 3
# below): every Agnes table name is representable by this pattern, so
# probing each unique token through `resolve` finds a policied reference
# without needing a full registry listing.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PolicyNameCollision(Exception):
    """A caller-introduced name -- a CTE alias or a subquery/derived-table
    alias (§5.2 rule 4) -- is spelled identically to a policied table's name.

    ``WITH invoices AS (SELECT ... FROM invoices ...) SELECT * FROM invoices``
    is the single most common analyst idiom and the default shape an LLM
    writes, so this is never resolved by guessing which occurrence the
    caller meant -- it is refused outright, with a structured reason (§16)
    so the caller (an LLM in practice) renames the CTE and retries instead
    of looping on the same ambiguity.
    """

    def __init__(self, table_id: str) -> None:
        self.table_id = table_id
        super().__init__(
            f"a CTE or subquery alias in this query is spelled identically to "
            f"the policied table {table_id!r}; rename it and retry"
        )


def _scan_unparseable_for_policied_table(sql: str, principal, resolve) -> str | None:
    """Best-effort answer to "does this SQL -- which failed to parse --
    reference a policied table" (§5.2 rule 3; §19's tripwire example is
    ``SELECT * FROM t SAMPLE 50%``, which DuckDB accepts and sqlglot does
    not parse).

    No AST is available, so there is no candidate-table list other than the
    raw text itself. Every word-shaped token is a candidate; ``PolicyError``
    -- ``_resolve_table_row``'s exact signal for "no such registered table"
    -- is swallowed as "not a match" so a query that merely mentions
    unregistered names keeps failing exactly as it did before this feature
    existed. Any OTHER exception (an identity/mapping problem on a genuine
    match) is a real, table-scoped failure and is not swallowed.
    """
    seen: set[str] = set()
    for match in _IDENTIFIER_RE.finditer(sql):
        word = match.group(0)
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            relation = resolve(word, principal)
        except PolicyError:
            continue
        if relation.policied:
            return relation.table_id
    return None


def rewrite_sql(
    sql: str,
    principal,
    *,
    resolve=policied_relation,
) -> tuple[str, dict, list[str]]:
    """Substitute every policied table reference in ``sql`` with its resolved
    relation (§5.2) -- the AST-rewrite half of the resolver's two consumers
    (§5; the other is Task 8's ``table_id``-shaped ``FROM`` builder).

    Returns ``(rewritten_sql, merged_params, policied_table_ids)``:

    - ``rewritten_sql`` is executable DuckDB SQL with every policied
      ``exp.Table`` node replaced by ``(<relation_sql>) AS <alias>`` --
      the alias preserved verbatim if the caller wrote one, else the
      table's own name (rule 2). A query that touches no policied table is
      returned byte-for-byte unchanged, not merely semantically unchanged
      -- enforcement is inert until a policy is attached (plan
      Architecture), and this is the only place that promise is upheld for
      every read that reaches this function, not only the ones a caller
      already suspects are policied.
    - ``merged_params`` unions each substituted relation's bind params --
      safe, because identity values are identical for every table in one
      request.
    - ``policied_table_ids`` lists the registry ids substituted, in the
      order first encountered, for the disclosure envelope (Task 11).

    Applied exactly once, non-recursively (rule 1): only ``exp.Table`` nodes
    already present in the ORIGINAL parse of the caller's SQL are ever
    considered -- a policy body's own ``FROM <table>`` never enters this
    scan, because ``relation.relation_sql`` is spliced into the output as
    literal text (a sentinel placed by the AST, swapped for the real text by
    a plain string replace AFTER generation) rather than being re-parsed and
    walked. Re-parsing it would also risk sqlglot normalizing the text it
    round-trips (e.g. ``list_contains`` -> ``ARRAY_CONTAINS``), silently
    drifting from what the admin wrote and what Task 3 validated -- the
    verbatim property Task 5 documents at length.

    Raises ``PolicyNameCollision`` if a caller CTE or subquery/derived-table
    alias shadows a policied table's name (rule 4), and ``PolicyError`` if
    the SQL references a policied table but does not parse (rule 3) -- the
    same reason code ``policied_relation`` itself uses for "failed to
    resolve", per §16's four-reason table (there is no fifth "unparseable"
    reason). Callers map both to an HTTP 400 naming the table (Task 7).
    """
    try:
        statement = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        # Rule 3: unparseable SQL is rejected outright when it touches a
        # policied table (the fail-closed property a TEMP VIEW would have
        # given for free, §5.1) and left alone otherwise, so a query
        # sqlglot merely lags DuckDB on does not regress for everyone else.
        table_id = _scan_unparseable_for_policied_table(sql, principal, resolve)
        if table_id is not None:
            raise PolicyError(table_id)
        return sql, {}, []

    table_nodes = list(statement.find_all(exp.Table))

    # Rule 4: a caller-chosen CTE or subquery/derived-table alias spelled
    # identically to a policied table's name -- checked on BOTH node shapes
    # because sqlglot places a derived-table alias on ``exp.Subquery``, not
    # ``exp.CTE`` (``SELECT * FROM t, (SELECT 1) invoices`` never produces
    # an ``exp.Table`` named "invoices" at all, so this cannot be folded
    # into the table-node loop below).
    shadow_names = {cte.alias for cte in statement.find_all(exp.CTE) if cte.alias}
    shadow_names |= {sub.alias for sub in statement.find_all(exp.Subquery) if sub.alias}
    shadow_names_lower = {name.lower() for name in shadow_names}

    # Rule 5 / §5.3: match by name, case-insensitively (DuckDB folds
    # unquoted identifiers) -- resolve every DISTINCT name once, whether it
    # appears as a real table reference, a shadowing alias, or both (the
    # ``WITH invoices AS (SELECT ... FROM invoices ...)`` idiom is both at
    # once, and must still raise the collision).
    candidate_names: dict[str, str] = {}
    for table in table_nodes:
        if table.name:
            candidate_names.setdefault(table.name.lower(), table.name)
    for name in shadow_names:
        candidate_names.setdefault(name.lower(), name)

    relations: dict[str, PoliciedRelation] = {}
    for lower_name, original_name in candidate_names.items():
        try:
            relation = resolve(original_name, principal)
        except PolicyError:
            # A name that resolves to no registered table is not this
            # function's concern -- a CTE name, an information_schema
            # view, anything sqlglot modeled as `exp.Table` that the
            # registry has never heard of. Swallowing keeps every OTHER
            # query working; rule 5's allowlist is enforced upstream
            # (#1264, the registry gate), not here.
            continue
        if not relation.policied:
            continue
        if lower_name in shadow_names_lower:
            raise PolicyNameCollision(relation.table_id)
        relations[lower_name] = relation

    if not relations:
        return sql, {}, []

    merged_params: dict[str, Any] = {}
    policied_table_ids: list[str] = []
    seen_ids: set[str] = set()
    sentinel_relation_sql: dict[str, str] = {}

    for table in table_nodes:
        relation = relations.get(table.name.lower()) if table.name else None
        if relation is None:
            continue  # rule 1/2: non-policied tables are left untouched

        # Reuse the original alias node (or, unaliased, the table's own
        # name identifier) so quoting/casing survive -- rule 2.
        alias_node = table.args.get("alias")
        alias_node = alias_node.copy() if alias_node is not None else exp.TableAlias(this=table.this.copy())

        sentinel = f"__agnes_policy_{uuid.uuid4().hex}__"
        sentinel_relation_sql[sentinel] = relation.relation_sql
        table.replace(exp.Subquery(this=exp.Var(this=sentinel), alias=alias_node))

        merged_params.update(relation.params)
        if relation.table_id not in seen_ids:
            seen_ids.add(relation.table_id)
            policied_table_ids.append(relation.table_id)

    rewritten_sql = statement.sql(dialect="duckdb")
    for sentinel, relation_sql in sentinel_relation_sql.items():
        rewritten_sql = rewritten_sql.replace(sentinel, relation_sql)

    return rewritten_sql, merged_params, policied_table_ids


# ---------------------------------------------------------------------------
# Task 8 -- shared FROM builder for `table_id`-shaped surfaces (§5).
# `/api/v2/sample`, `/api/v2/scan`'s local branch, and
# `mcp_per_table`'s `_build_select` have no caller SQL tree to substitute a
# policied table into -- unlike the SQL surfaces above (`rewrite_sql`), each
# builds its own `FROM <source>` from scratch: a throwaway `read_parquet(...)`
# in a fresh `:memory:` connection with nothing else attached, or a bare view
# reference on an already-open analytics connection. Neither has the base
# table's registry NAME resolvable as a relation the way the AST rewrite's
# target connection does, so a policy body's own `FROM <name>` would bind to
# nothing (or the wrong thing) without this wrap. These surfaces call
# `policied_relation` directly and hand the result here instead of going
# anywhere near `rewrite_sql`.
# ---------------------------------------------------------------------------


def policied_from_sql(relation: PoliciedRelation, *, table_name: str, source_sql: str) -> str:
    """Wrap a policied relation so its own ``FROM <table_name>`` resolves
    against ``source_sql`` -- the calling surface's OWN read of its physical
    source -- instead of the analytics-catalog master view none of these
    surfaces has open.

    Only call this when ``relation.policied``: the passthrough relation
    (``SELECT * FROM <name>``) names a catalog entry these surfaces don't
    have, so callers keep their pre-existing ``source_sql``-only execution
    path for that case completely untouched -- the inert case must stay
    byte-identical to what ran before this feature existed, not merely
    produce an equivalent result through this function.

    ``source_sql`` MUST be a FROM-able fragment carrying no ``?`` placeholder
    of its own -- a policy body binds named ``$user_email`` / ``$user_id`` /
    ``$user_groups`` parameters (§6.2), and DuckDB 1.5.2 refuses to mix
    positional and named parameters in one statement (verified empirically).
    A caller-controlled value that would otherwise be a bind parameter (a
    parquet path) must already be embedded as an escaped string literal;
    every caller here only ever passes a server-resolved path or a
    ``quote_ident``ed registry/view name, never request-controlled text.

    Returns a parenthesized derived-table expression, directly usable as a
    ``FROM {result}`` target or a ``DESCRIBE {result}`` subject (both
    verified against a parenthesized ``WITH ... SELECT ...`` body).
    """
    if not relation.policied:
        raise ValueError("policied_from_sql() called on a non-policied relation -- use source_sql directly instead")
    return f"(WITH {quote_ident(table_name)} AS (SELECT * FROM {source_sql}) {relation.relation_sql})"


# ---------------------------------------------------------------------------
# Task 9 -- effective schema (§11). `/api/v2/schema` (and, eventually, the
# where-validator) reads the RAW, unfiltered column list today, so a
# policy's `EXCLUDE (col)` is invisible to it -- an analyst sees a column
# that no longer exists on any read surface. `effective_schema` closes that
# gap the same way Task 8's surfaces read rows: resolve, then DESCRIBE the
# wrapped relation via `policied_from_sql`, against the analytics
# connection (none of these callers has a raw parquet path handy the way
# `/api/v2/sample`'s local branch does).
# ---------------------------------------------------------------------------


def effective_schema(table_id: str, principal) -> list[dict] | None:
    """Per-column ``hidden`` markers for a policied table (§11), derived
    from a live ``DESCRIBE`` of the resolved relation rather than the raw,
    unfiltered schema every read surface used before this feature existed.

    Returns ``None`` when the table carries no policy, or when
    ``policied_relation`` resolves ``principal`` to the admin bypass (§12)
    -- either way there is nothing to correct, and the caller
    (``/api/v2/schema``) keeps whatever raw schema it already built. §11
    exists ONLY to stop a policied table's schema surface from advertising
    a column the caller can never actually read; the inert/admin case is
    not this function's concern.

    Runs TWO ``DESCRIBE``s against the analytics connection, both scoped to
    the registry row's own ``.name`` -- what ``policied_relation``'s own
    passthrough calls "the base view" (§5.3), and what a policy body's own
    ``FROM <name>`` resolves against once wrapped by ``policied_from_sql``:
    one over the raw, unfiltered view (the reference column set) and one
    over the policy-wrapped relation (what a caller actually receives). A
    base column absent from the wrapped ``DESCRIBE``'s name set is
    ``hidden`` -- the security-critical marker (§11), and the only one this
    function computes.

    ``masked`` is deliberately NOT attempted. The obvious heuristic --
    comparing type per matching name -- misses the design doc's own
    canonical example (``md5(email) AS email``: VARCHAR in, VARCHAR out,
    same name, no type signal at all), and can't even be applied cleanly
    when a policy body's ``SELECT *`` isn't ALSO excluding the column it
    re-derives: DuckDB accepts that (two columns literally named the same),
    and this function dedupes by keeping the first occurrence rather than
    guessing which one is "the masked one" from a post-hoc DESCRIBE diff.
    Reliable masked-detection needs a static read of the policy body's own
    SELECT-list expressions, not a DESCRIBE diff -- left for a follow-up
    rather than shipping a marker that can be wrong on the exact example
    the design doc leads with.
    """
    from src.db import get_analytics_db_readonly

    relation = policied_relation(table_id, principal)
    if not relation.policied:
        return None

    row = _resolve_table_row(relation.table_id)
    base_ref = quote_ident(row["name"])

    conn = get_analytics_db_readonly()
    try:
        try:
            base_rows = conn.execute(f"DESCRIBE {base_ref}").fetchall()
        except Exception as exc:
            raise PolicyError(relation.table_id) from exc

        wrapped = policied_from_sql(relation, table_name=row["name"], source_sql=base_ref)
        try:
            effective_rows = conn.execute(f"DESCRIBE {wrapped}", relation.params).fetchall()
        except Exception as exc:
            raise PolicyError(relation.table_id) from exc
    finally:
        conn.close()

    # Duplicate output names ARE possible (see the docstring above) --
    # first occurrence wins, deterministically, rather than crashing or
    # silently preferring whichever a dict comprehension iterated last.
    effective_by_name: dict[str, tuple] = {}
    for r in effective_rows:
        effective_by_name.setdefault(r[0], r)

    columns: list[dict] = []
    seen_names: set[str] = set()
    for r in base_rows:
        name = r[0]
        seen_names.add(name)
        hit = effective_by_name.get(name)
        if hit is None:
            columns.append({"name": name, "type": r[1], "nullable": r[2] == "YES", "description": "", "hidden": True})
        else:
            columns.append(
                {"name": hit[0], "type": hit[1], "nullable": hit[2] == "YES", "description": "", "hidden": False}
            )

    # A policy can also ADD a column no base column carries (e.g. pulled in
    # from a `policy_mapping` join, §15) -- keep it, appended after the
    # base-derived list, rather than silently dropping something the
    # policy body deliberately returns.
    for r in effective_rows:
        if r[0] not in seen_names:
            columns.append({"name": r[0], "type": r[1], "nullable": r[2] == "YES", "description": "", "hidden": False})
            seen_names.add(r[0])

    return columns
