"""SESSION_SECRET provisioning in the customer-instance startup script.

app/startup_guards.py hard-fails a multi-process (role-split) deployment
unless both ``JWT_SECRET_KEY`` and ``SESSION_SECRET`` are set explicitly in
the environment — no per-node autogeneration is allowed, because a value
generated independently on each process desyncs sessions/JWTs across roles.

``JWT_SECRET_KEY`` already satisfies this: the module mints a dedicated
Secret Manager secret and the startup script fetches it fresh on every boot
(no on-VM fallback generation). These tests pin that ``SESSION_SECRET`` is
wired the exact same way, so a future edit that only touches one of the two
doesn't silently reopen the multi-process gap for the other.
"""

import re
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")
TPL = MODULE / "startup-script.sh.tpl"
MAIN_TF = MODULE / "main.tf"


def test_template_fetches_session_secret_like_jwt():
    tpl = TPL.read_text()
    jwt_fetch = re.search(
        r"JWT_KEY=\$\(gcloud secrets versions access latest --secret=agnes-\$\$\{CUSTOMER_NAME\}-jwt-secret\)",
        tpl,
    )
    assert jwt_fetch, "expected JWT_KEY fetch line not found — test fixture is stale"
    session_fetch = re.search(
        r"SESSION_KEY=\$\(gcloud secrets versions access latest --secret=agnes-\$\$\{CUSTOMER_NAME\}-session-secret\)",
        tpl,
    )
    assert session_fetch, (
        "startup-script.sh.tpl must fetch SESSION_KEY from Secret Manager the same way "
        "as JWT_KEY (no on-VM fallback generation — multi-process deployments need every "
        "process to agree on the same value)"
    )


def test_env_heredoc_writes_session_secret_alongside_jwt():
    tpl = TPL.read_text()
    assert "JWT_SECRET_KEY=$JWT_KEY" in tpl
    assert "SESSION_SECRET=$SESSION_KEY" in tpl, (
        "startup-script.sh.tpl must write SESSION_SECRET into /opt/agnes/.env — "
        "without it, app/startup_guards.py hard-fails any multi-process deployment"
    )
    # Keep them adjacent in the heredoc so a future edit can't drop one without
    # the diff being obvious.
    assert "JWT_SECRET_KEY=$JWT_KEY\nSESSION_SECRET=$SESSION_KEY\n" in tpl


def test_terraform_provisions_session_secret_like_jwt():
    main = MAIN_TF.read_text()
    assert 'resource "google_secret_manager_secret" "session"' in main
    assert 'secret_id = "agnes-${var.customer_name}-session-secret"' in main
    assert 'resource "random_password" "session"' in main
    assert 'resource "google_secret_manager_secret_version" "session"' in main
    assert 'resource "google_secret_manager_secret_iam_member" "vm_session"' in main


def test_vm_depends_on_session_secret_plumbing():
    """IAM grants + secret version must exist before the VM boots (same
    ordering rationale as the JWT secret) — a missing depends_on entry here
    would race the VM's first `gcloud secrets versions access` against IAM
    propagation."""
    main = MAIN_TF.read_text()
    depends_block = re.search(r"depends_on\s*=\s*\[(.*?)\]", main, re.DOTALL)
    assert depends_block, "google_compute_instance.vm depends_on block not found"
    block = depends_block.group(1)
    assert "google_secret_manager_secret_iam_member.vm_jwt" in block
    assert "google_secret_manager_secret_iam_member.vm_session" in block, (
        "vm_session IAM grant must be in the VM's depends_on, mirroring vm_jwt"
    )
    assert "google_secret_manager_secret_version.jwt" in block
    assert "google_secret_manager_secret_version.session" in block, (
        "session secret version must be in the VM's depends_on, mirroring the jwt version"
    )
