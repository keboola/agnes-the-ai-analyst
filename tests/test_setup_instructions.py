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


def test_resolve_lines_no_plugins_keeps_six_step_layout():
    """Backwards-compat: empty plugin list → original 6-step layout, Confirm = 6."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "6) Confirm:" in joined
    assert "7) Confirm:" not in joined
    assert "8) Confirm:" not in joined
    assert "claude plugin marketplace add" not in joined
    assert "claude plugin install" not in joined
    # Legacy `git config sslVerify=false` downgrade must NOT be emitted.
    # Match the specific config line, not the bare substring (which appears
    # in the preamble as a "don't do this" example).
    assert "git config --global" not in joined
    # Trust block isn't emitted without ca_pem either.
    assert "0) Trust the Agnes TLS certificate" not in joined
    # Confirm step's CA bundle / marketplace bullets must NOT appear when
    # those steps weren't emitted — otherwise the assistant is told to
    # report on phantom steps.
    assert "step 0(d)" not in joined
    assert "Which CA bundle source got picked" not in joined
    assert "Whether the marketplace add went via direct HTTPS" not in joined


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
    actually emitted. CA bundle bullet only when has_ca=True; marketplace
    direct-vs-clone bullet only when plugins are configured."""
    from app.web.setup_instructions import resolve_lines

    fake_ca = (
        "-----BEGIN CERTIFICATE-----\n"
        "FAKE\n"
        "-----END CERTIFICATE-----\n"
    )

    # No ca, no plugins: neither bullet present.
    plain = "\n".join(resolve_lines("agnes.whl"))
    assert "Which CA bundle source got picked" not in plain
    assert "Whether the marketplace add went via direct HTTPS" not in plain

    # ca only: CA bullet yes, marketplace bullet no.
    ca_only = "\n".join(resolve_lines("agnes.whl", ca_pem=fake_ca))
    assert "Which CA bundle source got picked" in ca_only
    assert "Whether the marketplace add went via direct HTTPS" not in ca_only

    # plugins only: marketplace bullet yes, CA bullet no.
    pl_only = "\n".join(
        resolve_lines("agnes.whl", plugin_install_names=["foo"], server_host="h")
    )
    assert "Which CA bundle source got picked" not in pl_only
    assert "Whether the marketplace add went via direct HTTPS" in pl_only

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
    assert "Whether the marketplace add went via direct HTTPS" in both


def test_marketplace_block_redetects_platform_for_self_containment():
    """Marketplace `case "$PLATFORM" in` would silently fall through to the
    `*)` catch-all on every platform if `$PLATFORM` from step 0 isn't in
    the current shell — which the prompt itself warns about
    ("env vars do NOT persist between separate Bash invocations"). Linux
    would then never get the direct-HTTPS attempt the comment promises.
    The marketplace block must therefore re-detect $PLATFORM via uname
    before its case statement, mirroring step 0(a)."""
    from app.web.setup_instructions import resolve_lines

    fake_ca = (
        "-----BEGIN CERTIFICATE-----\n"
        "FAKE\n"
        "-----END CERTIFICATE-----\n"
    )
    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            plugin_install_names=["foo"],
            server_host="agnes.example.com",
            ca_pem=fake_ca,
        )
    )
    # Locate the marketplace section.
    section_idx = joined.index("Register the Agnes Claude Code marketplace")
    section = joined[section_idx:]

    # Re-detection block must appear BEFORE the `case "$PLATFORM" in`
    # check so the variable is set when the case runs.
    redetect_idx = section.index('case "$(uname -s)" in')
    platform_case_idx = section.index('case "$PLATFORM" in')
    assert redetect_idx < platform_case_idx
    # All three platform branches must be covered (same shape as step 0(a)).
    redetect_block = section[redetect_idx:platform_case_idx]
    assert "Darwin" in redetect_block and "PLATFORM=macos" in redetect_block
    assert "Linux" in redetect_block and "PLATFORM=linux" in redetect_block
    assert "MINGW*|MSYS*|CYGWIN*" in redetect_block and "PLATFORM=windows" in redetect_block


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
    """Marketplace layout puts install/login/git/marketplace BEFORE diagnose
    and skills, so the human-loop skills question is the final blocking
    step before Confirm. Step numbers: 4 git, 5 marketplace, 6 diagnose,
    7 skills, 8 confirm."""
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines(
        "agnes.whl",
        plugin_install_names=["foo", "bar"],
        server_host="agnes.example.com",
    )
    joined = "\n".join(lines)
    # Step 4 — git pre-flight, with all three platforms' install commands.
    assert "4) Make sure git is installed" in joined
    assert "git --version" in joined
    assert "brew install git" in joined
    assert "winget install --id Git.Git -e --source winget --silent" in joined
    assert "sudo apt-get install git" in joined or "sudo dnf install git" in joined
    # Step 5 — marketplace + plugins.
    assert "5) Register the Agnes Claude Code marketplace and install plugins" in joined
    assert (
        'claude plugin marketplace add "https://x:{token}@agnes.example.com/marketplace.git/"'
        in joined
    )
    assert "claude plugin install foo@agnes --scope project" in joined
    assert "claude plugin install bar@agnes --scope project" in joined
    # Step 6 — diagnose now AFTER marketplace (used to be step 4 right after whoami).
    assert "6) Run diagnostics:" in joined
    # Step 7 — skills, the last interactive step before Confirm.
    assert "7) Skills" in joined
    # Step 8 — Confirm renumbered (no stray Confirms at other positions).
    assert "8) Confirm:" in joined
    for stray in ("4) Confirm:", "5) Confirm:", "6) Confirm:", "7) Confirm:"):
        assert stray not in joined
    # Crucial ordering invariants for the new layout.
    install_idx = joined.index("1) Install the CLI")
    login_idx = joined.index("2) Log in")
    verify_idx = joined.index("3) Verify the login:")
    git_idx = joined.index("4) Make sure git is installed")
    market_idx = joined.index("5) Register the Agnes Claude Code marketplace")
    diag_idx = joined.index("6) Run diagnostics:")
    skills_idx = joined.index("7) Skills")
    confirm_idx = joined.index("8) Confirm:")
    assert install_idx < login_idx < verify_idx < git_idx < market_idx < diag_idx < skills_idx < confirm_idx
    # No git-config sslVerify=false line unless self_signed_tls is set.
    assert "git config --global" not in joined
    # server_host is server-side substituted; the placeholder must be gone.
    assert "{server_host}" not in joined
    # server_url + token are still placeholders for click-time JS substitution.
    assert "{server_url}" in joined
    assert "{token}" in joined


def test_resolve_lines_self_signed_legacy_path_adds_git_config_line():
    """Legacy fallback (no ca_pem on disk + self_signed_tls=True): the host-scoped
    `git config sslVerify=false` downgrade is still emitted so existing
    AGNES_DEBUG_AUTH instances keep working until they roll a fullchain.pem."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            plugin_install_names=["foo"],
            self_signed_tls=True,
            server_host="agnes.example.com",
        )
    )
    assert 'git config --global http."{server_url}/".sslVerify false' in joined
    # The git-config line must come BEFORE the marketplace add inside the
    # marketplace step (regardless of which step number it lands on).
    git_idx = joined.index('git config --global')
    add_idx = joined.index('claude plugin marketplace add')
    assert git_idx < add_idx


def test_resolve_lines_self_signed_no_op_without_plugins():
    """`self_signed_tls=True` is a no-op when there are no plugins (no marketplace step to attach to)."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines("agnes.whl", plugin_install_names=[], self_signed_tls=True)
    )
    # Legacy downgrade line not present.
    assert "git config --global" not in joined
    assert "claude plugin" not in joined
    # No git pre-flight either when there's no marketplace step.
    assert "Make sure git is installed" not in joined
    assert "6) Confirm:" in joined  # original layout intact


def test_render_setup_instructions_with_plugins_substitutes_all_placeholders():
    from app.web.setup_instructions import render_setup_instructions

    out = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="T-XYZ",
        wheel_filename="agnes-1.0-py3-none-any.whl",
        plugin_install_names=["foo", "bar"],
        self_signed_tls=True,
        server_host="agnes.example.com",
    )
    # No raw placeholders remain in the final string.
    assert "{server_url}" not in out
    assert "{token}" not in out
    assert "{wheel_filename}" not in out
    assert "{server_host}" not in out
    # Token leaks into both the auth-import-token line and the marketplace URL.
    assert "T-XYZ" in out
    assert "https://x:T-XYZ@agnes.example.com/marketplace.git/" in out
    assert 'git config --global http."https://agnes.example.com/".sslVerify false' in out
    assert "claude plugin install foo@agnes --scope project" in out
    assert "claude plugin install bar@agnes --scope project" in out


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


def test_resolve_lines_with_ca_pem_marketplace_is_platform_aware():
    """When ca_pem is set + plugins requested, step 5 emits a platform branch:
    Linux → try direct HTTPS first, fall back to git clone on failure
    (node-based claude honors NODE_EXTRA_CA_CERTS);
    Windows + macOS → straight to git-clone fallback (Bun-compiled claude
    binary ignores OS trust store and CA env vars on both platforms)."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            plugin_install_names=["foo"],
            server_host="agnes.example.com",
            ca_pem=_FAKE_CA_PEM,
        )
    )
    # The platform branch + MARKETPLACE_VIA selector.
    assert "MARKETPLACE_VIA=clone" in joined
    assert "MARKETPLACE_VIA=direct" in joined
    # Locate the marketplace step's case block specifically — there is
    # ALSO a `case "$PLATFORM" in` block in step 0(c) (OS trust store
    # registration), so we anchor on the marketplace section header to
    # narrow the slice.
    section_idx = joined.index("Register the Agnes Claude Code marketplace")
    market_case_idx = joined.index('case "$PLATFORM" in', section_idx)
    market_esac_idx = joined.index("esac", market_case_idx)
    branch_block = joined[market_case_idx:market_esac_idx]
    assert "linux)" in branch_block
    # Direct attempt only in the linux branch.
    assert (
        'claude plugin marketplace add "https://x:{token}@agnes.example.com/marketplace.git/" 2>/dev/null'
        in branch_block
    )
    # The default `*)` branch must hard-set clone (no direct attempt).
    star_idx = branch_block.index("*)")
    star_branch = branch_block[star_idx:]
    assert "MARKETPLACE_VIA=clone" in star_branch
    assert "claude plugin marketplace add" not in star_branch
    # Git-clone fallback writes to ~/.agnes/marketplace and adds it as a local path.
    assert 'git clone "https://x:{token}@agnes.example.com/marketplace.git/" ~/.agnes/marketplace' in joined
    assert "claude plugin marketplace add ~/.agnes/marketplace" in joined
    # Harmless credential-manager-core warning is called out.
    assert "credential-manager-core" in joined
    # Plugin install line stays unchanged (errors checked in a sibling test).
    assert "claude plugin install foo@agnes --scope project" in joined


def test_resolve_lines_with_ca_pem_marketplace_strips_pat_after_clone():
    """After `git clone https://x:<PAT>@host/...`, the cloned repo's
    `.git/config` holds the PAT in plaintext at `[remote "origin"] url`.
    On default home setups that file syncs to iCloud/OneDrive and gets
    read by antivirus / sync agents. The marketplace step must run
    `git remote set-url origin <url-without-token>` after clone, plus a
    best-effort chmod tighten. claude registers the *local path* (not the
    remote URL), so stripping the token doesn't break marketplace
    registration — refreshes go via re-running setup with a fresh PAT."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            plugin_install_names=["foo"],
            server_host="agnes.example.com",
            ca_pem=_FAKE_CA_PEM,
        )
    )
    # Token-bearing clone line still exists (we need the token to authenticate
    # the initial clone) but a token-less remote set-url line follows.
    clone_idx = joined.index(
        'git clone "https://x:{token}@agnes.example.com/marketplace.git/"'
    )
    set_url_idx = joined.index(
        'git -C ~/.agnes/marketplace remote set-url origin "https://agnes.example.com/marketplace.git/"'
    )
    add_idx = joined.index("claude plugin marketplace add ~/.agnes/marketplace")
    assert clone_idx < set_url_idx < add_idx
    # Token-less URL must NOT contain the placeholder or `x:` prefix.
    set_url_line_end = joined.index("\n", set_url_idx)
    set_url_line = joined[set_url_idx:set_url_line_end]
    assert "{token}" not in set_url_line
    assert "x:" not in set_url_line

    # Best-effort chmod tighten — wrapped in `|| true` so MSYS / Git Bash
    # on Windows (where chmod is a no-op against NTFS ACLs) doesn't fail
    # the step.
    assert "chmod 700 ~/.agnes/marketplace ~/.agnes/marketplace/.git" in joined
    assert "chmod 600 ~/.agnes/marketplace/.git/config" in joined
    assert "|| true" in joined


def test_resolve_lines_with_ca_pem_marketplace_has_explicit_error_handling():
    """Each shell-out in the marketplace block must fail loudly with `exit 1`
    on a non-zero exit, not silently fall through to the next step. Without
    this, a failed `git clone` causes a confusing 'marketplace 'agnes' not
    found' error from the subsequent `claude plugin install`."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            plugin_install_names=["foo", "bar"],
            server_host="agnes.example.com",
            ca_pem=_FAKE_CA_PEM,
        )
    )
    # git clone has an `|| { ... exit 1 }` guard.
    assert (
        'git clone "https://x:{token}@agnes.example.com/marketplace.git/" '
        '~/.agnes/marketplace || {'
    ) in joined
    # `claude plugin marketplace add ~/.agnes/marketplace` (the local path
    # one — not the chmod best-effort lines) has its own guard.
    assert "claude plugin marketplace add ~/.agnes/marketplace || {" in joined
    # Each `claude plugin install <name>@agnes` has its own guard so we know
    # which plugin failed.
    assert "claude plugin install foo@agnes --scope project || {" in joined
    assert "claude plugin install bar@agnes --scope project || {" in joined
    # Error messages are written to stderr, not stdout.
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


def test_resolve_lines_with_ca_pem_suppresses_legacy_sslverify_line():
    """When ca_pem is supplied, the legacy `git config sslVerify=false`
    downgrade must NOT appear — the trust block subsumes it (full TLS
    validation re-enabled, just against the inlined cert)."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            plugin_install_names=["foo"],
            self_signed_tls=True,  # legacy flag — should be ignored when ca_pem set
            server_host="agnes.example.com",
            ca_pem=_FAKE_CA_PEM,
        )
    )
    # Legacy git-config sslVerify=false downgrade is suppressed when ca_pem is set.
    assert "git config --global" not in joined
    # But the marketplace step itself still renders.
    assert "claude plugin install foo@agnes --scope project" in joined
    # And the trust block is present.
    assert "0) Trust the Agnes TLS certificate" in joined


def test_resolve_lines_without_ca_pem_keeps_legacy_self_signed_path():
    """Legacy fallback: no ca_pem + self_signed_tls=True still emits the
    sslVerify=false line (so existing AGNES_DEBUG_AUTH instances keep
    working until they roll a fullchain.pem onto disk)."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            plugin_install_names=["foo"],
            self_signed_tls=True,
            server_host="agnes.example.com",
            # no ca_pem
        )
    )
    assert "0) Trust the Agnes TLS certificate" not in joined
    assert 'sslVerify false' in joined


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
    """Trust block is independent of the marketplace block — emit step 0
    even when plugin list is empty. Confirm step number stays at 6
    (the original layout) since step 0 is preamble, not numbered."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", ca_pem=_FAKE_CA_PEM))
    assert "0) Trust the Agnes TLS certificate" in joined
    assert "6) Confirm:" in joined
    assert "claude plugin marketplace add" not in joined


def test_render_setup_instructions_propagates_ca_pem():
    from app.web.setup_instructions import render_setup_instructions

    out = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="T-CA",
        wheel_filename="agnes-1.0-py3-none-any.whl",
        plugin_install_names=["foo"],
        self_signed_tls=True,
        server_host="agnes.example.com",
        ca_pem=_FAKE_CA_PEM,
    )
    assert "0) Trust the Agnes TLS certificate" in out
    assert "-----BEGIN CERTIFICATE-----" in out
    # ca_pem masks legacy sslVerify=false.
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


def test_skills_step_is_last_blocking_step_before_confirm():
    """In the new layout, skills is the LAST interactive step before Confirm
    (it used to come right after diagnose and before git/marketplace, which
    invited the assistant to "do the rest in parallel"). We've moved the
    install work earlier, so the skills question is now a single clear gate
    — there's nothing left to do in parallel and the assistant must wait
    for the user's answer.

    Assert two things:
      (a) The prompt explicitly tells the assistant to wait for the answer.
      (b) The skills step appears AFTER the marketplace step in the rendered
          line order — i.e., the legacy "skills before marketplace" flow
          isn't accidentally back."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", plugin_install_names=["foo"], server_host="h"))
    flattened = " ".join(joined.split())

    # (a) The prompt must instruct the assistant to wait — and must NOT
    # contain the obsolete "you can continue in parallel" hint.
    assert "Wait for the user's answer" in joined
    assert "don't depend on the answer" not in flattened
    assert "do not depend on the answer" not in flattened

    # (b) Skills comes after marketplace in the rendered line order.
    market_idx = joined.index("Register the Agnes Claude Code marketplace")
    skills_idx = joined.index("Skills (ask the user")
    assert market_idx < skills_idx


def test_no_marketplace_layout_keeps_diagnose_before_skills():
    """Without plugins, the layout collapses to: install → login → verify →
    diagnose → skills → confirm. (No git or marketplace steps to interleave.)
    Step numbers: 4 diagnose, 5 skills, 6 confirm."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    assert "4) Run diagnostics:" in joined
    assert "5) Skills" in joined
    assert "6) Confirm:" in joined
    diag_idx = joined.index("4) Run diagnostics:")
    skills_idx = joined.index("5) Skills")
    confirm_idx = joined.index("6) Confirm:")
    assert diag_idx < skills_idx < confirm_idx


def test_install_page_uses_versioned_wheel_url(monkeypatch, tmp_path):
    """End-to-end: the /install preview must render the PEP 427 wheel URL,
    so a user copy-pasting the snippet gets a URL `uv tool install` accepts."""
    wheel = tmp_path / "agnes_the_ai_analyst-2.0.0-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04")
    monkeypatch.setenv("AGNES_CLI_DIST_DIR", str(tmp_path))

    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.get("/install", headers={"host": "agnes.test", "Accept": "text/html"})
    assert resp.status_code == 200
    assert "/cli/wheel/agnes_the_ai_analyst-2.0.0-py3-none-any.whl" in resp.text
    # The bare alias must no longer appear in the rendered snippet.
    assert "/cli/agnes.whl" not in resp.text
