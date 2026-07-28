"""OAuth client_secret encryption at rest (#869).

The MCP OAuth SDK verifies clients by comparing the presented secret against
what our provider's get_client returns, so a one-way hash isn't verifiable in
the SDK path. Instead we encrypt at rest: stored ciphertext, decrypted back to
the raw value for the SDK's equality check. These tests pin that contract.
"""

import asyncio

import pytest

import app.secrets as secrets_mod


@pytest.fixture(autouse=True)
def _fresh_key(tmp_path, monkeypatch):
    """Isolate the encryption key under a tmp STATE_DIR and reset the cached
    Fernet so each test derives its key from the tmp state dir."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AGNES_OAUTH_ENC_KEY", raising=False)
    monkeypatch.setattr(secrets_mod, "_fernet_cached", None)
    yield
    monkeypatch.setattr(secrets_mod, "_fernet_cached", None)


def test_encrypt_then_decrypt_round_trips():
    raw = "super-secret-value-123"
    enc = secrets_mod.encrypt_client_secret(raw)
    assert enc != raw
    assert enc.startswith("enc:v1:")
    assert secrets_mod.decrypt_client_secret(enc) == raw


def test_encrypt_is_idempotent_and_total_on_empty():
    assert secrets_mod.encrypt_client_secret(None) is None
    assert secrets_mod.encrypt_client_secret("") == ""
    once = secrets_mod.encrypt_client_secret("s")
    assert secrets_mod.encrypt_client_secret(once) == once  # no double-encrypt


def test_decrypt_passes_through_legacy_plaintext_and_empty():
    # Rows written before encryption have no prefix — must keep working.
    assert secrets_mod.decrypt_client_secret("legacy-plaintext") == "legacy-plaintext"
    assert secrets_mod.decrypt_client_secret(None) is None
    assert secrets_mod.decrypt_client_secret("") == ""


def test_decrypt_fails_closed_on_corrupt_ciphertext():
    """A corrupt/unrotatable ciphertext must NOT decrypt to None (the SDK treats
    None as 'no secret required' and would let the client in) — it returns an
    unmatchable sentinel so the constant-time comparison rejects the client."""
    out = secrets_mod.decrypt_client_secret("enc:v1:not-a-valid-fernet-token")
    assert out is not None
    assert out != "enc:v1:not-a-valid-fernet-token"
    # Practically unguessable — the SDK's compare_digest against any presented
    # secret fails.
    assert secrets_mod.decrypt_client_secret("enc:v1:x") != ""


def test_register_client_encrypts_before_store_and_get_client_decrypts(monkeypatch):
    """End-to-end at the provider boundary: register stores ciphertext (never
    the raw secret), and get_client returns the raw value the SDK compares."""
    from app.auth.mcp_oauth import AgnesMCPOAuthProvider
    from mcp.shared.auth import OAuthClientInformationFull

    stored: dict = {}

    class _FakeRepo:
        def upsert_client(self, *, client_id, client_secret, redirect_uris, client_name, client_metadata):
            stored.update(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uris=redirect_uris,
                client_name=client_name,
                client_metadata=client_metadata,
            )

        def get_client(self, client_id):
            return {
                "client_id": stored["client_id"],
                "client_secret": stored["client_secret"],
                "redirect_uris": stored["redirect_uris"],
                "client_name": stored["client_name"],
                "client_metadata": stored["client_metadata"],
            }

    monkeypatch.setattr("src.repositories.oauth_clients_repo", lambda: _FakeRepo())

    raw_secret = "raw-client-secret-xyz"
    info = OAuthClientInformationFull(
        client_id="client-abc",
        client_secret=raw_secret,
        redirect_uris=["https://example.com/callback"],
        client_name="Test",
    )

    provider = AgnesMCPOAuthProvider()
    asyncio.run(provider.register_client(info))

    # At rest: never the raw secret.
    assert stored["client_secret"] != raw_secret
    assert stored["client_secret"].startswith("enc:v1:")

    # On read: the SDK gets the raw secret back for its equality check.
    client = asyncio.run(provider.get_client("client-abc"))
    assert client is not None
    assert client.client_secret == raw_secret
