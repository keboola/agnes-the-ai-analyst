"""Analytics backend selection + DuckLake config resolution.

The analytics query surface (today: the rebuilt-and-swapped
``{DATA_DIR}/analytics/server.duckdb`` file — "legacy") is moving, opt-in,
to a DuckLake catalog ("ducklake" — see
``docs/superpowers/specs/2026-07-16-three-plane-scalable-architecture.md``
§3.4). This module is the seam: it resolves *which* backend is active and
*where* the DuckLake catalog/data live, and nothing else — no DuckDB
sessions are opened here (that is ``src/ducklake_session.py``, a later
task), and no policy is enforced here (that is
``app.startup_guards.validate_deployment``).

Resolution mirrors the env-overrides-yaml shape used throughout
``app/instance_config.py`` and, for the backend-name specifically, the
shared-helper pattern in ``app/coordination/factory.py`` (
``resolve_backend_name`` / ``_coordination_backend``): a small resolver
function here, wrapped by a thin function in ``app/startup_guards.py`` so
tests can monkeypatch the guard's view of it independently of this
module's own cache.

- ``analytics.backend`` (instance.yaml) / ``AGNES_ANALYTICS_BACKEND``
  (env) — ``"legacy"`` (default) or ``"ducklake"``.
- ``ducklake.catalog_dsn`` (instance.yaml) / ``AGNES_DUCKLAKE_CATALOG_DSN``
  (env) — explicit DuckLake catalog target. A Postgres DSN
  (``postgresql://...`` / ``postgres://...``) is required for multi-process
  deployments (enforced by ``app.startup_guards.validate_deployment``, not
  here); left unset, single-process deployments fall back to a DuckDB-file
  catalog under ``{DATA_DIR}/analytics/catalog.ducklake``.
- ``ducklake.data_path`` (instance.yaml) / ``AGNES_DUCKLAKE_DATA_PATH``
  (env) — where DuckLake stores its own data files. Defaults to
  ``{DATA_DIR}/analytics/lake/``.

``legacy`` is the zero-config default — every existing deployment is
unaffected until an operator opts into ``ducklake`` explicitly.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

_VALID_BACKENDS = ("legacy", "ducklake")

_DEFAULT_CATALOG_FILENAME = "catalog.ducklake"
_DEFAULT_LAKE_DIRNAME = "lake"

_lock = threading.Lock()
_backend_cache: str | None = None


def _get_data_dir() -> Path:
    """Mirror ``src/db.py::_get_data_dir`` — same env var, same default,
    so the DuckLake catalog/data defaults land next to
    ``{DATA_DIR}/analytics/server.duckdb`` without a second source of
    truth for where ``DATA_DIR`` resolves."""
    return Path(os.environ.get("DATA_DIR", "./data"))


def resolve_analytics_backend_name() -> str:
    """Effective analytics backend name — env overrides instance.yaml.

    Raises ``ValueError`` for any token other than ``"legacy"`` /
    ``"ducklake"`` so a typo in either the env var or the YAML fails loud
    at resolution time rather than silently falling back.
    """
    from app.instance_config import get_value

    raw = os.environ.get("AGNES_ANALYTICS_BACKEND") or get_value("analytics", "backend", default="legacy")
    value = (raw or "legacy").strip().lower()
    if value not in _VALID_BACKENDS:
        raise ValueError(
            f"invalid analytics backend {value!r} (AGNES_ANALYTICS_BACKEND env var / "
            f"instance.yaml::analytics.backend) — must be one of {', '.join(_VALID_BACKENDS)}"
        )
    return value


def analytics_backend() -> str:
    """Return the process-wide effective analytics backend, resolving (and
    validating) it lazily on first call and caching the result until
    :func:`reset_analytics_backend_cache` — mirrors
    ``app.coordination.factory.coordination``'s singleton-cache shape,
    scaled down to a plain string since this module opens no session of
    its own (see the module docstring)."""
    global _backend_cache
    if _backend_cache is None:
        with _lock:
            if _backend_cache is None:
                _backend_cache = resolve_analytics_backend_name()
    return _backend_cache


def reset_analytics_backend_cache() -> None:
    """Drop the cached backend name so the next :func:`analytics_backend`
    call re-reads env/instance.yaml. Used by tests that flip
    ``AGNES_ANALYTICS_BACKEND`` / the yaml value across cases."""
    global _backend_cache
    with _lock:
        _backend_cache = None


def ducklake_catalog_dsn() -> str:
    """Effective DuckLake catalog target — env overrides instance.yaml.

    An explicit ``ducklake.catalog_dsn`` / ``AGNES_DUCKLAKE_CATALOG_DSN``
    always wins verbatim, whether it is a Postgres DSN
    (``postgresql://...``) or a bare file path — this accessor does not
    judge the value, it only resolves precedence.

    With nothing explicit set, returns the single-process-friendly
    default: a DuckDB-file catalog at
    ``{DATA_DIR}/analytics/catalog.ducklake`` — the literal path form the
    ``ATTACH 'ducklake:<path>' AS lake`` syntax expects for a file-backed
    catalog. That file-backed catalog is hard single-process (DuckDB file
    locking); a multi-process deployment that reaches this fallback
    (i.e. never configured an explicit PG DSN) is a misconfiguration, but
    catching that is deliberately NOT this function's job — see
    ``app.startup_guards.validate_deployment``, which raises
    ``DeploymentConfigError`` naming the missing config instead.
    """
    from app.instance_config import get_value

    raw = os.environ.get("AGNES_DUCKLAKE_CATALOG_DSN") or get_value("ducklake", "catalog_dsn", default="")
    explicit = (raw or "").strip()
    if explicit:
        return explicit
    return str(_get_data_dir() / "analytics" / _DEFAULT_CATALOG_FILENAME)


def ducklake_data_path() -> str:
    """Effective DuckLake data-file directory — env overrides instance.yaml.

    An explicit ``ducklake.data_path`` / ``AGNES_DUCKLAKE_DATA_PATH``
    always wins verbatim. Default: ``{DATA_DIR}/analytics/lake/`` — a
    directory (not a file) since DuckLake owns and manages the files
    underneath it directly.
    """
    from app.instance_config import get_value

    raw = os.environ.get("AGNES_DUCKLAKE_DATA_PATH") or get_value("ducklake", "data_path", default="")
    explicit = (raw or "").strip()
    if explicit:
        return explicit
    return str(_get_data_dir() / "analytics" / _DEFAULT_LAKE_DIRNAME) + "/"
