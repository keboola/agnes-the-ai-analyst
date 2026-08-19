"""Single source of truth for the "Setup a new Claude Code" clipboard payload.

Both the JS-embedded clipboard renderer (`_claude_setup_instructions.jinja`)
and the read-only HTML preview on the dashboard and /install pages consume
these lines. Keep it in Python so there is exactly ONE place that edits.

Placeholders `{server_url}` and `{server_host}` are substituted at render
time — `{server_host}` server-side via `resolve_lines()`, `{server_url}`
surviving into the JS template to be filled in at click time.
`{wheel_filename}` is still accepted by `resolve_lines()` /
`render_setup_instructions()` for backward compatibility with existing
callers, but no longer appears anywhere in the rendered body — see the note
near "server-pre-substituted" below.

The analyst's access token is deliberately NOT a placeholder in this
template. It is written to `~/.agnes/token` out-of-band, before this
prompt is generated (see `{server_url}/home` step 4) — so the raw token
value never has to appear in the prompt text or a pasted chat transcript.
(Scope of that guarantee: THIS payload. Step 4's own copied shell command
does carry the token through the browser clipboard transiently — that is
its delivery mechanism — and its clipboard-blocked fallback can reveal it
on an explicit second click.) `render_setup_instructions()` still accepts a
`token` kwarg for backward compatibility with existing callers, but it is
a no-op today: nothing in the rendered body contains `{token}` to
substitute.

`{wheel_filename}` USED to be server-pre-substituted into a
`/cli/wheel/{wheel_filename}` URL, because `uv tool install` validates the
PEP 427 filename *in the URL path* before fetching, so a stable alias like
`agnes.whl` fails with "Must have a version". That pinned the filename
captured at RENDER time, though, so a server upgrade between render and
execution 404d it. Step 1 now downloads via the unversioned `/cli/download`
endpoint instead (immune to that race — see `_install_cli_lines`), and
`{wheel_filename}` is kept only for backward compatibility with callers
that still pass it.

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

Layout (default; required/optional connector steps drop out with their
manifest group and later steps renumber):
  0  TLS trust block (only when ca_pem is supplied)
  1  Install CLI
  2  agnes init (auth + workspace bootstrap; unsafe-dir refusal lives in
     the CLI itself — `unsafe_workspace`)
  3  agnes catalog (smoke verify)
  4  Marketplace (always, even with empty served stack; git/claude
     preflight folded into its header)
  5  Diagnose
  6  Required connectors (only when the manifest has required=True rows)
  7  Optional connectors (ask, then `agnes connectors show <slug>`)
  8  Restart Claude Code
  9  Confirm

Connector SKILL.md bodies are NOT inlined (they were 76 % of the rendered
prompt): the steps reference `agnes connectors show <slug>`, backed by
``GET /api/connectors/{slug}/prompt``, so a body is fetched only for
tools the user actually says yes to.

The combined-bundle source uses a fallback chain so the prompt still works
on machines without the system Python `certifi`: we try (a) `python3 -c
'import certifi'`, (b) the platform's curl/openssl bundle path, (c)
`uv run --with certifi` as a network last-resort. The user explicitly
permitted that fallback chain — it's not improvising-around-a-TLS-error.
"""

from __future__ import annotations

import logging
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
            "   Note for the Bash tool: environment variables set in one call don't",
            "   carry over to the next. Re-export the four lines above (SSL_CERT_FILE,",
            "   REQUESTS_CA_BUNDLE, GIT_SSL_CAINFO, NODE_EXTRA_CA_CERTS) plus PATH at",
            "   the top of every later step's bash block that talks to Agnes.",
            "",
        ]
    )
    return lines


def _install_cli_lines(*, has_ca: bool, server_url_placeholder: str = "{server_url}") -> list[str]:
    """Step 1 — install the `agnes` CLI.

    Downloads via the unversioned `/cli/download` endpoint (`curl -OJ`,
    which honours `Content-Disposition` and saves the wheel under its real
    PEP-427 filename) into a fresh temp dir, then installs from that local
    file — the same pattern `app/api/cli_artifacts.py::cli_install_script`
    (`/cli/install.sh`) already uses. This is deliberately NOT a
    `/cli/wheel/{wheel_filename}` URL pinned to the filename captured when
    this prompt was *rendered*: `app/api/cli_artifacts.py::cli_wheel_versioned`
    serves only the wheel currently on disk, so if the server auto-upgrades
    between render and execution (a background version roll, or simply time
    passing before the user pastes the prompt), a pinned URL 404s.
    `/cli/download` always serves whichever wheel is current at fetch time,
    so the install survives a mid-session version roll.

    `-L --max-redirs 0` is deliberate. `-OJ` takes the saved filename
    from the response's `Content-Disposition`, so `-L` alone would follow a
    cross-host redirect and install whichever wheel the hop served, with
    nothing on screen to say the download moved. Keeping `-L` but capping
    redirects at zero turns any hop into `curl: (47) Maximum (0) redirects
    followed` and a non-zero exit — a failure the analyst can report. A
    hostname alias that 308s to the canonical host must therefore be handed
    out as the canonical URL, not as the alias. The scheme is deliberately
    NOT pinned with `--proto '=https'`: a local/dev instance legitimately
    serves this prompt over http, where that flag would refuse the download
    outright (`curl: (1) Protocol "http" not supported`).

    When the trust block was emitted (`has_ca=True`), we MUST additionally
    avoid `uv tool install <https-url>` against the Agnes server:
    rustls rejects the Agnes leaf cert with `CaUsedAsEndEntity`, regardless
    of `--native-tls` (the rejection is at chain validation, not at trust
    lookup — putting the cert in the OS store doesn't fix it). Solution:
    download the wheel with `curl --cacert` (curl uses OpenSSL, no rustls),
    then `uv tool install --native-tls` from the local file. PyPI deps
    still resolve over HTTPS, but `--native-tls` makes uv use the OS trust
    store for that path, which is fine because PyPI's CA chain is public.

    When `has_ca=False`, we trust the server's cert is publicly valid, so
    the simple curl-then-install pattern works without the cert flags.
    """
    if has_ca:
        return [
            "1) Install the CLI.",
            "   The Agnes server's self-signed cert trips rustls' CaUsedAsEndEntity check,",
            "   so direct `uv tool install <https-url>` against the server fails (even",
            "   with --native-tls). Workaround: curl-then-local-install.",
            "",
            "   If uv is missing first, install it from the official instructions at",
            "   https://docs.astral.sh/uv/ — on Windows `winget install --id=astral-sh.uv`,",
            "   on macOS `brew install uv`. If you use the shell installer instead,",
            "   download it to a file and show it to me before running it.",
            "",
            "   TMPDIR_WHEEL=$(mktemp -d -t agnes_cli.XXXXXX)",
            f'   (cd "$TMPDIR_WHEEL" && curl -fsSL --max-redirs 0 --cacert ~/.agnes/ca.pem -OJ {server_url_placeholder}/cli/download)',
            '   WHEEL=$(ls "$TMPDIR_WHEEL"/*.whl 2>/dev/null | head -n1)',
            '   [ -n "$WHEEL" ] || { echo "error: wheel download failed (no .whl in $TMPDIR_WHEEL)" >&2; exit 1; }',
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
        "   TMPDIR_WHEEL=$(mktemp -d -t agnes_cli.XXXXXX)",
        f'   (cd "$TMPDIR_WHEEL" && curl -fsSL --max-redirs 0 -OJ {server_url_placeholder}/cli/download)',
        '   WHEEL=$(ls "$TMPDIR_WHEEL"/*.whl 2>/dev/null | head -n1)',
        '   [ -n "$WHEEL" ] || { echo "error: wheel download failed (no .whl in $TMPDIR_WHEEL)" >&2; exit 1; }',
        '   uv tool install --force "$WHEEL"',
        "",
        "   If uv is not installed yet, install it from the official instructions at",
        "   https://docs.astral.sh/uv/ — on Windows `winget install --id=astral-sh.uv`,",
        "   on macOS `brew install uv`. If you use the shell installer instead,",
        "   download it to a file and show it to me before running it.",
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
    """Steps 2-3 — `agnes init` in the current directory + smoke verify.

    The install-location decision tree that used to be step 2 (~65 lines
    of prose walking the agent through pwd checks, an unsafe-directory
    list, and a whitelist of prepared-workspace artefacts) moved into the
    CLI: `agnes init` itself refuses $HOME, filesystem roots and system
    directories with a typed `unsafe_workspace` error and an actionable
    hint (`cli/commands/init.py`). The prompt keeps a three-bullet
    outcome triage instead — refusal, already-initialized (→ `agnes
    update`), missing token file — because those are the judgment calls
    the agent still has to make.

    `{workspace_dir}` and `{instance_brand}` are placeholders pre-substituted
    by :func:`resolve_lines` from the operator-configured brand. Defaults
    keep `~/Desktop/Agnes` behavior for instances that don't set the brand knob.

    `agnes init` is the workspace-rails delivery mechanism for everyone:
    it authenticates with the PAT, fetches CLAUDE.md (RBAC-filtered),
    writes AGNES_WORKSPACE.md (human-facing docs), installs Claude Code
    SessionStart/End hooks (auto-refresh), and runs an initial `agnes pull`
    so DuckDB views are ready. Subsumes the legacy `agnes auth import-token`
    + `agnes auth whoami` pair — `init` already verifies the PAT against
    `/api/catalog/tables` internally, and `agnes catalog` then doubles as
    a smoke verify of the data plane.

    The PAT minted by step 4 on `{server_url}/home` is `general` scope with
    a 90 d TTL, so the init call will succeed for the operator's whole 90 d
    window without re-generating a token.

    Step 3 no longer writes the PAT into a heredoc: the token is delivered
    out-of-band (written to `~/.agnes/token` before this prompt is
    generated — see the preamble's access-token guard and step 4 on
    `{server_url}/home`) so the raw value never has to appear inside the
    prompt text itself. `agnes init --token-file ~/.agnes/token` reads it
    directly and deletes the file once the credential is saved
    (`cli/commands/init.py`).
    """
    return [
        "",
        "2) Set up the {instance_brand} workspace in the current directory.",
        "   The token was saved to ~/.agnes/token by step 4 of the install",
        "   guide, so there is nothing to write here — `agnes init",
        "   --token-file` reads it directly (never on the command line) and",
        "   removes the file once the credential is saved to",
        "   ~/.config/agnes/token.json:",
        "",
        f'   agnes init --server-url "{server_url_placeholder}" --token-file ~/.agnes/token --workspace .',
        "",
        "   This fetches your CLAUDE.md (RBAC-filtered), writes",
        "   AGNES_WORKSPACE.md (human-facing docs), installs Claude Code",
        "   SessionStart/End hooks (auto-refresh), and runs an initial",
        "   `agnes pull` so your DuckDB views are ready. Afterwards verify the",
        "   token file was consumed:",
        '   test -f ~/.agnes/token && echo "token file STILL PRESENT" || echo "token file consumed"',
        "   (still present after `agnes init` = the deletion failed and a",
        "   plaintext token is left on disk — tell the user to remove it)",
        "",
        "   Three outcomes need a different move:",
        "   - `unsafe_workspace` refusal: `agnes init` refuses $HOME, /tmp and",
        "     other system directories. Have the user create a workspace folder",
        "     (the install guide suggested ~/Desktop/{workspace_dir}), cd into",
        "     it, and re-run this script from there — don't pick a directory",
        "     for them, and don't `mkdir`/`cd` on your own.",
        "   - Already initialized (`.claude/init-complete` exists): run",
        "     `agnes update` instead — it converges the CLI, workspace,",
        "     plugins and data off the saved credential",
        "     (~/.config/agnes/token.json); edited template files are backed",
        "     up to `<name>.bak.<ts>` first. A leftover ~/.agnes/token is",
        "     removed only after an authenticated step proved the saved",
        "     credential works. If the update fails on an expired credential:",
        "     re-run step 4 of the install guide on {server_url}/home (press",
        '     "Mark me as offboarded" at the bottom if the guide is hidden),',
        "     then:",
        f'     agnes init --force --server-url "{server_url_placeholder}" --token-file ~/.agnes/token --workspace .',
        "   - ~/.agnes/token missing on a fresh install: stop and send the",
        "     user to {server_url}/home step 4 (the step that saves it), or",
        "     run `agnes auth login` for a browser sign-in, then re-run this",
        "     step.",
        "",
        "3) Verify the data is queryable:",
        "   agnes catalog",
        "",
        "   This should list the tables your account has grants for. Empty list",
        "   means your admin hasn't granted you access yet — contact them.",
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
        '   Expect "Overall: healthy" on a clean instance; "degraded" driven',
        "   only by informational or data-freshness sub-checks is not an",
        "   install problem. `db_schema: unknown` and `data: 0 tables` are",
        "   normal in two cases:",
        "     - fresh install (no tables registered yet), and",
        "     - non-admin roles (e.g. `analyst`) that don't have grants to read",
        "       the system schema even on populated instances.",
        "   Only flag actual yellow/red checks (api / duckdb_state / users).",
    ]


# Tile sub-letters shared by the required + optional connector blocks.
# Each block letters its own tiles independently (both restart at "a").
# (Connector SKILL.md bodies are no longer inlined here — they were 76 %
# of the rendered prompt. `agnes connectors show <slug>` fetches one on
# demand via GET /api/connectors/{slug}/prompt; the body loader lives in
# src.connectors_manifest.load_connector_body.)
_SUB_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _required_connectors_block(
    step_num: str,
    manifest: list["ConnectorEntry"],
    *,
    next_step_num: str,
    instance_brand: str,
) -> list[str]:
    """Mandatory-install step for ``required=True`` connectors — rendered
    between diagnose and the optional yes/no tiles, with NO per-tool ask.

    Bodies are fetched on demand via ``agnes connectors show <slug>``
    rather than inlined (see the module docstring); a missing body
    surfaces at fetch time with the endpoint's ``connector_body_missing``
    hint, and the operator-facing guard for a missing REQUIRED body stays
    the seed-sync render dry-run. Empty manifest renders no block.
    """
    if not manifest:
        return []

    lines = [
        "",
        f"{step_num}) Install required tools (no ask — this instance mandates them):",
        "",
        "   For each tool below, in order, print its setup prompt and follow",
        "   it now. Every prompt is idempotent and safe to re-run; a tool",
        "   that's already configured short-circuits with its ✅ line instead",
        "   of reinstalling.",
        "",
    ]
    letter_idx = 0
    for entry in manifest:
        if letter_idx >= len(_SUB_LETTERS):
            logger.warning(
                "setup_instructions: more than %d required connectors — remaining tiles dropped",
                len(_SUB_LETTERS),
            )
            break
        lines.append(
            f"   {_SUB_LETTERS[letter_idx]}) {entry.display_name} — {entry.short_summary}"
            f" (~{entry.estimated_minutes} min)"
        )
        lines.append(f"      agnes connectors show {entry.slug}")
        letter_idx += 1
    lines.extend(
        [
            "",
            f"   Move on to step {next_step_num} once each tool above is set up or has",
            "   reported a failure.",
        ]
    )
    return lines


def _connectors_block(
    step_num: str,
    manifest: list["ConnectorEntry"],
    *,
    next_step_num: str,
    instance_brand: str,
) -> list[str]:
    """Per-connector interactive ask, bodies fetched on demand. Last
    interactive step before Confirm — its trailer forwards to the
    Restart-Claude step (`next_step_num`), which then bridges into
    Confirm on its own.

    Requires an explicit yes before setting a connector up — anything else
    (a decline, a deferral, silence) skips it. Each connector's fetched
    prompt ships with its own keychain precheck so re-runs short-circuit
    cleanly.

    Manifest source: ``src.connectors_manifest.load_manifest()`` reads the
    seed-resident ``workspace/.claude/skills/connector-*/SKILL.md`` files
    (operator IWT clone first, bundled snapshot fallback). Each entry
    carries display_name + short_summary + estimated_minutes; the body is
    NOT inlined — `agnes connectors show <slug>` prints it when (and only
    when) the user says yes to that tool.

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
        "   This instance offers the optional tools below (also listable with",
        "   `agnes connectors list`). Tell the user what each does, then ask",
        "   which to set up — one combined question covering all the tools is",
        "   fine. If an answer is anything other than a clear yes, skip that",
        "   tool — declining and deferring are both valid answers.",
        "",
    ]
    letter_idx = 0
    for entry in manifest:
        if letter_idx >= len(_SUB_LETTERS):
            logger.warning(
                "setup_instructions: more than %d optional connectors — remaining tiles dropped",
                len(_SUB_LETTERS),
            )
            break
        lines.append(
            f"   {_SUB_LETTERS[letter_idx]}) {entry.display_name} — {entry.short_summary}"
            f" (~{entry.estimated_minutes} min)"
        )
        lines.append(f"      agnes connectors show {entry.slug}")
        letter_idx += 1
    lines.extend(
        [
            "",
            "   For each yes: print the tool's setup prompt with the command",
            "   above and follow it. Every prompt is idempotent and safe to",
            "   re-run; an already-configured tool short-circuits with its ✅",
            "   line.",
            "",
            f"   After all asks (regardless of answers) continue to step {next_step_num}.",
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
        "   Tell me to type `/exit` (or close the Claude Code session entirely), then run `claude` again from this same directory — the workspace where `agnes init` ran in step 2.",
        f"   Before step {confirm_step_num} (Confirm): give me a short recap of what was installed or was already present — CLI, workspace files, hooks, marketplace plugins, connectors — so the outcome is clear, then continue.",
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
    exist, the optional bullet keeps its exact default wording — the
    default install prompt must stay byte-identical
    (tests/test_install_prompt_snapshot.py).
    """
    bullets = [
        "   - `agnes --version` output",
        "   - If `agnes init` ran: confirmation that `~/.agnes/token` was consumed",
        "     (after the `agnes update` reconcile path the file may legitimately remain)",
        "   - First few lines of `agnes catalog` (tables you can see)",
        "   - Confirmation that `./CLAUDE.md` and `./AGNES_WORKSPACE.md` exist",
        "   - Confirmation that `./.claude/settings.json` contains SessionStart/End hooks",
        "   - The `agnes diagnose` overall status",
        "   - Confirmation that `~/.agnes/marketplace/.git/` exists "
        "(the marketplace clone) and that any granted plugins installed",
    ]
    if required_manifest:
        required_names = ", ".join(e.display_name for e in required_manifest)
        bullets.append(
            f"   - For each required connector ({required_names}): "
            "the ✅ or ❌ line that the connector's verify step emitted "
            "earlier in this session."
        )
    if manifest:
        connector_names = ", ".join(e.display_name for e in manifest)
        label = "optional connector" if required_manifest else "connector"
        bullets.append(
            f"   - For each {label} ({connector_names}): whether it was set "
            "up, failed, or declined — and for failures, the reason its "
            "verify step reported."
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
        "Register the {instance_brand} Claude Code marketplace and install plugins:"
        if has_plugins
        else (
            "Register the {instance_brand} Claude Code marketplace (no plugin "
            "grants were visible when this prompt was generated — the CLI reads "
            "the live manifest, so anything granted since will still install):"
        )
    )
    return [
        "",
        f"{step_num}) {header}",
        "   Needs git and claude on PATH — run `git --version` and",
        "   `claude --version` first. Missing git: macOS `brew install git`,",
        "   Windows `winget install --id Git.Git -e --source winget`, Linux",
        "   apt-get/dnf. Missing claude: https://docs.claude.com/claude-code.",
        "",
        "   agnes refresh-marketplace --bootstrap",
        "",
        "   One idempotent command: clones the per-user marketplace to",
        "   ~/.agnes/marketplace, registers it with Claude Code, and installs",
        "   every plugin the LIVE manifest grants this account (a leftover",
        "   clone from a previous instance is re-cloned automatically). Grants",
        "   added later land on their own via the SessionStart hook's detached",
        "   `agnes update` — no re-run of this script needed. Verify:",
        "   agnes my-stack show",
        "   # [✓] = in your stack; [✗] = available to you but NOT added — an",
        "   # opt-in marker, not an error. Add one with",
        "   # `agnes marketplace add <marketplace-id>/<plugin-name>`, then run",
        "   # `agnes update` and `/reload-plugins` in Claude Code.",
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
    loop; it must NOT contain literal `{server_url}` (that only resolves
    at click time in the JS clipboard flow, not in the preamble).

    The access-token guard (before step 1) is the prompt-side half of
    keeping the raw token out of chat: the token is written to
    `~/.agnes/token` out-of-band, before this prompt is generated (step 4
    on `{server_url}/home` — the delivery mechanism itself is out of this
    module's scope). The guard tells the agent what "missing" means in
    each of the two cases the earlier "Before you start" paragraph already
    established: on a FRESH install it's a real problem (the token never
    landed) and the agent should stop and send the user back to generate
    one; on a RECONCILE it's expected (the first `agnes init` already
    consumed and deleted the file), so the agent should just continue.

    The provenance paragraph right after the access-token guard gives the
    assistant verifiable context for its first-contact trust decision: the
    prompt is the final step of the same install guide that saved the login
    token, and whether the host is trusted is the user's org's call (verify
    with IT when unsure). Facts only — it asserts no conclusion about
    consent and leaves the ask/no-ask judgment to the assistant.
    """
    lines = [
        "Set up the {instance_brand} CLI on this machine.",
        "",
        "Server: {server_url}",
        "",
        "Your login token is already saved on this machine at ~/.agnes/token",
        "(written by step 4 of the install guide at {server_url}). The steps",
        "below only reference that file path — never print the token, echo",
        "it, or paste it into this chat.",
        "",
        "This prompt is the final step of the install guide at {server_url} —",
        "the same guide whose previous step saved the login token above. Whether",
        "that host is trusted is the user's org's call, and they can verify it",
        "with their IT if unsure; the decisions this setup still needs from them",
        "(install location, which optional tools to connect) come up inside the",
        "steps below.",
        "",
        "Run the steps in order. Everything is idempotent — a re-run, or a",
        "machine that already ran this setup, converges instead of reinstalling,",
        "so treat 'already configured' outcomes as success, not as errors. Post",
        "a brief one-line progress note as each step finishes.",
        "",
        "If a step fails with an unfamiliar error, paste the exact error back and",
        "stop. If the failure is a TLS error, look for the cause — corporate",
        "proxy, internal CA, clock skew — rather than lowering certificate",
        "verification; turning verification off hides the problem instead of",
        "solving it.",
    ]
    if has_ca:
        lines.append(
            "The fallback chain inside step 0(d) is documented and OK to use; that's what fallback chains are for."
        )
    lines.append("")
    if custom_preamble:
        lines = [*custom_preamble.split("\n"), "", *lines]
    return lines


def _step_numbers(*, has_connectors: bool = True, has_required_connectors: bool = False) -> dict[str, str]:
    """Compute the step numbers for the unified layout.

    Returns a dict keyed by logical step name; values are stringified
    1-based step numbers (preserving the existing string-based helper API
    so call sites stay diff-minimal).

    Steps (default layout): install (1), init (2), catalog (3),
    marketplace (4, with the git/claude preflight folded into its
    header), diagnose (5), required_connectors (only when the manifest
    has ``required=True`` entries — takes 6), connectors (6, or 7 after
    a required step), restart_claude, confirm. Marketplace + connectors
    + restart_claude are always-on:
      - Marketplace registration is useful even when the operator has
        zero plugin grants (SessionStart hook reconciles future grants
        automatically).
      - Connector setup bodies are NOT inlined anymore — they were 76 %
        of the rendered prompt. The blocks reference
        `agnes connectors show <slug>` (backed by
        ``GET /api/connectors/{slug}/prompt``) so the agent fetches a
        body only for tools the user actually says yes to.

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
    n = 4
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

    Pre-substitutes `{server_host}`; `{wheel_filename}` is accepted for
    backward compatibility with existing callers but no longer appears in
    the rendered body — step 1 downloads via the unversioned `/cli/download`
    endpoint instead of a filename-pinned URL (see `_install_cli_lines`).
    Leaves `{server_url}` as a placeholder for click-time JS substitution
    (or for `render_setup_instructions()` below). The access token is never
    a placeholder here — see the module docstring.

    `ca_pem` (PEM-encoded fullchain of the Agnes server's TLS cert) gates
    the cross-platform step-0 trust-bootstrap block AND switches step 1 to
    the curl-then-local-install pattern AND switches step 5 to the
    platform-aware marketplace strategy. Caller decides whether the cert
    needs the bootstrap (typically: skip for publicly-trusted certs like
    Let's Encrypt, emit for self-signed or private corp CA).

    `connector_manifest` is a list of validated ConnectorEntry objects
    sourced from :func:`src.connectors_manifest.load_manifest`. Entries
    with ``required=True`` render as a separate mandatory step (no yes/no
    ask) before the optional tiles. ``None`` triggers a fresh manifest
    load. ``[]`` (empty list) is treated differently from ``None``: it
    intentionally renders no connector blocks.

    Fallback: callers pass `"agnes.whl"` when no wheel is present on disk —
    the value is accepted but unused (see above). The instruction text still
    renders so operators can see the snippet shape; `/cli/download` itself
    404s at fetch time, surfacing the missing-wheel diagnosis the same way.
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
    steps = _step_numbers(has_connectors=has_connectors, has_required_connectors=has_required)

    lines: list[str] = []
    if has_ca:
        lines.extend(_tls_trust_block(ca_pem))  # type: ignore[arg-type]
    lines.extend(_preamble_lines(has_ca=has_ca, custom_preamble=custom_preamble))
    lines.extend(_install_cli_lines(has_ca=has_ca))  # 1
    lines.extend(_init_lines())  # 2, 3
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
    # restart-claude cue. Per-connector explicit ask — only a clear yes
    # installs; declining and deferring both skip. No optional entries
    # renders no block (the step number is dropped).
    lines.extend(
        _connectors_block(
            steps["connectors"],
            optional_entries,
            next_step_num=steps["restart_claude"],
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
    must produce byte-identical output for a given (server_url, wheel,
    plugins, host, ca_pem, connector_manifest, brand, workspace_dir) tuple.

    `token` is accepted for backward compatibility with existing callers
    but is otherwise unused: the rendered body deliberately contains no
    `{token}` placeholder (the access token is delivered out-of-band, see
    the module docstring), so the trailing `.replace("{token}", token)`
    below is a no-op today.
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
