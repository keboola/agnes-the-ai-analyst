"""AGNES_VAULT_KEY provisioning in the customer-instance startup script.

The admin secrets vault (``app/secrets_vault.py`` — datasource, Slack and MCP
secrets encrypted in the state DB) is keyed by ``AGNES_VAULT_KEY``. Without it
every vault write returns 409 ``vault_key_not_configured``, and losing an
existing key makes previously-encrypted rows undecryptable forever.

These tests pin the startup-script contract:

* the key is minted once (valid Fernet format) and written into
  ``/opt/agnes/.env`` like the other app secrets;
* its durable home is the persistent DATA disk
  (``$DATA_MNT/state/agnes-vault.key``) — ``/opt/agnes/.env`` lives on the
  boot disk, which a VM recreate wipes, so the ``SCHEDULER_API_TOKEN``-style
  read-back-from-.env pattern alone is NOT enough here;
* a key an operator hand-added to ``.env`` (the pre-fix mitigation on live
  instances) is adopted into the keyfile instead of being clobbered.

The functional tests extract the marker-delimited block from the template and
execute it under bash against a tmpdir, so they exercise the real shell code,
not a re-implementation.
"""

import re
import shutil
import stat
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet

TPL = Path("infra/modules/customer-instance/startup-script.sh.tpl")

BEGIN = "# --- vault-key begin"
END = "# --- vault-key end"


def _vault_block() -> str:
    tpl = TPL.read_text()
    m = re.search(re.escape(BEGIN) + r".*?\n(.*?)" + re.escape(END), tpl, re.DOTALL)
    assert m, (
        "startup-script.sh.tpl must contain the marker-delimited vault-key "
        f"block ({BEGIN!r} ... {END!r}) — the functional tests below execute it"
    )
    block = m.group(1)
    # The block must be plain bash — no Terraform interpolation sequences —
    # so the test can execute exactly what ships in the template.
    assert "${" not in block and "%{" not in block, (
        "vault-key block must not use Terraform interpolation ('${' / '%{'); "
        "keep it plain bash so tests execute the shipped code verbatim"
    )
    return block


def _run_block(
    tmp_path: Path, env_content: str | None = None, keyfile_content: str | None = None
) -> tuple[str, Path, Path]:
    """Execute the template's vault-key block against a sandbox.

    Returns (captured AGNES_VAULT_KEY value, keyfile path, .env path).
    """
    bash = shutil.which("bash")
    assert bash, "bash required"
    app_dir = tmp_path / "app"
    data_mnt = tmp_path / "data"
    app_dir.mkdir(exist_ok=True)
    (data_mnt / "state").mkdir(parents=True, exist_ok=True)
    if env_content is not None:
        (app_dir / ".env").write_text(env_content)
    keyfile = data_mnt / "state" / "agnes-vault.key"
    if keyfile_content is not None:
        keyfile.write_text(keyfile_content)
    script = (
        "set -euo pipefail\n"
        f'APP_DIR="{app_dir}"\n'
        f'DATA_MNT="{data_mnt}"\n' + _vault_block() + '\nprintf "%s" "$AGNES_VAULT_KEY"\n'
    )
    proc = subprocess.run([bash, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, f"vault-key block failed: {proc.stderr}"
    return proc.stdout, keyfile, app_dir / ".env"


def test_template_wires_vault_key_into_env():
    """The .env heredoc must carry the key to the app containers."""
    tpl = TPL.read_text()
    assert "AGNES_VAULT_KEY=$AGNES_VAULT_KEY" in tpl, (
        "startup-script.sh.tpl must write AGNES_VAULT_KEY into /opt/agnes/.env"
    )


def test_fresh_boot_mints_valid_fernet_key(tmp_path):
    key, keyfile, _ = _run_block(tmp_path)
    # Must be a syntactically valid Fernet key (urlsafe base64, 32 bytes) —
    # exactly what app.secrets_vault.vault_key_configured() checks.
    Fernet(key.encode("ascii"))
    assert keyfile.read_text().strip() == key
    mode = stat.S_IMODE(keyfile.stat().st_mode)
    assert mode == 0o600, f"keyfile must be 0600, got {oct(mode)}"


def test_reboot_preserves_existing_keyfile(tmp_path):
    existing = Fernet.generate_key().decode()
    key, keyfile, _ = _run_block(tmp_path, keyfile_content=existing + "\n")
    assert key == existing, "an existing keyfile must win — rotating orphans vault rows"
    assert keyfile.read_text().strip() == existing


def test_hand_added_env_key_is_adopted_into_keyfile(tmp_path):
    """Live-instance mitigation path: operator added AGNES_VAULT_KEY to
    /opt/agnes/.env by hand. The block must adopt it into the durable keyfile
    (so the NEXT recreate keeps it) rather than minting a fresh key."""
    existing = Fernet.generate_key().decode()
    env = f"JWT_SECRET_KEY=x\nAGNES_VAULT_KEY={existing}\nDATA_DIR=/data\n"
    key, keyfile, _ = _run_block(tmp_path, env_content=env)
    assert key == existing
    assert keyfile.read_text().strip() == existing


def test_keyfile_wins_over_env(tmp_path):
    """The data-disk keyfile is the source of truth: after a VM recreate the
    boot disk's .env is freshly templated (or stale) — the keyfile paired with
    the surviving ciphertext must take precedence."""
    file_key = Fernet.generate_key().decode()
    env_key = Fernet.generate_key().decode()
    key, _, _ = _run_block(
        tmp_path,
        env_content=f"AGNES_VAULT_KEY={env_key}\n",
        keyfile_content=file_key + "\n",
    )
    assert key == file_key


def test_two_runs_are_stable(tmp_path):
    first, keyfile, _ = _run_block(tmp_path)
    second, _, _ = _run_block(tmp_path, keyfile_content=keyfile.read_text())
    assert first == second
