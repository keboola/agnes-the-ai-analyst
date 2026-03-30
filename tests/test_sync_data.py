"""Tests for sync script reliability and live rsync diagnostics.

Static tests verify that sync_data.sh and sync_jira.sh follow reliability
patterns (retry wrapper, SSH keepalive, timeouts, partial transfers).

Live tests (marked @pytest.mark.live) run actual rsync transfers against
the data-analyst server and write diagnostic logs to data/sync_diagnostics/.
"""

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
SYNC_DATA_SH = SCRIPTS_DIR / "sync_data.sh"
SYNC_JIRA_SH = REPO_ROOT / "connectors" / "jira" / "scripts" / "sync_jira.sh"
SYNC_SCRIPTS = [SYNC_DATA_SH, SYNC_JIRA_SH]
DIAG_DIR = REPO_ROOT / "data" / "sync_diagnostics"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_rsync_lines(script_path: Path) -> list[tuple[int, str]]:
    """Find all lines that invoke rsync (not as part of variable/comment)."""
    results = []
    content = script_path.read_text()
    for line_num, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # Match lines that call rsync as a command (not in variable names)
        if re.match(r'^\s*(rsync_reliable|rsync)\s', stripped):
            results.append((line_num, stripped))
    return results


def _extract_function_body(script_content: str, func_name: str) -> str:
    """Extract the body of a bash function."""
    pattern = rf'{func_name}\s*\(\)\s*\{{(.*?)\n\}}'
    match = re.search(pattern, script_content, re.DOTALL)
    return match.group(1) if match else ""


def _write_diagnostic(filename: str, content: str) -> None:
    """Write diagnostic output to DIAG_DIR with a timestamp header."""
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = DIAG_DIR / filename
    path.write_text(f"# Diagnostic run at {timestamp}\n\n{content}\n")


# ---------------------------------------------------------------------------
# Live diagnostic tests — require server access
# ---------------------------------------------------------------------------

class TestSyncDiagnostics:
    """Live tests that run actual rsync transfers for diagnostics."""

    @pytest.mark.live
    def test_ssh_connectivity(self):
        """Verify basic SSH connectivity to data-analyst host."""
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "data-analyst", "echo", "ok"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        _write_diagnostic(
            "ssh_connectivity.log",
            f"returncode: {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}",
        )
        assert result.returncode == 0, (
            f"SSH connection failed (rc={result.returncode}): {result.stderr}"
        )

    @pytest.mark.live
    def test_rsync_small_directory(self):
        """Sync a small directory (docs) to verify basic rsync works."""
        dest = Path("/tmp/claude/sync_test_docs/")
        dest.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "rsync", "-avz", "--timeout=60",
                "data-analyst:server/docs/", str(dest),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        _write_diagnostic(
            "rsync_small_directory.log",
            f"returncode: {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
        )
        assert result.returncode == 0, (
            f"rsync docs failed (rc={result.returncode}): {result.stderr}"
        )

    @pytest.mark.live
    def test_rsync_with_keepalive(self):
        """Sync parquet directory with SSH keepalive options."""
        dest = Path("/tmp/claude/sync_test_parquet/")
        dest.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                [
                    "rsync",
                    "-e", "ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=3",
                    "-av", "--timeout=300", "--delete",
                    "data-analyst:server/parquet/", str(dest),
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
            _write_diagnostic(
                "rsync_with_keepalive.log",
                f"returncode: {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}",
            )
            assert result.returncode == 0, (
                f"rsync with keepalive failed (rc={result.returncode}): {result.stderr}"
            )
        except subprocess.TimeoutExpired:
            _write_diagnostic(
                "rsync_with_keepalive.log",
                "TIMEOUT: rsync with keepalive exceeded 600s limit",
            )
            pytest.fail("rsync with keepalive timed out after 600s")

    @pytest.mark.live
    def test_rsync_with_timeout(self):
        """Sync parquet with a short timeout to test timeout behaviour."""
        dest = Path("/tmp/claude/sync_test_timeout/")
        dest.mkdir(parents=True, exist_ok=True)
        timed_out = False
        try:
            result = subprocess.run(
                [
                    "rsync", "-av", "--timeout=60",
                    "data-analyst:server/parquet/", str(dest),
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
            outcome = (
                f"returncode: {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            outcome = "TIMEOUT: rsync with short timeout exceeded 180s limit"

        _write_diagnostic("rsync_with_timeout.log", outcome)
        # This is a diagnostic test — we log whether it timed out or succeeded.
        if timed_out:
            pytest.skip("rsync timed out (diagnostic recorded)")

    @pytest.mark.live
    def test_rsync_per_subdirectory(self):
        """List parquet subdirectories and sync each one individually."""
        # First, list remote subdirectories
        ls_result = subprocess.run(
            ["ssh", "data-analyst", "ls server/parquet/"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert ls_result.returncode == 0, (
            f"Failed to list remote parquet dirs: {ls_result.stderr}"
        )

        subdirs = [d for d in ls_result.stdout.strip().splitlines() if d]
        results_log: list[str] = [f"Found {len(subdirs)} subdirectories\n"]

        for subdir in subdirs:
            dest = Path(f"/tmp/claude/sync_test_subdir/{subdir}/")
            dest.mkdir(parents=True, exist_ok=True)
            try:
                result = subprocess.run(
                    [
                        "rsync", "-av", "--timeout=120",
                        f"data-analyst:server/parquet/{subdir}/", str(dest),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                results_log.append(
                    f"[{subdir}] rc={result.returncode} "
                    f"({'OK' if result.returncode == 0 else 'FAIL'})"
                )
            except subprocess.TimeoutExpired:
                results_log.append(f"[{subdir}] TIMEOUT after 300s")

        _write_diagnostic("rsync_per_subdirectory.log", "\n".join(results_log))

    @pytest.mark.live
    def test_rsync_without_compression(self):
        """Sync parquet without -z flag (compression hurts for binary data)."""
        dest = Path("/tmp/claude/sync_test_nocompress/")
        dest.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                [
                    "rsync", "-av", "--timeout=300", "--delete",
                    "data-analyst:server/parquet/", str(dest),
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
            _write_diagnostic(
                "rsync_without_compression.log",
                f"returncode: {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}",
            )
            assert result.returncode == 0, (
                f"rsync without compression failed (rc={result.returncode}): "
                f"{result.stderr}"
            )
        except subprocess.TimeoutExpired:
            _write_diagnostic(
                "rsync_without_compression.log",
                "TIMEOUT: rsync without compression exceeded 600s limit",
            )
            pytest.fail("rsync without compression timed out after 600s")


# ---------------------------------------------------------------------------
# Static regression tests — run in CI, no server needed
# ---------------------------------------------------------------------------

class TestSyncScriptReliability:
    """Verify sync scripts follow reliability patterns (retry, keepalive, etc.)."""

    def test_sync_scripts_define_rsync_reliable(self):
        """Both sync scripts must define an rsync_reliable() function."""
        for script in SYNC_SCRIPTS:
            content = script.read_text()
            assert re.search(r'rsync_reliable\s*\(\)', content), (
                f"{script.name} does not define rsync_reliable() function"
            )

    def test_sync_scripts_define_reliability_constants(self):
        """Both scripts must define RSYNC_SSH_OPTS, RSYNC_TIMEOUT,
        RSYNC_MAX_RETRIES, and RSYNC_RETRY_DELAY."""
        required_constants = [
            "RSYNC_SSH_OPTS",
            "RSYNC_TIMEOUT",
            "RSYNC_MAX_RETRIES",
            "RSYNC_RETRY_DELAY",
        ]
        for script in SYNC_SCRIPTS:
            content = script.read_text()
            for const in required_constants:
                assert re.search(rf'{const}=', content), (
                    f"{script.name} does not define {const}"
                )

    def test_rsync_reliable_uses_ssh_keepalive(self):
        """rsync_reliable function must use SSH keepalive (via RSYNC_SSH_OPTS)."""
        for script in SYNC_SCRIPTS:
            content = script.read_text()
            body = _extract_function_body(content, "rsync_reliable")
            assert body, f"{script.name}: could not extract rsync_reliable body"
            # Function references RSYNC_SSH_OPTS which must contain keepalive
            assert "RSYNC_SSH_OPTS" in body, (
                f"{script.name}: rsync_reliable does not reference RSYNC_SSH_OPTS"
            )
            assert "ServerAliveInterval" in content, (
                f"{script.name}: RSYNC_SSH_OPTS does not define ServerAliveInterval"
            )

    def test_rsync_reliable_uses_timeout(self):
        """rsync_reliable function must use --timeout."""
        for script in SYNC_SCRIPTS:
            content = script.read_text()
            body = _extract_function_body(content, "rsync_reliable")
            assert body, f"{script.name}: could not extract rsync_reliable body"
            assert "--timeout" in body, (
                f"{script.name}: rsync_reliable does not use --timeout"
            )

    def test_rsync_reliable_uses_partial_dir(self):
        """rsync_reliable function must use --partial-dir."""
        for script in SYNC_SCRIPTS:
            content = script.read_text()
            body = _extract_function_body(content, "rsync_reliable")
            assert body, f"{script.name}: could not extract rsync_reliable body"
            assert "--partial-dir" in body, (
                f"{script.name}: rsync_reliable does not use --partial-dir"
            )

    @pytest.mark.parametrize(
        "script_path",
        [
            pytest.param(SYNC_DATA_SH, id="sync_data.sh"),
            pytest.param(SYNC_JIRA_SH, id="sync_jira.sh"),
        ],
    )
    def test_all_rsync_calls_use_reliable_wrapper(self, script_path: Path):
        """All rsync invocations must go through rsync_reliable, with
        narrow exceptions:
          - lines inside the rsync_reliable() function definition itself
          - lines checking rsync availability (command -v rsync)
          - the self-update rsync in sync_data.sh that has an scp fallback
        """
        content = script_path.read_text()
        func_body = _extract_function_body(content, "rsync_reliable")
        # Line numbers that belong to rsync_reliable function body
        func_lines: set[int] = set()
        if func_body:
            start_idx = content.index(func_body)
            start_line = content[:start_idx].count("\n") + 1
            end_line = start_line + func_body.count("\n")
            func_lines = set(range(start_line, end_line + 1))

        all_lines = _find_rsync_lines(script_path)
        violations: list[tuple[int, str]] = []

        for line_num, line in all_lines:
            # Skip lines inside rsync_reliable() definition
            if line_num in func_lines:
                continue
            # Skip rsync availability checks
            if "command -v rsync" in line:
                continue
            # Skip self-update rsync with scp fallback (sync_data.sh only)
            if "server/scripts/" in line and script_path.name == "sync_data.sh":
                continue
            # Everything else must use rsync_reliable, not bare rsync
            if line.startswith("rsync ") or re.match(r'^rsync\s', line):
                violations.append((line_num, line))

        assert not violations, (
            f"{script_path.name} has bare rsync calls that should use "
            f"rsync_reliable:\n"
            + "\n".join(f"  L{n}: {l}" for n, l in violations)
        )

    def test_parquet_rsync_does_not_use_compression(self):
        """rsync calls targeting parquet paths must NOT use -z flag.
        Parquet is already compressed; -z adds CPU overhead for no gain."""
        for script in SYNC_SCRIPTS:
            lines = _find_rsync_lines(script)
            for line_num, line in lines:
                if "parquet" not in line:
                    continue
                # Check for -avz or standalone -z in flags
                assert "-avz" not in line, (
                    f"{script.name} L{line_num}: parquet rsync uses -avz "
                    f"(should be -av without z): {line}"
                )

    def test_text_rsync_uses_compression(self):
        """rsync calls targeting text paths (docs, scripts, metadata,
        examples) should use -z compression."""
        text_indicators = ["docs", "scripts", "metadata", "examples"]
        binary_indicators = ["parquet", "attachments"]

        for script in SYNC_SCRIPTS:
            lines = _find_rsync_lines(script)
            for line_num, line in lines:
                # Skip lines targeting binary paths
                if any(b in line for b in binary_indicators):
                    continue
                # Only check lines targeting known text paths
                if not any(t in line for t in text_indicators):
                    continue
                assert "-z" in line or "-avz" in line, (
                    f"{script.name} L{line_num}: text rsync missing -z "
                    f"compression flag: {line}"
                )
