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

## Cross-platform trust strategy (when `ca_pem` is supplied)

The trust block (step 0) is the load-bearing piece. Three things bit us in
practice and the design here exists to dodge each one:

1. **rustls rejects the Agnes leaf cert as `CaUsedAsEndEntity`.** The Agnes
   server's self-signed cert is simultaneously its own CA (basicConstraints
   `CA:TRUE`) AND the leaf served on the wire — a setup OpenSSL tolerates
   but webpki/rustls strictly refuses. So `uv tool install <https-url>`
   never works against the Agnes wheel endpoint. We download the wheel via
   curl first (curl uses OpenSSL, accepts the cert), then `uv tool install
   --native-tls --force <local-file>` lets rustls reuse the OS trust store
   for PyPI dependency resolution. No HTTPS hop through rustls touches the
   Agnes host.

2. **`SSL_CERT_FILE` REPLACES the trust store, it doesn't append.** Pointing
   it at `~/.agnes/ca.pem` alone breaks every Python tool that needs to
   reach a public host (PyPI, GitHub) — `da` works fine because it only
   talks to Agnes, but `uv run --with <pkg>` immediately fails with
   `UnknownIssuer`. We materialize a combined bundle at
   `~/.agnes/ca-bundle.pem` (system roots + Agnes CA) and point all
   `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `GIT_SSL_CAINFO` at it.
   `NODE_EXTRA_CA_CERTS` keeps pointing at just `ca.pem` because Node's
   semantics is *additive* (appends to bundled roots), so a single-cert
   file is correct there.

3. **Bun-compiled `claude` (Windows + macOS distributions) ignores every
   CA env var AND the OS trust store for marketplace HTTPS.** On macOS
   arm64 the binary at `~/.local/bin/claude` is a Mach-O with a `__BUN`
   segment (single-file `bun build --compile`); on Windows claude.exe is
   the same shape. `strings` shows the binary recognizes
   `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`,
   `CURL_CA_BUNDLE` (including a "NODE_EXTRA_CA_CERTS detected" log
   string), but in practice the values never reach the TLS context — a
   known limitation of Bun's compiled-binary HTTPS path. Registering the
   cert in the OS trust store (Windows: `certutil -user -addstore Root`;
   macOS: `security add-trusted-cert`; Linux: `update-ca-certificates` /
   `update-ca-trust`) doesn't fix it on Windows or macOS either — the
   binary's bundled CA list isn't refreshable from the OS store.

   So the marketplace step always uses system `git clone` regardless of
   platform — system git honors `GIT_SSL_CAINFO` from the combined bundle
   in step 0(d). We tried having Linux attempt direct HTTPS first (where
   node-based claude DOES respect `NODE_EXTRA_CA_CERTS`), but `claude
   plugin marketplace add <https-url>` is broken end-to-end on every
   distribution: it does succeed at downloading the marketplace.json, but
   stores it as a single file. The plugin entries' `source: "./plugins/<name>"`
   paths are then resolved as local filesystem paths against that file's
   parent dir — and the plugin tree obviously isn't there. Only the clone
   path produces a real directory tree that `plugin install` can read.

   The OS trust-store registration in (c) is still done on all three
   platforms because it's needed for *non-claude* native tools — e.g.
   the system git fetch path itself (Schannel on Windows, Security
   framework on macOS) trusts via the OS store, not via env vars.

   Marketplace refresh: after the initial clone, `agnes refresh-marketplace`
   incrementally `git pull`s against the same clone and runs `claude plugin
   marketplace update agnes`. Credentials are injected per-pull via a
   one-shot git credential helper (PAT from `~/.config/agnes/token.json`)
   so the cloned repo's `origin` URL stays PAT-free at rest. The
   SessionStart hook (installed by `agnes init`) runs a detached `agnes
   update` on every Claude Code session, which reconciles the marketplace
   (among other steps) so changes server-side propagate automatically.

## Step ordering

The numbered steps are arranged so that:
  - All installation work (CLI, plugins) happens first, in one go.
  - `agnes init` is mandatory — it bundles auth, workspace bootstrap,
    CLAUDE.md fetch, and Claude Code SessionStart/End hooks into one
    non-interactive call. Replaces the old `agnes auth import-token` +
    `agnes auth whoami` pair.
  - `agnes diagnose` runs late so it doubles as a final smoke test after
    plugins are in place, instead of gating them. It is also the last
    step before Confirm — the whole prompt is non-interactive, no
    decision questions for the user.

Layout:
  0  TLS trust block (only when ca_pem is supplied)
  1  Install CLI
  2  agnes init (auth + workspace bootstrap)
  3  agnes catalog (smoke verify)
  4  Pre-flight: git + claude
  5  Marketplace (always, even with empty served stack)
  6  MCP servers (Atlassian Remote MCP)
  7  Diagnose
  8  Confirm

The combined-bundle source uses a fallback chain so the prompt still works
on machines without the system Python `certifi`: we try (a) `python3 -c
'import certifi'`, (b) the platform's curl/openssl bundle path, (c)
`uv run --with certifi` as a network last-resort. The user explicitly
permitted that fallback chain — it's not improvising-around-a-TLS-error.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # Avoid circular import at module load — connectors_manifest imports
    # from src.initial_workspace which is imported transitively from many
    # app modules. The forward reference under TYPE_CHECKING keeps the
    # type annotation expressive without paying the import cost.
    from src.connectors_manifest import ConnectorEntry  # noqa: F401

logger = logging.getLogger(__name__)

# Marketplace name as published by app.marketplace_server.packager.
# Hard-coded here (rather than imported) to keep this module dependency-free
# and trivially testable. If the value ever drifts, the regression test
# below catches it.
_MARKETPLACE_NAME = "agnes"


def _tls_trust_block(ca_pem: str) -> list[str]:
    """Step 0 — cross-platform TLS trust bootstrap for the Agnes server.

    Emitted only when the server has a non-publicly-trusted cert. Does four
    things in a single numbered block (see module docstring for the full
    rationale):

      (a) Detect platform (Windows Git Bash / macOS / Linux) and pick the
          shell rc file that the user's login shell actually reads.
          `$SHELL`-driven, NOT existence-of-rc-driven — old setups put a
          legacy `.bashrc` next to a default zsh shell on macOS, and the
          `[ -f .bashrc ]` heuristic silently writes to the wrong file.
      (b) Write the cert PEM to `~/.agnes/ca.pem` via single-quoted heredoc
          (so `$` / backtick chars in real-world certs never shell-expand).
      (c) Register the cert in the OS trust store (so native binaries that
          bypass our env vars — claude.exe, system git's Schannel backend,
          Python apps using `truststore` — still trust the host).
          Idempotent: re-running just re-affirms the entry.
      (d) Build a *combined* CA bundle (system roots + Agnes CA) at
          `~/.agnes/ca-bundle.pem`, with a fallback chain for the system
          roots source. Persist `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` /
          `GIT_SSL_CAINFO` pointing at the bundle, plus
          `NODE_EXTRA_CA_CERTS` pointing at just `ca.pem` (Node
          appends-not-replaces). Persistence is idempotent via a grep
          guard for the `AGNES_CA_PEM_TRUST` marker.
    """
    pem = ca_pem.strip()
    lines: list[str] = [
        "0) Trust the Agnes TLS certificate — cross-platform setup for a self-signed / private-CA host.",
        "",
        "   (a) Detect platform + pick the shell rc file your login shell actually reads.",
        "       Driven by $SHELL + uname (NOT by which rc files happen to exist on disk).",
        "",
        '       case "$(uname -s)" in',
        "         Darwin)               PLATFORM=macos ;;",
        "         Linux)                PLATFORM=linux ;;",
        "         MINGW*|MSYS*|CYGWIN*) PLATFORM=windows ;;",
        '         *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;',
        "       esac",
        '       SHELL_NAME="$(basename "${SHELL:-bash}")"',
        '       case "${SHELL_NAME}:${PLATFORM}" in',
        '         zsh:*)                   RC="$HOME/.zshrc" ;;',
        '         bash:macos)              RC="$HOME/.bash_profile" ;;',
        '         bash:windows|bash:linux) RC="$HOME/.bashrc" ;;',
        '         *)                       RC="$HOME/.profile" ;;',
        "       esac",
        '       echo "Platform: $PLATFORM, shell: $SHELL_NAME, rc: $RC"',
        "",
        "   (b) Write the cert (single-quoted heredoc so $/backticks in the body don't expand):",
        "",
        "       mkdir -p ~/.agnes",
        "       cat > ~/.agnes/ca.pem <<'AGNES_CA_PEM'",
    ]
    # PEM body is flush-left: `<<'DELIM'` heredocs preserve leading whitespace,
    # and any indent inside the cert breaks `openssl x509` / Python ssl parsers.
    lines.extend(pem.splitlines())
    lines.extend(
        [
            "AGNES_CA_PEM",
            "",
            "   (c) Register the cert in the OS trust store. Native binaries (claude.exe,",
            "       system git's Schannel/Security.framework backends) read the OS store",
            "       and ignore our env vars — without this, the later marketplace `git",
            "       clone` (when plugins are configured) and any user-side git/native",
            "       tooling against the Agnes host will fail.",
            "       No admin rights needed (user-store only). Idempotent.",
            "",
            '       case "$PLATFORM" in',
            "         windows)",
            '           WIN_CA="$(cygpath -w ~/.agnes/ca.pem)"',
            '           certutil.exe -user -addstore "Root" "$WIN_CA"',
            "           ;;",
            "         macos)",
            "           # Will prompt once for the keychain password.",
            "           security add-trusted-cert -r trustRoot \\",
            '             -k "$HOME/Library/Keychains/login.keychain-db" \\',
            "             ~/.agnes/ca.pem",
            "           ;;",
            "         linux)",
            "           if command -v update-ca-certificates >/dev/null 2>&1; then",
            "             sudo cp ~/.agnes/ca.pem /usr/local/share/ca-certificates/agnes.crt",
            "             sudo update-ca-certificates",
            "           elif command -v update-ca-trust >/dev/null 2>&1; then",
            "             sudo cp ~/.agnes/ca.pem /etc/pki/ca-trust/source/anchors/agnes.crt",
            "             sudo update-ca-trust",
            "           else",
            '             echo "WARN: install ~/.agnes/ca.pem into your distro\'s trust store manually" >&2',
            "           fi",
            "           ;;",
            "       esac",
            "",
            "   (d) Build a COMBINED CA bundle (system roots + Agnes CA) for Python tools",
            "       and curl. SSL_CERT_FILE *replaces* the trust store, so pointing it at",
            "       the Agnes CA alone would break public hosts (PyPI etc.). Source the",
            "       system roots from a fallback chain — the first source that produces",
            "       a non-empty, existing path wins. Don't abort on the first miss; that's",
            "       what the chain is for.",
            "",
            "       CERTIFI_PATH=\"$(python3 -c 'import certifi; print(certifi.where())' 2>/dev/null || true)\"",
            '       [ -z "$CERTIFI_PATH" ] && CERTIFI_PATH="$(python -c \'import certifi; print(certifi.where())\' 2>/dev/null || true)"',
            '       if [ -z "$CERTIFI_PATH" ]; then',
            "         for p in /mingw64/ssl/certs/ca-bundle.crt /usr/ssl/certs/ca-bundle.crt \\",
            "                  /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt \\",
            "                  /etc/ssl/cert.pem; do",
            '           [ -f "$p" ] && CERTIFI_PATH="$p" && break',
            "         done",
            "       fi",
            '       if [ -z "$CERTIFI_PATH" ]; then',
            "         CERTIFI_PATH=\"$(uv run --native-tls --with certifi --no-project python -c 'import certifi; print(certifi.where())' 2>/dev/null || true)\"",
            "       fi",
            '       if [ -z "$CERTIFI_PATH" ] || [ ! -f "$CERTIFI_PATH" ]; then',
            '         echo "ERROR: locate a system CA bundle. Install Python 3 + certifi and re-run." >&2',
            "         exit 1",
            "       fi",
            '       echo "Base CA bundle: $CERTIFI_PATH"',
            '       cat "$CERTIFI_PATH" ~/.agnes/ca.pem > ~/.agnes/ca-bundle.pem',
            "",
            "   (e) Persist env vars in the rc file picked in (a). Idempotent — won't",
            "       duplicate on re-run thanks to the AGNES_CA_PEM_TRUST grep guard.",
            "       Note the asymmetry: SSL_CERT_FILE (and REQUESTS_CA_BUNDLE, GIT_SSL_CAINFO)",
            "       point at the COMBINED bundle because those tools REPLACE trust.",
            "       NODE_EXTRA_CA_CERTS points at just ca.pem because Node APPENDS to its",
            "       bundled roots.",
            "",
            "       if ! grep -q 'AGNES_CA_PEM_TRUST' \"$RC\" 2>/dev/null; then",
            "         cat >> \"$RC\" <<'AGNES_RC_BLOCK'",
            "# AGNES_CA_PEM_TRUST — added by Agnes setup",
            "# Combined bundle (system roots + Agnes CA) for tools that REPLACE trust:",
            'export SSL_CERT_FILE="$HOME/.agnes/ca-bundle.pem"',
            'export REQUESTS_CA_BUNDLE="$HOME/.agnes/ca-bundle.pem"',
            'export GIT_SSL_CAINFO="$HOME/.agnes/ca-bundle.pem"',
            "# Single-cert file for Node (APPENDS to bundled roots):",
            'export NODE_EXTRA_CA_CERTS="$HOME/.agnes/ca.pem"',
            'export PATH="$HOME/.local/bin:$PATH"',
            "AGNES_RC_BLOCK",
            "       fi",
            "       # Apply for THIS shell too:",
            '       export SSL_CERT_FILE="$HOME/.agnes/ca-bundle.pem"',
            '       export REQUESTS_CA_BUNDLE="$HOME/.agnes/ca-bundle.pem"',
            '       export GIT_SSL_CAINFO="$HOME/.agnes/ca-bundle.pem"',
            '       export NODE_EXTRA_CA_CERTS="$HOME/.agnes/ca.pem"',
            '       export PATH="$HOME/.local/bin:$PATH"',
            "",
            "   IMPORTANT for the Bash tool: env vars do NOT persist between separate",
            "   Bash invocations. Re-export the four lines above (SSL_CERT_FILE,",
            "   REQUESTS_CA_BUNDLE, GIT_SSL_CAINFO, NODE_EXTRA_CA_CERTS) plus PATH at",
            "   the top of every later step's bash block that talks to Agnes.",
            "",
        ]
    )
    return lines


def _install_cli_lines(*, has_ca: bool, server_url_placeholder: str = "{server_url}") -> list[str]:
    """Step 1 — install the `agnes` CLI.

    When the trust block was emitted (`has_ca=True`), we MUST avoid
    `uv tool install <https-url>` against the Agnes wheel endpoint:
    rustls rejects the Agnes leaf cert with `CaUsedAsEndEntity`, regardless
    of `--native-tls` (the rejection is at chain validation, not at trust
    lookup — putting the cert in the OS store doesn't fix it). Solution:
    download the wheel with `curl --cacert` (curl uses OpenSSL, no rustls),
    then `uv tool install --native-tls` from the local file. PyPI deps
    still resolve over HTTPS, but `--native-tls` makes uv use the OS trust
    store for that path, which is fine because PyPI's CA chain is public.

    When `has_ca=False`, we trust the server's cert is publicly valid, so
    the simple direct install works.
    """
    if has_ca:
        return [
            "1) Install the CLI.",
            "   The Agnes server's self-signed cert trips rustls' CaUsedAsEndEntity check,",
            "   so direct `uv tool install <https-url>` against the wheel endpoint fails",
            "   (even with --native-tls). Workaround: curl-then-local-install.",
            "",
            "   If uv is missing first:",
            "     curl -LsSf https://astral.sh/uv/install.sh | sh",
            '     export PATH="$HOME/.local/bin:$PATH"',
            "",
            "   WHEEL=/tmp/{wheel_filename}",
            f'   curl -fsSL --cacert ~/.agnes/ca.pem -o "$WHEEL" {server_url_placeholder}/cli/wheel/{{wheel_filename}}',
            '   uv tool install --native-tls --force "$WHEEL"',
            "",
            "   If `agnes --version` fails after install because ~/.local/bin is not on PATH:",
            '     export PATH="$HOME/.local/bin:$PATH"',
            "     # Persist for future shells. Use `grep -qF` (fixed-string,",
            "     # not regex) + `||` short-circuit so a re-run doesn't append",
            "     # a duplicate. Pick the rc file your login shell reads:",
            '     RC="$HOME/.zshrc"  # or ~/.bashrc / ~/.bash_profile',
            "     grep -qF '$HOME/.local/bin' \"$RC\" 2>/dev/null \\",
            '       || echo \'export PATH="$HOME/.local/bin:$PATH"\' >> "$RC"',
            "     # (The trust block in step 0 already does this for you on first run.)",
        ]
    return [
        "1) Install the CLI:",
        f"   uv tool install --force {server_url_placeholder}/cli/wheel/{{wheel_filename}}",
        "",
        "   If uv is not installed yet:",
        "     curl -LsSf https://astral.sh/uv/install.sh | sh",
        "",
        "   If `agnes --version` fails after install because ~/.local/bin is not on PATH:",
        '     export PATH="$HOME/.local/bin:$PATH"',
        "     # Persist for future shells. Use `grep -qF` (fixed-string, not",
        "     # regex) + `||` short-circuit so a re-run doesn't append a",
        "     # duplicate. Pick the rc file your login shell reads:",
        '     RC="$HOME/.zshrc"  # or ~/.bashrc / ~/.bash_profile',
        "     grep -qF '$HOME/.local/bin' \"$RC\" 2>/dev/null \\",
        '       || echo \'export PATH="$HOME/.local/bin:$PATH"\' >> "$RC"',
    ]


def _init_lines(server_url_placeholder: str = "{server_url}") -> list[str]:
    """Steps 2-4 — install-location decision, then `agnes init` + smoke verify.

    Step 2 picks the install directory using a three-way decision tree on
    the user's current cwd instead of demanding a specific "expected"
    path. Earlier flows hard-coded `~/Desktop/{workspace_dir}` as the
    Right Answer and treated every other cwd as a recoverable mistake —
    which scolded users who intentionally `cd`'d into a project folder
    (e.g. `~/Devel/acme-data-app/`) before pasting the script.

    The new tree:

    - **REFUSE** if cwd is `$HOME` exactly, or a system path (`/`,
      `/tmp`, `/etc`, `/usr`, `/var`, `/opt`, `/root`, `/bin`, `/sbin`,
      `/boot`, `/sys`, `/proc`). Installing into any of these dumps
      `.claude/`, `.agnes/`, `AGNES_WORKSPACE.md`, marketplace clones,
      etc. into a directory that already has unrelated meaning. The old
      flow's `'install here'` keyword silently accepted `$HOME` — this
      one refuses.
    - **PROCEED SILENTLY** if cwd is empty, or contains only the
      whitelisted artefacts a prepared workspace might already hold
      (`.git`, `.claude`, `.agnes`, `AGNES_WORKSPACE.md`, `README.md`).
      The user clearly created+cd'd into a workspace folder before
      pasting; no need to interrupt them.
    - **CONFIRM ONCE** for anything else (cwd has unrelated content).
      Neutral framing: *"I'll install {brand} in <pwd>. Reply 'ok' to
      continue here, 'default' to install in ~/Desktop/{workspace_dir}
      instead, or 'abort'."* The 'default' branch runs the `mkdir + cd`
      itself so the user doesn't have to re-paste. Anything else stops
      cleanly without touching the filesystem. Users who want a
      different custom path /exit, `cd` to their preferred location,
      restart `claude`, and re-paste — Claude Code can't change the
      parent shell's cwd from inside a session anyway.

    `{workspace_dir}` and `{instance_brand}` are placeholders pre-substituted
    by :func:`resolve_lines` from the operator-configured brand. Defaults
    keep `~/Desktop/Agnes` behavior for instances that don't set the brand knob.

    `agnes init --workspace .` continues to use the current cwd, so once
    step 2 has confirmed (or auto-accepted) the install dir, step 3
    lands in the right place. Step 9's restart-claude cue references
    "this same directory" so users on a custom path see accurate
    guidance.

    `agnes init` is the workspace-rails delivery mechanism for everyone:
    it authenticates with the PAT, fetches CLAUDE.md (RBAC-filtered),
    writes AGNES_WORKSPACE.md (human-facing docs), installs Claude Code
    SessionStart/End hooks (auto-refresh), and runs an initial `agnes pull`
    so DuckDB views are ready. Subsumes the legacy `agnes auth import-token`
    + `agnes auth whoami` pair — `init` already verifies the PAT against
    `/api/catalog/tables` internally, and `agnes catalog` then doubles as
    a smoke verify of the data plane.

    The PAT minted by `/setup` is `general` scope with a 90 d TTL, so the
    init call will succeed for the operator's whole 90 d window without
    re-clicking "Generate prompt".
    """
    return [
        "",
        "2) Confirm the install location.",
        '   The /home page\'s "Step 2 — pick a folder" recommended',
        "       mkdir -p ~/Desktop/{workspace_dir} && cd ~/Desktop/{workspace_dir}",
        "   but the install works in any workspace folder the user prepared.",
        "   Decide where to install using the three branches below — do NOT",
        "   silently `mkdir`/`cd` anywhere, and do NOT treat the user's current",
        "   cwd as a mistake unless it lands in the REFUSE list.",
        "",
        "   Run:",
        "       pwd",
        "",
        "   2a) REFUSE — these paths are never safe to install into. STOP and",
        "       tell the user verbatim:",
        "",
        "           \"I won't install {instance_brand} into <pwd> — it's a home or",
        "           system directory and the install would scatter .claude/,",
        "           .agnes/, AGNES_WORKSPACE.md and marketplace clones across",
        "           it. Please pick a workspace folder (e.g. ~/Desktop/{workspace_dir}",
        "           or ~/work/{workspace_dir}), cd into it, and re-paste this",
        '           script from there."',
        "",
        "       Then stop — no `mkdir`, no `cd`, no further steps. The refuse",
        "       list is exact match on:",
        "           $HOME    /    /tmp    /etc    /usr    /var    /opt",
        "           /root    /bin    /sbin    /boot    /sys    /proc",
        "",
        "   2b) PROCEED SILENTLY — if the cwd is a prepared workspace, just",
        "       continue to step 3 without prompting. The whitelisted artefacts",
        "       a prepared workspace may already hold are:",
        "           .git    .claude    .agnes    AGNES_WORKSPACE.md    README.md",
        "       To check, run (fixed-string match, no regex):",
        "",
        "           ls -A | grep -Fxv -e .git -e .claude -e .agnes -e AGNES_WORKSPACE.md -e README.md | head -1",
        "",
        "       If the output is empty (cwd is empty OR contains only the",
        "       whitelisted artefacts above) → the user clearly prepared this",
        "       folder; continue to step 3 in <pwd>. Remember <pwd> as the",
        "       install dir for step 9.",
        "",
        "   2c) CONFIRM — for any other cwd (unrelated content present), tell",
        "       the user verbatim, exactly once:",
        "",
        "           \"I'll install {instance_brand} in <pwd>. Reply 'ok' to",
        "           continue here, 'default' to install in ~/Desktop/{workspace_dir}",
        "           instead, or 'abort' to stop. (For a different custom path:",
        "           type /exit, `cd` to where you want it, then run `claude`",
        '           again and re-paste this script.)"',
        "",
        "       Wait for the user's reply.",
        "         - 'ok' / 'yes' / 'install here' / Enter → continue to step 3",
        "                       in <pwd>. Remember <pwd> as the install dir.",
        "         - 'default' → run:",
        "                       mkdir -p ~/Desktop/{workspace_dir} && cd ~/Desktop/{workspace_dir}",
        "                       Then continue to step 3 in the new cwd.",
        "                       Remember ~/Desktop/{workspace_dir} as the install",
        "                       dir for step 9.",
        "         - 'abort' / anything else → stop without making any changes.",
        "                       Do NOT run `mkdir`, do NOT `cd`, do NOT continue.",
        "",
        "3) Bootstrap your {instance_brand} workspace in this directory.",
        "   Write the PAT to a file FIRST, then run `agnes init` with",
        '   `--token-file`. Passing the JWT inline via `--token "eyJ..."`',
        "   Piping the token through a file keeps it out of",
        "   the command-line argv entirely.",
        "",
        "   mkdir -p ~/.agnes && umask 077 && cat > ~/.agnes/token <<'AGNES_PAT'",
        "{token}",
        "AGNES_PAT",
        f'   agnes init --server-url "{server_url_placeholder}" --token-file ~/.agnes/token --workspace .',
        "",
        "   ALREADY INSTALLED? If `.claude/init-complete` already exists in this",
        "   directory, the workspace is initialised and `agnes init` will refuse.",
        "   Run `agnes update` instead — it uses your SAVED credentials (no token",
        "   needed) and converges the CLI, workspace template, plugins and data,",
        "   repairing anything broken. Template/default workspace files you edited",
        "   are backed up to `<name>.bak.<ts>` before being updated; Agnes-owned",
        "   hooks/statusLine/commands are re-applied on top. Then skip to step 4.",
        "   (If `agnes update` fails on auth because your saved token expired, run",
        f'   `agnes init --force --server-url "{server_url_placeholder}" --token-file ~/.agnes/token`',
        "   — `agnes init` always needs an explicit --server-url; this refreshes",
        "   the token and now backs up your edited template files before updating.)",
        "",
        "   This authenticates with the PAT, fetches your CLAUDE.md (RBAC-filtered),",
        "   writes AGNES_WORKSPACE.md (human-facing docs), installs Claude Code",
        "   SessionStart/End hooks (auto-refresh), and runs an initial `agnes pull`",
        "   so your DuckDB views are ready.",
        "",
        "4) Verify the data is queryable:",
        "   agnes catalog",
        "",
        "   This should list the tables your account has grants for. Empty list",
        "   means your admin hasn't granted you access yet — contact them.",
        "",
        "   Tip: this setup session's transcript uploads to the server like any",
        "   other (`agnes push` scrubs the PAT client-side first — see step 3).",
        "   If a FUTURE session covers something the user does not want uploaded,",
        "   they can type `/agnes-private` themselves — that session's transcript",
        "   is then skipped by `agnes push` (audit-logged to",
        "   `.claude/agnes-sessions-private-skipped.txt`) and the statusbar shows",
        "   `🔒 agnes-private`.",
    ]


def _diagnose_lines(*, diagnose_num: str) -> list[str]:
    """Diagnose step — runs AFTER the marketplace + MCP blocks.

    Putting it last (instead of right after `whoami`) means it doubles as
    a server-health smoke test that runs once everything else is in place,
    not as a gate before them.

    The bundled `agnes skills` knowledge base (markdown documents listable
    via `agnes skills list` / readable via `agnes skills show <name>`) is
    intentionally NOT surfaced as its own setup step (#242 dropped that
    interactive prompt). Discovery happens organically when CLAUDE.md or
    another skill references a specific entry (see the
    `agnes skills show agnes-data-querying` mention in the CLAUDE.md
    template's BigQuery section). Bulk-copying every skill into
    `~/.claude/skills/agnes/` at setup time was an opinion question with
    no obvious right answer; on-demand lookup is the one-size-fits-all
    default.
    """
    return [
        "",
        f"{diagnose_num}) Run diagnostics:",
        "   agnes diagnose",
        "",
        '   This should print "Overall: healthy". `db_schema: unknown` and',
        "   `data: 0 tables` are NORMAL in two cases:",
        "     - fresh install (no tables registered yet), and",
        "     - non-admin roles (e.g. `analyst`) that don't have grants to read",
        "       the system schema even on populated instances.",
        "   Only flag actual yellow/red checks (api / duckdb_state / users).",
    ]


def _load_connector_body(slug: str) -> Optional[str]:
    """Read the post-frontmatter body of a connector skill's SKILL.md,
    sourced from the operator's Initial Workspace Template clone (preferred)
    or the bundled snapshot in the wheel (fallback).

    Returns ``None`` when neither tier has the file — the caller logs a
    warning and skips the entry rather than rendering a half-block.
    """
    from src.initial_workspace import resolve_seed_file

    rel_path = f"workspace/.claude/skills/{slug}/SKILL.md"
    result = resolve_seed_file(rel_path)
    if result is None:
        return None
    content, _source = result

    # Strip YAML frontmatter — the prompt body starts after the closing `---`.
    stripped = content.lstrip()
    if not stripped.startswith("---"):
        return content
    body = stripped[3:]
    end_match = re.search(r"^---\s*$", body, re.MULTILINE)
    if not end_match:
        # Malformed frontmatter — return the raw content rather than
        # silently emitting an empty body. The manifest validator already
        # logged the issue; double-failure here would lose context.
        return content
    return body[end_match.end() :].lstrip("\n")


# Tile sub-letters shared by the required + optional connector blocks.
# Each block letters its own tiles independently (both restart at "a").
_SUB_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _required_connectors_block(
    step_num: str,
    manifest: list["ConnectorEntry"],
    *,
    next_step_num: str,
    instance_brand: str,
) -> list[str]:
    """Mandatory-install step for ``required=True`` connectors — rendered
    between diagnose and the optional Y/n tiles, with NO per-tool ask.

    Same fail-soft body handling as :func:`_connectors_block` (missing
    SKILL.md body → warn + skip, letters stay tight) so a bad seed commit
    never 500s ``/home``; the operator-facing guard for a missing
    REQUIRED body is the seed-sync render dry-run, which reports it as an
    error. Empty manifest renders no block.
    """
    if not manifest:
        return []

    lines = [
        "",
        f"{step_num}) Install required tools (mandatory — run every prompt below now):",
        "",
        "   The tools below are required by this instance — do NOT ask the user",
        "   whether to set them up, and do not skip any. Run each inline prompt",
        "   now, in order. Every prompt is idempotent and safe to re-run; a tool",
        "   that is already configured short-circuits with its ✅ line instead of",
        "   reinstalling.",
        "",
    ]
    letter_idx = 0
    for entry in manifest:
        if letter_idx >= len(_SUB_LETTERS):
            logger.warning(
                "setup_instructions: more than %d required connectors — "
                "remaining tiles dropped",
                len(_SUB_LETTERS),
            )
            break
        body = _load_connector_body(entry.slug)
        if body is None:
            logger.warning(
                "setup_instructions: required connector %s body not found in "
                "seed — skipped",
                entry.slug,
            )
            continue
        body = body.replace("{instance_brand}", instance_brand)
        lines.append(
            f"   {_SUB_LETTERS[letter_idx]}) {entry.display_name} — {entry.short_summary}"
        )
        lines.append("      Follow this inline prompt verbatim:")
        lines.append("")
        for body_line in body.split("\n"):
            lines.append(f"      {body_line}" if body_line else "")
        lines.append("")
        letter_idx += 1
    lines.extend(
        [
            f"   Continue to step {next_step_num} only after every required tool above has",
            "   printed its ✅ line (or surfaced a ❌ that you reported back to the user).",
        ]
    )
    return lines


def _connectors_block(
    step_num: str,
    manifest: list["ConnectorEntry"],
    *,
    confirm_step_num: str,
    instance_brand: str,
) -> list[str]:
    """Per-connector interactive ask + inline prompt. Last interactive
    step before Confirm.

    Defaults to install (Y) — the user has to actively type "no" to skip.
    Default-install matches "wire everything up" — the common path. Each
    connector ships with its own step-0 keychain precheck so re-runs
    short-circuit cleanly.

    Manifest source: ``src.connectors_manifest.load_manifest()`` reads the
    seed-resident ``workspace/.claude/skills/connector-*/SKILL.md`` files
    (operator IWT clone first, bundled snapshot fallback). Each entry
    carries display_name + short_summary + estimated_minutes; the body
    text comes from the same file via :func:`_load_connector_body`.

    Order: stable, alphabetical by display_name (set in
    ``load_manifest``). Empty manifest renders no block.

    Receives only the optional (non-required) entries; ``required=True``
    entries render in :func:`_required_connectors_block`.
    """
    if not manifest:
        return []

    lines = [
        "",
        f"{step_num}) Connect the user's tools (last interactive ask before Confirm):",
        "",
        '   For each tool below, ask the user verbatim: "Set up <NAME> now? (Y/n)".',
        "   Treat empty/Enter as YES — the default is install. Only skip when the",
        '   user types an explicit "no" / "n" / "skip". Wait for each answer',
        "   before moving to the next. The prompts below are idempotent and",
        "   safe to re-run if anything goes sideways.",
        "",
    ]
    # Sub-letter index tracks ONLY the connectors we actually rendered
    # (not the raw enumerate index) — if a connector body is missing
    # from the seed we want the letter sequence to stay tight (a, b, c)
    # rather than skip to (a, c, d). The bug was visible to users as
    # "b)" and "c)" with no "a)" in the rendered install prompt.
    letter_idx = 0
    for entry in manifest:
        if letter_idx >= len(_SUB_LETTERS):
            logger.warning(
                "setup_instructions: more than %d optional connectors — "
                "remaining tiles dropped",
                len(_SUB_LETTERS),
            )
            break
        body = _load_connector_body(entry.slug)
        if body is None:
            logger.warning(
                "setup_instructions: connector %s body not found in seed — skipped",
                entry.slug,
            )
            continue
        # Substitute brand placeholder. Atlassian / Asana / GWS bodies all
        # reference {instance_brand} in their token-label hints.
        body = body.replace("{instance_brand}", instance_brand)
        lines.append(f"   {_SUB_LETTERS[letter_idx]}) {entry.display_name} — {entry.short_summary}")
        lines.append(f'      Ask: "Set up {entry.display_name} now? (Y/n)"')
        lines.append("      If yes (default) — follow this inline prompt verbatim:")
        lines.append("")
        for body_line in body.split("\n"):
            lines.append(f"      {body_line}" if body_line else "")
        lines.append("")
        letter_idx += 1
    lines.extend(
        [
            f"   After all asks (regardless of answers) continue to step {confirm_step_num}.",
        ]
    )
    return lines


def _restart_claude_lines(step_num: str, *, confirm_step_num: str) -> list[str]:
    """Final 'restart Claude Code' instruction emitted immediately before
    Confirm. Marketplace plugins, MCP server registrations, and the
    SessionStart hooks installed during init only load on the next
    Claude Code session — without this step the user sits inside the
    setup session with stale state and re-discovers the requirement
    later. The marketplace step's trailer already mentions /exit
    + claude conditionally; this is the unconditional equivalent so
    every path (with or without plugins) ends on the same cue.

    `confirm_step_num` is threaded in (mirroring how `_finale_lines`
    receives it) so the trailing recap line can name the Confirm step
    explicitly. The recap intentionally overlaps the Confirm summary in
    `_finale_lines` as a short bridge — it asks for a plain-language
    outcome summary right before the structured Confirm bullets.
    """
    return [
        "",
        f"{step_num}) Restart Claude Code so every plugin, MCP server, and SessionStart hook installed above actually loads:",
        "   Tell me to type `/exit` (or close the Claude Code session entirely), then run `claude` again from this same directory — the install dir confirmed in step 2 (`~/Desktop/{workspace_dir}` on the default path, or whatever cwd the user explicitly accepted with 'install here').",
        "   The next session boots with all marketplace plugins, every connector's keychain entries / OAuth grants, and the agnes-welcome + agnes-update SessionStart hooks active. This is the last action before the Confirm summary — once I'm back in Claude Code, setup is complete.",
        f"   Before step {confirm_step_num} (Confirm): after all the steps and asks above (whatever the answers), give me a short recap of what was installed or was already present — CLI, workspace files, hooks, marketplace plugins, connectors — so the outcome is clear, then continue.",
    ]


def _finale_lines(
    *,
    confirm_step_num: str,
    has_ca: bool,
    manifest: list["ConnectorEntry"],
    required_manifest: Optional[list["ConnectorEntry"]] = None,
) -> list[str]:
    """Final Confirm step. Bullets it asks the assistant to report on must
    only reference earlier steps that were actually emitted, otherwise the
    assistant either hallucinates an answer or asks the user about a
    non-existent step. The CA-bundle-source bullet only makes sense when
    the trust block ran (`has_ca`). The marketplace clone bullet is
    unconditional now — preflight + marketplace are always emitted.

    Connector bullets are dynamic: they list the display names from
    ``required_manifest`` (mandatory installs — no "declined" wording,
    those can't be declined) and ``manifest`` (the optional tiles), so
    adding/removing a connector in the seed flows through to the Confirm
    summary without a code change. An empty group omits its bullet (its
    connector block wasn't emitted either). When no required entries
    exist, the optional bullet keeps its legacy wording verbatim — the
    default install prompt must stay byte-identical
    (tests/test_install_prompt_snapshot.py).
    """
    bullets = [
        "   - `agnes --version` output",
        "   - First few lines of `agnes catalog` (tables you can see)",
        "   - Confirmation that `./CLAUDE.md` and `./AGNES_WORKSPACE.md` exist",
        "   - Confirmation that `./.claude/settings.json` contains SessionStart/End hooks",
        "   - The `agnes diagnose` overall status",
        "   - Whether skills were copied or left on-demand",
        "   - Confirmation that `~/.agnes/marketplace/.git/` exists "
        "(the marketplace clone) and that any granted plugins installed",
    ]
    if required_manifest:
        required_names = ", ".join(e.display_name for e in required_manifest)
        bullets.append(
            f"   - For each required connector ({required_names}): "
            "the verbatim ✅ or ❌ line that the connector's verify step "
            "emitted earlier in this session."
        )
    if manifest:
        connector_names = ", ".join(e.display_name for e in manifest)
        label = "optional connector" if required_manifest else "connector"
        bullets.append(
            f"   - For each {label} ({connector_names}): "
            "the verbatim ✅ or ❌ line that the connector's verify step "
            "emitted earlier in this session (e.g. `✅ Asana ready — ...` "
            "or `❌ Atlassian setup failed: ...`). If the user declined "
            "a connector, say declined."
        )
    if has_ca:
        bullets.append(
            "   - Which CA bundle source got picked in step 0(d) "
            "(system Python certifi / system curl bundle / uv-fetched)"
        )
    return [
        f"{confirm_step_num}) Confirm:",
        '   Tell me "{instance_brand} workspace is ready" and summarize:',
        *bullets,
    ]


def _preflight_block(step_num: str) -> list[str]:
    """Pre-flight check — runs before the marketplace clone.

    `claude plugin marketplace add` (and our git-clone fallback) shells out
    to `git`, AND the marketplace step calls `claude` itself, so a missing
    binary on either side fails the step with a confusing error. We check
    both here so the user gets a single clear "install X" message instead
    of debugging a downstream error.

    Cross-platform install commands cover the three supported workstation
    OSes:
      - macOS: Homebrew (`brew install git`). The Xcode CLT bundle also
        ships git; we prefer brew because it's non-interactive.
      - Windows: winget (`winget install --id Git.Git -e ...`). Bundled
        with Windows 10 1809+ and Windows 11; non-interactive with --silent.
      - Linux: apt or dnf, depending on distro family.

    For `claude` we point at the official platform installer docs rather
    than vendoring an install one-liner — Anthropic ships per-platform
    installers (npm on Linux, native binary on macOS/Windows) and the
    canonical instructions live at https://docs.claude.com/claude-code.

    `step_num` is parameterized because step ordering shifted between
    layouts (the marketplace block now runs before diagnose/skills, so
    preflight + marketplace are steps 4-5 instead of 6-7).
    """
    return [
        "",
        f"{step_num}) Make sure git and claude are installed (required for the marketplace clone):",
        "     git --version",
        "     claude --version",
        "",
        '   If `git --version` fails ("command not found" or similar), install git:',
        "     - macOS:   brew install git",
        "     - Windows: winget install --id Git.Git -e --source winget --silent",
        "     - Linux:   sudo apt-get install git    OR    sudo dnf install git",
        "",
        "   If `claude --version` fails, install Claude Code:",
        "     - npm (Linux): npm i -g @anthropic-ai/claude-code",
        "     - macOS / Windows native installer: see https://docs.claude.com/claude-code",
        "",
        "   Then re-run both `--version` checks to confirm before continuing.",
    ]


def _marketplace_block(
    plugin_install_names: list[str],
    step_num: str,
) -> list[str]:
    """Build the marketplace + plugin-install block.

    `plugin_install_names` may be empty: registering the per-user
    marketplace clone with Claude Code is useful even when the operator
    has zero plugin grants, because it pre-wires the SessionStart hook
    and the grant flow — admin grants land on the next Claude Code
    session without re-running setup. The block copy adapts for the
    empty case so the comment-bullet doesn't promise plugin installs
    that won't happen.

    `step_num` is parameterized because step ordering shifted between
    layouts (this block now runs before diagnose/skills, so it's step 5
    instead of the old step 7).

    The whole block is one CLI invocation: ``agnes refresh-marketplace
    --bootstrap``. The CLI handles clone + PAT-strip + chmod + register-
    with-Claude + auto-install-from-manifest internally. This is what
    used to be a 15-line shell sequence inline; pulling it into the CLI
    bought:

      1. **Claude Code permission gate friendliness.** The agent-driven
         onboarding flow inside Claude Code denies ``rm -rf`` by default;
         the inline script tripped on it. Wrapping the destructive prep
         inside agnes lets the CLI's already-trusted permission grant
         cover it (Python ``shutil.rmtree`` doesn't pattern-match the
         shell ``rm -rf`` block).
      2. **Idempotence without inline ``rm``.** Re-running the install
         prompt over an existing clone now does fetch+reset under the
         hood (no destructive cleanup needed). The prompt's "safe to
         re-run" promise holds without forcing the operator to delete
         anything by hand.
      3. **One source of truth.** ``agnes refresh-marketplace`` is the
         same reconcile the detached SessionStart ``agnes update`` hook
         runs on every session, so install + auto-refresh share the same
         code path — version-aware reconcile, hook JSON output, credential
         helper PAT injection, all consistent.

    Why always clone (with the CLI doing it) instead of trying direct
    HTTPS marketplace add first? ``claude plugin marketplace add
    <https-url>`` does succeed against our ``/marketplace.git/`` endpoint
    (returns 200 + JSON), but Claude Code stores the response as a
    single-file marketplace and resolves plugin ``source:
    "./plugins/<name>"`` paths as local filesystem refs — so the
    subsequent ``claude plugin install`` looks for plugin trees at
    ``<marketplace-dir>/plugins/<name>/`` and 404s because the dir is a
    file. Only the git-clone path produces a real directory tree with
    plugin contents in place. Broken end-to-end on every Claude Code
    distribution; cloning is the only reliable install path.

    TLS handling for the in-binary ``git clone`` is fully covered by the
    cross-platform trust block (step 0) when the server's cert needs
    bootstrapping (`ca_pem` non-empty), and by the OS trust store when
    the cert is publicly-trusted. There used to be a legacy fallback
    here that emitted a host-scoped ``git config http.<host>.sslVerify
    false`` line for the ``AGNES_DEBUG_AUTH`` path; that's gone — it
    masked operator misconfigurations (a ``self_signed_tls=True``
    instance without ``/data/state/certs/fullchain.pem`` on disk) and
    its ``sslVerify=false`` shell command tripped Claude Code auto-mode
    classifiers. Operators serving a self-signed or private-CA cert
    must place the fullchain at ``AGNES_TLS_FULLCHAIN_PATH`` (default
    ``/data/state/certs/fullchain.pem``) so step 0 can read it via
    ``_read_agnes_ca_pem``.
    """
    has_plugins = bool(plugin_install_names)
    header = (
        "Register the Agnes Claude Code marketplace and install plugins:"
        if has_plugins
        else "Register the Agnes Claude Code marketplace (no plugin grants visible when this prompt was generated):"
    )
    # Both branches phrase grants as a render-time snapshot, not a timeless
    # fact: grants change after the prompt is generated, and the prompt is
    # also served grant-blind on unauthenticated pages. The CLI reads the
    # LIVE manifest, so the instruction is always "install what is granted
    # now, then verify with `agnes my-stack show`" rather than a baked-in
    # claim the agent could contradict mid-install.
    bullet_5 = (
        "   #   5. install every plugin the live manifest grants this account"
        if has_plugins
        else (
            "   #   5. install every plugin the live manifest grants this account"
            " (none were visible when this prompt was generated; anything granted"
            " since still installs here)"
        )
    )
    verify_lines = [
        "   Then verify what landed:",
        "   agnes my-stack show",
        "   # [✓] = in your stack; [✗] = available to you but NOT added — an",
        "   # opt-in marker, not an error. Add one with",
        "   # `agnes marketplace add <marketplace-id>/<plugin-name>`, then run",
        "   # `agnes update` and `/reload-plugins` in Claude Code.",
    ]
    if has_plugins:
        trailer = [
            *verify_lines,
            "",
            "   These run non-interactively. After they finish, tell the user to /exit",
            "   and run `claude` again so the new plugins load. From then on, the",
            "   SessionStart hook keeps the marketplace clone in sync via a detached",
            "   `agnes update` on every Claude Code session.",
        ]
    else:
        trailer = [
            *verify_lines,
            "",
            "   No plugin grants were visible for this account when this prompt was",
            "   generated (grants added since — or a prompt copied while logged",
            "   out — won't show here; `agnes my-stack show` above is the live",
            "   truth). Registering the marketplace regardless pre-wires the",
            "   SessionStart hook: when an admin grants you a plugin later, the",
            "   detached `agnes update` (run by the hook on every Claude Code",
            "   session) will reconcile and install it automatically — no need to",
            "   re-run this setup script.",
        ]
    return [
        "",
        f"{step_num}) {header}",
        "   # `agnes refresh-marketplace --bootstrap` does:",
        "   #   1. clone the per-user marketplace bare repo to ~/.agnes/marketplace",
        "   #   2. strip the PAT from the cloned origin URL (refreshes use a",
        "   #      per-invocation git credential helper, not the URL)",
        "   #   3. best-effort chmod 700/600 on POSIX (no-op on Windows NTFS)",
        "   #   4. `claude plugin marketplace add ~/.agnes/marketplace`",
        bullet_5,
        "   # Idempotent — re-runs over an existing clone do fetch+reset+reconcile",
        "   # via the same path the SessionStart hook uses. A leftover clone from",
        "   # a PREVIOUS instance (origin pointing at another host) is detected",
        "   # and re-cloned from the current server automatically.",
        "   agnes refresh-marketplace --bootstrap || {",
        '     echo "ERROR: agnes refresh-marketplace --bootstrap failed" >&2',
        "     exit 1",
        "   }",
        "",
        *trailer,
    ]


def _preamble_lines(*, has_ca: bool, custom_preamble: str = "") -> list[str]:
    """Header that opens the prompt before the numbered steps. The
    `step 0(d) fallback chain` reference is only emitted when the trust
    block actually exists (`has_ca`); without it the line points at a
    non-existent step. The "don't disable TLS verification" advice itself
    stays unconditional — it's good guidance regardless of whether the
    server runs with a private CA.

    `custom_preamble` is an operator-authored block prepended at the very
    top (above `Set up the {instance_brand} CLI…`). Empty/unset emits zero
    extra lines so the default output is byte-identical. Any
    `{instance_brand}` etc. inside it is substituted by the `resolve_lines`
    loop; it must NOT contain literal `{server_url}`/`{token}` (those only
    resolve at click time in the JS clipboard flow, not in the preamble)."""
    lines = [
        "Set up the {instance_brand} CLI on this machine.",
        "",
        "Server: {server_url}",
        "Personal access token: {token}",
        "(Just generated; treat it as a secret.)",
        "",
        "Run these, in order. The script is idempotent — safe to re-run if a step",
        "fails partway through.",
        "",
        "FIRST, check whether this machine already ran this setup: if the target",
        "workspace contains `.claude/init-complete` (or `agnes --version` already",
        "works), you are RECONCILING an existing install, not starting fresh —",
        "still run every step in order (each converges to the desired state",
        "rather than reinstalling), but expect 'already configured' outcomes and",
        "do NOT treat them as errors. Leftover state from a previous instance",
        "(e.g. an old marketplace clone) is handled by the steps themselves.",
        "",
        "If a step fails with an unfamiliar error, paste the",
        "exact error back and stop. Do NOT improvise around TLS errors by disabling",
        "verification (`-k`, `NODE_TLS_REJECT_UNAUTHORIZED=0`,",
        "`git -c http.sslVerify=false`, etc.) — those are dead ends that hide the",
        "real problem.",
    ]
    if has_ca:
        lines.append(
            "The fallback chain inside step 0(d) is documented and OK to use; that's what fallback chains are for."
        )
    lines.append("")
    if custom_preamble:
        lines = [*custom_preamble.split("\n"), "", *lines]
    return lines


def _step_numbers(
    *, has_connectors: bool = True, has_required_connectors: bool = False
) -> dict[str, str]:
    """Compute the step numbers for the unified layout.

    Returns a dict keyed by logical step name; values are stringified
    1-based step numbers (preserving the existing string-based helper API
    so call sites stay diff-minimal).

    Steps (default layout): install (1), mkdir/cd (2), init (3),
    catalog (4), preflight (5), marketplace (6), diagnose (7),
    required_connectors (only when the manifest has ``required=True``
    entries — takes 8), connectors (8, or 9 after a required step),
    restart_claude, confirm. Preflight +
    marketplace + connectors + restart_claude are always-on:
      - Marketplace registration is useful even when the operator has
        zero plugin grants (SessionStart hook reconciles future grants
        automatically).
      - Connectors are per-connector default-yes asks sourced from the
        seed manifest — the user can decline each individually, so
        always-emitting the block costs nothing for users who skip
        everything. The Atlassian Remote MCP registration
        (`claude mcp add ...`) lives INSIDE the Atlassian connector's
        SKILL.md body in the seed, so all Atlassian setup is grouped
        together rather than scattered across the setup script.

    The interactive "Skills" step that previously sat between diagnose
    and Confirm was deleted in #242 — on-demand `agnes skills show
    <name>` is the one-size-fits-all default; bulk-copying every skill
    into ``~/.claude/skills/agnes/`` was an opinion question without an
    obvious right answer.

    `has_connectors` / `has_required_connectors` gate their steps: an
    absent group drops its number (empty string in the dict) and every
    later step shifts down — numbering stays contiguous off the single
    counter.

    Step-0 (TLS trust block) sits outside this numbering — it is gated by
    has_ca and has its own "0)" header rendered inside the trust block
    helper.
    """
    n = 5
    preflight = str(n)
    n += 1
    marketplace = str(n)
    n += 1
    diagnose = str(n)
    n += 1
    required_connectors = str(n) if has_required_connectors else ""
    if has_required_connectors:
        n += 1
    connectors = str(n) if has_connectors else ""
    if has_connectors:
        n += 1
    restart_claude = str(n)
    n += 1
    confirm = str(n)
    return {
        "preflight": preflight,
        "marketplace": marketplace,
        "diagnose": diagnose,
        "required_connectors": required_connectors,
        "connectors": connectors,
        "restart_claude": restart_claude,
        "confirm": confirm,
    }


def resolve_lines(
    wheel_filename: str,
    *,
    plugin_install_names: list[str] | None = None,
    server_host: str = "",
    ca_pem: str | None = None,
    connector_manifest: Optional[list["ConnectorEntry"]] = None,
    instance_brand: str = "Agnes",
    workspace_dir: str = "Agnes",
    custom_preamble: str = "",
) -> list[str]:
    """Return the template lines with server-side placeholders substituted.

    Pre-substitutes `{wheel_filename}` and `{server_host}`. Leaves
    `{server_url}` and `{token}` as placeholders for click-time JS
    substitution (or for `render_setup_instructions()` below).

    `ca_pem` (PEM-encoded fullchain of the Agnes server's TLS cert) gates
    the cross-platform step-0 trust-bootstrap block AND switches step 1 to
    the curl-then-local-install pattern AND switches step 5 to the
    platform-aware marketplace strategy. Caller decides whether the cert
    needs the bootstrap (typically: skip for publicly-trusted certs like
    Let's Encrypt, emit for self-signed or private corp CA).

    `connector_manifest` is a list of validated ConnectorEntry objects
    sourced from :func:`src.connectors_manifest.load_manifest`. Entries
    with ``required=True`` render as a separate mandatory step (no Y/n
    ask) before the optional tiles. ``None`` triggers a fresh manifest
    load. ``[]`` (empty list) is treated differently from ``None``: it
    intentionally renders no connector blocks.

    Fallback: callers pass `"agnes.whl"` when no wheel is present on disk.
    The resulting URL (`/cli/wheel/agnes.whl`) will 404 at download time, but
    the instruction text still renders so operators can see the snippet shape
    and diagnose the missing wheel on the server.
    """
    names = list(plugin_install_names or [])
    has_ca = bool(ca_pem and ca_pem.strip())

    # Distinguish "caller didn't pass anything → load fresh from seed" from
    # "caller passed []  → intentionally render empty connector section".
    # Codex C-1 fix: don't silently rehydrate when caller wanted empty.
    if connector_manifest is None:
        from src.connectors_manifest import load_manifest

        connector_manifest = load_manifest()

    required_entries = [e for e in connector_manifest if e.required]
    optional_entries = [e for e in connector_manifest if not e.required]
    has_required = bool(required_entries)
    has_connectors = bool(optional_entries)
    # Step layout. Preflight + marketplace + MCP go BEFORE diagnose;
    # required connectors (mandatory, no ask) come right after diagnose;
    # optional connectors are the LAST interactive ask before Confirm —
    # once plugins + MCP + diagnose are settled, the only remaining work
    # is plugging the user's tools. An absent group (no required entries,
    # no optional entries, or an empty manifest) drops its step and the
    # rest renumber — _step_numbers handles it.
    steps = _step_numbers(
        has_connectors=has_connectors, has_required_connectors=has_required
    )

    lines: list[str] = []
    if has_ca:
        lines.extend(_tls_trust_block(ca_pem))  # type: ignore[arg-type]
    lines.extend(_preamble_lines(has_ca=has_ca, custom_preamble=custom_preamble))
    lines.extend(_install_cli_lines(has_ca=has_ca))  # 1
    lines.extend(_init_lines())  # 2, 3
    lines.extend(_preflight_block(steps["preflight"]))  # 4
    lines.extend(_marketplace_block(names, step_num=steps["marketplace"]))  # 5
    lines.extend(_diagnose_lines(diagnose_num=steps["diagnose"]))  # 6
    if has_required:
        lines.extend(
            _required_connectors_block(
                steps["required_connectors"],
                required_entries,
                next_step_num=steps["connectors"] or steps["restart_claude"],
                instance_brand=instance_brand,
            )
        )
    # Optional connectors are the LAST interactive ask before the
    # restart-claude cue. Per-connector default-yes — empty/Enter is
    # install, explicit "no" skips. No optional entries renders no block
    # (the step number is dropped).
    lines.extend(
        _connectors_block(
            steps["connectors"],
            optional_entries,
            confirm_step_num=steps["confirm"],
            instance_brand=instance_brand,
        )
    )
    # Restart-claude lands between connectors and confirm so the user
    # picks up freshly-registered plugins / MCP servers / hooks on the
    # next session — without this every path silently expected the user
    # to know they had to re-launch.
    lines.extend(_restart_claude_lines(steps["restart_claude"], confirm_step_num=steps["confirm"]))
    lines.append("")
    lines.extend(
        _finale_lines(
            confirm_step_num=steps["confirm"],
            has_ca=has_ca,
            manifest=optional_entries,
            required_manifest=required_entries,
        )
    )

    return [
        line.replace("{wheel_filename}", wheel_filename)
        .replace("{server_host}", server_host)
        .replace("{workspace_dir}", workspace_dir)
        .replace("{instance_brand}", instance_brand)
        for line in lines
    ]


def render_setup_instructions(
    server_url: str,
    token: str,
    wheel_filename: str = "agnes.whl",
    *,
    plugin_install_names: list[str] | None = None,
    server_host: str = "",
    ca_pem: str | None = None,
    connector_manifest: Optional[list["ConnectorEntry"]] = None,
    instance_brand: str = "Agnes",
    workspace_dir: str = "Agnes",
    custom_preamble: str = "",
) -> str:
    """Render the setup instructions as a single string.

    Used server-side for tests and any non-JS rendering path. The browser
    clipboard flow uses the JS renderer embedded in the Jinja partial; both
    must produce byte-identical output for a given (server_url, token,
    wheel, plugins, host, ca_pem, connector_manifest, brand, workspace_dir)
    tuple.
    """
    lines = resolve_lines(
        wheel_filename,
        plugin_install_names=plugin_install_names,
        server_host=server_host,
        ca_pem=ca_pem,
        connector_manifest=connector_manifest,
        instance_brand=instance_brand,
        workspace_dir=workspace_dir,
        custom_preamble=custom_preamble,
    )
    text = "\n".join(lines)
    return text.replace("{server_url}", server_url).replace("{token}", token)
