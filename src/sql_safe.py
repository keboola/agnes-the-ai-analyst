"""Shared identifier-validation helpers for SQL identifier safety.

Used wherever code constructs SQL by string interpolation against caller-controlled
identifiers (table/dataset names from registry, alias from _remote_attach, etc.).
The DuckDB BQ extension treats identifiers literally — escaping at the call site
is unsafe; whitelist via regex instead.
"""

import logging
import re

logger = logging.getLogger(__name__)

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

# GCP project IDs: 6-30 chars, lowercase letters / digits / hyphens, must start
# with letter, cannot end with hyphen.
# See https://cloud.google.com/resource-manager/docs/creating-managing-projects
_SAFE_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def is_safe_identifier(name: str) -> bool:
    """Return True if `name` is a safe SQL identifier (alnum+underscore, ≤64 chars, leading non-digit)."""
    if not isinstance(name, str):
        return False
    return bool(_SAFE_IDENTIFIER.match(name))


def validate_identifier(name: str, context: str) -> bool:
    """Validate a SQL identifier; log a warning if rejected. Returns True if safe."""
    if not is_safe_identifier(name):
        logger.warning("Rejected unsafe %s identifier: %r", context, name)
        return False
    return True


def is_safe_project_id(project_id: str) -> bool:
    """Return True if `project_id` matches the GCP project ID grammar.

    GCP rules: 6–30 chars, ``[a-z][a-z0-9-]+[a-z0-9]``. Used to gate
    project_id values from ``instance.yaml`` before they get f-stringed
    into BQ-extension SQL (ATTACH, ``bigquery_query()``, etc.).
    """
    if not isinstance(project_id, str):
        return False
    return bool(_SAFE_PROJECT_ID.match(project_id))


def validate_project_id(project_id: str) -> bool:
    """Validate a GCP project ID; log a warning if rejected. Returns True if safe."""
    if not is_safe_project_id(project_id):
        logger.warning("Rejected unsafe project_id: %r", project_id)
        return False
    return True
