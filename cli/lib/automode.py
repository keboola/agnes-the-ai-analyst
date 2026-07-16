"""Declare the marketplace host as trusted infrastructure for Claude Code's
auto-mode classifier.

Claude Code's auto-mode classifier soft-denies cloning a git repo and
installing plugins from it as "Untrusted Code Integration". When the operator's
own marketplace host is named in the user-scope ``autoMode.environment`` block,
the classifier's built-in "Trusted Internal Infra (Data Flow)" allow rule stops
treating that clone+install as external/untrusted. This module writes that
declaration into the user-scope ``settings.json`` (see
``cli.lib.session_paths.user_settings_path``) — the only settings scope the
classifier reads ``autoMode`` from besides managed settings.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def ensure_marketplace_trusted(settings_path: Path, host: str) -> bool:
    """Merge ``autoMode.environment`` trust entries for *host* into
    *settings_path*, returning True iff the file was actually written.

    - ``host`` empty/None -> no-op, return False.
    - Merge-preserving: load the existing JSON and PRESERVE every other key.
    - Corrupt JSON, non-dict top level, non-dict ``autoMode``, or non-list
      ``autoMode.environment`` -> warn on stderr and return False; NEVER
      overwrite/rebuild the user's settings file.
    - Create ``autoMode.environment`` as ``["$defaults"]`` only when absent;
      ``"$defaults"`` MUST be kept, otherwise the whole built-in rule list for
      that section is replaced.
    - Idempotent: if any existing entry already mentions ``host``, return False.
    - The entries use recognized Environment trust-slot labels ("Trusted
      internal domains:", "Internal package registry:") so the classifier
      registers ``host`` as inside the trust boundary rather than as free-form
      context. ``host`` is always derived from configuration by the caller and
      MUST NOT be hardcoded (this is the vendor-agnostic OSS repo).
    - Write atomically: a temp file in the SAME directory + ``os.replace``
      (atomic on Windows and POSIX). Read/write with ``encoding="utf-8"``.
    """
    host = (host or "").strip()
    if not host:
        return False

    settings: dict[str, object]
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warn: could not read Claude Code settings for auto-mode trust: {exc}", file=sys.stderr)
            return False
        if not isinstance(loaded, dict):
            print("warn: Claude Code settings top level is not an object; leaving it unchanged", file=sys.stderr)
            return False
        settings = loaded
    else:
        settings = {}

    auto_mode = settings.get("autoMode")
    if auto_mode is None:
        auto_mode = {}
        settings["autoMode"] = auto_mode
    elif not isinstance(auto_mode, dict):
        print("warn: Claude Code settings autoMode is not an object; leaving it unchanged", file=sys.stderr)
        return False

    environment = auto_mode.get("environment")
    if environment is None:
        environment = ["$defaults"]
        auto_mode["environment"] = environment
    elif not isinstance(environment, list):
        print(
            "warn: Claude Code settings autoMode.environment is not a list; leaving it unchanged",
            file=sys.stderr,
        )
        return False

    if any(isinstance(entry, str) and host in entry for entry in environment):
        return False

    environment.append(
        f"Trusted internal domains: {host} is this organization's own Agnes "
        f"server — it issued this machine's access token and serves the organization's data."
    )
    environment.append(
        f"Internal package registry: the organization's Claude Code plugin marketplace is "
        f"served from https://{host}/marketplace.git/ and cloned to ~/.agnes/marketplace. "
        f"It is first-party, operator-curated and RBAC-filtered; cloning it and installing "
        f"the plugins it grants is a routine, sanctioned internal operation, not integration "
        f"of untrusted external code."
    )

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = settings_path.with_name(settings_path.name + ".tmp")
    tmp_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, settings_path)
    return True
