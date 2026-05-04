"""Reader smoke matrix — every reader CLI command on a freshly-bootstrapped
zero-grants workspace, asserts no traceback. The load-bearing test for
'nothing crashes on missing dirs'."""

import os
import subprocess
import sys

import pytest

from tests.fixtures.analyst_bootstrap import NONEXISTENT_TABLE


# Use `python -m cli.main` (not the `.venv/bin/agnes` shim) for the same
# iCloud-shim-race reason Task 20's fixtures use it.
AGNES = [sys.executable, "-m", "cli.main"]


READER_COMMANDS = [
    AGNES + ["catalog"],
    AGNES + ["catalog", "--metrics"],
    AGNES + ["schema", NONEXISTENT_TABLE],
    AGNES + ["describe", NONEXISTENT_TABLE],
    AGNES + ["query", "SELECT 1"],
    AGNES + ["explore", NONEXISTENT_TABLE],
    AGNES + ["disk-info"],
    AGNES + ["snapshot", "list"],
    AGNES + ["snapshot", "create", NONEXISTENT_TABLE, "--as", "x", "--estimate"],
    AGNES + ["status"],
    AGNES + ["diagnose"],
    AGNES + ["auth", "whoami"],
    AGNES + ["skills", "list"],
    AGNES + ["skills", "show", "agnes-data-querying"],
]


@pytest.mark.parametrize("cmd", READER_COMMANDS, ids=lambda c: " ".join(c[3:]) if len(c) > 3 else "agnes")
def test_reader_does_not_crash_on_zero_grants(zero_grants_workspace, fastapi_test_server, cmd):
    """Exit 0 (success) or exit 1 (friendly hint) is OK; tracebacks are forbidden."""
    env = os.environ.copy()
    env["AGNES_LOCAL_DIR"] = str(zero_grants_workspace)
    env["AGNES_SERVER"] = fastapi_test_server.url
    # Token already saved by `agnes init` during fixture setup; AGNES_TOKEN
    # env override would defeat that. Leave it unset and let cli.config read
    # the saved token.json.
    result = subprocess.run(cmd, cwd=zero_grants_workspace, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode in (0, 1), \
        f"{cmd} crashed: rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    assert "Traceback" not in result.stderr, f"{cmd} threw: {result.stderr}"
