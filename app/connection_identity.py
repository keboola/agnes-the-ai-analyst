"""Which ``data_source.<source>`` leaves decide *where* a registration points.

A registered table stores its upstream coordinates relative to the instance's
one connection per source: a Snowflake row keeps ``bucket='GOLD'`` and resolves
the *database* from ``data_source.snowflake.database`` at extract-build time,
baking the result into ``_remote_attach.url`` and into the remote view's
``sf."GOLD"."T"``. Repoint that connection and every existing row of the source
keeps naming a database/schema pair the new upstream does not have: a
``query_mode='remote'`` read fails at bind time (``schema "GOLD" does not
exist``), a materialized sync fails at COPY time, and ``last_sync_status`` goes
on reporting the last *successful* run — so the instance looks healthy while
its data is stale.

The leaves listed here are the ones that change *which upstream* answers, as
opposed to the tuning knobs that live in the same block (scan caps, timeouts,
pool sizes) and break nothing. Changing a credential-pointer leaf
(``token_env``, ``private_key_env``) counts: it swaps which secret is
presented, and therefore which grants apply.

Adding a connector that reads ``data_source.<name>.*``? Add its identity leaves
here — ``tests/test_admin_server_config_connection_guard.py`` fails otherwise.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Per source: the leaves under ``data_source.<source>`` that identify the
# upstream. Everything not listed is treated as tuning and saves unguarded.
CONNECTION_IDENTITY_LEAVES: Dict[str, frozenset[str]] = {
    "snowflake": frozenset(
        {
            "account",
            "user",
            "database",
            "warehouse",
            "role",
            "auth_type",
            "token_env",
            "private_key_env",
        }
    ),
    "bigquery": frozenset({"project", "billing_project", "location"}),
    "databricks": frozenset({"host", "warehouse_id", "catalog", "token_env"}),
    "keboola": frozenset({"stack_url", "token_env"}),
}


def identity_changes(
    source: str,
    before: Dict[str, Any] | None,
    after: Dict[str, Any] | None,
) -> List[Dict[str, Any]]:
    """Return the identity leaves whose value differs between two config blocks.

    Each entry is ``{"field", "before", "after"}``, ordered by field name so the
    refusal message is stable. A leaf that appears only in ``after`` counts as a
    change: going from "no role" to a role decides which grants apply, exactly
    the class of edit this guard exists to surface.
    """
    leaves = CONNECTION_IDENTITY_LEAVES.get(source)
    if not leaves:
        return []

    before = before or {}
    after = after or {}
    changes: List[Dict[str, Any]] = []
    for field in sorted(leaves):
        if field not in after:
            # Absent from the patch-merged block means the operator did not
            # touch it, so there is nothing to warn about.
            continue
        old, new = before.get(field), after[field]
        if old != new:
            changes.append({"field": field, "before": old, "after": new})
    return changes
