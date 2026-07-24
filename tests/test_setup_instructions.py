"""Tests for the setup-instructions template + resolver.

`uv tool install` validates the PEP 427 filename in the URL path before
fetching, so our setup snippet cannot use a stable alias like `agnes.whl`.
These tests pin the wheel-filename substitution behavior, the marketplace
block layout, and the cross-platform TLS trust block (`ca_pem` path).

The trust-block tests assert behaviors that came out of a real-world
multi-machine setup pass — see the v2 design notes in the module docstring
of `app/web/setup_instructions.py` for the rationale behind each assertion
(combined CA bundle vs. single-cert SSL_CERT_FILE, OS-trust-store
registration for native binaries, platform-aware marketplace strategy,
curl-then-local-install around rustls' `CaUsedAsEndEntity`).
"""


def test_resolve_lines_substitutes_wheel_filename():
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines("agnes_the_ai_analyst-2.0.0-py3-none-any.whl")
    joined = "\n".join(lines)
    assert "{wheel_filename}" not in joined
    assert "/cli/wheel/agnes_the_ai_analyst-2.0.0-py3-none-any.whl" in joined


def test_resolve_lines_fallback_filename_is_honoured():
    """Callers pass `'agnes.whl'` when no wheel is on disk; substitution still works."""
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines("agnes.whl")
    assert "{wheel_filename}" not in "\n".join(lines)
    assert any("/cli/wheel/agnes.whl" in line for line in lines)


def test_render_setup_instructions_wires_all_placeholders():
    from app.web.setup_instructions import render_setup_instructions

    out = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="T-123",
        wheel_filename="agnes_the_ai_analyst-2.0.0-py3-none-any.whl",
    )
    assert "{server_url}" not in out
    assert "{token}" not in out
    assert "{wheel_filename}" not in out
    assert "https://agnes.example.com/cli/wheel/agnes_the_ai_analyst-2.0.0-py3-none-any.whl" in out
    assert "T-123" in out


def test_init_step_has_no_security_judgment_suppression():
    """The install prompt must NOT instruct the agent to suppress its
    own security judgment around the PAT that lands in the transcript. The
    former NOTE ordering the agent not to warn / not to mark the session
    private / not to run `agnes mark-private`, and the `--token` inline
    "auto-classifier / credential-exfil" framing, are removed — Claude Code's
    hardened security protocol reads such copy as an attempt to bypass its own
    protections and blocks the install. Only the legitimate mechanics remain:
    write the PAT to a file so it never lands in the command-line argv.
    """
    from app.web.setup_instructions import _init_lines

    joined = "\n".join(_init_lines())
    # Legit mechanics preserved — file-based token keeps it out of argv.
    assert "~/.agnes/token" in joined
    assert "--token-file" in joined
    assert "argv" in joined
    # Anti-safety suppression must be gone (2 paragraphs + 1 sentence here).
    assert "security incident" not in joined
    assert "do not warn" not in joined
    assert "do not mark this session" not in joined
    assert "do not run `agnes mark-private`" not in joined
    assert "auto-classifier" not in joined
    assert "credential-exfil" not in joined
    assert "escape hatch" not in joined
    assert "! agnes init" not in joined


def test_step4_has_no_agnes_private_tip():
    """The install prompt must NOT carry the `/agnes-private` private-session
    tip — private-session guidance belongs in the workspace docs, not the
    one-shot setup prompt. Step 4 ends at the catalog-grants hint."""
    from app.web.setup_instructions import _init_lines

    joined = "\n".join(_init_lines())
    assert "/agnes-private" not in joined
    assert "agnes-sessions-private-skipped" not in joined
    assert "deliberate action" not in joined
    assert "never run it for them" not in joined
    # Step 4 still verifies the data plane.
    assert "4) Verify the data is queryable:" in joined
    assert "agnes catalog" in joined


def test_resolve_lines_no_plugins_unified_layout():
    """Unified always-on layout: 1 install, 2 mkdir/cd, 3 init, 4 catalog,
    5 preflight, 6 marketplace, 7 diagnose, 8 connectors, 9 restart-claude,
    10 confirm.
    Preflight + marketplace + MCP + connectors block are emitted even when
    the operator has zero plugin grants — registering the per-user
    marketplace clone pre-wires the SessionStart hook, the Atlassian
    Remote MCP applies to every analyst whose work touches Jira/
    Confluence, and the connectors block is per-connector default-yes
    (the user can decline each individually). Skills step deleted in
    #242."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    # Mandatory unified-flow steps.
    assert "1) Install the CLI" in joined
    assert "3) Bootstrap your Agnes workspace" in joined
    assert "4) Verify the data is queryable:" in joined
    assert "5) Make sure git and claude are installed" in joined
    assert "6) Register the Agnes Claude Code marketplace" in joined
    assert "7) Run diagnostics:" in joined
    assert "8) Connect the user's tools" in joined
    assert "10) Confirm:" in joined
    # No stray Confirms at other positions.
    assert "11) Confirm:" not in joined
    assert "6) Confirm:" not in joined
    # Restart-claude step lands between connectors and Confirm.
    assert "9) Restart Claude Code" in joined
    # Skills step is intentionally absent.
    assert "Skills (ask the user" not in joined
    # The marketplace step header adapts to the no-grants-visible copy
    # rather than the plugin-installing variant — phrased as a render-time
    # snapshot (grants may change after the prompt is generated), with the
    # live-truth verification step alongside.
    assert "no plugin grants visible when this prompt was generated" in joined
    assert "agnes my-stack show" in joined
    assert "agnes refresh-marketplace --bootstrap" in joined
    # MCP step uses SSE transport for Atlassian's hosted Remote MCP.
    assert "claude mcp add --transport sse atlassian https://mcp.atlassian.com/v1/sse" in joined
    # Legacy `git config sslVerify=false` downgrade must NOT be emitted.
    # Match the specific config line, not the bare substring (which appears
    # in the preamble as a "don't do this" example).
    assert "git config --global" not in joined
    # Trust block isn't emitted without ca_pem either.
    assert "0) Trust the Agnes TLS certificate" not in joined
    assert "step 0(d)" not in joined
    assert "Which CA bundle source got picked" not in joined
    # Legacy admin-only auth verbs are gone — `agnes init` subsumes them.
    assert "agnes auth import-token" not in joined
    assert "agnes auth whoami" not in joined


def test_preamble_step_zero_d_reference_only_when_trust_block_emitted():
    """The preamble's "fallback chain inside step 0(d)" line is only
    correct when step 0 actually exists. Without ca_pem the reference
    points at a non-existent step."""
    from app.web.setup_instructions import resolve_lines

    no_ca = "\n".join(resolve_lines("agnes.whl"))
    assert "step 0(d)" not in no_ca
    # The "don't disable TLS verification" guidance still appears (it's
    # generic safety advice, valid regardless of trust block).
    assert "NODE_TLS_REJECT_UNAUTHORIZED" in no_ca

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKEFAKEFAKE\n-----END CERTIFICATE-----\n"
    with_ca = "\n".join(resolve_lines("agnes.whl", ca_pem=fake_ca))
    # Trust block emits step 0 → preamble's step 0(d) reference is now valid.
    assert "step 0(d)" in with_ca


def test_finale_bullets_match_emitted_steps():
    """The Confirm step's bullets must reference only steps that were
    actually emitted. CA bundle bullet is gated on `has_ca`. The
    marketplace clone bullet is unconditional now (Fix B in 2026-05-10
    init-report response: marketplace block is always emitted regardless
    of plugin grants)."""
    from app.web.setup_instructions import resolve_lines

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"

    # No ca, no plugins: marketplace bullet present, CA bullet absent.
    plain = "\n".join(resolve_lines("agnes.whl"))
    assert "Which CA bundle source got picked" not in plain
    assert "~/.agnes/marketplace/.git/" in plain

    # ca only: both bullets present.
    ca_only = "\n".join(resolve_lines("agnes.whl", ca_pem=fake_ca))
    assert "Which CA bundle source got picked" in ca_only
    assert "~/.agnes/marketplace/.git/" in ca_only

    # plugins only: marketplace bullet yes, CA bullet no.
    pl_only = "\n".join(resolve_lines("agnes.whl", plugin_install_names=["foo"], server_host="h"))
    assert "Which CA bundle source got picked" not in pl_only
    assert "~/.agnes/marketplace/.git/" in pl_only

    # Both: both bullets present.
    both = "\n".join(
        resolve_lines(
            "agnes.whl",
            plugin_install_names=["foo"],
            server_host="h",
            ca_pem=fake_ca,
        )
    )
    assert "Which CA bundle source got picked" in both
    assert "~/.agnes/marketplace/.git/" in both


def test_trust_block_rc_heredoc_writes_exactly_8_lines():
    """The trust block emits a heredoc that appends to the user's shell rc.
    The companion `agnes-client-reset.sh` strips the block via awk that
    `skip = 8` from the AGNES_CA_PEM_TRUST marker, so the heredoc MUST
    write exactly 8 lines (marker + 7 export/comment lines). If the
    heredoc body is 9+ lines, repeated install/reset cycles leave stray
    empty lines in the rc file (Devin Review round 3 BUG_0001).

    Source of truth pinning: this test cross-checks the marker count with
    the reset script's `skip = N` so the two stay in sync."""
    from app.web.setup_instructions import _tls_trust_block

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"
    lines = _tls_trust_block(fake_ca)
    joined = "\n".join(lines)

    # Locate heredoc bounds in the emitted shell.
    start = joined.index("<<'AGNES_RC_BLOCK'")
    end = joined.index("\nAGNES_RC_BLOCK\n", start)
    # Body = lines BETWEEN the opening `<<'AGNES_RC_BLOCK'` line and the
    # closing `AGNES_RC_BLOCK` delimiter.
    after_open = joined.index("\n", start) + 1  # first body line starts here
    body = joined[after_open:end]
    body_lines = body.split("\n")

    # Must be exactly 8 lines: marker + 7 content lines.
    assert len(body_lines) == 8, (
        f"Heredoc body has {len(body_lines)} lines; reset script awk "
        f"skips 8 lines, so any drift leaves stray lines in the rc file. "
        f"Body was:\n" + "\n".join(f"  {i + 1:2d} {ln!r}" for i, ln in enumerate(body_lines))
    )
    # First body line MUST be the marker (anchor for the reset awk).
    assert body_lines[0] == "# AGNES_CA_PEM_TRUST — added by Agnes setup"


def test_trust_block_rc_heredoc_count_matches_reset_script_skip():
    """Stronger version of the previous test: read the actual `skip = N`
    integer literal out of `scripts/dev/agnes-client-reset.sh` and assert
    it matches the heredoc body line count. If someone changes either
    side without updating the other, this test fails loudly."""
    import re
    from pathlib import Path
    from app.web.setup_instructions import _tls_trust_block

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"
    joined = "\n".join(_tls_trust_block(fake_ca))
    start = joined.index("<<'AGNES_RC_BLOCK'")
    end = joined.index("\nAGNES_RC_BLOCK\n", start)
    after_open = joined.index("\n", start) + 1
    body_line_count = len(joined[after_open:end].split("\n"))

    # Resolve the reset script relative to this test file (works from any cwd).
    repo_root = Path(__file__).resolve().parents[1]
    reset_sh = (repo_root / "scripts" / "dev" / "agnes-client-reset.sh").read_text()
    match = re.search(r"AGNES_CA_PEM_TRUST.*?skip\s*=\s*(\d+)", reset_sh, re.DOTALL)
    assert match, "Could not locate `skip = N` near AGNES_CA_PEM_TRUST in reset script"
    reset_skip = int(match.group(1))

    assert body_line_count == reset_skip, (
        f"Heredoc body has {body_line_count} lines but reset script skips "
        f"{reset_skip}. Update one side to match — either trim the heredoc "
        f"or bump the awk skip count."
    )


def test_trust_block_step_0c_does_not_reference_stale_step_number():
    """Step 0(c) used to say 'without this, step 7's marketplace add fails'
    but after the layout reordering, marketplace is step 5 (when plugins
    exist) or doesn't exist at all (when no plugins). The reference must
    not name a stale step number."""
    from app.web.setup_instructions import resolve_lines

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"
    joined = "\n".join(resolve_lines("agnes.whl", ca_pem=fake_ca))
    # The stale "step 7's marketplace add" string must be gone.
    assert "step 7's marketplace add" not in joined
    # Replacement text describes the consequence without a step number.
    assert "marketplace `git" in joined and "clone`" in joined


def test_resolve_lines_with_plugins_uses_install_first_diagnose_last_layout():
    """Marketplace layout puts install/mkdir/init/catalog/preflight/marketplace
    BEFORE diagnose, so diagnose is the final smoke test before the
    restart-claude cue. Step numbers: 5 preflight, 6 marketplace, 7 diagnose,
    8 connectors, 9 restart-claude, 10 confirm. No skills step —
    interactive copy-or-on-demand question was confusing; on-demand
    `agnes skills show` is the default."""
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines(
        "agnes.whl",
        plugin_install_names=["foo", "bar"],
        server_host="agnes.example.com",
    )
    joined = "\n".join(lines)
    # Step 4 — pre-flight, with all three platforms' install commands.
    assert "5) Make sure git and claude are installed" in joined
    assert "git --version" in joined
    assert "claude --version" in joined
    assert "brew install git" in joined
    assert "winget install --id Git.Git -e --source winget --silent" in joined
    assert "sudo apt-get install git" in joined or "sudo dnf install git" in joined
    # Step 5 — marketplace + stack install.
    assert "6) Register the Agnes Claude Code marketplace" in joined
    assert "agnes refresh-marketplace --bootstrap" in joined
    # The destructive prep + per-plugin install commands are inside the
    # CLI; the prompt must not emit the inline shell forms in
    # operator-runnable lines.
    executable = _executable_lines(joined)
    assert "rm -rf ~/.agnes/marketplace" not in executable
    assert "git clone " not in executable
    assert "git remote set-url origin" not in executable
    assert "claude plugin marketplace add" not in executable
    assert "claude plugin install foo@agnes" not in executable
    assert "claude plugin install bar@agnes" not in executable
    # Step 6 — Atlassian MCP registration (Fix C in 2026-05-10 init-report response).
    # Step 7 — diagnose now AFTER marketplace + MCP wiring.
    assert "7) Run diagnostics:" in joined
    # Step 8 — connectors, last interactive step before restart-claude
    # (skills step deleted in #242).
    assert "8) Connect the user's tools" in joined
    # Step 9 — restart-claude. Step 10 — Confirm.
    assert "9) Restart Claude Code" in joined
    assert "10) Confirm:" in joined
    for stray in (
        "4) Confirm:",
        "5) Confirm:",
        "6) Confirm:",
        "7) Confirm:",
        "8) Confirm:",
        "9) Confirm:",
        "11) Confirm:",
    ):
        assert stray not in joined
    # Crucial ordering invariants for the new layout.
    install_idx = joined.index("1) Install the CLI")
    init_idx = joined.index("3) Bootstrap your Agnes workspace")
    catalog_idx = joined.index("4) Verify the data is queryable:")
    git_idx = joined.index("5) Make sure git and claude are installed")
    market_idx = joined.index("6) Register the Agnes Claude Code marketplace")
    diag_idx = joined.index("7) Run diagnostics:")
    conn_idx = joined.index("8) Connect the user's tools")
    restart_idx = joined.index("9) Restart Claude Code")
    confirm_idx = joined.index("10) Confirm:")
    assert install_idx < init_idx < catalog_idx < git_idx < market_idx < diag_idx < conn_idx < restart_idx < confirm_idx
    # Legacy `git config sslVerify=false` downgrade is gone — see CHANGELOG.
    assert "git config --global" not in joined
    # server_host is server-side substituted; the placeholder must be gone.
    assert "{server_host}" not in joined
    # server_url + token are still placeholders for click-time JS substitution.
    assert "{server_url}" in joined
    assert "{token}" in joined


def test_preflight_checks_both_git_and_claude():
    """Pre-flight (step 4 when marketplace is gated on) checks BOTH binaries
    before the marketplace clone — `git --version` is needed for the clone
    itself, `claude --version` is needed for the `claude plugin
    marketplace add` / `claude plugin install` calls. Either missing
    breaks the marketplace step in a confusing way, so we surface the
    failure before we get there.
    """
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            plugin_install_names=["foo"],
            server_host="agnes.example.com",
        )
    )
    # Both version checks present.
    assert "git --version" in joined
    assert "claude --version" in joined
    # Header mentions both tools.
    assert "Make sure git and claude are installed" in joined
    # Install hints for claude — npm one-liner for Linux/WSL plus a doc URL
    # for native installers on macOS / Windows. We don't try to one-line a
    # native installer; the canonical instructions live upstream.
    assert "npm i -g @anthropic-ai/claude-code" in joined
    assert "https://docs.claude.com/claude-code" in joined
    # Both checks come BEFORE the marketplace add line.
    git_check_idx = joined.index("git --version")
    claude_check_idx = joined.index("claude --version")
    market_idx = joined.index("claude plugin marketplace add")
    assert git_check_idx < market_idx
    assert claude_check_idx < market_idx


def test_render_setup_instructions_with_plugins_substitutes_all_placeholders():
    from app.web.setup_instructions import render_setup_instructions

    out = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="T-XYZ",
        wheel_filename="agnes-1.0-py3-none-any.whl",
        plugin_install_names=["foo", "bar"],
        server_host="agnes.example.com",
    )
    # No raw placeholders remain in the final string.
    assert "{server_url}" not in out
    assert "{token}" not in out
    assert "{wheel_filename}" not in out
    assert "{server_host}" not in out
    # Token still appears for `agnes init` (step 2). The marketplace
    # step uses `agnes refresh-marketplace --bootstrap` which reads the
    # token from the agnes config that step 2 just wrote, so no token
    # in any URL inside step 5.
    assert "T-XYZ" in out
    # The legacy `git config --global ... sslVerify false` downgrade is gone
    # (see CHANGELOG: it tripped Claude Code auto-mode classifiers and was
    # only ever a safety net for AGNES_DEBUG_AUTH instances without a
    # fullchain.pem on disk). Self-signed and private-CA cases are now
    # exclusively handled by the step 0 trust block (gated on `ca_pem`).
    assert "git config --global" not in out
    # Marketplace step is the one-liner; no per-plugin install lines.
    assert "agnes refresh-marketplace --bootstrap" in out
    assert "claude plugin install foo@agnes" not in out
    assert "claude plugin install bar@agnes" not in out


_FAKE_CA_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBkTCB+wIJAKf9$x`cNotARealCert\n"  # `$` and backtick: smoke test for shell-quote safety
    "thisIsNotARealCertificateBodyJustAnInlinePlaceholder==\n"
    "-----END CERTIFICATE-----\n"
)


def test_resolve_lines_with_ca_pem_emits_step_zero_trust_block():
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines("agnes.whl", ca_pem=_FAKE_CA_PEM)
    joined = "\n".join(lines)

    # Step 0 header (must come BEFORE step 1 in the rendered prompt).
    assert "0) Trust the Agnes TLS certificate" in joined
    # The "1) Install the CLI" line wording differs between the ca_pem and
    # no-ca_pem paths; the ca_pem path leads with "1) Install the CLI."
    # (period). Ordering is what matters.
    assert joined.index("0) Trust the Agnes TLS certificate") < joined.index("1) Install the CLI")

    # PEM body inlined verbatim, flush-left (heredoc would corrupt indented content).
    assert "-----BEGIN CERTIFICATE-----" in joined
    assert "-----END CERTIFICATE-----" in joined
    # The PEM is passed inside a single-quoted heredoc so `$` / backtick
    # in real-world cert bodies are NOT shell-expanded — preserve verbatim.
    assert "MIIBkTCB+wIJAKf9$x`cNotARealCert" in joined
    assert "<<'AGNES_CA_PEM'" in joined


def test_resolve_lines_with_ca_pem_emits_cross_platform_substeps():
    """Step 0 must contain the v2 cross-platform sub-blocks: platform detection,
    OS-trust-store registration, combined CA bundle build, env persistence."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", ca_pem=_FAKE_CA_PEM))

    # (a) Platform detection — uname-driven, with all three families covered.
    assert 'case "$(uname -s)" in' in joined
    assert "Darwin" in joined and "PLATFORM=macos" in joined
    assert "Linux" in joined and "PLATFORM=linux" in joined
    # MINGW/MSYS/CYGWIN cover Git Bash on Windows.
    assert "MINGW*|MSYS*|CYGWIN*" in joined and "PLATFORM=windows" in joined
    # Shell rc selection driven by $SHELL, not file existence.
    assert 'SHELL_NAME="$(basename "${SHELL:-bash}")"' in joined
    assert "bash:macos)" in joined and ".bash_profile" in joined  # macOS bash → .bash_profile

    # (c) OS trust store registration — one command per platform.
    assert "certutil.exe -user -addstore" in joined  # Windows
    assert "security add-trusted-cert -r trustRoot" in joined  # macOS
    assert "update-ca-certificates" in joined  # Linux Debian
    assert "update-ca-trust" in joined  # Linux RHEL

    # (d) Combined CA bundle — multi-source fallback chain.
    assert "ca-bundle.pem" in joined  # the combined bundle path
    assert "import certifi; print(certifi.where())" in joined  # system Python source
    # System curl bundle paths covering Git-for-Windows, macOS Homebrew, Debian, RHEL.
    assert "/mingw64/ssl/certs/ca-bundle.crt" in joined
    assert "/etc/ssl/certs/ca-certificates.crt" in joined
    assert "/etc/ssl/cert.pem" in joined
    # uv-fetched as last resort.
    assert "uv run --native-tls --with certifi --no-project" in joined


def test_resolve_lines_with_ca_pem_uses_combined_bundle_for_replace_envs():
    """SSL_CERT_FILE/REQUESTS_CA_BUNDLE/GIT_SSL_CAINFO must point at the
    COMBINED bundle (~/.agnes/ca-bundle.pem), not at the single Agnes cert.
    Pointing them at the single cert would replace the trust store and
    break PyPI / public-host access for any Python tool in the same shell.
    NODE_EXTRA_CA_CERTS keeps pointing at just ca.pem because Node's
    semantics is additive (appends to bundled roots)."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", ca_pem=_FAKE_CA_PEM))

    # REPLACE-semantics envs → combined bundle.
    assert 'export SSL_CERT_FILE="$HOME/.agnes/ca-bundle.pem"' in joined
    assert 'export REQUESTS_CA_BUNDLE="$HOME/.agnes/ca-bundle.pem"' in joined
    assert 'export GIT_SSL_CAINFO="$HOME/.agnes/ca-bundle.pem"' in joined
    # APPEND-semantics env → single-cert file.
    assert 'export NODE_EXTRA_CA_CERTS="$HOME/.agnes/ca.pem"' in joined

    # Persisted to shell rc behind an idempotent grep guard so re-running
    # setup doesn't duplicate the block.
    assert "AGNES_CA_PEM_TRUST" in joined  # marker grep-checks for
    assert "AGNES_RC_BLOCK" in joined  # the rc-append heredoc delimiter


def test_resolve_lines_with_ca_pem_switches_step_one_to_curl_then_local_install():
    """Step 1's install path differs by has_ca:
    - has_ca=True  → curl-then-local-install (avoids rustls CaUsedAsEndEntity)
    - has_ca=False → direct `uv tool install <https-url>` (legacy)
    """
    from app.web.setup_instructions import resolve_lines

    joined_ca = "\n".join(resolve_lines("agnes-1.0-py3-none-any.whl", ca_pem=_FAKE_CA_PEM))
    # curl-with-cacert downloads the wheel locally...
    assert "curl -fsSL --cacert ~/.agnes/ca.pem" in joined_ca
    assert "WHEEL=/tmp/agnes-1.0-py3-none-any.whl" in joined_ca
    # ...then uv installs from the local file with --native-tls.
    assert 'uv tool install --native-tls --force "$WHEEL"' in joined_ca
    # The direct `uv tool install <server-url>` form must NOT appear in the ca_pem path.
    assert "uv tool install --force {server_url}/cli/wheel/" not in joined_ca

    # No-ca_pem path keeps the legacy direct install.
    joined_plain = "\n".join(resolve_lines("agnes-1.0-py3-none-any.whl"))
    assert "uv tool install --force {server_url}/cli/wheel/agnes-1.0-py3-none-any.whl" in joined_plain
    assert "curl -fsSL --cacert" not in joined_plain
    assert "uv tool install --native-tls" not in joined_plain


def _executable_lines(section: str) -> str:
    """Strip shell comment lines so 'not in' assertions match against
    operator-runnable code, not the prose documentation we put in
    comments. A line is a comment when its first non-whitespace character
    is `#`."""
    out: list[str] = []
    for line in section.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def test_resolve_lines_with_ca_pem_marketplace_is_one_liner():
    """Step 5 collapses to a single CLI invocation: `agnes refresh-marketplace
    --bootstrap`. The CLI does clone + PAT-strip + chmod + register-with-Claude
    + auto-install internally so the prompt itself emits no `rm -rf`, no
    `git clone`, no per-plugin install lines.

    The motivation is the Claude Code agent permission gate: when a user
    pastes the install prompt into a Claude Code session, the agent that
    executes it is denied `rm -rf` by default. Pulling the destructive
    prep into the agnes binary (which uses Python `shutil.rmtree`, not
    the `rm -rf` shell pattern) lets the CLI's own permission grant cover
    the cleanup — the prompt stays Claude-Code-friendly.

    Direct HTTPS via `claude plugin marketplace add <https-url>` is broken
    end-to-end on every Claude Code distribution (see _marketplace_block
    docstring), so we never emit it as an alternative."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            plugin_install_names=["foo", "bar"],
            server_host="agnes.example.com",
            ca_pem=_FAKE_CA_PEM,
        )
    )
    # The marketplace step contains the one-liner.
    assert "agnes refresh-marketplace --bootstrap" in joined
    # And nothing else relating to the marketplace install — the inline
    # shell sequence has been pulled into the CLI. We strip comment lines
    # before asserting because the prompt does include a comment block
    # describing what the CLI does internally; that prose is documentation,
    # not operator-runnable code.
    section_idx = joined.index("Register the Agnes Claude Code marketplace")
    section = _executable_lines(joined[section_idx:])
    assert "rm -rf ~/.agnes/marketplace" not in section
    assert "git clone " not in section
    assert "git -C ~/.agnes/marketplace remote set-url" not in section
    assert "chmod 700 ~/.agnes/marketplace" not in section
    assert "claude plugin marketplace add" not in section
    assert "claude plugin install foo@agnes" not in section
    assert "claude plugin install bar@agnes" not in section
    # And no platform-aware switch in the marketplace section (there's
    # still one in step 0(c) for OS trust-store registration; we anchored
    # on the marketplace header above to narrow the slice).
    assert 'case "$PLATFORM"' not in section
    assert "MARKETPLACE_VIA=" not in section


def test_resolve_lines_with_ca_pem_marketplace_has_explicit_error_handling():
    """The marketplace one-liner must still fail loudly with `exit 1` on
    a non-zero exit (so a CLI bootstrap failure blocks downstream steps
    instead of letting them silently misbehave)."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            plugin_install_names=["foo", "bar"],
            server_host="agnes.example.com",
            ca_pem=_FAKE_CA_PEM,
        )
    )
    assert "agnes refresh-marketplace --bootstrap || {" in joined
    # Error message goes to stderr.
    assert ">&2" in joined


def test_diagnose_step_documents_non_admin_role_state():
    """`db_schema: unknown` is normal in two cases — fresh install AND
    non-admin roles (e.g. analyst) without grants on the system schema.
    The original wording only mentioned 'fresh install', leading
    operators on populated instances to chase a phantom yellow check.
    Both contexts must be called out."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "db_schema: unknown" in joined
    assert "0 tables" in joined
    # Both contexts called out.
    assert "fresh install" in joined.lower()
    assert "non-admin" in joined.lower() or "analyst" in joined.lower()


def test_resolve_lines_no_sslverify_downgrade_anywhere():
    """The legacy `git config sslVerify=false` downgrade is gone in every
    rendering combination. Self-signed and private-CA servers must place
    the fullchain at AGNES_TLS_FULLCHAIN_PATH (default
    /data/state/certs/fullchain.pem) so step 0 picks it up via
    _read_agnes_ca_pem; publicly-trusted certs need no trust block at
    all. There is no third path."""
    from app.web.setup_instructions import resolve_lines

    for kwargs in (
        {"plugin_install_names": ["foo"], "server_host": "agnes.example.com"},
        {"plugin_install_names": ["foo"], "server_host": "agnes.example.com", "ca_pem": _FAKE_CA_PEM},
        {"plugin_install_names": [], "server_host": "agnes.example.com"},
    ):
        joined = "\n".join(resolve_lines("agnes.whl", **kwargs))
        assert "git config --global" not in joined, f"sslVerify downgrade leaked through with kwargs={kwargs!r}"
        assert "sslVerify false" not in joined, f"sslVerify downgrade leaked through with kwargs={kwargs!r}"


def test_resolve_lines_ca_pem_empty_string_is_treated_as_absent():
    """`ca_pem=''` (or whitespace-only) must NOT emit the trust block —
    same as None. Guards against `Path.read_text()` returning empty for
    a touched-but-unwritten cert file."""
    from app.web.setup_instructions import resolve_lines

    for empty in ("", "   ", "\n\n"):
        joined = "\n".join(resolve_lines("agnes.whl", ca_pem=empty))
        assert "0) Trust the Agnes TLS certificate" not in joined
        # Also: the no-ca install path is used, not the curl-first one.
        assert "curl -fsSL --cacert" not in joined


def test_resolve_lines_ca_pem_works_without_plugins():
    """Trust block is independent of the marketplace + MCP + connectors
    blocks — emit step 0 even when plugin list is empty. Confirm step is
    at 9 in the always-on layout (skills step deleted in #242, connectors
    added in #243). Step 0 is preamble, not numbered."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", ca_pem=_FAKE_CA_PEM))
    assert "0) Trust the Agnes TLS certificate" in joined
    assert "10) Confirm:" in joined
    # Marketplace block is now emitted unconditionally; the bootstrap
    # one-liner does the `claude plugin marketplace add` internally so
    # the literal string isn't in the prompt text — the user-facing
    # invocation is `agnes refresh-marketplace --bootstrap`.
    assert "agnes refresh-marketplace --bootstrap" in joined


def test_render_setup_instructions_propagates_ca_pem():
    from app.web.setup_instructions import render_setup_instructions

    out = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="T-CA",
        wheel_filename="agnes-1.0-py3-none-any.whl",
        plugin_install_names=["foo"],
        server_host="agnes.example.com",
        ca_pem=_FAKE_CA_PEM,
    )
    assert "0) Trust the Agnes TLS certificate" in out
    assert "-----BEGIN CERTIFICATE-----" in out
    # The legacy `git config sslVerify=false` downgrade was deleted; the
    # ca_pem trust block is the sole TLS-bootstrap path now.
    assert "git config --global" not in out
    # Other placeholders still substituted.
    assert "{server_url}" not in out
    assert "{token}" not in out
    assert "T-CA" in out
    # Curl-then-local-install path is rendered (with placeholders resolved).
    assert "https://agnes.example.com/cli/wheel/agnes-1.0-py3-none-any.whl" in out
    assert 'uv tool install --native-tls --force "$WHEEL"' in out


def test_diagnose_step_documents_normal_states():
    """Step 4 (diagnose) must call out that `db_schema: unknown` and
    `data: 0 tables` are normal on a fresh install — without that the
    operator running the prompt may chase phantom 'errors'."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "db_schema: unknown" in joined
    assert "0 tables" in joined
    assert "NORMAL" in joined or "normal" in joined


def test_no_skills_step_emitted():
    """Skills step was removed: the interactive copy-or-on-demand question
    was confusing for new users (named opinion call with no obvious right
    answer after a wall of technical steps). On-demand lookup via
    `agnes skills show <name>` is the one-size-fits-all default; CLAUDE.md
    references specific skills (e.g. agnes-data-querying) when relevant.

    Regression guard: the rendered prompt must not contain a numbered
    Skills step or the bulk-copy shell loop into ~/.claude/skills/agnes/.
    """
    from app.web.setup_instructions import resolve_lines

    for kwargs in (
        {},
        {"plugin_install_names": ["foo"], "server_host": "h"},
    ):
        joined = "\n".join(resolve_lines("agnes.whl", **kwargs))
        assert "Skills (ask the user" not in joined
        assert "8) Skills" not in joined
        assert "9) Skills" not in joined
        assert "~/.claude/skills/agnes/" not in joined
        assert "for s in $(agnes skills list" not in joined
        assert "Wait for the user's answer" not in joined


def test_no_plugins_layout_keeps_diagnose_before_connectors():
    """Always-on layout: install → mkdir/cd → init → catalog → preflight →
    marketplace → diagnose → connectors → restart_claude → confirm,
    regardless of plugin grants. Step numbers: 7 diagnose, 8 connectors,
    9 restart-claude, 10 confirm. Skills step deleted in #242."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "7) Run diagnostics:" in joined
    assert "8) Connect the user's tools" in joined
    assert "9) Restart Claude Code" in joined
    assert "10) Confirm:" in joined
    diag_idx = joined.index("7) Run diagnostics:")
    conn_idx = joined.index("8) Connect the user's tools")
    restart_idx = joined.index("9) Restart Claude Code")
    confirm_idx = joined.index("10) Confirm:")
    assert diag_idx < conn_idx < restart_idx < confirm_idx


def test_unified_flow_uses_only_agnes_verbs():
    """No-legacy-`da`-verbs invariant for the unified /setup prompt.

    Pin: every line emitted by `resolve_lines()` must use the `agnes` CLI
    verb. The legacy `da` namespace was removed in the broader
    clean-analyst-bootstrap rewrite, but the setup prompt is generated
    string-by-string and a stale `da sync` / `da analyst setup` reference
    could survive a refactor unnoticed.

    Match `"da "` (with the trailing space) so we don't false-positive on
    `Darwin`, `adapter`, `database`, etc. — any actual `da <verb>` invocation
    is followed by a space.

    Also re-verifies that `agnes init` carries an explicit `--token` arg
    (commit 8784f10a fixed a stale-on-disk-token override: `init --token X`
    must use X for the verify call, not the on-disk token). Without
    `--token` in the emitted line, that fix's contract isn't surfaced to
    the user.
    """
    from app.web.setup_instructions import resolve_lines

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"

    # Check both layouts (with and without marketplace) and both has_ca
    # variants, since each path stitches together different helper output.
    for kwargs in (
        {},
        {"plugin_install_names": ["foo"], "server_host": "h"},
        {"ca_pem": fake_ca},
        {"plugin_install_names": ["foo"], "server_host": "h", "ca_pem": fake_ca},
    ):
        joined = "\n".join(resolve_lines("agnes.whl", **kwargs))
        # No legacy `da <verb>` invocation anywhere.
        assert "da " not in joined, (
            f"Legacy `da ` verb leaked into resolve_lines output (kwargs={kwargs!r}).\n"
            f"Search the rendered prompt for the offending line."
        )
        # `agnes init --token` is the contract that commit 8784f10a's
        # ContextVar override pivots on. Pin it so a future refactor that
        # accidentally drops `--token` from the emitted command surfaces as
        # a test failure, not as a confusing 401 in production.
        assert "agnes init --server-url" in joined
        assert "--token" in joined


def test_install_page_uses_versioned_wheel_url(monkeypatch, tmp_path):
    """End-to-end: the /install preview must render the PEP 427 wheel URL,
    so a user copy-pasting the snippet gets a URL `uv tool install` accepts."""
    wheel = tmp_path / "agnes_the_ai_analyst-2.0.0-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04")
    monkeypatch.setenv("AGNES_CLI_DIST_DIR", str(tmp_path))

    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/setup", headers={"host": "agnes.test", "Accept": "text/html"})
    assert resp.status_code == 200
    assert "/cli/wheel/agnes_the_ai_analyst-2.0.0-py3-none-any.whl" in resp.text
    # The bare alias must no longer appear in the rendered snippet.
    assert "/cli/agnes.whl" not in resp.text


# ---------------------------------------------------------------------------
# Connector block (step 8) — per-connector default-yes interactive asks
# wired to seed-resident connector-*/SKILL.md files (bundled snapshot
# fallback when no Initial Workspace Template is configured).
# ---------------------------------------------------------------------------


def test_connectors_block_renders_all_three_asks():
    """Step 8 must contain a default-yes ask for Asana, Google Workspace,
    and Atlassian (Jira / Confluence) and inline each connector's
    SKILL.md body verbatim. The bundled snapshot in the wheel is the
    source when no IWT is configured.
    """
    from src import connectors_manifest as cm

    from app.web.setup_instructions import resolve_lines

    cm.invalidate_cache()
    joined = "\n".join(resolve_lines("agnes.whl"))
    assert 'Ask: "Set up Asana now? (Y/n)"' in joined
    assert 'Ask: "Set up Google Workspace now? (Y/n)"' in joined
    assert 'Ask: "Set up Atlassian (Jira / Confluence) now? (Y/n)"' in joined
    assert "Treat empty/Enter as YES — the default is install" in joined


def test_connectors_block_sub_letters_skip_missing_bodies(monkeypatch):
    """Devin Review on PR #462: when a connector's SKILL.md body is
    missing from the seed, the original `_connectors_block` used the
    raw `enumerate` index to pick a sub-letter, so a skipped middle
    connector caused a gap (e.g. ``a) Asana`` + ``c) GWS`` with no
    ``b)``).

    Regression guard: with the middle connector deliberately missing
    a body, the rendered sub-letters must stay tight a/b/c..., not
    a/c/d... — ``letter_idx`` is incremented ONLY on rendered entries.
    """
    from src import connectors_manifest as cm

    from app.web import setup_instructions as si

    cm.invalidate_cache()

    # Patch _load_connector_body to return None for the middle slug.
    # Manifest order is Asana → Atlassian → GWS (alphabetical by
    # display_name), so dropping Atlassian's body must NOT bump GWS
    # to letter ``c)``.
    real_load = si._load_connector_body

    def patched(slug):
        if slug == "connector-atlassian":
            return None
        return real_load(slug)

    monkeypatch.setattr(si, "_load_connector_body", patched)

    joined = "\n".join(si.resolve_lines("agnes.whl"))

    # Asana rendered first (a), GWS second (b) — Atlassian skipped.
    assert "   a) Asana" in joined
    assert "   b) Google Workspace" in joined
    # The gap-bug shape (Atlassian's slot left as ``b)``, GWS pushed
    # to ``c)``) MUST NOT appear.
    assert "   b) Atlassian" not in joined
    assert "   c) Google Workspace" not in joined


def test_connectors_block_gws_body_describes_oauth_app_branch():
    """The bundled GWS SKILL.md carries BOTH the operator-OAuth-app branch
    (~2 min, frictionless) and the manual GCP walkthrough (~20 min) in
    one body — the seed skill reads `~/.claude/agnes/.env` at runtime to
    pick the right one. Verify the operator-OAuth-app prose is present
    in the rendered output.
    """
    from src import connectors_manifest as cm

    from app.web.setup_instructions import resolve_lines

    cm.invalidate_cache()
    joined = "\n".join(resolve_lines("agnes.whl"))
    # Operator-app branch landmark: the inlined client_secret.json schema
    # block referencing AGNES_GWS_CLIENT_ID from the per-tenant .env file.
    assert "AGNES_GWS_CLIENT_ID" in joined
    assert "client_secret.json" in joined


def test_connectors_block_gws_body_describes_manual_branch():
    """The same bundled body also covers the fallback flow when no
    operator OAuth app is provisioned. Verify the manual `gws auth setup`
    walkthrough text is present.
    """
    from src import connectors_manifest as cm

    from app.web.setup_instructions import resolve_lines

    cm.invalidate_cache()
    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "gws auth setup" in joined


def test_step_numbering_with_connectors_step():
    """_step_numbers must return preflight=5, marketplace=6, diagnose=7,
    connectors=8, restart_claude=9, confirm=10. Anchors the numeric
    expectations the rest of the test suite assumes. (Skills step deleted
    in #242; connectors added in #243; standalone `mcp_servers` step
    retired and folded into the Atlassian connector's prompt body;
    explicit mkdir/cd step added between install and init shifts later
    step numbers up by 1; explicit restart-Claude step added between
    connectors and confirm shifts confirm up by 1 more.)"""
    from app.web.setup_instructions import _step_numbers

    steps = _step_numbers()
    assert steps["preflight"] == "5"
    assert steps["marketplace"] == "6"
    assert "mcp_servers" not in steps
    assert steps["diagnose"] == "7"
    assert steps["required_connectors"] == ""  # none in the default layout
    assert steps["connectors"] == "8"
    assert steps["restart_claude"] == "9"
    assert steps["confirm"] == "10"
    assert "skills" not in steps  # deleted in #242


def test_finale_bullets_mention_connector_outcomes():
    """The Confirm step's summary bullets reference the verbatim ✅/❌ line
    each connector's verify step emitted earlier. Connector names are
    rendered dynamically from the seed manifest — adding a fourth
    connector flows through to the Confirm summary without a code change.
    """
    from src import connectors_manifest as cm

    from app.web.setup_instructions import resolve_lines

    cm.invalidate_cache()
    joined = "\n".join(resolve_lines("agnes.whl"))
    # Bundled manifest sorts alphabetically by display_name: Asana,
    # Atlassian (Jira / Confluence), Google Workspace.
    assert "Asana, Atlassian (Jira / Confluence), Google Workspace" in joined
    assert "✅" in joined
    assert "❌" in joined


# ---------------------------------------------------------------------------
# Required connectors — `required: true` frontmatter renders a separate
# mandatory "Install required tools" step (no Y/n ask) between diagnose
# and the optional tiles; steps renumber around it.
# ---------------------------------------------------------------------------


def _connector_entry(slug: str, name: str, *, required: bool = False):
    from src.connectors_manifest import ConnectorEntry

    return ConnectorEntry(
        slug=slug,
        display_name=name,
        short_summary=f"{name} summary.",
        estimated_minutes=1,
        required=required,
    )


def _fake_bodies(monkeypatch, missing: frozenset[str] = frozenset()):
    """Serve a deterministic body per slug so these tests don't depend on
    the bundled seed content; `missing` slugs resolve to None (the
    missing-body path)."""
    from app.web import setup_instructions as si

    def fake_load(slug: str):
        if slug in missing:
            return None
        return f"Install {slug} for {{instance_brand}}."

    monkeypatch.setattr(si, "_load_connector_body", fake_load)


def test_required_block_mix_layout(monkeypatch):
    """Mix combo: required entries take step 8 (no asks), optional tiles
    shift to 9, restart 10, confirm 11; letters restart per block; the
    inlined body gets the 6-space indent + {instance_brand} substitution.
    """
    from app.web.setup_instructions import resolve_lines

    _fake_bodies(monkeypatch)
    manifest = [
        _connector_entry("connector-xtool", "XTool", required=True),
        _connector_entry("connector-ytool", "YTool", required=True),
        _connector_entry("connector-ztool", "ZTool"),
    ]
    joined = "\n".join(
        resolve_lines(
            "agnes.whl", connector_manifest=manifest, instance_brand="BrandCo"
        )
    )

    req_idx = joined.index(
        "8) Install required tools (mandatory — run every prompt below now):"
    )
    opt_idx = joined.index(
        "9) Connect the user's tools (last interactive ask before Confirm):"
    )
    restart_idx = joined.index("10) Restart Claude Code")
    confirm_idx = joined.index("11) Confirm:")
    assert joined.index("7) Run diagnostics:") < req_idx < opt_idx
    assert opt_idx < restart_idx < confirm_idx

    # Required tools get no ask; the optional tile keeps its ask.
    assert 'Ask: "Set up XTool now? (Y/n)"' not in joined
    assert 'Ask: "Set up YTool now? (Y/n)"' not in joined
    assert 'Ask: "Set up ZTool now? (Y/n)"' in joined
    assert "do NOT ask the user" in joined

    # Letter sequences are independent per block.
    assert "   a) XTool" in joined
    assert "   b) YTool" in joined
    assert "   a) ZTool" in joined

    # Trailer names the next step; body inlined with brand substituted.
    assert "Continue to step 9 only after every required tool above has" in joined
    assert "      Install connector-xtool for BrandCo." in joined


def test_required_only_omits_optional_step_and_renumbers(monkeypatch):
    from app.web.setup_instructions import resolve_lines

    _fake_bodies(monkeypatch)
    manifest = [_connector_entry("connector-xtool", "XTool", required=True)]
    joined = "\n".join(resolve_lines("agnes.whl", connector_manifest=manifest))

    assert "8) Install required tools" in joined
    assert "Connect the user's tools" not in joined
    assert "9) Restart Claude Code" in joined
    assert "10) Confirm:" in joined
    assert "11) Confirm:" not in joined
    # With no optional step, the trailer points at restart (step 9).
    assert "Continue to step 9 only after every required tool above has" in joined


def test_default_manifest_has_no_required_step():
    """OSS default: bundled connectors are all optional — the mandatory
    step and the split finale wording must not appear (byte-identity of
    the full default prompt is test_install_prompt_snapshot.py's job)."""
    from src import connectors_manifest as cm

    from app.web.setup_instructions import resolve_lines

    cm.invalidate_cache()
    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "Install required tools" not in joined
    assert "For each optional connector" not in joined
    assert "For each required connector" not in joined


def test_empty_manifest_renumbers_past_both_blocks():
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", connector_manifest=[]))
    assert "Install required tools" not in joined
    assert "Connect the user's tools" not in joined
    assert "8) Restart Claude Code" in joined
    assert "9) Confirm:" in joined


def test_step_numbers_required_combos():
    """The four combos of (required, optional) presence — numbering stays
    contiguous off the single counter."""
    from app.web.setup_instructions import _step_numbers

    def slots(**kwargs):
        steps = _step_numbers(**kwargs)
        return (
            steps["required_connectors"],
            steps["connectors"],
            steps["restart_claude"],
            steps["confirm"],
        )

    assert slots(has_connectors=False, has_required_connectors=False) == (
        "", "", "8", "9",
    )
    assert slots(has_connectors=True, has_required_connectors=False) == (
        "", "8", "9", "10",
    )
    assert slots(has_connectors=False, has_required_connectors=True) == (
        "8", "", "9", "10",
    )
    assert slots(has_connectors=True, has_required_connectors=True) == (
        "8", "9", "10", "11",
    )


def test_required_block_letters_stay_tight_on_missing_body(monkeypatch):
    """#462 mirror for the required block: a skipped middle body must not
    leave a letter gap."""
    from app.web.setup_instructions import resolve_lines

    _fake_bodies(monkeypatch, missing=frozenset({"connector-btool"}))
    manifest = [
        _connector_entry("connector-atool", "ATool", required=True),
        _connector_entry("connector-btool", "BTool", required=True),
        _connector_entry("connector-ctool", "CTool", required=True),
    ]
    joined = "\n".join(resolve_lines("agnes.whl", connector_manifest=manifest))

    assert "   a) ATool" in joined
    assert "   b) CTool" in joined
    assert "   b) BTool" not in joined
    assert "   c) CTool" not in joined


def test_finale_bullets_split_required_and_optional(monkeypatch):
    """Mix: the Confirm summary carries one bullet per group — required
    first (no "declined" wording; those can't be declined), optional with
    the legacy declined sentence. Required-only: no "declined" at all."""
    from app.web.setup_instructions import resolve_lines

    _fake_bodies(monkeypatch)
    manifest = [
        _connector_entry("connector-xtool", "XTool", required=True),
        _connector_entry("connector-ztool", "ZTool"),
    ]
    joined = "\n".join(resolve_lines("agnes.whl", connector_manifest=manifest))
    req_idx = joined.index("For each required connector (XTool):")
    opt_idx = joined.index("For each optional connector (ZTool):")
    declined_idx = joined.index("If the user declined")
    assert req_idx < opt_idx < declined_idx

    manifest = [_connector_entry("connector-xtool", "XTool", required=True)]
    joined = "\n".join(resolve_lines("agnes.whl", connector_manifest=manifest))
    assert "For each required connector (XTool):" in joined
    assert "If the user declined" not in joined
    assert "say declined" not in joined


def test_restart_claude_step_emitted_unconditionally():
    """`9) Restart Claude Code` renders in every layout (with / without
    plugins, with / without CA) so users never finish setup sitting in
    a stale Claude Code session that has not loaded the freshly-installed
    plugins / MCP servers / SessionStart hooks."""
    from app.web.setup_instructions import resolve_lines

    fake_ca = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"
    for kwargs in (
        {},
        {"plugin_install_names": ["foo"], "server_host": "h"},
        {"ca_pem": fake_ca},
        {"plugin_install_names": ["foo"], "server_host": "h", "ca_pem": fake_ca},
    ):
        joined = "\n".join(resolve_lines("agnes.whl", **kwargs))
        assert "9) Restart Claude Code" in joined, f"missing restart step for kwargs={kwargs!r}"
        # The body should mention /exit + re-running `claude` from the
        # workspace dir so the SessionStart hook (workspace-scoped) fires.
        assert "/exit" in joined
        assert "claude` again" in joined


def test_restart_claude_substitutes_workspace_dir():
    """The restart-claude body interpolates the workspace folder so the
    user sees their actual `~/Desktop/<brand>` path, not a literal placeholder."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            instance_brand="Foundry AI",
            workspace_dir="FoundryAI",
        )
    )
    assert "9) Restart Claude Code" in joined
    assert "~/Desktop/FoundryAI" in joined
    assert "{workspace_dir}" not in joined


def _read_bundled_connector_body(slug: str) -> str:
    """Read the raw SKILL.md content from the bundled seed snapshot.
    Used by the post-A1.2 prompt-content regression tests. The path
    mirrors what ``setup_instructions._load_connector_body`` reads at
    render time — keeps a single source of truth.
    """
    from src.initial_workspace import bundled_seed_path

    path = bundled_seed_path() / "workspace" / ".claude" / "skills" / slug / "SKILL.md"
    return path.read_text(encoding="utf-8")


def test_asana_prompt_uses_pat_not_mcp():
    """Asana reverted to PAT + REST after the hosted MCP path turned out
    to be too token-hungry. Pin both directions: the PAT/keychain flow
    is present in the bundled SKILL.md, AND no MCP-add line is present
    that would silently flip Asana back to MCP.
    """
    body = _read_bundled_connector_body("connector-asana")
    # PAT path present.
    assert "agnes-asana-pat" in body
    assert "https://app.asana.com/api/1.0/users/me" in body
    assert "✅ Asana ready" in body
    # MCP path is gone.
    assert "claude mcp add --transport http asana" not in body
    assert "mcp.asana.com/mcp" not in body
    # The body carries the {instance_brand} placeholder — renderer
    # substitutes at render time (not file read time).
    assert "Claude Code — {instance_brand}" in body


def test_asana_prompt_brand_threads_through_renderer():
    """When `resolve_lines` is called with a brand string, the PAT-label
    placeholder renders with that brand instead of the default 'Agnes'.
    The substitution happens in the renderer (not in the SKILL.md file
    itself) so a single seed file works for every instance.
    """
    from src import connectors_manifest as cm

    from app.web.setup_instructions import resolve_lines

    cm.invalidate_cache()
    joined = "\n".join(resolve_lines("agnes.whl", instance_brand="Foundry AI"))
    assert "Claude Code — Foundry AI" in joined
    assert "{instance_brand}" not in joined


def test_atlassian_prompt_instructs_1_year_expiry():
    """Atlassian PATs default to short-lived; pin the seed-resident
    SKILL.md to direct the user to pick the longest expiry option
    (today: 1 year) so the connector doesn't go stale every couple of
    months.
    """
    body = _read_bundled_connector_body("connector-atlassian")
    assert '"1 year"' in body
    # Acknowledge the lack of a query-param hook so a future contributor
    # doesn't waste an hour trying to deep-link the expiry.
    assert "NO query-parameter hook" in body
    assert "✅ Atlassian ready" in body
    assert "❌ Atlassian setup failed" in body
    assert "Claude Code — {instance_brand}" in body


def test_atlassian_prompt_brand_threads_through_renderer():
    from src import connectors_manifest as cm

    from app.web.setup_instructions import resolve_lines

    cm.invalidate_cache()
    joined = "\n".join(resolve_lines("agnes.whl", instance_brand="Foundry AI"))
    # Atlassian token label uses the brand placeholder.
    assert 'name it "Claude Code — Foundry AI"' in joined


def test_gws_prompt_emits_pass_fail_contract():
    """GWS verify step must emit the uniform ✅/❌ marker the finale
    summary scans for."""
    body = _read_bundled_connector_body("connector-gws")
    assert "✅ Google Workspace ready" in body
    assert "❌ Google Workspace setup failed" in body


# ---------------------------------------------------------------------------
# Step 2 — install location decision tree (refuse / silent / confirm).
# ---------------------------------------------------------------------------


def test_step_2_uses_three_branch_decision_tree():
    """Step 2 must use a refuse / proceed-silently / confirm-once tree
    instead of hard-coding `~/Desktop/<workspace_dir>` as the only
    acceptable path. The old flow scolded any user who cd'd into a
    project folder before pasting; the new flow respects intentional cwd
    and only protects against destructive defaults ($HOME / system dirs).

    Contract:
      - Step header renamed to "Confirm the install location".
      - All three branches (REFUSE, PROCEED SILENTLY, CONFIRM) are
        documented in the script.
      - `pwd` check is still emitted.
      - The refuse list explicitly names `$HOME` plus the system dirs
        the install must never touch.
      - The silent-proceed branch whitelists the workspace artefacts a
        prepared folder might already hold (`.git`, `.claude`, `.agnes`,
        `AGNES_WORKSPACE.md`, `README.md`) so a re-paste into an
        already-initialised workspace doesn't prompt.
      - The confirm branch offers 'ok'/'default'/'abort' (instead of the
        old 'install here'/'abort' pair) — 'default' lets the user opt
        into the recommended `~/Desktop/<workspace_dir>` path without
        re-pasting, and the legacy 'install here' phrasing remains as a
        synonym for 'ok' for muscle-memory compatibility.
      - The script never auto-runs `mkdir` for the user except inside
        the 'default' branch of the confirm prompt.
    """
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))

    # New step header.
    assert "2) Confirm the install location." in joined

    # `pwd` check still present.
    assert "pwd" in joined

    # All three branches documented.
    assert "2a) REFUSE" in joined
    assert "2b) PROCEED SILENTLY" in joined
    assert "2c) CONFIRM" in joined

    # Refuse list explicitly names $HOME + the system paths.
    for path in ("$HOME", "/tmp", "/etc", "/usr", "/var", "/opt", "/root"):
        assert path in joined, f"refuse list missing {path!r}"

    # Silent-proceed whitelist contains the workspace artefacts.
    for artefact in (".git", ".claude", ".agnes", "AGNES_WORKSPACE.md", "README.md"):
        assert artefact in joined, f"silent-proceed whitelist missing {artefact!r}"

    # Confirm prompt offers the new three-way decision.
    assert "'ok'" in joined
    assert "'default'" in joined
    assert "'abort'" in joined

    # Legacy 'install here' phrasing kept as an 'ok' synonym (muscle memory).
    assert "'install here'" in joined

    # The 'default' branch is the only place mkdir runs automatically.
    assert "mkdir -p ~/Desktop/Agnes && cd ~/Desktop/Agnes" in joined

    # No auto-mkdir from the very-old flow.
    assert 'mkdir -p "$HOME/Agnes"' not in joined
    assert 'New-Item -ItemType Directory -Force -Path "$HOME\\Agnes"' not in joined
    assert 'Set-Location "$HOME\\Agnes"' not in joined


def test_step_2_substitutes_custom_brand_and_workspace_dir():
    """Brand + workspace_dir threading: every visible reference resolves
    against the operator's configured values, no placeholders leak."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            instance_brand="Foundry AI",
            workspace_dir="FoundryAI",
        )
    )
    assert "2) Confirm the install location." in joined
    # Default path threads through both the silent-proceed reference and
    # the confirm-prompt 'default' branch.
    assert "~/Desktop/FoundryAI" in joined
    assert "mkdir -p ~/Desktop/FoundryAI && cd ~/Desktop/FoundryAI" in joined
    # Brand surfaces in the refuse + confirm copy.
    assert "I won't install Foundry AI" in joined
    assert "I'll install Foundry AI in <pwd>" in joined
    # No placeholders survive.
    assert "{workspace_dir}" not in joined
    assert "{instance_brand}" not in joined


def test_step_2_refuse_branch_lists_home_and_system_paths():
    """REFUSE must explicitly enumerate $HOME plus the system dirs.
    Without this list a model might decide an OS path is 'fine' and
    install into /etc or /root, scattering files where they don't belong."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    # All paths the refuse line claims to block must appear in the
    # rendered script so the AI follower can match against pwd output.
    for path in (
        "$HOME",
        "/tmp",
        "/etc",
        "/usr",
        "/var",
        "/opt",
        "/root",
        "/bin",
        "/sbin",
        "/boot",
        "/sys",
        "/proc",
    ):
        assert path in joined, f"refuse path {path!r} missing"


def test_step_9_restart_references_install_dir_not_hardcoded():
    """Step 9 must describe the restart cwd as the directory confirmed
    in step 2 (mentioning the default path) rather than a bare hardcoded
    `~/{workspace_dir}`. The phrasing keeps the user-flow connection
    visible across step 2's three branches and step 9's restart cue."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "9) Restart Claude Code" in joined
    # Wording references the step-2 confirmation.
    assert "install dir confirmed in step 2" in joined
    # Default path still mentioned as the expected baseline.
    assert "~/Desktop/Agnes" in joined
    # Step 9 still mentions 'install here' as the legacy synonym so
    # users who learned the old flow recognise it.
    assert "'install here'" in joined


# ---------------------------------------------------------------------------
# Change B — recap bridge line at the end of the restart-Claude step.
# ---------------------------------------------------------------------------

def test_restart_claude_step_ends_with_recap_before_confirm():
    """The restart-Claude step (9) closes with a recap cue naming the
    Confirm step (10). It intentionally overlaps `_finale_lines`' Confirm
    summary as a short bridge — a plain-language outcome summary right
    before the structured Confirm bullets."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "Before step 10 (Confirm):" in joined
    assert "short recap of what was installed or was already present" in joined
    # The recap belongs to the restart step, so it lands after the step-9
    # header and before the step-10 Confirm header.
    recap_idx = joined.index("Before step 10 (Confirm):")
    restart_idx = joined.index("9) Restart Claude Code")
    confirm_idx = joined.index("10) Confirm:")
    assert restart_idx < recap_idx < confirm_idx


# ---------------------------------------------------------------------------
# Change C — operator-authored custom_preamble injected at the TOP.
# ---------------------------------------------------------------------------

def test_custom_preamble_appears_first_above_cli_line():
    """A non-empty `custom_preamble` is prepended above the
    `Set up the … CLI` opening line (before the numbered steps)."""
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines("agnes.whl", custom_preamble="TRUST LINE ONE\nTRUST LINE TWO")
    joined = "\n".join(lines)
    # Preamble text present and lands before the CLI opener.
    assert "TRUST LINE ONE" in joined
    assert "TRUST LINE TWO" in joined
    assert joined.index("TRUST LINE ONE") < joined.index("Set up the Agnes CLI")
    # The very first rendered line is the preamble's first line.
    assert lines[0] == "TRUST LINE ONE"


def test_custom_preamble_substitutes_instance_brand():
    """`{instance_brand}` inside the preamble is substituted by the
    resolve_lines placeholder loop (just like the rest of the prompt)."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines(
        "agnes.whl",
        instance_brand="Foundry AI",
        custom_preamble="TRUST LINE {instance_brand}",
    ))
    assert "TRUST LINE Foundry AI" in joined
    assert "{instance_brand}" not in joined


def test_empty_custom_preamble_is_byte_identical_to_no_arg():
    """Empty `custom_preamble` (the default) must emit ZERO extra lines —
    the rendered prompt is byte-identical to the no-arg call. Whitespace
    stripping is the getter's job (`get_instance_custom_preamble` follows
    the env>yaml>default pattern and `.strip()`s), so `resolve_lines`
    itself only needs to treat the empty string as absent."""
    from app.web.setup_instructions import resolve_lines

    baseline = "\n".join(resolve_lines("agnes.whl"))
    assert "\n".join(resolve_lines("agnes.whl", custom_preamble="")) == baseline


def test_render_setup_instructions_forwards_custom_preamble():
    """The string-rendering entry point threads `custom_preamble` through
    to the resolver."""
    from app.web.setup_instructions import render_setup_instructions

    out = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="T-CP",
        wheel_filename="agnes-1.0-py3-none-any.whl",
        custom_preamble="OPERATOR TRUST NOTE",
    )
    assert out.startswith("OPERATOR TRUST NOTE")
