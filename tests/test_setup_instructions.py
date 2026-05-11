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


def test_resolve_lines_no_plugins_unified_layout():
    """Unified always-on layout: 1 install, 2 init, 3 catalog, 4 preflight,
    5 marketplace, 6 mcp_servers, 7 diagnose, 8 confirm. Preflight +
    marketplace + MCP block are emitted even when the operator's served
    stack is empty — registering the per-user marketplace clone pre-wires
    Claude Code for future stack changes (admin grants, system pins,
    Flea installs), and the Atlassian Remote MCP applies to every analyst
    whose work touches Jira/Confluence. Skills step deleted — the
    interactive copy-or-on-demand question was confusing and the
    on-demand path is the one-size-fits-all default."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    # Mandatory unified-flow steps.
    assert "1) Install the CLI" in joined
    assert "2) Bootstrap your Agnes workspace" in joined
    assert "3) Verify the data is queryable:" in joined
    assert "4) Make sure git and claude are installed" in joined
    assert "5) Register the Agnes Claude Code marketplace" in joined
    assert "6) Register the Atlassian MCP server" in joined
    assert "7) Run diagnostics:" in joined
    assert "8) Confirm:" in joined
    # No stray Confirms at other positions.
    assert "9) Confirm:" not in joined
    assert "10) Confirm:" not in joined
    assert "6) Confirm:" not in joined
    # No skills step in any form.
    assert "Skills (ask the user" not in joined
    assert "Skills" not in joined or "agnes skills" in joined  # comment refs still OK
    assert "8) Skills" not in joined
    assert "~/.claude/skills/agnes/" not in joined
    # The marketplace step header adapts to the empty-stack copy
    # rather than the plugin-installing variant.
    assert "your stack is empty for now" in joined
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

    fake_ca = (
        "-----BEGIN CERTIFICATE-----\n"
        "FAKEFAKEFAKE\n"
        "-----END CERTIFICATE-----\n"
    )
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

    fake_ca = (
        "-----BEGIN CERTIFICATE-----\n"
        "FAKE\n"
        "-----END CERTIFICATE-----\n"
    )

    # No ca, no plugins: marketplace bullet present, CA bullet absent.
    plain = "\n".join(resolve_lines("agnes.whl"))
    assert "Which CA bundle source got picked" not in plain
    assert "~/.agnes/marketplace/.git/" in plain

    # ca only: both bullets present.
    ca_only = "\n".join(resolve_lines("agnes.whl", ca_pem=fake_ca))
    assert "Which CA bundle source got picked" in ca_only
    assert "~/.agnes/marketplace/.git/" in ca_only

    # plugins only: marketplace bullet yes, CA bullet no.
    pl_only = "\n".join(
        resolve_lines("agnes.whl", plugin_install_names=["foo"], server_host="h")
    )
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

    fake_ca = (
        "-----BEGIN CERTIFICATE-----\n"
        "FAKE\n"
        "-----END CERTIFICATE-----\n"
    )
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
        f"Body was:\n" + "\n".join(f"  {i+1:2d} {ln!r}" for i, ln in enumerate(body_lines))
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

    fake_ca = (
        "-----BEGIN CERTIFICATE-----\n"
        "FAKE\n"
        "-----END CERTIFICATE-----\n"
    )
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

    fake_ca = (
        "-----BEGIN CERTIFICATE-----\n"
        "FAKE\n"
        "-----END CERTIFICATE-----\n"
    )
    joined = "\n".join(resolve_lines("agnes.whl", ca_pem=fake_ca))
    # The stale "step 7's marketplace add" string must be gone.
    assert "step 7's marketplace add" not in joined
    # Replacement text describes the consequence without a step number.
    assert "marketplace `git" in joined and "clone`" in joined


def test_resolve_lines_with_plugins_uses_install_first_diagnose_last_layout():
    """Marketplace layout puts install/init/catalog/preflight/marketplace
    BEFORE diagnose, so diagnose is the final smoke test before Confirm.
    Step numbers: 4 preflight, 5 marketplace, 6 mcp, 7 diagnose,
    8 confirm. No skills step — interactive copy-or-on-demand question
    was confusing; on-demand `agnes skills show` is the default."""
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines(
        "agnes.whl",
        plugin_install_names=["foo", "bar"],
        server_host="agnes.example.com",
    )
    joined = "\n".join(lines)
    # Step 4 — pre-flight, with all three platforms' install commands.
    assert "4) Make sure git and claude are installed" in joined
    assert "git --version" in joined
    assert "claude --version" in joined
    assert "brew install git" in joined
    assert "winget install --id Git.Git -e --source winget --silent" in joined
    assert "sudo apt-get install git" in joined or "sudo dnf install git" in joined
    # Step 5 — marketplace + stack install. Collapsed to a single CLI call:
    # `agnes refresh-marketplace --bootstrap` does clone + PAT-strip +
    # chmod + register-with-Claude + auto-install-from-manifest internally.
    # Pulling that out of the inline shell script avoided Claude Code's
    # agent-driven `rm -rf` permission gate that the old multi-line
    # sequence tripped on.
    assert "5) Register the Agnes Claude Code marketplace and install your current stack" in joined
    assert "agnes refresh-marketplace --bootstrap" in joined
    # The destructive prep + per-plugin install commands are now inside
    # the CLI; the prompt must not emit the inline shell forms in
    # operator-runnable lines (comment lines documenting what the CLI
    # does internally are fine — they're prose, not commands).
    executable = _executable_lines(joined)
    assert "rm -rf ~/.agnes/marketplace" not in executable
    assert "git clone " not in executable
    assert "git remote set-url origin" not in executable
    assert "claude plugin marketplace add" not in executable
    assert "claude plugin install foo@agnes" not in executable
    assert "claude plugin install bar@agnes" not in executable
    # Step 6 — Atlassian MCP registration.
    assert "6) Register the Atlassian MCP server" in joined
    # Step 7 — diagnose now AFTER marketplace + MCP wiring.
    assert "7) Run diagnostics:" in joined
    # Step 8 — Confirm.
    assert "8) Confirm:" in joined
    # No skills step in any form.
    assert "Skills (ask the user" not in joined
    assert "8) Skills" not in joined
    assert "~/.claude/skills/agnes/" not in joined
    for stray in ("4) Confirm:", "5) Confirm:", "6) Confirm:", "7) Confirm:", "9) Confirm:"):
        assert stray not in joined
    # Crucial ordering invariants for the new layout.
    install_idx = joined.index("1) Install the CLI")
    init_idx = joined.index("2) Bootstrap your Agnes workspace")
    catalog_idx = joined.index("3) Verify the data is queryable:")
    git_idx = joined.index("4) Make sure git and claude are installed")
    market_idx = joined.index("5) Register the Agnes Claude Code marketplace")
    mcp_idx = joined.index("6) Register the Atlassian MCP server")
    diag_idx = joined.index("7) Run diagnostics:")
    confirm_idx = joined.index("8) Confirm:")
    assert install_idx < init_idx < catalog_idx < git_idx < market_idx < mcp_idx < diag_idx < confirm_idx
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
    assert "case \"$(uname -s)\" in" in joined
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
    assert 'WHEEL=/tmp/agnes-1.0-py3-none-any.whl' in joined_ca
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
        {"plugin_install_names": ["foo"], "server_host": "agnes.example.com",
         "ca_pem": _FAKE_CA_PEM},
        {"plugin_install_names": [], "server_host": "agnes.example.com"},
    ):
        joined = "\n".join(resolve_lines("agnes.whl", **kwargs))
        assert "git config --global" not in joined, (
            f"sslVerify downgrade leaked through with kwargs={kwargs!r}"
        )
        assert "sslVerify false" not in joined, (
            f"sslVerify downgrade leaked through with kwargs={kwargs!r}"
        )


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
    """Trust block is independent of the marketplace + MCP blocks — emit
    step 0 even when plugin list is empty. Confirm step is at 8 in the
    post-skills-removal layout. Step 0 is preamble, not numbered."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", ca_pem=_FAKE_CA_PEM))
    assert "0) Trust the Agnes TLS certificate" in joined
    assert "8) Confirm:" in joined
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


def test_no_plugins_layout_diagnose_before_confirm():
    """Always-on layout (post-skills-removal):
    install → init → catalog → preflight → marketplace → mcp_servers →
    diagnose → confirm. Step numbers: 7 diagnose, 8 confirm."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "7) Run diagnostics:" in joined
    assert "8) Confirm:" in joined
    diag_idx = joined.index("7) Run diagnostics:")
    confirm_idx = joined.index("8) Confirm:")
    assert diag_idx < confirm_idx


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

    fake_ca = (
        "-----BEGIN CERTIFICATE-----\n"
        "FAKE\n"
        "-----END CERTIFICATE-----\n"
    )

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
