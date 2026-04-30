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

   So the marketplace step branches on platform:
     - Windows + macOS → straight to system-`git clone` fallback
       (system git honors `GIT_SSL_CAINFO`, so the clone works).
     - Linux → typically the node-based npm install where
       `NODE_EXTRA_CA_CERTS` does take effect; try direct first, fall
       back to git clone on failure.

   The OS trust-store registration in (c) is still done on all three
   platforms because it's needed for *non-claude* native tools — e.g.
   the system git fetch path itself (Schannel on Windows, Security
   framework on macOS) trusts via the OS store, not via env vars.

## Step ordering

The numbered steps are arranged so that:
  - All installation work (CLI, plugins) happens first, in one go.
  - The interactive question (skills copy vs on-demand) is the LAST step
    before Confirm — by that point everything else is done, the user only
    needs to decide one thing, and the assistant blocks on their answer.
  - `da diagnose` runs late so it doubles as a final smoke test after
    plugins are in place, instead of gating them.

Layout (with marketplace plugins to install):
  0  TLS trust block (only when ca_pem is supplied)
  1  Install CLI
  2  Login
  3  Verify
  4  Git check
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
        "",
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
    """Step 1 — install the `da` CLI.

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
            "   If `da --version` fails after install because ~/.local/bin is not on PATH:",
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
        "   If `da --version` fails after install because ~/.local/bin is not on PATH:",
        "     export PATH=\"$HOME/.local/bin:$PATH\"",
        "     # persist: append the same line to your ~/.zshrc or ~/.bashrc",
    ]


# Steps 2-3: login + verify. Static — these always come right after install.
_LOGIN_VERIFY_LINES: list[str] = [
    "",
    "2) Log in (also saves the server URL):",
    "   da auth import-token --token \"{token}\" --server \"{server_url}\"",
    "",
    "3) Verify the login:",
    "   da auth whoami",
]


def _diagnose_skills_lines(*, diagnose_num: str, skills_num: str) -> list[str]:
    """Diagnose + skills steps — moved AFTER the marketplace block.

    Putting these last (instead of right after `whoami`) means: by the time
    we ask the user the skills question, all installation work is finished —
    the only thing the prompt is still waiting on is one human-loop answer.
    `da diagnose` then doubles as a server-health smoke test that runs after
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
        "   da diagnose",
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
        "   listable via `da skills list` and readable via `da skills show <name>`.",
        "",
        "   Ask the user verbatim: \"Do you want me to copy the Agnes skills into",
        "   ~/.claude/skills/agnes/ so they are always loaded in Claude Code,",
        "   or should I pull them on-demand via `da skills show <name>` when",
        "   needed?\"",
        "",
        "   Wait for the user's answer before moving to Confirm.",
        "",
        "   If they say copy:",
        "     mkdir -p ~/.claude/skills/agnes",
        "     for s in $(da skills list | awk '{print $1}'); do",
        "       da skills show \"$s\" > ~/.claude/skills/agnes/\"$s\".md",
        "     done",
        "     echo \"Copied skills to ~/.claude/skills/agnes/\"",
    ]


def _finale_lines(*, confirm_step_num: str, has_ca: bool, has_marketplace: bool) -> list[str]:
    """Final Confirm step. Bullets it asks the assistant to report on must
    only reference earlier steps that were actually emitted, otherwise the
    assistant either hallucinates an answer or asks the user about a
    non-existent step. The CA-bundle-source bullet only makes sense when
    the trust block ran (`has_ca`); the marketplace direct-vs-clone bullet
    only makes sense when the marketplace block ran (`has_marketplace`).
    Skills + diagnose + version + whoami always render, so their bullets
    are unconditional."""
    bullets = [
        "   - `da --version` output",
        "   - `da auth whoami` output (email + role)",
        "   - Whether skills were copied or left on-demand",
        "   - The `da diagnose` overall status",
    ]
    if has_ca:
        bullets.append(
            "   - Which CA bundle source got picked in step 0(d) "
            "(system Python certifi / system curl bundle / uv-fetched)"
        )
    if has_marketplace:
        bullets.append(
            "   - Whether the marketplace add went via direct HTTPS or via the "
            "git-clone fallback (and on which platform)"
        )
    return [
        f"{confirm_step_num}) Confirm:",
        "   Tell me \"Agnes CLI is ready\" and summarize:",
        *bullets,
    ]


def _git_check_block(step_num: str) -> list[str]:
    """Git pre-flight check — runs before the marketplace clone.

    `claude plugin marketplace add` (and our git-clone fallback) shells out
    to `git`, so a missing git binary fails the marketplace step with a
    confusing error. Cross-platform install commands cover the three
    supported workstation OSes:
      - macOS: Homebrew (`brew install git`). The Xcode CLT bundle also
        ships git; we prefer brew because it's non-interactive.
      - Windows: winget (`winget install --id Git.Git -e ...`). Bundled
        with Windows 10 1809+ and Windows 11; non-interactive with --silent.
      - Linux: apt or dnf, depending on distro family.

    `step_num` is parameterized because step ordering shifted between
    layouts (the marketplace block now runs before diagnose/skills, so
    git-check + marketplace are steps 4-5 instead of 6-7).
    """
    return [
        "",
        f"{step_num}) Make sure git is installed (required for the marketplace clone):",
        "     git --version",
        "",
        "   If that fails (\"command not found\" or similar), install git:",
        "     - macOS:   brew install git",
        "     - Windows: winget install --id Git.Git -e --source winget --silent",
        "     - Linux:   sudo apt-get install git    OR    sudo dnf install git",
        "",
        "   Then re-run `git --version` to confirm before continuing.",
    ]


def _marketplace_block(
    plugin_install_names: list[str],
    self_signed_tls: bool,
    has_ca: bool,
    step_num: str,
) -> list[str]:
    """Build the marketplace + plugin-install block.

    Pre-condition: `plugin_install_names` is non-empty (caller checks).

    `step_num` is parameterized because step ordering shifted between
    layouts (this block now runs before diagnose/skills, so it's step 5
    instead of the old step 7).

    With `has_ca=True`: the user has the trust block from step 0, so we know
    the cert is in the OS store and our env vars are set. Strategy:
      - Windows: claude.exe is a Bun-compiled binary that ignores both the
        Windows trust store AND NODE_EXTRA_CA_CERTS for marketplace HTTPS.
        Skip the direct attempt; system `git clone` honors GIT_SSL_CAINFO
        (the combined bundle from step 0) and works.
      - macOS: same story. `claude` on macOS arm64 ships as a Mach-O binary
        with a `__BUN` segment (single-file Bun build); empirically it
        ignores SSL_CERT_FILE / NODE_EXTRA_CA_CERTS / login keychain alike,
        even though `strings` shows the binary recognizes those env-var
        names. Go straight to git-clone on macOS too.
      - Linux: still ships node-based claude on most distros (npm install
        path), where NODE_EXTRA_CA_CERTS does take effect. Try direct
        first, fall back to git clone on failure.

    Token hygiene: after the clone, we strip the PAT from the cloned repo's
    `origin` URL (`git remote set-url`) and chmod ~/.agnes/marketplace tight.
    Reason: `git clone https://x:<PAT>@host/...` writes the URL verbatim
    into `.git/config`, where it sits in plaintext for anything that reads
    home (cloud sync, antivirus scanners, peer processes). claude's
    marketplace registration uses the local FS path, not the remote URL,
    so stripping the token after clone is harmless — to refresh later, the
    user re-runs setup from the dashboard with a fresh PAT.

    With `has_ca=False`: the legacy path. If `self_signed_tls=True` we emit
    the host-scoped `git config sslVerify=false` downgrade so the marketplace
    git-clone (under claude's hood) works against an untrusted endpoint.
    """
    if has_ca:
        lines: list[str] = [
            "",
            f"{step_num}) Register the Agnes Claude Code marketplace and install plugins.",
            "",
            "   Strategy depends on platform:",
            "     - Windows + macOS: `claude` ships as a Bun-compiled native binary on",
            "       these platforms, which ignores the OS trust store and our CA env",
            "       vars for marketplace HTTPS. Skip the direct attempt and use a",
            "       system `git clone` (system git honors GIT_SSL_CAINFO from step 0).",
            "     - Linux: claude is typically the node-based npm install, where",
            "       NODE_EXTRA_CA_CERTS works. Try direct first; fall back to git",
            "       clone on failure.",
            "",
            "   case \"$PLATFORM\" in",
            "     linux)",
            "       if claude plugin marketplace add \"https://x:{token}@{server_host}/marketplace.git/\" 2>/dev/null; then",
            "         MARKETPLACE_VIA=direct",
            "       else",
            "         MARKETPLACE_VIA=clone",
            "       fi",
            "       ;;",
            "     *)",
            "       MARKETPLACE_VIA=clone",
            "       ;;",
            "   esac",
            "",
            "   if [ \"$MARKETPLACE_VIA\" = \"clone\" ]; then",
            "     # Heads-up: 'git: credential-manager-core is not a git command' is a",
            "     # harmless warning from a stale git config — the clone itself succeeds.",
            "     rm -rf ~/.agnes/marketplace",
            "     git clone \"https://x:{token}@{server_host}/marketplace.git/\" ~/.agnes/marketplace || {",
            "       echo \"ERROR: marketplace clone failed — verify step 0 trust block + network reachability\" >&2",
            "       exit 1",
            "     }",
            "     # Strip the PAT from the cloned repo's origin URL so it doesn't sit",
            "     # in plaintext at ~/.agnes/marketplace/.git/config. Future marketplace",
            "     # refreshes go via re-running setup (new PAT) from the dashboard, not",
            "     # via `git pull` against this clone.",
            "     git -C ~/.agnes/marketplace remote set-url origin \"https://{server_host}/marketplace.git/\"",
            "     # Best-effort tighten on POSIX; chmod is a no-op on Windows NTFS via",
            "     # MSYS / Git Bash, hence the `|| true` so the step never fails there.",
            "     chmod 700 ~/.agnes/marketplace ~/.agnes/marketplace/.git 2>/dev/null || true",
            "     chmod 600 ~/.agnes/marketplace/.git/config 2>/dev/null || true",
            "     claude plugin marketplace add ~/.agnes/marketplace || {",
            "       echo \"ERROR: claude plugin marketplace add failed\" >&2",
            "       exit 1",
            "     }",
            "   fi",
            "",
        ]
        for name in plugin_install_names:
            lines.append(
                f"   claude plugin install {name}@{_MARKETPLACE_NAME} --scope project || {{"
            )
            lines.append(
                f"     echo \"ERROR: claude plugin install {name}@{_MARKETPLACE_NAME} failed\" >&2; exit 1"
            )
            lines.append("   }")
        lines.extend([
            "",
            "   These run non-interactively. After they finish, tell the user to /exit",
            "   and run `claude` again so the new plugins load.",
        ])
        return lines

    # Legacy path: no ca_pem on disk. Keep the old behavior verbatim
    # (host-scoped sslVerify=false when self_signed_tls is set, otherwise
    # plain direct HTTPS) so existing AGNES_DEBUG_AUTH instances keep
    # working until they roll a fullchain.pem.
    lines = [
        "",
        f"{step_num}) Register the Agnes Claude Code marketplace and install plugins:",
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


def resolve_lines(
    wheel_filename: str,
    *,
    plugin_install_names: list[str] | None = None,
    self_signed_tls: bool = False,
    server_host: str = "",
    ca_pem: str | None = None,
) -> list[str]:
    """Return the template lines with server-side placeholders substituted.

    Pre-substitutes `{wheel_filename}` and `{server_host}`. Leaves
    `{server_url}` and `{token}` as placeholders for click-time JS
    substitution (or for `render_setup_instructions()` below).

    When `plugin_install_names` is empty/None, the output matches the
    original 6-step layout (Confirm = step 6). When non-empty, a step-6
    git-check + step-7 marketplace block are inserted and Confirm becomes
    step 8.

    `ca_pem` (PEM-encoded fullchain of the Agnes server's TLS cert) gates
    the cross-platform step-0 trust-bootstrap block AND switches step 1 to
    the curl-then-local-install pattern AND switches step 7 to the
    platform-aware marketplace strategy. Caller decides whether the cert
    needs the bootstrap (typically: skip for publicly-trusted certs like
    Let's Encrypt, emit for self-signed or private corp CA).

    `self_signed_tls=True` is the legacy fallback when no `ca_pem` is
    available — it prepends a host-scoped
    `git config http."<host>/".sslVerify false` inside the marketplace
    block (TLS *downgrade*, not bootstrap). When `ca_pem` is set, this
    flag is ignored because the trust block subsumes it. No-op when the
    marketplace block isn't rendered (no plugins).

    Fallback: callers pass `"agnes.whl"` when no wheel is present on disk.
    The resulting URL (`/cli/wheel/agnes.whl`) will 404 at download time, but
    the instruction text still renders so operators can see the snippet shape
    and diagnose the missing wheel on the server.
    """
    names = list(plugin_install_names or [])
    has_marketplace = bool(names)
    has_ca = bool(ca_pem and ca_pem.strip())
    # Trust block subsumes the legacy sslVerify-off downgrade. Don't emit
    # both: with `~/.agnes/ca-bundle.pem` wired into GIT_SSL_CAINFO, git already
    # trusts the host without disabling verification.
    effective_self_signed = self_signed_tls and not has_ca

    # Step layout. Marketplace goes BEFORE diagnose/skills, so the human-loop
    # skills question is the last step before Confirm. Numbers shift between
    # the no-marketplace layout (only 4 = diagnose, 5 = skills, 6 = confirm)
    # and the marketplace layout (4 = git, 5 = marketplace, 6 = diagnose,
    # 7 = skills, 8 = confirm).
    if has_marketplace:
        git_step, marketplace_step = "4", "5"
        diagnose_step, skills_step, confirm_step = "6", "7", "8"
    else:
        git_step = marketplace_step = ""  # unused; here just for symmetry
        diagnose_step, skills_step, confirm_step = "4", "5", "6"

    lines: list[str] = []
    if has_ca:
        lines.extend(_tls_trust_block(ca_pem))  # type: ignore[arg-type]
    lines.extend(_preamble_lines(has_ca=has_ca))
    lines.extend(_install_cli_lines(has_ca=has_ca))   # 1
    lines.extend(_LOGIN_VERIFY_LINES)                  # 2, 3
    if has_marketplace:
        lines.extend(_git_check_block(git_step))       # 4
        lines.extend(_marketplace_block(              # 5
            names, effective_self_signed, has_ca=has_ca, step_num=marketplace_step,
        ))
    # Diagnose + skills come AFTER the marketplace block (or right after
    # whoami if there's no marketplace step at all).
    lines.extend(_diagnose_skills_lines(
        diagnose_num=diagnose_step, skills_num=skills_step,
    ))
    lines.append("")
    lines.extend(_finale_lines(
        confirm_step_num=confirm_step,
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
    self_signed_tls: bool = False,
    server_host: str = "",
    ca_pem: str | None = None,
) -> str:
    """Render the setup instructions as a single string.

    Used server-side for tests and any non-JS rendering path. The browser
    clipboard flow uses the JS renderer embedded in the Jinja partial; both
    must produce byte-identical output for a given (server_url, token,
    wheel, plugins, flag, host, ca_pem) tuple.
    """
    lines = resolve_lines(
        wheel_filename,
        plugin_install_names=plugin_install_names,
        self_signed_tls=self_signed_tls,
        server_host=server_host,
        ca_pem=ca_pem,
    )
    text = "\n".join(lines)
    return text.replace("{server_url}", server_url).replace("{token}", token)
