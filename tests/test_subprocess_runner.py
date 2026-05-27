"""Tests for ``src/_subprocess_runner.py``.

Covers the contract: JSON in / JSON out via stdin/stdout, error
propagation, timeout handling. Uses inline ``python -c`` worker payloads
as stand-ins for the real worker modules (the runner is connector-
agnostic; the real workers get their own integration tests next to
their own module).
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from src._subprocess_runner import SubprocessJobError, run_subprocess_job


def _write_worker_module(tmp_path: Path, body: str) -> str:
    """Create a temp module that the runner can invoke via ``python -m``.

    Returns the dotted module path. Caller is responsible for invoking
    inside the same Python interpreter; we add the tmp dir to ``PYTHONPATH``
    via ``env_extra``.
    """
    mod_dir = tmp_path / "_workers"
    mod_dir.mkdir(exist_ok=True)
    (mod_dir / "__init__.py").write_text("")
    name = f"_worker_{abs(hash(body)) % 100000}"
    (mod_dir / f"{name}.py").write_text(textwrap.dedent(body))
    return f"_workers.{name}", str(tmp_path)


def test_returns_json_result_from_worker_stdout(tmp_path):
    mod, parent = _write_worker_module(tmp_path, """
        import json, sys
        args = json.load(sys.stdin)
        print(json.dumps({"echo": args, "doubled": args["x"] * 2}))
    """)
    result = run_subprocess_job(
        mod,
        {"x": 21},
        env_extra={"PYTHONPATH": parent + os.pathsep + os.environ.get("PYTHONPATH", "")},
        cwd=tmp_path,
    )
    assert result == {"echo": {"x": 21}, "doubled": 42}


def test_non_zero_exit_raises_with_stderr(tmp_path):
    mod, parent = _write_worker_module(tmp_path, """
        import sys
        print("something on stderr — operator should see this", file=sys.stderr)
        sys.exit(7)
    """)
    with pytest.raises(SubprocessJobError) as exc:
        run_subprocess_job(
            mod, {}, env_extra={"PYTHONPATH": parent + os.pathsep + os.environ.get("PYTHONPATH", "")},
            cwd=tmp_path,
        )
    assert exc.value.exit_code == 7
    assert "operator should see this" in exc.value.stderr


def test_empty_stdout_raises_even_on_zero_exit(tmp_path):
    """A worker that exits 0 but emits nothing on stdout violates the contract."""
    mod, parent = _write_worker_module(tmp_path, """
        import sys
        # No stdout write.
        sys.exit(0)
    """)
    with pytest.raises(SubprocessJobError, match="empty stdout"):
        run_subprocess_job(
            mod, {}, env_extra={"PYTHONPATH": parent + os.pathsep + os.environ.get("PYTHONPATH", "")},
            cwd=tmp_path,
        )


def test_invalid_json_on_stdout_raises(tmp_path):
    mod, parent = _write_worker_module(tmp_path, """
        print("not json")
    """)
    with pytest.raises(SubprocessJobError, match="not valid JSON"):
        run_subprocess_job(
            mod, {}, env_extra={"PYTHONPATH": parent + os.pathsep + os.environ.get("PYTHONPATH", "")},
            cwd=tmp_path,
        )


def test_timeout_kills_worker_and_raises(tmp_path):
    mod, parent = _write_worker_module(tmp_path, """
        import time, sys
        # Don't read stdin — keeps the parent's communicate() blocked on
        # write back-pressure, simulating a hung worker.
        time.sleep(30)
        print("{}")
    """)
    with pytest.raises(SubprocessJobError, match="timed out"):
        run_subprocess_job(
            mod,
            {},
            timeout_sec=2,
            env_extra={"PYTHONPATH": parent + os.pathsep + os.environ.get("PYTHONPATH", "")},
            cwd=tmp_path,
        )


def test_env_extra_reaches_worker(tmp_path):
    mod, parent = _write_worker_module(tmp_path, """
        import json, os, sys
        json.load(sys.stdin)
        print(json.dumps({"saw_token": os.environ.get("AGNES_TEST_TOKEN")}))
    """)
    result = run_subprocess_job(
        mod,
        {},
        env_extra={
            "PYTHONPATH": parent + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "AGNES_TEST_TOKEN": "shh-secret",
        },
        cwd=tmp_path,
    )
    assert result == {"saw_token": "shh-secret"}
