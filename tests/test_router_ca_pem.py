"""Unit tests for `app.web.router._read_agnes_ca_pem`.

The helper inspects the on-disk Agnes server cert and decides whether the
setup prompt should inline it as a trust-bootstrap step. Tests cover:

  - Self-signed leaf (subject == issuer) → return PEM (bootstrap needed).
  - CA-signed leaf with issuer NOT in `certifi` → return PEM.
  - CA-signed leaf with issuer in `certifi` → return None (publicly trusted).
  - Missing / empty / non-PEM file → return None.
  - AGNES_TLS_FULLCHAIN_PATH override is honored.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _self_signed_pem(common_name: str = "agnes.example.com") -> bytes:
    """Mirror what `agnes-tls-rotate.sh` self-signed fallback produces."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.utcnow() - dt.timedelta(minutes=1))
        .not_valid_after(dt.datetime.utcnow() + dt.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _ca_signed_pem(issuer_cn: str = "Made Up Private CA") -> bytes:
    """Build a leaf signed by a CA whose subject CN is `issuer_cn` —
    distinct from the leaf's CN, so issuer != subject (not self-signed).
    Returned PEM is the leaf only (matches our parser, which reads the
    first cert in the chain)."""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.utcnow() - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.utcnow() + dt.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf.example.com")])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.utcnow() - dt.timedelta(minutes=1))
        .not_valid_after(dt.datetime.utcnow() + dt.timedelta(days=30))
        .sign(ca_key, hashes.SHA256())
    )
    return leaf_cert.public_bytes(serialization.Encoding.PEM)


def test_read_agnes_ca_pem_self_signed_returns_pem(tmp_path, monkeypatch):
    """Self-signed leaf (issuer == subject) needs trust-bootstrap on
    every workstation — return PEM so the prompt inlines it."""
    pem = _self_signed_pem()
    cert_path = tmp_path / "fullchain.pem"
    cert_path.write_bytes(pem)
    monkeypatch.setenv("AGNES_TLS_FULLCHAIN_PATH", str(cert_path))

    from app.web.router import _read_agnes_ca_pem
    out = _read_agnes_ca_pem()
    assert out is not None
    assert "-----BEGIN CERTIFICATE-----" in out


def test_read_agnes_ca_pem_private_ca_returns_pem(tmp_path, monkeypatch):
    """CA-signed leaf where the issuer isn't in `certifi`'s trust store —
    still needs bootstrap because the user's OS doesn't know the CA."""
    pem = _ca_signed_pem(issuer_cn="Definitely Not A Real CA Root XYZ123")
    cert_path = tmp_path / "fullchain.pem"
    cert_path.write_bytes(pem)
    monkeypatch.setenv("AGNES_TLS_FULLCHAIN_PATH", str(cert_path))

    from app.web.router import _read_agnes_ca_pem
    out = _read_agnes_ca_pem()
    assert out is not None
    assert "-----BEGIN CERTIFICATE-----" in out


def test_read_agnes_ca_pem_publicly_trusted_returns_none(tmp_path, monkeypatch):
    """If the leaf's issuer matches a CA shipped in `certifi` (i.e. any
    publicly-trusted root), the user's OS already trusts the chain —
    skip the prompt's trust-bootstrap step."""
    import certifi
    # Pick the first real CA out of certifi's bundle and pretend our leaf
    # is signed by it. The leaf-signature itself is fake (we sign with our
    # own key, not the CA's), but `_read_agnes_ca_pem` only compares the
    # *issuer name*, not the signature — so DN match is enough to assert
    # the publicly-trusted code path.
    with open(certifi.where(), "rb") as fh:
        trust_pem = fh.read()
    real_ca = next(iter(x509.load_pem_x509_certificates(trust_pem)))
    real_issuer_cn = None
    for attr in real_ca.subject:
        if attr.oid == NameOID.COMMON_NAME:
            real_issuer_cn = attr.value
            break
    if not real_issuer_cn:  # pragma: no cover — every cert has a CN; defensive
        real_issuer_cn = real_ca.subject.rfc4514_string()

    # Build a leaf whose issuer's RFC4514 matches the real CA's.
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf.example.com")])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(real_ca.subject)  # exact-match issuer DN
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.utcnow() - dt.timedelta(minutes=1))
        .not_valid_after(dt.datetime.utcnow() + dt.timedelta(days=30))
        .sign(leaf_key, hashes.SHA256())
    )
    cert_path = tmp_path / "fullchain.pem"
    cert_path.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setenv("AGNES_TLS_FULLCHAIN_PATH", str(cert_path))

    from app.web.router import _read_agnes_ca_pem
    assert _read_agnes_ca_pem() is None


def test_read_agnes_ca_pem_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_TLS_FULLCHAIN_PATH", str(tmp_path / "nope.pem"))
    from app.web.router import _read_agnes_ca_pem
    assert _read_agnes_ca_pem() is None


def test_read_agnes_ca_pem_empty_file_returns_none(tmp_path, monkeypatch):
    p = tmp_path / "empty.pem"
    p.write_text("")
    monkeypatch.setenv("AGNES_TLS_FULLCHAIN_PATH", str(p))
    from app.web.router import _read_agnes_ca_pem
    assert _read_agnes_ca_pem() is None


def test_read_agnes_ca_pem_garbage_returns_none(tmp_path, monkeypatch):
    """Non-PEM body (e.g. an HTML error page mistakenly stored at the path)
    must not crash the dashboard render — return None and fall through."""
    p = tmp_path / "garbage.pem"
    p.write_text("<html>500 server error</html>")
    monkeypatch.setenv("AGNES_TLS_FULLCHAIN_PATH", str(p))
    from app.web.router import _read_agnes_ca_pem
    assert _read_agnes_ca_pem() is None
