"""Connector-catalogued attachment sources.

A connector that downloads attachment binaries to the server declares here
HOW those binaries are catalogued: which table is the catalogue, which
column carries the id, which column carries the on-disk path, and which
directory is the permitted root for those paths. The generic download
surface — ``GET /api/attachments/{source}/{attachment_id}/download``
(``app/api/attachments.py``) and ``agnes attachment get`` — serves any
declared source, so registering a second source (Zendesk, GitHub, …) is one
``_SOURCES`` entry plus a root helper; the route and the CLI command need
no change.

Spec-dict pattern per ``src/connection_specs.py`` / ``app/resource_types.py``:
a module-level dict of frozen dataclasses, no registration protocol. The
``root`` helper defers its connector import so this module stays free of
connector dependencies, and re-derives the path per call so a repointed
connector config takes effect live.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AttachmentSource:
    """One connector's attachment catalogue declaration.

    ``root`` is a callable, not a path: connector config (env-derived data
    dirs) must be read at request time so tests and operators can repoint it
    without re-importing this module.
    """

    source: str
    table: str
    id_column: str
    path_column: str
    root: Callable[[], Path]


def _jira_attachments_root() -> Path:
    # Derive the root the same way JiraService does (`self.data_dir /
    # "attachments"`, connectors/jira/service.py) rather than hardcoding a
    # literal — JIRA_DATA_DIR is operator-settable. `local_path` is the
    # authoritative catalogue column; `hierarchical_path` is a different
    # scheme that does NOT match the on-disk layout — never resolve against it.
    from connectors.jira.service import Config

    return Config.JIRA_DATA_DIR / "attachments"


_SOURCES: dict[str, AttachmentSource] = {
    "jira": AttachmentSource(
        source="jira",
        table="jira_attachments",
        id_column="attachment_id",
        path_column="local_path",
        root=_jira_attachments_root,
    ),
}


def get_attachment_source(source: str) -> AttachmentSource | None:
    """Resolve ``source`` to its declaration, or ``None`` if undeclared."""
    return _SOURCES.get(source)


def list_attachment_sources() -> list[str]:
    """Names of all declared sources, sorted."""
    return sorted(_SOURCES)
