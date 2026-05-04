"""Tests for analyst-branch rendering of /setup paste prompt."""

from app.web.setup_instructions import render_setup_instructions


def test_render_analyst_role_basic():
    text = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="agnes_pat_TEST",
        wheel_filename="agnes-0.32.0-py3-none-any.whl",
        role="analyst",
    )
    # Required content for analyst role:
    assert "uv tool install" in text
    assert "agnes init" in text
    assert "--token" in text and "agnes_pat_TEST" in text
    assert "--server-url" in text and "https://agnes.example.com" in text
    assert "agnes catalog" in text  # smoke verify step
    # Forbidden content (admin-only):
    assert "marketplace" not in text
    assert "claude plugin install" not in text
    assert "agnes skills install" not in text
    assert "agnes diagnose" not in text


def test_render_admin_role_unchanged():
    """Default role=admin keeps the existing layout."""
    text = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="agnes_pat_TEST",
        wheel_filename="agnes-0.32.0-py3-none-any.whl",
        # role omitted — defaults to "admin"
    )
    assert "agnes auth import-token" in text  # admin uses import-token, not agnes init
    assert "agnes diagnose" in text  # admin keeps diagnose


def test_render_analyst_with_ca_pem():
    """Analyst role + private CA → TLS trust block reused from admin path."""
    text = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="agnes_pat_TEST",
        wheel_filename="agnes-0.32.0-py3-none-any.whl",
        role="analyst",
        ca_pem="-----BEGIN CERTIFICATE-----\nMIIBxxx\n-----END CERTIFICATE-----",
    )
    assert "AGNES_CA_PEM" in text  # heredoc marker from trust block
    assert "ca-bundle.pem" in text
    assert "agnes init" in text  # analyst-specific step still present


def test_render_analyst_confirm_is_step_4():
    """Pin the analyst Confirm step number so a future renumbering breaks the test
    instead of silently emitting `4) Confirm:` while step 3 has actually moved.
    Steps: 0 (TLS optional), 1 (install), 2 (init), 3 (verify), 4 (confirm).
    """
    text = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="agnes_pat_TEST",
        wheel_filename="agnes-0.32.0-py3-none-any.whl",
        role="analyst",
    )
    assert "4) Confirm:" in text
    # Also pin the init/verify step numbers
    assert "2) Bootstrap your analyst workspace" in text
    assert "3) Verify the data is queryable" in text


def test_render_analyst_finale_mentions_workspace_md():
    """Confirm bullets reference both CLAUDE.md and AGNES_WORKSPACE.md
    (which `agnes init` writes per Task 11). Init-step prose must also mention
    AGNES_WORKSPACE.md so the operator knows what to verify."""
    text = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="agnes_pat_TEST",
        wheel_filename="agnes-0.32.0-py3-none-any.whl",
        role="analyst",
    )
    assert "AGNES_WORKSPACE.md" in text
    # Mentioned twice — once in the init prose, once in the confirm bullet
    assert text.count("AGNES_WORKSPACE.md") >= 2
