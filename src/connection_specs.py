"""Per-type validation for source_connections.config (spec 2026-06-12 §3.1).

Mirrors the ResourceTypeSpec pattern in app/resource_types.py: adding a
source type registers a spec here — no DB migration. Validation runs at
registration time (admin API / seeding), so consumers downstream never
see a denormalized config (e.g. a trailing-slash stack URL).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict


@dataclass(frozen=True)
class ConnectionSpec:
    source_type: str
    validate: Callable[[Dict[str, Any]], Dict[str, Any]]  # returns normalized config


def _validate_keboola(config: Dict[str, Any]) -> Dict[str, Any]:
    url = str(config.get("stack_url") or "").strip().rstrip("/")
    if not url:
        raise ValueError("keboola connection requires config.stack_url")
    if not url.startswith("https://"):
        raise ValueError(f"stack_url must be https://, got: {url!r}")
    return {**config, "stack_url": url}


def _validate_bigquery(config: Dict[str, Any]) -> Dict[str, Any]:
    project = str(config.get("project") or "").strip()
    if not project:
        raise ValueError("bigquery connection requires config.project")
    out = {**config, "project": project}
    out.setdefault("location", "us")
    return out


_SPECS: Dict[str, ConnectionSpec] = {
    "keboola": ConnectionSpec("keboola", _validate_keboola),
    "bigquery": ConnectionSpec("bigquery", _validate_bigquery),
}


def validate_connection_config(source_type: str, config: Dict[str, Any]) -> Dict[str, Any]:
    spec = _SPECS.get(source_type)
    if spec is None:
        raise ValueError(f"unknown source_type: {source_type!r}")
    return spec.validate(config)
