"""Tests for the setup-instructions template + resolver.

`uv tool install` validates the PEP 427 filename in the URL path before
fetching, so our setup snippet cannot use a stable alias like `agnes.whl`.
These tests pin the wheel-filename substitution behavior.
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
    assert "claude plugin marketplace add" not in joined
    assert "claude plugin install" not in joined
    assert "sslVerify" not in joined


def test_resolve_lines_with_plugins_inserts_git_check_marketplace_and_renumbers_confirm():
    from app.web.setup_instructions import resolve_lines

    lines = resolve_lines(
        "agnes.whl",
        plugin_install_names=["foo", "bar"],
        server_host="agnes.example.com",
    )
    joined = "\n".join(lines)
    # Step 6 — git pre-flight, with both Mac + Windows install commands.
    assert "6) Make sure git is installed" in joined
    assert "git --version" in joined
    assert "brew install git" in joined
    assert "winget install --id Git.Git -e --source winget --silent" in joined
    # Step 7 — marketplace + plugins.
    assert "7) Register the Agnes Claude Code marketplace and install plugins:" in joined
    assert (
        'claude plugin marketplace add "https://x:{token}@agnes.example.com/marketplace.git/"'
        in joined
    )
    assert "claude plugin install foo@agnes --scope project" in joined
    assert "claude plugin install bar@agnes --scope project" in joined
    # Step 8 — Confirm renumbered (no stray 6/7 Confirm).
    assert "8) Confirm:" in joined
    assert "6) Confirm:" not in joined
    assert "7) Confirm:" not in joined
    # Git pre-flight must come BEFORE marketplace add inside the script.
    assert joined.index("6) Make sure git is installed") < joined.index(
        "7) Register the Agnes Claude Code marketplace"
    )
    # No git-config sslVerify line unless self_signed_tls is set.
    assert "sslVerify" not in joined
    # server_host is server-side substituted; the placeholder must be gone.
    assert "{server_host}" not in joined
    # server_url + token are still placeholders for click-time JS substitution.
    assert "{server_url}" in joined
    assert "{token}" in joined


def test_resolve_lines_self_signed_adds_git_config_line():
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
    # The git-config line must come BEFORE the marketplace add inside step 6.
    git_idx = joined.index('git config --global')
    add_idx = joined.index('claude plugin marketplace add')
    assert git_idx < add_idx


def test_resolve_lines_self_signed_no_op_without_plugins():
    """`self_signed_tls=True` is a no-op when there are no plugins (no marketplace step to attach to)."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines("agnes.whl", plugin_install_names=[], self_signed_tls=True)
    )
    assert "sslVerify" not in joined
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
    assert joined.index("0) Trust the Agnes TLS certificate") < joined.index("1) Install the CLI:")

    # PEM body inlined verbatim, flush-left (heredoc would corrupt indented content).
    assert "-----BEGIN CERTIFICATE-----" in joined
    assert "-----END CERTIFICATE-----" in joined
    # The PEM is passed inside a single-quoted heredoc so `$` / backtick
    # in real-world cert bodies are NOT shell-expanded — preserve verbatim.
    assert "MIIBkTCB+wIJAKf9$x`cNotARealCert" in joined
    assert "<<'AGNES_CA_PEM'" in joined

    # All three trust env vars exported in the current shell.
    assert 'export SSL_CERT_FILE="$HOME/.agnes/ca.pem"' in joined
    assert 'export NODE_EXTRA_CA_CERTS="$HOME/.agnes/ca.pem"' in joined
    assert 'export GIT_SSL_CAINFO="$HOME/.agnes/ca.pem"' in joined

    # Persisted to shell rc behind an idempotent grep guard so re-running
    # setup doesn't duplicate the block.
    assert "AGNES_CA_PEM_TRUST" in joined  # marker grep-checks for
    assert "AGNES_RC_BLOCK" in joined  # the rc-append heredoc delimiter


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
    assert "sslVerify" not in joined
    # But the marketplace step itself still renders.
    assert "claude plugin marketplace add" in joined
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
    assert "sslVerify" not in out
    # Other placeholders still substituted.
    assert "{server_url}" not in out
    assert "{token}" not in out
    assert "T-CA" in out


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
