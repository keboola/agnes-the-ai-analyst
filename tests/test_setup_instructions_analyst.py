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
