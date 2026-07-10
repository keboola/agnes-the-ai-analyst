"""Boot-time install of operator-provided wheels for stdio MCP sources.

stdio-transport sources (see ``connectors/mcp/client.py``) spawn their
``command`` as a subprocess inside the app container. The container
filesystem is ephemeral — anything an operator installs by hand
(``docker exec pip install <wheel>``) disappears on the next container
recreate, and recreates are routine now that auto-upgrade tracks
releases. The result was a silently broken source: scheduled
materializes fail with command-not-found until someone reinstalls.

Contract: drop the server's wheel(s) into ``${DATA_DIR}/mcp/wheels/``
(the persistent data volume). At startup the app installs each wheel
with ``pip install --user --no-deps`` (``--no-deps`` so a third-party
wheel can never upgrade/clobber the app's own pinned dependencies — if
the MCP server needs extra deps, drop their wheels alongside) and puts
``~/.local/bin`` on PATH so console scripts resolve when the stdio
client spawns them.

Fail-soft by design: a bad wheel logs an ERROR and is retried next
boot; it never blocks startup. Idempotent via a content-hash marker
(``.installed.json``) so unchanged wheels cost one hash per boot, not a
pip run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_MARKER_NAME = ".installed.json"
_PIP_TIMEOUT_SECONDS = 180


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_user_bin_on_path() -> None:
    """Prepend ``~/.local/bin`` to PATH (idempotent).

    ``pip install --user`` puts console scripts there; the stdio client
    resolves ``command`` through this process's PATH when it spawns the
    MCP subprocess.
    """
    user_bin = str(Path.home() / ".local" / "bin")
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if user_bin not in parts:
        os.environ["PATH"] = user_bin + os.pathsep + os.environ.get("PATH", "")


def install_operator_wheels(data_dir: Optional[Path] = None) -> List[str]:
    """Install wheels from ``<data_dir>/mcp/wheels/``; return installed names.

    Missing directory is a silent no-op (the feature is opt-in by
    creating it). Every failure is per-wheel fail-soft: log + continue,
    and the wheel stays out of the marker so the next boot retries it.
    """
    if data_dir is None:
        from src.db import _get_data_dir

        data_dir = _get_data_dir()

    wheels_dir = Path(data_dir) / "mcp" / "wheels"
    if not wheels_dir.is_dir():
        return []

    marker_path = wheels_dir / _MARKER_NAME
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(marker, dict):
            marker = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        marker = {}

    installed: List[str] = []
    for whl in sorted(wheels_dir.glob("*.whl")):
        try:
            digest = _sha256(whl)
        except OSError as exc:
            logger.error("mcp wheel bootstrap: cannot read %s: %s", whl.name, exc)
            continue
        if marker.get(whl.name) == digest:
            continue
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--user",
            "--no-deps",
            "--no-warn-script-location",
            str(whl),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_PIP_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.error("mcp wheel bootstrap: pip failed for %s: %s", whl.name, exc)
            continue
        if result.returncode != 0:
            logger.error(
                "mcp wheel bootstrap: pip exited %s for %s: %s",
                result.returncode,
                whl.name,
                (result.stderr or "")[-500:],
            )
            continue
        marker[whl.name] = digest
        installed.append(whl.name)
        logger.info("mcp wheel bootstrap: installed %s", whl.name)

    # Drop marker entries for wheels the operator removed, then persist.
    marker = {k: v for k, v in marker.items() if (wheels_dir / k).exists()}
    try:
        marker_path.write_text(json.dumps(marker, indent=0), encoding="utf-8")
    except OSError as exc:
        logger.warning("mcp wheel bootstrap: cannot write marker: %s", exc)

    return installed
