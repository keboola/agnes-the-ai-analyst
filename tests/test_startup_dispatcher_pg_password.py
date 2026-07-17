"""DISPATCHER_PG_PASSWORD provisioning in the customer-instance startup script.

The opt-in LLM dispatcher's ledger Postgres (``docker-compose.dispatcher.yml``,
data dir ``$DATA_MNT/dispatcher-postgres``) is keyed by a password minted at
boot. ``postgres:16-alpine`` only applies ``POSTGRES_PASSWORD`` on first
initdb of an empty data dir — once the ledger has data, the container keeps
whatever password it was first given.

These tests pin the startup-script contract (same shape as
``test_startup_vault_key.py``'s ``AGNES_VAULT_KEY`` handling):

* the password is minted once and written into ``/opt/agnes/.env`` for the
  dispatcher/ledger-postgres containers to pick up;
* its durable home is the persistent DATA disk
  (``$DATA_MNT/dispatcher-postgres/.pg-password``) — ``/opt/agnes/.env`` lives
  on the boot disk, which a VM recreate wipes, so re-minting on every boot
  would desync the password from the surviving (persistent-disk) database and
  lock the dispatcher out of its own ledger;
* a password already present in ``.env`` (e.g. from before this fix shipped)
  is adopted into the keyfile instead of being clobbered by a fresh mint.

The functional tests extract the marker-delimited block from the template and
execute it under bash against a tmpdir, so they exercise the real shell code,
not a re-implementation.
"""

import re
import shutil
import stat
import subprocess
from pathlib import Path

TPL = Path("infra/modules/customer-instance/startup-script.sh.tpl")

BEGIN = "# --- dispatcher-pg-password begin"
END = "# --- dispatcher-pg-password end"


def _password_block() -> str:
    tpl = TPL.read_text()
    m = re.search(re.escape(BEGIN) + r".*?\n(.*?)" + re.escape(END), tpl, re.DOTALL)
    assert m, (
        "startup-script.sh.tpl must contain the marker-delimited "
        f"dispatcher-pg-password block ({BEGIN!r} ... {END!r}) — the "
        "functional tests below execute it"
    )
    block = m.group(1)
    # The block must be plain bash — no Terraform interpolation sequences —
    # so the test can execute exactly what ships in the template.
    assert "${" not in block and "%{" not in block, (
        "dispatcher-pg-password block must not use Terraform interpolation "
        "('${' / '%{'); keep it plain bash so tests execute the shipped code "
        "verbatim"
    )
    return block


def _run_block(
    tmp_path: Path, env_content: str | None = None, keyfile_content: str | None = None
) -> tuple[str, Path, Path]:
    """Execute the template's dispatcher-pg-password block against a sandbox.

    Returns (captured DISPATCHER_PG_PASSWORD value, keyfile path, .env path).
    """
    bash = shutil.which("bash")
    assert bash, "bash required"
    app_dir = tmp_path / "app"
    data_mnt = tmp_path / "data"
    app_dir.mkdir(exist_ok=True)
    data_mnt.mkdir(exist_ok=True)
    if env_content is not None:
        (app_dir / ".env").write_text(env_content)
    keyfile = data_mnt / "dispatcher-postgres" / ".pg-password"
    if keyfile_content is not None:
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        keyfile.write_text(keyfile_content)
    script = (
        "set -euo pipefail\n"
        f'APP_DIR="{app_dir}"\n'
        f'DATA_MNT="{data_mnt}"\n' + _password_block() + '\nprintf "%s" "$DISPATCHER_PG_PASSWORD"\n'
    )
    proc = subprocess.run([bash, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, f"dispatcher-pg-password block failed: {proc.stderr}"
    return proc.stdout, keyfile, app_dir / ".env"


def test_template_wires_password_into_env():
    """The .env heredoc must carry the password to the compose containers."""
    tpl = TPL.read_text()
    assert "DISPATCHER_PG_PASSWORD=$DISPATCHER_PG_PASSWORD" in tpl, (
        "startup-script.sh.tpl must write DISPATCHER_PG_PASSWORD into /opt/agnes/.env"
    )


def test_fresh_boot_mints_password_and_persists_it(tmp_path):
    password, keyfile, _ = _run_block(tmp_path)
    assert password, "a password must be minted"
    assert keyfile.read_text().strip() == password
    mode = stat.S_IMODE(keyfile.stat().st_mode)
    assert mode == 0o600, f"keyfile must be 0600, got {oct(mode)}"


def test_reboot_preserves_existing_keyfile(tmp_path):
    """A VM reboot (data disk persists) must not re-mint the password —
    doing so would desync it from the already-initialized ledger database."""
    existing = "existing-hex-password"
    password, keyfile, _ = _run_block(tmp_path, keyfile_content=existing + "\n")
    assert password == existing
    assert keyfile.read_text().strip() == existing


def test_hand_added_env_password_is_adopted_into_keyfile(tmp_path):
    """A password already present in .env (pre-fix state) is adopted into
    the durable keyfile rather than being clobbered by a fresh mint."""
    existing = "legacy-env-password"
    env = f"JWT_SECRET_KEY=x\nDISPATCHER_PG_PASSWORD={existing}\nDATA_DIR=/data\n"
    password, keyfile, _ = _run_block(tmp_path, env_content=env)
    assert password == existing
    assert keyfile.read_text().strip() == existing


def test_keyfile_wins_over_env(tmp_path):
    """VM recreate: the boot disk's .env is freshly templated (or stale) —
    the persistent-disk keyfile paired with the surviving ledger DB must
    take precedence, or the dispatcher can no longer authenticate to it."""
    file_password = "data-disk-password"
    env_password = "stale-boot-disk-password"
    password, _, _ = _run_block(
        tmp_path,
        env_content=f"DISPATCHER_PG_PASSWORD={env_password}\n",
        keyfile_content=file_password + "\n",
    )
    assert password == file_password


def test_two_runs_are_stable(tmp_path):
    first, keyfile, _ = _run_block(tmp_path)
    second, _, _ = _run_block(tmp_path, keyfile_content=keyfile.read_text())
    assert first == second
