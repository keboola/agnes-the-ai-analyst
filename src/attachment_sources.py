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
    # Column carrying the file's ORIGINAL name (as the upstream system shows
    # it), when the on-disk name differs — Jira stores files as
    # "<id>_<filename>", so serving the path basename would hand the user an
    # id-prefixed name. None = fall back to the path basename.
    filename_column: str | None = None


def _jira_attachments_root() -> Path:
    # Derive the root the same way JiraService does (`self.data_dir /
    # "attachments"`, connectors/jira/service.py) rather than hardcoding a
    # literal — JIRA_DATA_DIR is operator-settable. `local_path` is the
    # authoritative catalogue column; `hierarchical_path` is a different
    # scheme that does NOT match the on-disk layout — never resolve against it.
    from connectors.jira.service import Config

    return Config.JIRA_DATA_DIR / "attachments"


_SOURCES: dict[str, AttachmentSource] = {
    # The catalogue is the connector's "attachments" table VERBATIM: the
    # orchestrator names master views straight off _meta.table_name, and the
    # Jira connector emits its tables UNPREFIXED (JIRA_TABLES in
    # connectors/jira/extract_init.py) — "jira_attachments" exists only in
    # the legacy Data Broker path and resolves nowhere on an Agnes server.
    # tests/test_attachment_download.py pins this declaration against
    # JIRA_TABLES and the transform's real output columns.
    "jira": AttachmentSource(
        source="jira",
        table="attachments",
        id_column="attachment_id",
        path_column="local_path",
        root=_jira_attachments_root,
        filename_column="filename",
    ),
}


def get_attachment_source(source: str) -> AttachmentSource | None:
    """Resolve ``source`` to its declaration, or ``None`` if undeclared."""
    return _SOURCES.get(source)


def list_attachment_sources() -> list[str]:
    """Names of all declared sources, sorted."""
    return sorted(_SOURCES)
