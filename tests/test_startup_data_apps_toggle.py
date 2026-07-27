"""Static contract for the data-apps enablement's Terraform → startup plumbing.

Mirrors ``test_startup_studio_toggle.py``. Pins the three-part infra contract so
a rename or dropped template argument can't silently break durable enablement:

* ``variables.tf`` declares ``data_apps_enabled`` (bool, default false) +
  ``data_apps_runtime_image`` (string);
* ``main.tf`` forwards both into ``templatefile(...)``;
* ``startup-script.sh.tpl`` — ONLY when ``data_apps_enabled`` — mints/persists
  ``APPS_RUNNER_TOKEN``, resolves ``DOCKER_GID`` from the docker socket, and
  emits ``COMPOSE_PROFILES=apps`` + ``AGNES_DATA_APPS_ENABLED=true`` (+ the
  runner token/prefix/gid) into the app ``.env``. Disabled instances render a
  byte-identical ``.env`` (the whole block is absent).
"""

import re
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")


def test_variables_tf_declares_toggle_and_image():
    body = (MODULE / "variables.tf").read_text()
    m = re.search(r'variable\s+"data_apps_enabled"\s*\{(.*?)\n\}', body, re.DOTALL)
    assert m, "variables.tf must declare data_apps_enabled"
    assert re.search(r"type\s*=\s*bool", m.group(1))
    assert re.search(r"default\s*=\s*false", m.group(1))
    assert re.search(r'variable\s+"data_apps_runtime_image"\s*\{', body)


def test_main_tf_forwards_both_into_templatefile():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r"data_apps_enabled\s*=\s*var\.data_apps_enabled", body)
    assert re.search(r"data_apps_runtime_image\s*=\s*var\.data_apps_runtime_image", body)


def test_tpl_env_block_guarded_by_toggle():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    # The .env keys the feature needs, all inside a single `if data_apps_enabled`.
    for key in (
        "COMPOSE_PROFILES=apps",
        "AGNES_DATA_APPS_ENABLED=true",
        "APPS_RUNNER_TOKEN=$APPS_RUNNER_TOKEN",
        "DOCKER_GID=$DOCKER_GID",
    ):
        assert key in body, key
    # No unconditional AGNES_DATA_APPS_ENABLED leak.
    assert body.count("AGNES_DATA_APPS_ENABLED=true") == 1
    # Token minted with the same read-back-then-openssl pattern as the scheduler token.
    assert "APPS_RUNNER_TOKEN=$(openssl rand -hex 32)" in body
    # DOCKER_GID resolved from the socket (so uid 999 can reach the daemon).
    assert "stat -c '%g' /var/run/docker.sock" in body


def test_tpl_data_apps_blocks_are_toggle_gated():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    # Two positive `if data_apps_enabled` blocks (the token/DOCKER_GID prep
    # before the .env heredoc + the .env keys inside it), only positive guards
    # (no `!data_apps_enabled`), so a default instance renders none of it.
    assert body.count("%{ if data_apps_enabled ~}") == 2
    assert "!data_apps_enabled" not in body
    # The APPS_RUNNER_TOKEN prep must precede its use in the .env heredoc.
    assert body.index("APPS_RUNNER_TOKEN=$(openssl") < body.index("APPS_RUNNER_TOKEN=$APPS_RUNNER_TOKEN")
