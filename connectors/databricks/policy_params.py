"""Bind an access policy's identity values through the Databricks Statement
Execution API's own parameter mechanism (table access policies design §6.2,
§7.1).

``policied_relation(..., dialect="databricks")`` hands back a policy body whose
``$name`` placeholders sqlglot already rendered as ``:name`` — the marker
syntax the API binds via its ``parameters`` request field. For two of the three
identity variables that is the whole story: ``$user_email`` and ``$user_id``
are strings, so they bind as scalar ``STRING`` parameters and the SQL text
never sees their values.

``$user_groups`` is a list, and the API binds **scalars only** — there is no
array parameter type. That leaves three options, and only one of them is
honest:

1. Refuse policies that reference ``$user_groups`` on this engine. Group
   membership is *the* canonical policy shape, so this would ship a feature
   that denies the common case.
2. Interpolate the group names into the SQL text. That breaks §6.2's whole
   guarantee — the one control standing between a group literally named
   ``'); DROP`` and the warehouse — for the single value that is hardest to
   constrain (group names come from a Workspace sync, §6.3).
3. Keep every value bound and move the *shape* into the SQL: replace the
   array-valued ``:user_groups`` marker with ``ARRAY(:p0, :p1, …)``, an array
   *expression* built from scalar markers. The values still travel as request
   fields; only the arity is visible in the text.

This module does (3). The substitution is AST-level, not textual: a regex over
``:user_groups`` would also rewrite the same characters inside a string
literal, and a policy body is exactly the kind of SQL that contains literals.

Because the substituted node is an array-valued *expression*, it works
wherever the marker stood — ``ARRAY_CONTAINS(:user_groups, col)`` is the
idiom §6.5 recommends, but a policy using ``:user_groups`` any other way
(``SIZE()``, an ``EXPLODE``, a join against a mapping table) keeps working
without this module knowing the surrounding shape.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import sqlglot
from sqlglot import exp

#: Prefix for the generated scalar markers that reconstruct ``$user_groups``.
#: Not a user-authored name: the save-time validator restricts policy variables
#: to the three known identities (``src.access_policy_validate``), so no
#: authored body can collide with this.
_GROUP_MARKER_PREFIX = "agnes_policy_group_"

#: Every identity value binds as STRING. ``user_id`` is a string in the users
#: table, ``user_email`` obviously so, and group names likewise — there is no
#: numeric identity variable to widen this for.
_PARAM_TYPE = "STRING"


class DatabricksPolicyBindingError(Exception):
    """The policy body could not be prepared for parameter binding.

    Callers turn this into the same table-scoped ``PolicyError`` every other
    resolution failure raises (§16, §17) — never into a message quoting the
    policy body or the engine's own error.
    """


def _scalar_parameter(name: str, value: Any) -> Dict[str, Any]:
    return {"name": name, "value": "" if value is None else str(value), "type": _PARAM_TYPE}


def bind_policy_parameters(relation_sql: str, params: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    """Return ``(sql, parameters)`` ready for the Statement Execution API.

    ``relation_sql`` is a Databricks-dialect policy body from
    ``policied_relation(..., dialect="databricks")``; ``params`` is that
    relation's ``.params`` — the subset of ``user_email`` / ``user_id`` /
    ``user_groups`` the body actually references.

    Scalars pass through untouched (their ``:name`` markers already match).
    A list value has its marker replaced by ``ARRAY(...)`` over generated
    scalar markers, or by a typed empty array when the caller belongs to no
    groups — ``ARRAY()`` alone is ``ARRAY<VOID>`` on Databricks and would fail
    to type-check against a string column, so the empty case is cast
    explicitly. An empty group list is a legitimate, security-relevant state
    (it must match nothing), never an error.
    """
    array_params = {k: list(v) for k, v in params.items() if isinstance(v, (list, tuple, set))}
    parameters: List[Dict[str, Any]] = [
        _scalar_parameter(name, value) for name, value in params.items() if name not in array_params
    ]

    if not array_params:
        return relation_sql, parameters

    try:
        tree = sqlglot.parse_one(relation_sql, dialect="databricks")
    except Exception as exc:  # noqa: BLE001 — any parse failure denies
        raise DatabricksPolicyBindingError("policy body could not be parsed for parameter binding") from exc
    if tree is None:
        raise DatabricksPolicyBindingError("policy body parsed to nothing")

    generated: Dict[str, List[str]] = {}
    for var_name, values in array_params.items():
        generated[var_name] = [f"{_GROUP_MARKER_PREFIX}{var_name}_{i}" for i in range(len(values))]
        parameters.extend(_scalar_parameter(marker, v) for marker, v in zip(generated[var_name], values))

    substituted = False

    def _replace(node: exp.Expression) -> exp.Expression:
        nonlocal substituted
        if isinstance(node, exp.Placeholder) and node.name in array_params:
            substituted = True
            markers = generated[node.name]
            if not markers:
                return exp.cast(exp.func("array", dialect="databricks"), "ARRAY<STRING>", dialect="databricks")
            return exp.func("array", *[exp.Placeholder(this=m) for m in markers], dialect="databricks")
        return node

    rewritten = tree.transform(_replace)

    if not substituted:
        # `params` only ever carries variables `_referenced_variables` found in
        # the body, so a marker that survives transpilation must be findable
        # here. If it is not, the body no longer matches what we are about to
        # bind — deny rather than execute a statement with an unbound `:name`
        # (which Databricks would reject anyway) or, worse, one whose group
        # filter silently vanished.
        raise DatabricksPolicyBindingError("array-valued policy variable not found in the transpiled body")

    return rewritten.sql(dialect="databricks"), parameters
