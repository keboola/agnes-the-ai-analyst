"""Allowlists and policy for the connector → orchestrator trust boundary.

The orchestrator reads `_remote_attach` rows that connectors write into their
`extract.duckdb`, then calls `INSTALL`, `LOAD`, and `ATTACH` based on those
values. Treating the connector as adversarial (compromised image, supply-chain,
malicious fork) means the orchestrator picks **what** can be installed and
**which** env vars can be referenced — not the connector. See
`docs/superpowers/plans/2026-04-27-issue-81-trust-boundary.md` for the full
threat model.
"""

from __future__ import annotations

import os
import re

# DuckDB extensions the orchestrator is willing to load on behalf of a
# connector. Built-in extensions go in `_BUILTIN_EXTENSIONS`; community
# extensions go in `_COMMUNITY_EXTENSIONS`. The two sets are disjoint and
# tell the install path whether to issue `INSTALL ... FROM community` or
# only `LOAD`.
_BUILTIN_EXTENSIONS: frozenset[str] = frozenset()  # none in current OSS
_COMMUNITY_EXTENSIONS: frozenset[str] = frozenset({
    "keboola",
    "bigquery",
})

# Env vars whose values may be passed as the auth `TOKEN` in `ATTACH`. The
# default is intentionally tight — every name in the runtime env that is not
# on this list cannot be exfiltrated to a connector-controlled URL.
# Operators add deployment-specific names via AGNES_REMOTE_ATTACH_TOKEN_ENVS.
_DEFAULT_TOKEN_ENVS: frozenset[str] = frozenset({
    "KBC_TOKEN",
    "KBC_STORAGE_TOKEN",
    "KEBOOLA_STORAGE_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",  # path, not a secret value
})

# Names must additionally match this regex (defense against weird input).
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _parse_csv_env(name: str) -> set[str]:
    """Parse a comma-separated env var into a stripped set of non-empty tokens."""
    raw = os.environ.get(name, "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def get_allowed_extensions() -> dict[str, set[str]]:
    """Return the effective extension allowlist as a dict of {kind: set}.

    `kind` is "builtin" or "community" — the install path needs to know
    which to use. Operator override AGNES_REMOTE_ATTACH_EXTENSIONS replaces
    the default community set; built-ins are not configurable from env (a
    typo there would silently disable a working integration with no clear
    failure mode, and built-ins do not pose a supply-chain risk).
    """
    override = _parse_csv_env("AGNES_REMOTE_ATTACH_EXTENSIONS")
    community = override if override else set(_COMMUNITY_EXTENSIONS)
    return {"builtin": set(_BUILTIN_EXTENSIONS), "community": community}


def is_extension_allowed(extension: str) -> bool:
    allow = get_allowed_extensions()
    return extension in allow["builtin"] or extension in allow["community"]


def is_builtin_extension(extension: str) -> bool:
    return extension in get_allowed_extensions()["builtin"]


def get_allowed_token_envs() -> set[str]:
    """Return the effective token-env allowlist.

    Operator override AGNES_REMOTE_ATTACH_TOKEN_ENVS *replaces* the default
    set (so an operator can shrink it as well as expand it). The startup
    code logs the effective set so a typo is visible.
    """
    override = _parse_csv_env("AGNES_REMOTE_ATTACH_TOKEN_ENVS")
    return override if override else set(_DEFAULT_TOKEN_ENVS)


def is_token_env_allowed(token_env: str) -> bool:
    """Return True if ``token_env`` may be read and passed as a TOKEN.

    Two checks: structural (`^[A-Z][A-Z0-9_]{0,63}$`) and membership in the
    allowlist. The structural check refuses things that aren't a valid env
    var name regardless of allowlist contents.
    """
    if not isinstance(token_env, str) or not _ENV_NAME_RE.match(token_env):
        return False
    return token_env in get_allowed_token_envs()


def escape_sql_string_literal(value: str) -> str:
    """Double single-quotes for safe use inside DuckDB single-quoted literals.

    Mirrors `src/db.py:_attach_extracts` (line ~411) so the read-only query
    path and the orchestrator rebuild path use the same escape.
    """
    return value.replace("'", "''")
