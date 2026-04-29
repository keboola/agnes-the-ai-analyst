"""Single source of truth for the "Setup a new Claude Code" clipboard payload.

Both the JS-embedded clipboard renderer (`_claude_setup_instructions.jinja`)
and the read-only HTML preview on the dashboard and /install pages consume
these lines. Keep it in Python so there is exactly ONE place that edits.

Placeholders `{server_url}`, `{token}`, `{wheel_filename}`, and `{server_host}`
are substituted at render time. `{wheel_filename}` and `{server_host}` are
pre-substituted server-side via `resolve_lines()`; `{server_url}` and
`{token}` survive into the JS template and are filled in at click time.

`{wheel_filename}` is server-pre-substituted because `uv tool install`
validates the PEP 427 filename *in the URL path* before fetching, so a
stable alias like `agnes.whl` fails with "Must have a version" — we need
the real versioned filename inlined.

`{server_host}` is server-pre-substituted because the `git config` and
`claude plugin marketplace add` lines need the bare host (no scheme), and
the click-time JS only knows the full origin (`{server_url}`).
"""

from __future__ import annotations

# Steps 1-5: install CLI, login, verify, diagnose, skills. Static.
_PROLOGUE_LINES: list[str] = [
    "Set up the Agnes CLI on this machine.",
    "",
    "Server: {server_url}",
    "Personal access token: {token}",
    "(Just generated; treat it as a secret.)",
    "",
    "Run these, in order. If any step fails, paste the exact error back and stop.",
    "",
    "1) Install the CLI:",
    "   uv tool install --force {server_url}/cli/wheel/{wheel_filename}",
    "",
    "   If uv is not installed yet:",
    "     curl -LsSf https://astral.sh/uv/install.sh | sh",
    "",
    "   If `da --version` fails after install because ~/.local/bin is not on PATH:",
    "     export PATH=\"$HOME/.local/bin:$PATH\"",
    "     # persist: append the same line to your ~/.zshrc or ~/.bashrc",
    "",
    "2) Log in (also saves the server URL):",
    "   da auth import-token --token \"{token}\" --server \"{server_url}\"",
    "",
    "3) Verify the login:",
    "   da auth whoami",
    "",
    "4) Run diagnostics:",
    "   da diagnose",
    "",
    "   This should print \"Overall: healthy\" and a list of green checks. If",
    "   anything is yellow/red, paste the full output back.",
    "",
    "5) Skills (ask the user first):",
    "   The CLI ships with reusable markdown skills (setup, connectors,",
    "   corporate-memory, deploy, notifications, security, troubleshoot),",
    "   listable via `da skills list` and readable via `da skills show <name>`.",
    "",
    "   Ask the user verbatim: \"Do you want me to copy the Agnes skills into",
    "   ~/.claude/skills/agnes/ so they are always loaded in Claude Code,",
    "   or should I pull them on-demand via `da skills show <name>` when",
    "   needed?\"",
    "",
    "   If they say copy:",
    "     mkdir -p ~/.claude/skills/agnes",
    "     for s in $(da skills list | awk '{print $1}'); do",
    "       da skills show \"$s\" > ~/.claude/skills/agnes/\"$s\".md",
    "     done",
    "     echo \"Copied skills to ~/.claude/skills/agnes/\"",
]

# Final step: confirm. The leading number is filled in at render time
# (6 when no marketplace block was inserted, 7 when it was) so the prompt
# stays sequentially numbered.
_FINALE_LINES_TEMPLATE: list[str] = [
    "{confirm_step_num}) Confirm:",
    "   Tell me \"Agnes CLI is ready\" and summarize:",
    "   - `da --version` output",
    "   - `da auth whoami` output (email + role)",
    "   - Whether skills were copied or left on-demand",
    "   - The `da diagnose` overall status",
]

# Marketplace name as published by app.marketplace_server.packager.
# Hard-coded here (rather than imported) to keep this module dependency-free
# and trivially testable. If the value ever drifts, the regression test
# below catches it.
_MARKETPLACE_NAME = "agnes"


def _git_check_block() -> list[str]:
    """Step 6 — ensure git is on PATH before the marketplace step clones.

    `claude plugin marketplace add` shells out to `git clone` under the
    hood, so a missing git binary fails step 7 with a confusing error.
    Cross-platform install commands cover the two supported workstation
    OSes:
      - macOS: Homebrew (`brew install git`). The Xcode CLT bundle also
        ships git; we prefer brew because it's non-interactive.
      - Windows: winget (`winget install --id Git.Git -e ...`). Bundled
        with Windows 10 1809+ and Windows 11; non-interactive with --silent.
    """
    return [
        "",
        "6) Make sure git is installed (required for the marketplace clone):",
        "     git --version",
        "",
        "   If that fails (\"command not found\" or similar), install git:",
        "     - macOS:   brew install git",
        "     - Windows: winget install --id Git.Git -e --source winget --silent",
        "",
        "   Then re-run `git --version` to confirm before continuing.",
    ]


def _marketplace_block(
    plugin_install_names: list[str],
    self_signed_tls: bool,
) -> list[str]:
    """Build the optional step-7 block.

    Pre-condition: `plugin_install_names` is non-empty (caller checks).
    """
    lines: list[str] = [
        "",
        "7) Register the Agnes Claude Code marketplace and install plugins:",
    ]
    if self_signed_tls:
        lines.extend([
            "   # Self-signed TLS cert on this Agnes instance — scoped to the host above.",
            "   git config --global http.\"{server_url}/\".sslVerify false",
        ])
    lines.append(
        "   claude plugin marketplace add \"https://x:{token}@{server_host}/marketplace.git/\""
    )
    for name in plugin_install_names:
        lines.append(f"   claude plugin install {name}@{_MARKETPLACE_NAME} --scope project")
    lines.extend([
        "",
        "   These run non-interactively. After they finish, tell the user to /exit",
        "   and run `claude` again so the new plugins load.",
    ])
    return lines


def resolve_lines(
    wheel_filename: str,
    *,
    plugin_install_names: list[str] | None = None,
    self_signed_tls: bool = False,
    server_host: str = "",
) -> list[str]:
    """Return the template lines with server-side placeholders substituted.

    Pre-substitutes `{wheel_filename}` and `{server_host}`. Leaves
    `{server_url}` and `{token}` as placeholders for click-time JS
    substitution (or for `render_setup_instructions()` below).

    When `plugin_install_names` is empty/None, the output matches the
    original 6-step layout (Confirm = step 6). When non-empty, a step-6
    marketplace block is inserted and Confirm becomes step 7.

    `self_signed_tls=True` (typically driven by the server-side
    `AGNES_DEBUG_AUTH` env flag) prepends a host-scoped
    `git config http."<host>/".sslVerify false` line inside the
    marketplace block. No-op when the marketplace block isn't rendered.

    Fallback: callers pass `"agnes.whl"` when no wheel is present on disk.
    The resulting URL (`/cli/wheel/agnes.whl`) will 404 at download time, but
    the instruction text still renders so operators can see the snippet shape
    and diagnose the missing wheel on the server.
    """
    names = list(plugin_install_names or [])
    has_marketplace = bool(names)

    lines = list(_PROLOGUE_LINES)
    if has_marketplace:
        lines.extend(_git_check_block())
        lines.extend(_marketplace_block(names, self_signed_tls))
    confirm_step_num = "8" if has_marketplace else "6"
    lines.append("")
    for fl in _FINALE_LINES_TEMPLATE:
        lines.append(fl.replace("{confirm_step_num}", confirm_step_num))

    return [
        line.replace("{wheel_filename}", wheel_filename).replace("{server_host}", server_host)
        for line in lines
    ]


def render_setup_instructions(
    server_url: str,
    token: str,
    wheel_filename: str = "agnes.whl",
    *,
    plugin_install_names: list[str] | None = None,
    self_signed_tls: bool = False,
    server_host: str = "",
) -> str:
    """Render the setup instructions as a single string.

    Used server-side for tests and any non-JS rendering path. The browser
    clipboard flow uses the JS renderer embedded in the Jinja partial; both
    must produce byte-identical output for a given (server_url, token,
    wheel, plugins, flag, host) tuple.
    """
    lines = resolve_lines(
        wheel_filename,
        plugin_install_names=plugin_install_names,
        self_signed_tls=self_signed_tls,
        server_host=server_host,
    )
    text = "\n".join(lines)
    return text.replace("{server_url}", server_url).replace("{token}", token)
