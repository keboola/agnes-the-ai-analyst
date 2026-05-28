"""Heuristic classifier — given an MCP tool's schema, suggest registration mode.

Rules (in priority order):

1. ``mutating=skip`` if the tool name starts with create/update/delete/remove/set,
   OR if its description hints at writes ("creates", "updates", "deletes").
   Skipped in read-only mode (the default), surfaced as a suggestion in
   write mode (out of scope for v1 — see RFC #461 §3 Policy Engine).

2. ``passthrough`` if the inputSchema has any ``required`` parameter.
   Required parameters mean "look up something specific" — point lookups,
   filtered queries, real-time calls. Materializing these wholesale doesn't
   make sense; they only return data when called with concrete args.

3. ``materialize`` otherwise (no required params = "give me everything"
   bulk-list tool, e.g. listAccounts, dumpInvoices, listTickets).

The classifier is a SUGGESTION, not a decision — the admin always reviews
the proposal and can override every choice before writing to ``tool_registry``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Naming heuristics for mutation detection.
_MUTATING_PREFIXES = ("create", "update", "delete", "remove", "set", "add", "patch", "put", "insert", "edit", "modify")
_MUTATING_HINTS = ("creates ", "updates ", "deletes ", "removes ", "modifies ", "writes ", "mutates ", "side-effect")


@dataclass
class ToolProposal:
    name: str
    suggested_mode: str          # "materialize" | "passthrough" | "skip"
    reason: str
    description: Optional[str]
    input_schema: Optional[Dict[str, Any]]


def _is_mutating(name: str, description: Optional[str]) -> bool:
    n = name.lower()
    for prefix in _MUTATING_PREFIXES:
        if n.startswith(prefix):
            return True
    if description:
        d = description.lower()
        for hint in _MUTATING_HINTS:
            if hint in d:
                return True
    return False


def _has_required_params(input_schema: Optional[Dict[str, Any]]) -> bool:
    if not input_schema:
        return False
    required = input_schema.get("required")
    return bool(required) and isinstance(required, list) and len(required) > 0


def classify(name: str, description: Optional[str], input_schema: Optional[Dict[str, Any]]) -> ToolProposal:
    if _is_mutating(name, description):
        return ToolProposal(
            name=name,
            suggested_mode="skip",
            reason="mutating-named or write-described tool; read-only mode default",
            description=description,
            input_schema=input_schema,
        )
    if _has_required_params(input_schema):
        return ToolProposal(
            name=name,
            suggested_mode="passthrough",
            reason="has required parameters → parameterized lookup, live call",
            description=description,
            input_schema=input_schema,
        )
    return ToolProposal(
        name=name,
        suggested_mode="materialize",
        reason="no required params → bulk-list candidate, schedulable",
        description=description,
        input_schema=input_schema,
    )


def classify_all(tools: List[Any]) -> List[ToolProposal]:
    """Classify a list of ``ToolInfo`` (from ``client.list_tools``)."""
    out: List[ToolProposal] = []
    for t in tools:
        out.append(classify(t.name, t.description, t.input_schema))
    return out
