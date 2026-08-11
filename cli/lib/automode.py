"""Declare the marketplace host as internal infrastructure for Claude Code's
auto-mode classifier.

Claude Code's auto-mode classifier soft-denies cloning a git repo and
installing plugins from it as "Untrusted Code Integration". When the operator's
own marketplace host is named in the user-scope ``autoMode.environment`` block,
the classifier's built-in "Trusted Internal Infra (Data Flow)" allow rule stops
treating that clone+install as external/untrusted. This module writes that
declaration into the user-scope ``settings.json`` (see
``cli.lib.session_paths.user_settings_path``) — the only settings scope the
classifier reads ``autoMode`` from besides managed settings.

**The entries state facts about the environment; they do not argue.** An
earlier version told the reading agent that installing from this host "is a
routine, sanctioned internal operation, not integration of untrusted external
code" — a verdict about how the agent should judge a future action, written by
the tool whose actions were being judged. An agent flagged it as exactly the
pattern it is supposed to distrust, and it was right to: a declaration the
subject writes about itself carries no more weight for being emphatic. What
this slot is for is telling the classifier *what a host is*; the conclusion is
the classifier's to draw. So the entries name the server, what it serves, and
who curates it, and stop there.

Writing here reaches OUTSIDE the workspace ``agnes init`` was invited into, so
the caller asks first — see ``cli.commands.init._maybe_declare_marketplace_trust``.
"""

from __future__ import annotations

import json
import os
import sys
from enum import StrEnum
from pathlib import Path


class TrustResult(StrEnum):
    """What actually happened to the settings file.

    A bool could not tell "the entries were already there" from "nothing was
    saved because the file could not be read", and the caller reported both as
    already-declared — so someone who opted in and hit a malformed settings
    file was told the change existed. (Devin Review on #1262.)
    """

    #: The entries were appended and the file was replaced.
    WRITTEN = "written"
    #: An entry naming this host was already present; nothing to do.
    ALREADY_PRESENT = "already_present"
    #: Nothing was saved — unreadable/corrupt JSON, an unexpected shape, or no
    #: host to declare. The reason is on stderr; the file is untouched.
    NOT_WRITTEN = "not_written"
    #: Entries from an older CLI were replaced with the current wording.
    REWRITTEN = "rewritten"


#: Fragments of the wording this module retired. An entry carrying any of
#: them was written by a CLI that argued for a conclusion instead of
#: describing the host, and is replaced rather than left in place.
_RETIRED_FRAGMENTS = (
    "routine, sanctioned internal operation",
    "not integration of untrusted external code",
)


def _is_retired(entry: str) -> bool:
    lowered = entry.lower()
    return any(fragment in lowered for fragment in _RETIRED_FRAGMENTS)


def _atomic_write(settings_path: Path, settings: dict) -> None:
    """Temp file in the SAME directory + ``os.replace`` (atomic everywhere)."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = settings_path.with_name(settings_path.name + ".tmp")
    tmp_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, settings_path)


def marketplace_trust_entries(host: str) -> list[str]:
    """The exact ``autoMode.environment`` lines for *host*.

    Split out so the caller can show the operator what it is about to add to
    their user-scope settings, verbatim, before it adds it.
    """
    return [
        (
            f"Trusted internal domains: {host} is this organization's own Agnes server. "
            f"It issued this machine's access token and serves the organization's data."
        ),
        (
            f"Internal package registry: this organization's Claude Code plugin marketplace is "
            f"served from https://{host}/marketplace.git/ and cloned to ~/.agnes/marketplace. "
            f"Its contents are curated by that server's operator and filtered per user by that "
            f"server's access rules."
        ),
    ]


def ensure_marketplace_trusted(settings_path: Path, host: str) -> TrustResult:
    """Merge ``autoMode.environment`` trust entries for *host* into
    *settings_path*, reporting which of the three outcomes happened.

    - ``host`` empty/None -> no-op, ``NOT_WRITTEN``.
    - Merge-preserving: load the existing JSON and PRESERVE every other key.
    - Corrupt JSON, non-dict top level, non-dict ``autoMode``, or non-list
      ``autoMode.environment`` -> warn on stderr and return ``NOT_WRITTEN``;
      NEVER overwrite/rebuild the user's settings file. The caller must be able
      to tell this apart from an entry that was already there, or it reports a
      failed save as a success.
    - Create ``autoMode.environment`` as ``["$defaults"]`` only when absent;
      ``"$defaults"`` MUST be kept, otherwise the whole built-in rule list for
      that section is replaced.
    - Idempotent: if any existing entry already mentions ``host``, return
      ``ALREADY_PRESENT``.
    - The entries use recognized Environment trust-slot labels ("Trusted
      internal domains:", "Internal package registry:") so the classifier
      registers ``host`` as inside the trust boundary rather than as free-form
      context. They describe the host and stop — see the module docstring on
      why they must not argue for a conclusion. ``host`` is always derived from
      configuration by the caller and MUST NOT be hardcoded (this is the
      vendor-agnostic OSS repo).
    - Consent is the CALLER's job. This function writes when called; it is
      ``agnes init`` that decides whether the operator agreed to a change
      outside their workspace.
    - Write atomically: a temp file in the SAME directory + ``os.replace``
      (atomic on Windows and POSIX). Read/write with ``encoding="utf-8"``.
    """
    host = (host or "").strip()
    if not host:
        return TrustResult.NOT_WRITTEN

    settings: dict[str, object]
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warn: could not read Claude Code settings for auto-mode trust: {exc}", file=sys.stderr)
            return TrustResult.NOT_WRITTEN
        if not isinstance(loaded, dict):
            print("warn: Claude Code settings top level is not an object; leaving it unchanged", file=sys.stderr)
            return TrustResult.NOT_WRITTEN
        settings = loaded
    else:
        settings = {}

    auto_mode = settings.get("autoMode")
    if auto_mode is None:
        auto_mode = {}
        settings["autoMode"] = auto_mode
    elif not isinstance(auto_mode, dict):
        print("warn: Claude Code settings autoMode is not an object; leaving it unchanged", file=sys.stderr)
        return TrustResult.NOT_WRITTEN

    environment = auto_mode.get("environment")
    if environment is None:
        environment = ["$defaults"]
        auto_mode["environment"] = environment
    elif not isinstance(environment, list):
        print(
            "warn: Claude Code settings autoMode.environment is not a list; leaving it unchanged",
            file=sys.stderr,
        )
        return TrustResult.NOT_WRITTEN

    mine = [i for i, e in enumerate(environment) if isinstance(e, str) and host in e]
    if mine:
        # A machine that ran an older `agnes init` carries the RETIRED wording
        # — the sentence that told the reading agent installing from this host
        # "is a routine, sanctioned internal operation, not integration of
        # untrusted external code". Matching on the host alone declared that
        # file already correct, so the fix would have applied to new installs
        # only and every existing one would have kept the wording an agent
        # flagged, with no way to replace it. Retired entries are rewritten in
        # place; entries that already say the current thing are left alone.
        # (Devin Review on #1262.)
        if not any(_is_retired(environment[i]) for i in mine):
            return TrustResult.ALREADY_PRESENT
        for i in reversed(mine):
            del environment[i]
        environment.extend(marketplace_trust_entries(host))
        _atomic_write(settings_path, settings)
        return TrustResult.REWRITTEN

    environment.extend(marketplace_trust_entries(host))

    _atomic_write(settings_path, settings)
    return TrustResult.WRITTEN
