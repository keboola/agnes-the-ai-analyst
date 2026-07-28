"""Pre-seed vscode-mcp public OAuth client (DuckDB v85)

VS Code native MCP is a public client (token_endpoint_auth_method=none,
no client_secret, PKCE required). Pre-seeding a well-known client_id lets
users who see the manual-registration dialog simply enter 'vscode-mcp'
without running Dynamic Client Registration separately.

Revision ID: 0032_vscode_mcp_client_v85
Revises: 0031_oauth_refresh_resource_v84
"""

from alembic import op

revision: str = "0032_vscode_mcp_client_v85"
down_revision: str = "0031_oauth_refresh_resource_v84"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO oauth_clients
            (client_id, client_secret, redirect_uris, client_name, client_metadata, created_at)
        VALUES (
            'vscode-mcp',
            NULL,
            '["https://vscode.dev/redirect"]',
            'VS Code (native MCP)',
            '{"token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}',
            CURRENT_TIMESTAMP
        )
        ON CONFLICT (client_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM oauth_clients WHERE client_id = 'vscode-mcp'")
