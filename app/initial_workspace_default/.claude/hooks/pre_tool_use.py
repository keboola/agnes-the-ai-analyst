#!/usr/bin/env python3
"""Bundled PreToolUse safety hook.

Reads a JSON payload from stdin per the Claude Code hook spec, returns
a JSON decision object on stdout. Refuses workspace-destructive Bash
commands, hosts outside the Agnes egress allowlist, and prompts for
admin mutations.

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

_ENV_DUMP = ("env", "printenv")
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


def _hosts_in_command(cmd: str) -> list[str]:
    hosts = []
    # schemed URLs
    for u in re.findall(r"https?://([^/\s'\"]+)", cmd):
        hosts.append(u.split(":")[0])
    # bare hosts as curl/wget arguments (scheme-defaulting)
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
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


def _decide(payload: dict) -> dict:
    tool = payload.get("tool_name")
    if tool != "Bash":
        return {"permissionDecision": "allow"}
    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str):
        return {"permissionDecision": "allow"}

    lower = cmd.strip().lower()

    # Destructive ops against persistent workspace dirs
    if any(p in cmd for p in DESTRUCTIVE_PATHS) and any(lower.startswith(pref) for pref in DESTRUCTIVE_PREFIXES):
        return {
            "permissionDecision": "deny",
            "permissionDecisionReason": "Refusing to delete from persistent workspace/snapshots or workspace/scripts. "
            "Use a fresh path or ask the user explicitly.",
        }

    # Env reconnaissance
    if lower in _ENV_DUMP or lower.startswith("cat /proc/self/environ"):
        return {
            "permissionDecision": "deny",
            "permissionDecisionReason": "Refusing to dump the process environment.",
        }

    # Filesystem enumeration outside the workspace
    if any(lower.startswith(p) for p in _ENUM_PREFIXES):
        return {
            "permissionDecision": "deny",
            "permissionDecisionReason": "Refusing to enumerate outside the working directory.",
        }

    # Outbound network — block hosts outside allowlist (schemed OR scheme-less)
    for host in _hosts_in_command(cmd):
        if host not in ALLOWED_HOSTS:
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": f"Outbound network to {host!r} is not in the Agnes egress allowlist. "
                "Allowed: " + ", ".join(sorted(ALLOWED_HOSTS)),
            }

    # Admin mutations need user confirmation
    if any(lower.startswith(p) for p in ADMIN_PROMPT_PREFIXES):
        return {
            "permissionDecision": "ask",
            "permissionDecisionReason": "This command mutates the Agnes access-control layer; confirm before running.",
        }

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
