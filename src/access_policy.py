"""The access-policy resolver — the single junction every enforcement point
binds against (table access policies design doc §5, §6, §12).

``policied_relation(table_id, principal)`` turns a registered table and the
calling principal into a :class:`PoliciedRelation`: an *unexecuted*,
parenthesizable ``SELECT`` a caller can read from, plus the bind parameters
for it. This module never runs that SQL — each enforcement surface (Task 6's
AST rewrite for SQL surfaces, Task 8's ``FROM``-builder for ``table_id``
surfaces) does, binding ``params`` through the engine's own named-parameter
mechanism, never string interpolation (§6.2).

Two outcomes:

- **Passthrough** (``policied=False``) — the table has no
  ``access_policy_sql``, or it does but the caller is an unrestricted admin
  (§12: admin bypass follows the credential *surface*, not merely group
  membership). ``relation_sql`` is a bare ``SELECT * FROM <base view>``.
- **Policied** (``policied=True``) — a policy is attached and the caller has
  a resolvable identity. ``relation_sql`` is the policy body *verbatim* —
  its ``$name`` placeholders are left as bind markers, never rewritten,
  because DuckDB (and, from Task 10, BigQuery via transpile) both bind named
  parameters natively (§6.2, §7.2). ``params`` carries only the
  ``user_email`` / ``user_id`` / ``user_groups`` keys the policy text
  actually references.

Identity resolution (§12): a plain user dict binds itself; an
``AgentPrincipal`` binds its *owner*'s identity (an agent's declared scope
narrows which tables it reaches, never who it reaches them as — handing it
``$user_groups`` for anyone but the owner would be a privilege escalation);
a ``SessionPrincipal`` (co-drive, several live participants, no single
identity) has nothing to bind and is refused outright rather than guessed.
"""

from __future__ import annotations

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

    Never executes ``relation_sql`` — that is each enforcement surface's job.
    """
    if dialect == "bigquery":
        # Task 10 fills this arm: transpile the policy to BigQuery SQL and
        # bind named ``@param`` parameters (§7.2).
        raise NotImplementedError("policied_relation(dialect='bigquery') is not implemented yet (Task 10)")
    if dialect != "duckdb":
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

    return PoliciedRelation(relation_sql=policy_sql, params=params, policied=True, table_id=resolved_id)


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
