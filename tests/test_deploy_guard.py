"""Deploy Guard Tests - Pre-merge CI tests to prevent deployment failures.

These tests validate consistency between deploy.sh, sudoers files, systemd
services, and server scripts. They run against the real repository structure
(no mocks) and automatically discover files/scripts/services.

Supports `# deploy-guard: ignore` comments in sudoers files to suppress
known false positives.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

# Repository root (two levels up from tests/)
REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "server"
DEPLOY_SH = SERVER_DIR / "deploy.sh"
BIN_DIR = SERVER_DIR / "bin"
SRC_DIR = REPO_ROOT / "src"
WEBAPP_DIR = REPO_ROOT / "webapp"


def _read_deploy_sh() -> str:
    """Read deploy.sh contents."""
    return DEPLOY_SH.read_text()


def _read_file(path: Path) -> str:
    """Read file contents, return empty string if not found."""
    if path.is_file():
        return path.read_text()
    return ""


def _find_sudoers_files() -> list[Path]:
    """Discover all sudoers-* files in server/."""
    return sorted(SERVER_DIR.glob("sudoers-*"))


def _find_service_files() -> list[Path]:
    """Discover all *.service files in server/."""
    return sorted(SERVER_DIR.glob("*.service"))


def _find_timer_files() -> list[Path]:
    """Discover all *.timer files in server/."""
    return sorted(SERVER_DIR.glob("*.timer"))


def _find_shell_scripts() -> list[Path]:
    """Discover all shell scripts in server/bin/ and server/*.sh."""
    scripts = []
    for f in sorted(BIN_DIR.glob("*")):
        if f.is_file():
            content = f.read_text(errors="replace")
            if content.startswith("#!/bin/bash") or content.startswith("#!/bin/sh"):
                scripts.append(f)
    for f in sorted(SERVER_DIR.glob("*.sh")):
        if f.is_file():
            scripts.append(f)
    return scripts


def _parse_sudoers_commands(sudoers_path: Path) -> list[dict]:
    """Parse sudoers file, extract allowed commands.

    Returns list of dicts with keys: user, command, line, ignored.
    """
    results = []
    content = sudoers_path.read_text()
    for line_num, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Match: user ALL=(ALL) NOPASSWD: /path/to/command [args]
        m = re.match(
            r"^(\S+)\s+ALL=\(ALL\)\s+NOPASSWD:\s+(.+)$", stripped
        )
        if m:
            user = m.group(1)
            command = m.group(2).strip()
            # Unescape sudoers backslash-colon (e.g., deploy\:data-ops -> deploy:data-ops)
            command = command.replace("\\:", ":")
            # Check for deploy-guard: ignore in preceding comment
            ignored = False
            if line_num >= 2:
                prev_line = content.splitlines()[line_num - 2].strip()
                if "deploy-guard: ignore" in prev_line:
                    ignored = True
            results.append({
                "user": user,
                "command": command,
                "line": line_num,
                "ignored": ignored,
                "file": sudoers_path.name,
            })
    return results


def _resolve_deploy_variables() -> dict[str, str]:
    """Extract variable assignments from deploy.sh header.

    Returns dict of variable_name -> value for simple assignments like:
        APP_DIR="/opt/data-analyst"
        REPO_DIR="${APP_DIR}/repo"
    """
    content = _read_deploy_sh()
    variables = {}
    for line in content.splitlines():
        stripped = line.strip()
        m = re.match(r'^(\w+)="([^"]*)"$', stripped)
        if not m:
            m = re.match(r"^(\w+)='([^']*)'$", stripped)
        if not m:
            m = re.match(r"^(\w+)=(\S+)$", stripped)
        if m:
            name, value = m.group(1), m.group(2)
            # Resolve references to other variables
            for var_name, var_value in variables.items():
                value = value.replace(f"${{{var_name}}}", var_value)
                value = value.replace(f"${var_name}", var_value)
            variables[name] = value
    return variables


def _extract_sudo_commands_from_deploy() -> list[str]:
    """Extract all sudo commands from deploy.sh.

    Returns normalized command strings with known variables resolved
    and unknown variables replaced with *.
    """
    content = _read_deploy_sh()
    variables = _resolve_deploy_variables()
    commands = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # Find sudo /path/to/command patterns
        # Skip lines with || true (optional commands that may fail)
        if "|| true" in stripped:
            continue
        for m in re.finditer(r"sudo\s+(/\S+(?:\s+[^|;&\n]+)?)", stripped):
            cmd = m.group(1).strip()
            # Remove trailing comments
            cmd = re.sub(r"\s*#.*$", "", cmd)
            # Remove shell redirections (e.g., > /dev/null, 2>/dev/null)
            cmd = re.sub(r"\s*\d*>.*$", "", cmd)
            cmd = re.sub(r"\s*[|].*$", "", cmd)
            # Remove quotes
            cmd = cmd.replace('"', '').replace("'", "")
            # Resolve known variables first
            for var_name, var_value in variables.items():
                cmd = cmd.replace(f"${{{var_name}}}", var_value)
                cmd = cmd.replace(f"${var_name}", var_value)
            # Replace remaining unknown variables with *
            cmd = re.sub(r'\$\{?\w+\}?', '*', cmd)
            cmd = cmd.strip()
            if cmd:
                commands.append(cmd)
    return _deduplicate_commands(commands)


def _deduplicate_commands(commands: list[str]) -> list[str]:
    """Remove duplicate commands after normalization."""
    seen = set()
    result = []
    for cmd in commands:
        if cmd not in seen:
            seen.add(cmd)
            result.append(cmd)
    return result


def _extract_service_cp_from_deploy() -> list[str]:
    """Extract service file names that deploy.sh copies to /etc/systemd/system/.

    Returns list of service/timer basenames.
    """
    content = _read_deploy_sh()
    results = []
    # Match patterns like:
    #   sudo /usr/bin/cp "${REPO_DIR}/server/foo.service" /etc/systemd/system/foo.service
    #   sudo /usr/bin/cp "${REPO_DIR}/server/foo.timer" /etc/systemd/system/foo.timer
    for m in re.finditer(
        r'sudo\s+/usr/bin/cp\s+["\']?\$\{?\w+\}?/server/(\S+\.(?:service|timer))["\']?\s+'
        r'/etc/systemd/system/',
        content,
    ):
        results.append(m.group(1))

    # Also match quoted form: "...server/foo.service"
    for m in re.finditer(
        r'sudo\s+/usr/bin/cp\s+"[^"]*?/server/([^"]+\.(?:service|timer))"\s+'
        r'/etc/systemd/system/',
        content,
    ):
        filename = m.group(1)
        if filename not in results:
            results.append(filename)

    return results


def _parse_service_file(path: Path) -> dict:
    """Parse a systemd service file and return key directives."""
    content = path.read_text()
    result = {"User": None, "Group": None, "ExecStart": None}
    for line in content.splitlines():
        stripped = line.strip()
        for key in result:
            if stripped.startswith(f"{key}="):
                result[key] = stripped.split("=", 1)[1].strip()
    return result


# =============================================================================
# 1. Sudoers <-> Deploy Consistency (P0)
# =============================================================================


class TestSudoersDeployConsistency:
    """Verify that all sudo commands in deploy.sh have matching sudoers rules."""

    def test_deploy_sudo_commands_have_sudoers_rules(self):
        """Every sudo command in deploy.sh must have a matching sudoers rule.

        Catches: New sudo operation in deploy.sh without sudoers rule (#123).
        """
        deploy_commands = _extract_sudo_commands_from_deploy()
        assert deploy_commands, "Should find sudo commands in deploy.sh"

        # Collect all sudoers rules
        all_sudoers_commands = []
        for sf in _find_sudoers_files():
            all_sudoers_commands.extend(_parse_sudoers_commands(sf))

        sudoers_patterns = [
            entry["command"] for entry in all_sudoers_commands
        ]

        missing = []
        for cmd in deploy_commands:
            if not _command_matches_any_sudoers_rule(cmd, sudoers_patterns):
                missing.append(cmd)

        assert not missing, (
            f"deploy.sh uses sudo commands without matching sudoers rules:\n"
            + "\n".join(f"  - {cmd}" for cmd in missing)
        )

    def test_sudoers_commands_are_used(self):
        """Each sudoers rule should be referenced somewhere in the codebase.

        Low-confidence test - uses deploy-guard: ignore to suppress false positives.

        Catches: Stale/dead sudoers rules (code hygiene).
        """
        # Collect all codebase content to search
        search_files = [DEPLOY_SH]
        search_files.extend(BIN_DIR.glob("*"))
        search_files.extend(WEBAPP_DIR.glob("*.py"))

        codebase_content = ""
        for f in search_files:
            if f.is_file():
                codebase_content += _read_file(f) + "\n"

        unused = []
        for sf in _find_sudoers_files():
            for entry in _parse_sudoers_commands(sf):
                if entry["ignored"]:
                    continue
                # Extract the binary path from the sudoers command
                binary = entry["command"].split()[0]
                binary_name = Path(binary).name

                # Check if the binary or command pattern appears in codebase
                if binary_name not in codebase_content and binary not in codebase_content:
                    unused.append(
                        f"{entry['file']}:{entry['line']} -> {entry['command']}"
                    )

        assert not unused, (
            f"Sudoers rules not referenced in codebase "
            f"(add '# deploy-guard: ignore' to suppress):\n"
            + "\n".join(f"  - {u}" for u in unused)
        )


def _command_matches_any_sudoers_rule(command: str, sudoers_patterns: list[str]) -> bool:
    """Check if a deploy.sh sudo command matches any sudoers rule.

    Both sides use wildcards: sudoers rules use * for glob matching,
    and deploy.sh commands have shell variables normalized to *.
    """
    for pattern in sudoers_patterns:
        if _sudoers_rule_matches(pattern, command):
            return True
    return False


def _sudoers_rule_matches(rule: str, command: str) -> bool:
    """Check if a sudoers rule matches a given command.

    Sudoers uses glob-like wildcards where * matches any string.
    The command may also contain * from variable normalization.

    Matching strategy:
    1. Binary (first token) must match.
    2. Compare argument-by-argument: a sudoers arg with * matches
       any command arg, and vice versa.
    3. If arg counts differ, the side with fewer args uses * to match rest.
    """
    # Direct string match
    if rule == command:
        return True

    rule_parts = rule.split()
    cmd_parts = command.split()
    if not rule_parts or not cmd_parts:
        return False

    # Binary must match exactly
    if rule_parts[0] != cmd_parts[0]:
        return False

    # If rule has no args beyond binary, it matches any args
    if len(rule_parts) == 1:
        return True

    # Compare arguments using regex with wildcard expansion
    rule_args = rule_parts[1:]
    cmd_args = cmd_parts[1:]

    # Strategy 1: Full regex matching (both directions)
    rule_regex = re.escape(" ".join(rule_args)).replace(r"\*", ".*")
    cmd_str = " ".join(cmd_args)
    cmd_regex = re.escape(" ".join(cmd_args)).replace(r"\*", ".*")
    rule_str = " ".join(rule_args)

    try:
        if re.fullmatch(rule_regex, cmd_str):
            return True
        if re.fullmatch(cmd_regex, rule_str):
            return True
    except re.error:
        pass

    # Strategy 2: Positional arg-by-arg with wildcard matching
    max_len = max(len(rule_args), len(cmd_args))
    padded_rule = rule_args + ["*"] * (max_len - len(rule_args))
    padded_cmd = cmd_args + ["*"] * (max_len - len(cmd_args))

    all_match = True
    for r_arg, c_arg in zip(padded_rule, padded_cmd):
        if r_arg == "*" or c_arg == "*":
            continue
        r_pat = re.escape(r_arg).replace(r"\*", ".*")
        c_pat = re.escape(c_arg).replace(r"\*", ".*")
        try:
            if not (re.fullmatch(r_pat, c_arg) or re.fullmatch(c_pat, r_arg)):
                all_match = False
                break
        except re.error:
            all_match = False
            break

    return all_match


# =============================================================================
# 2. Systemd Services (P0/P2)
# =============================================================================


class TestSystemdServices:
    """Verify consistency of systemd service and timer files."""

    def test_all_deployed_services_exist(self):
        """Every service/timer that deploy.sh copies must exist in server/.

        Catches: deploy.sh references non-existent service file.
        """
        deployed = _extract_service_cp_from_deploy()
        assert deployed, "Should find service deployments in deploy.sh"

        missing = []
        for filename in deployed:
            if not (SERVER_DIR / filename).is_file():
                missing.append(filename)

        assert not missing, (
            f"deploy.sh deploys non-existent service files:\n"
            + "\n".join(f"  - server/{f}" for f in missing)
        )

    def test_services_with_timers_have_both_files(self):
        """For each *.timer, the corresponding *.service must exist.

        If the timer has an explicit Unit= directive, that service is checked
        instead of the default (timer stem + .service).

        Catches: Timer without service file.
        """
        missing = []
        for timer in _find_timer_files():
            # Check if timer has explicit Unit= directive
            timer_content = timer.read_text()
            unit_match = re.search(r"^Unit=(\S+)", timer_content, re.MULTILINE)
            if unit_match:
                service_name = unit_match.group(1)
            else:
                service_name = timer.stem + ".service"

            if not (SERVER_DIR / service_name).is_file():
                missing.append(f"{timer.name} -> {service_name}")

        assert not missing, (
            f"Timer files without corresponding service:\n"
            + "\n".join(f"  - {m}" for m in missing)
        )

    def test_new_services_have_sudoers_for_systemctl(self):
        """Every service deployed by deploy.sh must have sudoers rules for systemctl.

        Catches: New service without sudoers for restart/start/stop (#123).
        """
        all_sudoers_content = ""
        for sf in _find_sudoers_files():
            all_sudoers_content += sf.read_text() + "\n"

        # Get service names from deploy.sh cp commands
        deployed = _extract_service_cp_from_deploy()
        service_names = {
            f.replace(".service", "").replace(".timer", "")
            for f in deployed
            if f.endswith(".service")
        }

        missing = []
        for svc in sorted(service_names):
            # Check that at least one systemctl operation exists for this service
            has_systemctl = bool(
                re.search(rf"systemctl\s+\S+\s+{re.escape(svc)}", all_sudoers_content)
            )
            if not has_systemctl:
                missing.append(svc)

        assert not missing, (
            f"Services deployed by deploy.sh without systemctl sudoers rules:\n"
            + "\n".join(f"  - {s}" for s in missing)
        )

    def test_service_files_have_valid_structure(self):
        """All service files must have required systemd sections."""
        for svc in _find_service_files():
            content = svc.read_text()
            assert "[Service]" in content, (
                f"{svc.name} missing [Service] section"
            )
            assert "[Unit]" in content, (
                f"{svc.name} missing [Unit] section"
            )
            assert "ExecStart=" in content, (
                f"{svc.name} missing ExecStart directive"
            )

    def test_exec_start_post_does_not_reference_runtime_files(self):
        """ExecStartPost commands must not operate on files created by the service itself.

        ExecStartPost runs immediately after the process starts, not when it's ready.
        If a service creates files asynchronously (sockets, pidfiles), ExecStartPost
        cannot reliably operate on them.

        Catches: notify-bot startup failure where ExecStartPost tried to chgrp a
        socket that didn't exist yet (#192/#193).
        """
        problems = []
        for svc in _find_service_files():
            content = svc.read_text()

            # Find RuntimeDirectory directive
            runtime_dir_match = re.search(r"RuntimeDirectory=(\S+)", content)
            if not runtime_dir_match:
                continue

            runtime_dir = runtime_dir_match.group(1)
            runtime_path = f"/run/{runtime_dir}"

            # Check if ExecStartPost references files in RuntimeDirectory
            exec_post_matches = re.findall(r"ExecStartPost=(.+)", content)
            for post_cmd in exec_post_matches:
                # Skip if it's a systemd special command (like -/bin/true)
                if post_cmd.startswith("-") or post_cmd.startswith("+"):
                    post_cmd = post_cmd[1:]

                # Check if command references the RuntimeDirectory path
                if runtime_path in post_cmd:
                    problems.append(
                        f"{svc.name}: ExecStartPost references {runtime_path}, "
                        f"but files in RuntimeDirectory may not exist yet at startup. "
                        f"Consider using a different approach (os.chown() in application code, "
                        f"RuntimeDirectoryGroup, or adding deploy user to required group)."
                    )

        assert not problems, (
            f"Service files with unsafe ExecStartPost:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    def test_timer_files_have_valid_structure(self):
        """All timer files must have required systemd sections."""
        for timer in _find_timer_files():
            content = timer.read_text()
            assert "[Timer]" in content, (
                f"{timer.name} missing [Timer] section"
            )
            assert "[Install]" in content, (
                f"{timer.name} missing [Install] section"
            )


# =============================================================================
# 3. File Ownership & Permissions (P0)
# =============================================================================


class TestFileOwnership:
    """Verify ownership and permission consistency in deploy.sh."""

    # Explicit list of critical directories and their expected ownership.
    # Maintained manually - extend when new critical directories are added.
    CRITICAL_DIRS = {
        "/data/scripts": {"owner": "root", "group": "data-ops"},
        "/data/docs": {"owner": "root", "group": "data-ops"},
        "/data/examples": {"owner": "root", "group": "data-ops"},
        "/data/notifications": {"owner": "root", "group": "data-ops"},
        "/data/auth": {"owner": "www-data", "group": "data-ops"},
        "/data/corporate-memory": {"owner": "root", "group": "data-ops"},
        "/data/user_sessions": {"owner": "root", "group": "data-ops"},
        "/data/src_data/raw/jira": {"owner": "root", "group": "data-ops"},
        "/opt/data-analyst": {"owner": "root", "group": "data-ops"},
    }

    def test_service_user_matches_file_ownership(self):
        """Verify chown commands in deploy.sh match expectations for critical dirs.

        Catches: Service runs as www-data but files owned by root (#108).
        """
        deploy_content = _read_deploy_sh()

        mismatches = []
        for dir_path, expected in self.CRITICAL_DIRS.items():
            # Find chown commands for this directory in deploy.sh
            # Pattern: chown [-R] owner:group /path
            chown_pattern = re.compile(
                rf"chown\s+(?:-R\s+)?(\S+?)[:\\](\S+?)\s+[\"']?{re.escape(dir_path)}[\"']?"
            )
            matches = chown_pattern.findall(deploy_content)

            if not matches:
                # Directory might be created without explicit chown
                continue

            for owner, group in matches:
                if owner != expected["owner"]:
                    mismatches.append(
                        f"{dir_path}: expected owner={expected['owner']}, "
                        f"found owner={owner}"
                    )
                if group != expected["group"]:
                    mismatches.append(
                        f"{dir_path}: expected group={expected['group']}, "
                        f"found group={group}"
                    )

        assert not mismatches, (
            f"Ownership mismatches in deploy.sh:\n"
            + "\n".join(f"  - {m}" for m in mismatches)
        )

    def test_deploy_chmod_sets_required_permissions(self):
        """Files owned by www-data must have at least 644 permissions.

        Catches: mkstemp creates 600, webapp needs 644 (#108).
        """
        deploy_content = _read_deploy_sh()

        # Find paths chowned to www-data
        www_paths = re.findall(
            r"chown\s+(?:-R\s+)?www-data[:\\]\S+\s+(\S+)",
            deploy_content,
        )

        problems = []
        for path in www_paths:
            path = path.strip("\"'")
            # Check if there's a chmod for this path
            has_chmod = bool(
                re.search(
                    rf"chmod\s+(?:-R\s+)?\S+\s+[\"']?{re.escape(path)}[\"']?",
                    deploy_content,
                )
            )
            if not has_chmod:
                problems.append(
                    f"{path}: chown to www-data without corresponding chmod"
                )

        assert not problems, (
            f"Missing chmod for www-data owned paths:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


# =============================================================================
# 4. Symlinks and Paths (P1)
# =============================================================================


class TestSymlinksAndPaths:
    """Verify symlink targets and hardcoded paths are consistent."""

    def test_symlink_targets_in_add_analyst(self):
        """All symlink targets in add-analyst must point to paths that deploy.sh creates.

        Catches: Script creates symlink to /data/X, but deploy.sh copies to /data/Y
        (#157, #158).
        """
        add_analyst = BIN_DIR / "add-analyst"
        if not add_analyst.is_file():
            pytest.skip("add-analyst not found")

        content = add_analyst.read_text()
        deploy_content = _read_deploy_sh()

        # Find all ln -sf TARGET patterns
        symlinks = re.findall(r"ln\s+-sf?\s+(/\S+)", content)

        missing = []
        for target in symlinks:
            # The target directory must be created by deploy.sh (mkdir -p)
            # or be a well-known path
            target_base = target.rstrip("/")
            # Check deploy.sh creates this or a parent
            found = False
            parts = Path(target_base).parts
            for i in range(len(parts), 0, -1):
                check_path = "/".join(parts[:i])
                if not check_path.startswith("/"):
                    check_path = "/" + check_path
                if check_path in deploy_content:
                    found = True
                    break
            if not found:
                missing.append(target)

        assert not missing, (
            f"Symlink targets in add-analyst not found in deploy.sh:\n"
            + "\n".join(f"  - {t}" for t in missing)
        )

    def test_deploy_copies_match_source_files(self):
        """Every file that deploy.sh copies from repo must exist.

        Catches: deploy.sh references files that were deleted or moved.
        """
        deploy_content = _read_deploy_sh()

        # Find cp commands copying from ${REPO_DIR}/ or repo-relative paths
        # Pattern: cp ... ${REPO_DIR}/path or "${REPO_DIR}/path"
        cp_sources = re.findall(
            r'cp\s+(?:-r\s+)?"?\$\{REPO_DIR\}/([^"}\s$]+)',
            deploy_content,
        )

        missing = []
        for rel_path in cp_sources:
            # Skip shell variable expansions (e.g., loop vars like ${script_file})
            if "${" in rel_path or "$" in rel_path:
                continue
            # Handle glob patterns (e.g., examples/notifications/*.py)
            if "*" in rel_path:
                # Check the directory exists
                dir_path = REPO_ROOT / rel_path.rsplit("/", 1)[0]
                if not dir_path.is_dir():
                    missing.append(rel_path)
            else:
                full_path = REPO_ROOT / rel_path
                if not full_path.exists():
                    missing.append(rel_path)

        assert not missing, (
            f"deploy.sh copies files that don't exist in repo:\n"
            + "\n".join(f"  - {p}" for p in missing)
        )


# =============================================================================
# 5. Documentation <-> Code (P2)
# =============================================================================


class TestDocumentation:
    """Verify documentation matches deployed services."""

    def test_server_md_documents_all_services(self):
        """All *.service files should be mentioned in dev_docs/server.md.

        Catches: New service without documentation.
        """
        server_md = REPO_ROOT / "dev_docs" / "server.md"
        if not server_md.is_file():
            pytest.skip("dev_docs/server.md not found")

        doc_content = server_md.read_text()
        services = _find_service_files()

        undocumented = []
        for svc in services:
            svc_name = svc.stem  # e.g., "notify-bot"
            if svc_name not in doc_content:
                undocumented.append(svc.name)

        assert not undocumented, (
            f"Services not documented in dev_docs/server.md:\n"
            + "\n".join(f"  - {s}" for s in undocumented)
        )


# =============================================================================
# 6. Shell Script Hygiene (P1)
# =============================================================================


class TestShellScriptHygiene:
    """Verify shell scripts follow safety best practices."""

    def test_shell_scripts_use_strict_mode(self):
        """All bash scripts must use set -euo pipefail (or equivalent).

        Exceptions:
        - Scripts that are simple exec wrappers (single exec command)
        - Scripts with only display/query operations (list-*, read-only)

        Catches: Scripts silently continuing on error, leading to partial deployments.
        """
        scripts = _find_shell_scripts()
        assert scripts, "Should find shell scripts"

        non_strict = []
        for script in scripts:
            content = script.read_text()

            # Skip trivial scripts: exec wrappers or read-only display scripts
            non_comment_lines = [
                l.strip() for l in content.splitlines()
                if l.strip() and not l.strip().startswith("#")
            ]
            if len(non_comment_lines) <= 2:
                continue

            # Skip read-only scripts that only display information (no side effects)
            # These are safe to run without strict mode.
            has_side_effects = any(
                re.search(pattern, content)
                for pattern in [
                    r"\buseradd\b", r"\buserdel\b", r"\busermod\b",
                    r"\bmkdir\b", r"\bcp\b", r"\bmv\b", r"\brm\b",
                    r"\bchmod\b", r"\bchown\b", r"\bln\b", r"\btee\b",
                    r"\bsystemctl\b",
                ]
            )
            if not has_side_effects:
                continue

            # Check for set -euo pipefail or individual set commands
            has_set_e = bool(re.search(r"set\s+-[a-z]*e", content))
            has_set_u = bool(re.search(r"set\s+-[a-z]*u", content))
            has_pipefail = "pipefail" in content

            if not has_set_e:
                non_strict.append(
                    f"{script.relative_to(REPO_ROOT)}: missing 'set -e'"
                )
            elif not has_set_u:
                non_strict.append(
                    f"{script.relative_to(REPO_ROOT)}: missing 'set -u'"
                )
            elif not has_pipefail:
                non_strict.append(
                    f"{script.relative_to(REPO_ROOT)}: missing 'pipefail'"
                )

        assert not non_strict, (
            f"Shell scripts without strict mode:\n"
            + "\n".join(f"  - {s}" for s in non_strict)
        )

    def test_shell_scripts_have_shebang(self):
        """All scripts in server/bin/ must start with a shebang line."""
        problems = []
        for script in sorted(BIN_DIR.glob("*")):
            if not script.is_file():
                continue
            first_line = script.read_text(errors="replace").split("\n", 1)[0]
            if not first_line.startswith("#!"):
                problems.append(str(script.relative_to(REPO_ROOT)))

        assert not problems, (
            f"Scripts without shebang line:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


# =============================================================================
# 7. Python Modules & Dependencies (P1)
# =============================================================================


class TestPythonDependencies:
    """Verify Python source files are importable and dependencies are declared."""

    def test_all_src_modules_have_valid_syntax(self):
        """All .py files in src/ and webapp/ must have valid Python syntax.

        Uses py_compile to check syntax without executing module-level code,
        avoiding side effects like DB connections or API calls.

        Catches: Syntax errors, missing parentheses, indentation issues.
        """
        problems = []
        for directory in [SRC_DIR, WEBAPP_DIR]:
            if not directory.is_dir():
                continue
            for py_file in sorted(directory.rglob("*.py")):
                try:
                    result = subprocess.run(
                        [sys.executable, "-m", "py_compile", str(py_file)],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if result.returncode != 0:
                        problems.append(f"{py_file.relative_to(REPO_ROOT)}: {result.stderr.strip()}")
                except subprocess.TimeoutExpired:
                    problems.append(f"{py_file.relative_to(REPO_ROOT)}: compilation timed out")

        assert not problems, (
            f"Python files with syntax errors:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    def test_requirements_txt_exists_and_nonempty(self):
        """requirements.txt must exist and contain at least one package."""
        req_file = REPO_ROOT / "requirements.txt"
        assert req_file.is_file(), "requirements.txt not found"

        content = req_file.read_text().strip()
        packages = [
            line for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert packages, "requirements.txt is empty (no packages declared)"

    def test_no_duplicate_requirements(self):
        """requirements.txt should not have duplicate package declarations."""
        req_file = REPO_ROOT / "requirements.txt"
        if not req_file.is_file():
            pytest.skip("requirements.txt not found")

        content = req_file.read_text()
        seen = {}
        duplicates = []
        for line_num, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Extract package name (before >=, ==, etc.)
            pkg_name = re.split(r"[><=!~\[]", stripped)[0].strip().lower()
            if pkg_name in seen:
                duplicates.append(
                    f"{pkg_name} (lines {seen[pkg_name]} and {line_num})"
                )
            else:
                seen[pkg_name] = line_num

        assert not duplicates, (
            f"Duplicate packages in requirements.txt:\n"
            + "\n".join(f"  - {d}" for d in duplicates)
        )
