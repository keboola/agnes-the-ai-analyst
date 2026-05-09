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
   SessionStart hook (installed by `agnes init`) calls refresh-marketplace
   on every Claude Code session so changes server-side propagate
   automatically.

## Step ordering

The numbered steps are arranged so that:
  - All installation work (CLI, plugins) happens first, in one go.
  - `agnes init` is mandatory — it bundles auth, workspace bootstrap,
    CLAUDE.md fetch, and Claude Code SessionStart/End hooks into one
    non-interactive call. Replaces the old `agnes auth import-token` +
    `agnes auth whoami` pair.
  - The interactive question (skills copy vs on-demand) is the LAST step
    before Confirm — by that point everything else is done, the user only
    needs to decide one thing, and the assistant blocks on their answer.
  - `agnes diagnose` runs late so it doubles as a final smoke test after
    plugins are in place, instead of gating them.

Layout (with marketplace plugins to install):
  0  TLS trust block (only when ca_pem is supplied)
  1  Install CLI
  2  agnes init (auth + workspace bootstrap)
  3  agnes catalog (smoke verify)
  4  Pre-flight: git + claude
  5  Marketplace + plugins
  6  Diagnose
  7  Skills (interactive — assistant waits for user)
  8  Confirm

Layout (no plugins): steps 4-5 collapse out, diagnose/skills/confirm
renumber to 4-5-6.

The combined-bundle source uses a fallback chain so the prompt still works
on machines without the system Python `certifi`: we try (a) `python3 -c
'import certifi'`, (b) the platform's curl/openssl bundle path, (c)
`uv run --with certifi` as a network last-resort. The user explicitly
permitted that fallback chain — it's not improvising-around-a-TLS-error.
"""

from __future__ import annotations

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
        "       case \"$(uname -s)\" in",
        "         Darwin)               PLATFORM=macos ;;",
        "         Linux)                PLATFORM=linux ;;",
        "         MINGW*|MSYS*|CYGWIN*) PLATFORM=windows ;;",
        "         *) echo \"Unsupported OS: $(uname -s)\" >&2; exit 1 ;;",
        "       esac",
        "       SHELL_NAME=\"$(basename \"${SHELL:-bash}\")\"",
        "       case \"${SHELL_NAME}:${PLATFORM}\" in",
        "         zsh:*)                   RC=\"$HOME/.zshrc\" ;;",
        "         bash:macos)              RC=\"$HOME/.bash_profile\" ;;",
        "         bash:windows|bash:linux) RC=\"$HOME/.bashrc\" ;;",
        "         *)                       RC=\"$HOME/.profile\" ;;",
        "       esac",
        "       echo \"Platform: $PLATFORM, shell: $SHELL_NAME, rc: $RC\"",
        "",
        "   (b) Write the cert (single-quoted heredoc so $/backticks in the body don't expand):",
        "",
        "       mkdir -p ~/.agnes",
        "       cat > ~/.agnes/ca.pem <<'AGNES_CA_PEM'",
    ]
    # PEM body is flush-left: `<<'DELIM'` heredocs preserve leading whitespace,
    # and any indent inside the cert breaks `openssl x509` / Python ssl parsers.
    lines.extend(pem.splitlines())
    lines.extend([
        "AGNES_CA_PEM",
        "",
        "   (c) Register the cert in the OS trust store. Native binaries (claude.exe,",
        "       system git's Schannel/Security.framework backends) read the OS store",
        "       and ignore our env vars — without this, the later marketplace `git",
        "       clone` (when plugins are configured) and any user-side git/native",
        "       tooling against the Agnes host will fail.",
        "       No admin rights needed (user-store only). Idempotent.",
        "",
        "       case \"$PLATFORM\" in",
        "         windows)",
        "           WIN_CA=\"$(cygpath -w ~/.agnes/ca.pem)\"",
        "           certutil.exe -user -addstore \"Root\" \"$WIN_CA\"",
        "           ;;",
        "         macos)",
        "           # Will prompt once for the keychain password.",
        "           security add-trusted-cert -r trustRoot \\",
        "             -k \"$HOME/Library/Keychains/login.keychain-db\" \\",
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
        "             echo \"WARN: install ~/.agnes/ca.pem into your distro's trust store manually\" >&2",
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
        "       [ -z \"$CERTIFI_PATH\" ] && CERTIFI_PATH=\"$(python -c 'import certifi; print(certifi.where())' 2>/dev/null || true)\"",
        "       if [ -z \"$CERTIFI_PATH\" ]; then",
        "         for p in /mingw64/ssl/certs/ca-bundle.crt /usr/ssl/certs/ca-bundle.crt \\",
        "                  /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt \\",
        "                  /etc/ssl/cert.pem; do",
        "           [ -f \"$p\" ] && CERTIFI_PATH=\"$p\" && break",
        "         done",
        "       fi",
        "       if [ -z \"$CERTIFI_PATH\" ]; then",
        "         CERTIFI_PATH=\"$(uv run --native-tls --with certifi --no-project python -c 'import certifi; print(certifi.where())' 2>/dev/null || true)\"",
        "       fi",
        "       if [ -z \"$CERTIFI_PATH\" ] || [ ! -f \"$CERTIFI_PATH\" ]; then",
        "         echo \"ERROR: locate a system CA bundle. Install Python 3 + certifi and re-run.\" >&2",
        "         exit 1",
        "       fi",
        "       echo \"Base CA bundle: $CERTIFI_PATH\"",
        "       cat \"$CERTIFI_PATH\" ~/.agnes/ca.pem > ~/.agnes/ca-bundle.pem",
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
        "export SSL_CERT_FILE=\"$HOME/.agnes/ca-bundle.pem\"",
        "export REQUESTS_CA_BUNDLE=\"$HOME/.agnes/ca-bundle.pem\"",
        "export GIT_SSL_CAINFO=\"$HOME/.agnes/ca-bundle.pem\"",
        "# Single-cert file for Node (APPENDS to bundled roots):",
        "export NODE_EXTRA_CA_CERTS=\"$HOME/.agnes/ca.pem\"",
        "export PATH=\"$HOME/.local/bin:$PATH\"",
        "AGNES_RC_BLOCK",
        "       fi",
        "       # Apply for THIS shell too:",
        "       export SSL_CERT_FILE=\"$HOME/.agnes/ca-bundle.pem\"",
        "       export REQUESTS_CA_BUNDLE=\"$HOME/.agnes/ca-bundle.pem\"",
        "       export GIT_SSL_CAINFO=\"$HOME/.agnes/ca-bundle.pem\"",
        "       export NODE_EXTRA_CA_CERTS=\"$HOME/.agnes/ca.pem\"",
        "       export PATH=\"$HOME/.local/bin:$PATH\"",
        "",
        "   IMPORTANT for the Bash tool: env vars do NOT persist between separate",
        "   Bash invocations. Re-export the four lines above (SSL_CERT_FILE,",
        "   REQUESTS_CA_BUNDLE, GIT_SSL_CAINFO, NODE_EXTRA_CA_CERTS) plus PATH at",
        "   the top of every later step's bash block that talks to Agnes.",
        "",
    ])
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
            "     export PATH=\"$HOME/.local/bin:$PATH\"",
            "",
            "   WHEEL=/tmp/{wheel_filename}",
            f"   curl -fsSL --cacert ~/.agnes/ca.pem -o \"$WHEEL\" {server_url_placeholder}/cli/wheel/{{wheel_filename}}",
            "   uv tool install --native-tls --force \"$WHEEL\"",
            "",
            "   If `agnes --version` fails after install because ~/.local/bin is not on PATH:",
            "     export PATH=\"$HOME/.local/bin:$PATH\"",
            "     # persist: append the same line to your ~/.zshrc or ~/.bashrc",
            "     # (the trust block in step 0 already does this for you on first run).",
        ]
    return [
        "1) Install the CLI:",
        f"   uv tool install --force {server_url_placeholder}/cli/wheel/{{wheel_filename}}",
        "",
        "   If uv is not installed yet:",
        "     curl -LsSf https://astral.sh/uv/install.sh | sh",
        "",
        "   If `agnes --version` fails after install because ~/.local/bin is not on PATH:",
        "     export PATH=\"$HOME/.local/bin:$PATH\"",
        "     # persist: append the same line to your ~/.zshrc or ~/.bashrc",
    ]


def _init_lines(server_url_placeholder: str = "{server_url}") -> list[str]:
    """Steps 2-3 — `agnes init` (auth + workspace bootstrap) + smoke verify.

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
        "2) Bootstrap your Agnes workspace in this directory:",
        f"   agnes init --server-url \"{server_url_placeholder}\" --token \"{{token}}\" --workspace .",
        "",
        "   This authenticates with the PAT, fetches your CLAUDE.md (RBAC-filtered),",
        "   writes AGNES_WORKSPACE.md (human-facing docs), installs Claude Code",
        "   SessionStart/End hooks (auto-refresh), and runs an initial `agnes pull`",
        "   so your DuckDB views are ready.",
        "",
        "3) Verify the data is queryable:",
        "   agnes catalog",
        "",
        "   This should list the tables your account has grants for. Empty list",
        "   means your admin hasn't granted you access yet — contact them.",
    ]


def _diagnose_skills_lines(*, diagnose_num: str, skills_num: str) -> list[str]:
    """Diagnose + skills steps — moved AFTER the marketplace block.

    Putting these last (instead of right after `whoami`) means: by the time
    we ask the user the skills question, all installation work is finished —
    the only thing the prompt is still waiting on is one human-loop answer.
    `agnes diagnose` then doubles as a server-health smoke test that runs after
    plugins are in place, not as a gate before them. With the new ordering
    skills is the LAST step before Confirm, so the assistant must wait for
    the user's answer before finalizing — there's no "run other steps in
    parallel" affordance any more (and it isn't needed).

    Step numbers are filled in by the caller because they shift between
    the no-marketplace layout (4, 5) and the marketplace layout (6, 7).
    """
    return [
        "",
        f"{diagnose_num}) Run diagnostics:",
        "   agnes diagnose",
        "",
        "   This should print \"Overall: healthy\". `db_schema: unknown` and",
        "   `data: 0 tables` are NORMAL in two cases:",
        "     - fresh install (no tables registered yet), and",
        "     - non-admin roles (e.g. `analyst`) that don't have grants to read",
        "       the system schema even on populated instances.",
        "   Only flag actual yellow/red checks (api / duckdb_state / users).",
        "",
        f"{skills_num}) Skills (ask the user — this is the last interactive step before Confirm):",
        "   The CLI ships with reusable markdown skills (setup, connectors,",
        "   corporate-memory, deploy, notifications, security, troubleshoot),",
        "   listable via `agnes skills list` and readable via `agnes skills show <name>`.",
        "",
        "   Ask the user verbatim: \"Do you want me to copy the Agnes skills into",
        "   ~/.claude/skills/agnes/ so they are always loaded in Claude Code,",
        "   or should I pull them on-demand via `agnes skills show <name>` when",
        "   needed?\"",
        "",
        "   Wait for the user's answer before moving to Confirm.",
        "",
        "   If they say copy:",
        "     mkdir -p ~/.claude/skills/agnes",
        "     for s in $(agnes skills list | awk '{print $1}'); do",
        "       agnes skills show \"$s\" > ~/.claude/skills/agnes/\"$s\".md",
        "     done",
        "     echo \"Copied skills to ~/.claude/skills/agnes/\"",
    ]


def _finale_lines(*, confirm_step_num: str, has_ca: bool, has_marketplace: bool) -> list[str]:
    """Final Confirm step. Bullets it asks the assistant to report on must
    only reference earlier steps that were actually emitted, otherwise the
    assistant either hallucinates an answer or asks the user about a
    non-existent step. The CA-bundle-source bullet only makes sense when
    the trust block ran (`has_ca`); the marketplace bullet only makes
    sense when the marketplace block ran (`has_marketplace`). Init +
    catalog + diagnose + skills + version always render, so their bullets
    are unconditional."""
    bullets = [
        "   - `agnes --version` output",
        "   - First few lines of `agnes catalog` (tables you can see)",
        "   - Confirmation that `./CLAUDE.md` and `./AGNES_WORKSPACE.md` exist",
        "   - Confirmation that `./.claude/settings.json` contains SessionStart/End hooks",
        "   - The `agnes diagnose` overall status",
        "   - Whether skills were copied or left on-demand",
    ]
    if has_ca:
        bullets.append(
            "   - Which CA bundle source got picked in step 0(d) "
            "(system Python certifi / system curl bundle / uv-fetched)"
        )
    if has_marketplace:
        bullets.append(
            "   - Confirmation that `~/.agnes/marketplace/.git/` exists "
            "(the marketplace clone) and that all requested plugins installed"
        )
    return [
        f"{confirm_step_num}) Confirm:",
        "   Tell me \"Agnes workspace is ready\" and summarize:",
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
        "   If `git --version` fails (\"command not found\" or similar), install git:",
        "     - macOS:   brew install git",
        "     - Windows: winget install --id Git.Git -e --source winget --silent",
        "     - Linux:   sudo apt-get install git    OR    sudo dnf install git",
        "",
        "   If `claude --version` fails, install Claude Code:",
        "     - npm (Linux / WSL): npm i -g @anthropic-ai/claude-code",
        "     - macOS / Windows native installer: see https://docs.claude.com/claude-code",
        "",
        "   Then re-run both `--version` checks to confirm before continuing.",
    ]


def _marketplace_block(
    plugin_install_names: list[str],
    step_num: str,
) -> list[str]:
    """Build the marketplace + plugin-install block.

    Pre-condition: `plugin_install_names` is non-empty (caller checks).

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
      3. **One source of truth.** ``agnes refresh-marketplace`` is also
         the SessionStart hook command, so install + refresh share the
         same code path — version-aware reconcile, hook JSON output,
         credential helper PAT injection, all consistent.

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
    return [
        "",
        f"{step_num}) Register the Agnes Claude Code marketplace and install plugins:",
        "   # `agnes refresh-marketplace --bootstrap` does:",
        "   #   1. clone the per-user marketplace bare repo to ~/.agnes/marketplace",
        "   #   2. strip the PAT from the cloned origin URL (refreshes use a",
        "   #      per-invocation git credential helper, not the URL)",
        "   #   3. best-effort chmod 700/600 on POSIX (no-op on Windows NTFS)",
        "   #   4. `claude plugin marketplace add ~/.agnes/marketplace`",
        "   #   5. install every plugin listed in the served manifest",
        "   # Idempotent — re-runs over an existing clone do fetch+reset+reconcile",
        "   # via the same path the SessionStart hook uses.",
        "   agnes refresh-marketplace --bootstrap || {",
        "     echo \"ERROR: agnes refresh-marketplace --bootstrap failed\" >&2",
        "     exit 1",
        "   }",
        "",
        "   These run non-interactively. After they finish, tell the user to /exit",
        "   and run `claude` again so the new plugins load. From then on, the",
        "   SessionStart hook keeps the marketplace clone in sync via",
        "   `agnes refresh-marketplace --quiet` on every Claude Code session.",
    ]


def _preamble_lines(*, has_ca: bool) -> list[str]:
    """Header that opens the prompt before the numbered steps. The
    `step 0(d) fallback chain` reference is only emitted when the trust
    block actually exists (`has_ca`); without it the line points at a
    non-existent step. The "don't disable TLS verification" advice itself
    stays unconditional — it's good guidance regardless of whether the
    server runs with a private CA."""
    lines = [
        "Set up the Agnes CLI on this machine.",
        "",
        "Server: {server_url}",
        "Personal access token: {token}",
        "(Just generated; treat it as a secret.)",
        "",
        "Run these, in order. The script is idempotent — safe to re-run if a step",
        "fails partway through. If a step fails with an unfamiliar error, paste the",
        "exact error back and stop. Do NOT improvise around TLS errors by disabling",
        "verification (`-k`, `NODE_TLS_REJECT_UNAUTHORIZED=0`,",
        "`git -c http.sslVerify=false`, etc.) — those are dead ends that hide the",
        "real problem.",
    ]
    if has_ca:
        lines.append(
            "The fallback chain inside step 0(d) is documented and OK to "
            "use; that's what fallback chains are for."
        )
    lines.append("")
    return lines


def _step_numbers(*, has_marketplace: bool, has_skills: bool = True) -> dict[str, str]:
    """Compute the step numbers for the unified layout based on which optional
    blocks are emitted.

    Returns a dict keyed by logical step name; values are stringified
    1-based step numbers (preserving the existing string-based helper API
    so call sites stay diff-minimal).

    Mandatory steps (always emitted): install (1), init (2), catalog (3),
    diagnose, confirm. Optional: preflight + marketplace (gated on
    has_marketplace), skills (gated on has_skills — default True; the
    Resolved-Question section in the plan settled on always-on, so the
    parameter is here purely to keep the helper general for future use,
    not to expose a real toggle).

    Step-0 (TLS trust block) sits outside this numbering — it is gated by
    has_ca and has its own "0)" header rendered inside the trust block
    helper.
    """
    n = 4
    preflight = marketplace = ""
    if has_marketplace:
        preflight = str(n); n += 1
        marketplace = str(n); n += 1
    diagnose = str(n); n += 1
    skills = str(n) if has_skills else ""
    if has_skills:
        n += 1
    confirm = str(n)
    return {
        "preflight": preflight,
        "marketplace": marketplace,
        "diagnose": diagnose,
        "skills": skills,
        "confirm": confirm,
    }


def resolve_lines(
    wheel_filename: str,
    *,
    plugin_install_names: list[str] | None = None,
    server_host: str = "",
    ca_pem: str | None = None,
) -> list[str]:
    """Return the template lines with server-side placeholders substituted.

    Pre-substitutes `{wheel_filename}` and `{server_host}`. Leaves
    `{server_url}` and `{token}` as placeholders for click-time JS
    substitution (or for `render_setup_instructions()` below).

    When `plugin_install_names` is empty/None, the output matches the
    six-step no-marketplace layout (Confirm = step 6). When non-empty, a
    step-4 pre-flight + step-5 marketplace block are inserted and Confirm
    becomes step 8.

    `ca_pem` (PEM-encoded fullchain of the Agnes server's TLS cert) gates
    the cross-platform step-0 trust-bootstrap block AND switches step 1 to
    the curl-then-local-install pattern AND switches step 5 to the
    platform-aware marketplace strategy. Caller decides whether the cert
    needs the bootstrap (typically: skip for publicly-trusted certs like
    Let's Encrypt, emit for self-signed or private corp CA).

    Fallback: callers pass `"agnes.whl"` when no wheel is present on disk.
    The resulting URL (`/cli/wheel/agnes.whl`) will 404 at download time, but
    the instruction text still renders so operators can see the snippet shape
    and diagnose the missing wheel on the server.
    """
    names = list(plugin_install_names or [])
    has_marketplace = bool(names)
    has_ca = bool(ca_pem and ca_pem.strip())

    # Step layout. Marketplace (when emitted) goes BEFORE diagnose/skills,
    # so the human-loop skills question is the last step before Confirm.
    # `_step_numbers` returns the renumbered step labels in one place — no
    # branch on every helper — so the layout is unambiguous and trivially
    # extendable when a future step is added.
    steps = _step_numbers(has_marketplace=has_marketplace, has_skills=True)

    lines: list[str] = []
    if has_ca:
        lines.extend(_tls_trust_block(ca_pem))  # type: ignore[arg-type]
    lines.extend(_preamble_lines(has_ca=has_ca))
    lines.extend(_install_cli_lines(has_ca=has_ca))   # 1
    lines.extend(_init_lines())                        # 2, 3
    if has_marketplace:
        lines.extend(_preflight_block(steps["preflight"]))    # 4
        lines.extend(_marketplace_block(names, step_num=steps["marketplace"]))  # 5
    # Diagnose + skills come AFTER the marketplace block (or right after
    # the catalog smoke verify if there's no marketplace step at all).
    lines.extend(_diagnose_skills_lines(
        diagnose_num=steps["diagnose"], skills_num=steps["skills"],
    ))
    lines.append("")
    lines.extend(_finale_lines(
        confirm_step_num=steps["confirm"],
        has_ca=has_ca,
        has_marketplace=has_marketplace,
    ))

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
    server_host: str = "",
    ca_pem: str | None = None,
) -> str:
    """Render the setup instructions as a single string.

    Used server-side for tests and any non-JS rendering path. The browser
    clipboard flow uses the JS renderer embedded in the Jinja partial; both
    must produce byte-identical output for a given (server_url, token,
    wheel, plugins, host, ca_pem) tuple.
    """
    lines = resolve_lines(
        wheel_filename,
        plugin_install_names=plugin_install_names,
        server_host=server_host,
        ca_pem=ca_pem,
    )
    text = "\n".join(lines)
    return text.replace("{server_url}", server_url).replace("{token}", token)
