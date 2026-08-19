"""Install, at process start, the community extensions remote rows will need.

The query path (:func:`src.db._reattach_remote_extensions`) issues ``LOAD``
without ``INSTALL`` on purpose: a read-only query must never reach out to the
network. That is only safe while the extension is already on disk — and DuckDB
installs community extensions under a per-container directory that a container
recreate wipes. So after every restart (a nightly auto-upgrade is enough) the
``LOAD`` fails, the ATTACH is skipped, and every query against a
``query_mode='remote'`` row answers ``Catalog "<alias>" does not exist`` with
nothing in the response to say why. Re-saving the registration fixed it only
because that path runs the connector's own ATTACH, which does INSTALL.

This module moves the network work to startup, where it belongs: walk the
extracts, read each ``_remote_attach``, and INSTALL whatever the rows name.
Built-ins are skipped (they ship with DuckDB; ``INSTALL … FROM community``
would fail), anything outside the extension allowlist is refused — the extract
is connector-supplied input and does not get to choose what we install — and
every failure is reported rather than raised, so a network blip degrades to the
old behaviour instead of blocking the process from starting.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.duckdb_conn import _open_duckdb
from src.orchestrator_security import get_allowed_extensions, is_builtin_extension

logger = logging.getLogger(__name__)


def _install_extension(extension: str) -> None:
    """INSTALL one community extension into this container's extension dir.

    Its own short-lived in-memory connection: installing is a filesystem side
    effect, so a later ``LOAD`` on any connection in this container finds it.
    """
    conn = _open_duckdb(":memory:")
    try:
        conn.execute(f"INSTALL {extension} FROM community")
    finally:
        conn.close()


def prewarm_remote_attach_extensions(extracts_dir: Path) -> dict[str, list[str]]:
    """INSTALL the community extensions named by every ``_remote_attach`` row.

    Returns ``{"installed": [...], "refused": [...], "failed": [...]}`` — a
    report, never an exception: this runs in the startup path and a remote
    source that cannot be prepared must not stop the process from serving the
    local ones.
    """
    result: dict[str, list[str]] = {"installed": [], "refused": [], "failed": []}
    if not extracts_dir.exists():
        return result

    allowed_community = get_allowed_extensions()["community"]
    seen: set[str] = set()

    for ext_dir in sorted(p for p in extracts_dir.iterdir() if p.is_dir()):
        db_file = ext_dir / "extract.duckdb"
        if not db_file.exists():
            continue
        try:
            ro = _open_duckdb(str(db_file), read_only=True)
        except Exception as exc:  # noqa: BLE001 - an unreadable extract is not fatal
            logger.debug("prewarm: cannot open %s: %s", db_file, exc)
            continue
        try:
            has_it = ro.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = '_remote_attach'"
            ).fetchone()
            rows = ro.execute("SELECT extension FROM _remote_attach").fetchall() if has_it else []
        except Exception as exc:  # noqa: BLE001 - legacy extract without the table
            logger.debug("prewarm: no readable _remote_attach in %s: %s", db_file, exc)
            rows = []
        finally:
            try:
                ro.close()
            except Exception:  # noqa: BLE001
                pass

        for (extension,) in rows:
            name = (extension or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if is_builtin_extension(name):
                continue
            if name not in allowed_community:
                logger.warning(
                    "prewarm: extract %s asks for extension %r which is not in the allowlist — refusing to install it",
                    ext_dir.name,
                    name,
                )
                result["refused"].append(name)
                continue
            try:
                _install_extension(name)
            except Exception as exc:  # noqa: BLE001 - never break startup
                logger.warning(
                    "prewarm: INSTALL %s failed (%s) — remote rows using it will fail to ATTACH until this succeeds",
                    name,
                    exc,
                )
                result["failed"].append(name)
                continue
            logger.info("prewarm: installed community extension %r for remote ATTACH", name)
            result["installed"].append(name)

    return result


def prewarm_from_env() -> dict[str, Any]:
    """Convenience entry point for the startup path: resolve the extracts dir."""
    import os

    extracts = Path(os.environ.get("DATA_DIR", "./data")) / "extracts"
    return prewarm_remote_attach_extensions(extracts)
