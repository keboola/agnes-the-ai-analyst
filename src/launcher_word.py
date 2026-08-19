"""The one word an analyst types to open their workspace.

Shared by the two sides that must agree on it: ``cli/lib/shortcut.py``, which
installs the launcher script, and the server's install guide, which tells the
analyst what to type. They used to derive it separately — the CLI by stripping
the folder name to alphanumerics and dodging name collisions, the page by
lowercasing the folder name — so any workspace whose name was not already
alphanumeric, or whose word collided, was documented under a name that did not
exist on disk.

Lives in ``src/`` because ``cli/`` imports from ``src/`` and never the reverse.
"""

from __future__ import annotations

import re

SHELL_BUILTINS: frozenset[str] = frozenset(
    {
        "alias",
        "bg",
        "break",
        "builtin",
        "cd",
        "command",
        "continue",
        "echo",
        "eval",
        "exec",
        "exit",
        "export",
        "false",
        "fc",
        "fg",
        "getopts",
        "hash",
        "jobs",
        "kill",
        "let",
        "local",
        "logout",
        "printf",
        "pwd",
        "read",
        "readonly",
        "return",
        "set",
        "shift",
        "source",
        "test",
        "times",
        "trap",
        "true",
        "type",
        "ulimit",
        "umask",
        "unalias",
        "unset",
        "wait",
    }
)

RESERVED_COMMANDS: frozenset[str] = frozenset(
    {
        "agnes",
        "claude",
    }
)


def sanitized_word(workspace_name: str) -> str:
    """Workspace folder name stripped to lowercase alphanumerics.

    This raw word — before any collision suffix — is also the name the IWT
    contract uses for the ``bin/<word>`` launcher script.
    """
    return re.sub(r"[^A-Za-z0-9]", "", workspace_name).lower()


def launcher_word(workspace_name: str) -> str:
    """The installed launcher's name: sanitized, plus a collision suffix.

    Appends ``"ai"`` when the sanitized word would shadow a POSIX shell
    built-in (workspace ``"Test"`` -> ``"testai"``) or a command the toolchain
    depends on (workspace ``"Agnes"`` -> ``"agnesai"``, #783). Returns ``""``
    when the name has no alphanumeric characters at all.

    Deterministic, so the server can name the same word the CLI will install.
    The CLI applies one further check this cannot express — a word already
    taken by some other executable on the *client's* PATH — and skips the
    shortcut with a warning when even the suffixed word is taken.
    """
    word = sanitized_word(workspace_name)
    if word in SHELL_BUILTINS or word in RESERVED_COMMANDS:
        word = word + "ai"
    return word
