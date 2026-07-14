"""Incident-coverage ratchet (Task 10 of the 2026-07-14
chat-sandbox-secret-broker plan, spec §7.4).

Maps each incident-closure / guarantee acceptance-criteria (AC) id from the
design spec to the real ``path::test_name`` node that satisfies it, and
asserts that node actually exists and is not disabled via a bare
``@pytest.mark.skip`` (the e2b-tier tests are legitimately
``@pytest.mark.skipif(not AGNES_E2E_E2B, ...)``-gated — that's a manual
operator gate, not a disabled test, so skipif is fine).

This is a ratchet, not a full spec sweep: if a future edit renames or
deletes one of these tests without updating ``REQUIRED``, this file fails
loudly instead of silently losing coverage for a closed incident.
"""

from __future__ import annotations

import ast
from pathlib import Path

# AC id -> "path::test_name" of the real test that satisfies it. Node names
# were confirmed against the committed test files (grep -n "def test_"), not
# copied from the plan's illustrative names.
REQUIRED: dict[str, str] = {
    # e2b-tier adversarial suite (Task 11) — manual AGNES_E2E_E2B gate.
    "AC-F-nosecret": "tests/e2e/test_adversarial.py::test_no_secret_anywhere",
    "AC-F3": "tests/e2e/test_adversarial.py::test_hook_disabled_egress_blocked",
    "AC-F4c": "tests/e2e/test_adversarial.py::test_non_bash_egress_blocked",
    "AC-F-allowed-sink": "tests/e2e/test_adversarial.py::test_no_exfil_via_allowlisted_host",
    # PreToolUse hook hardening (Task 3) — unit-tier, per-PR.
    "AC-G-schemeless": "tests/test_pre_tool_use_hook.py::test_schemeless_curl_denied",
    # Broker routes (Task 6) — app-tier, per-PR.
    "AC-G-ticket-reuse": "tests/test_broker_routes.py::test_expired_ticket_401",
    "AC-G-rbac-fidelity": "tests/test_broker_routes.py::test_agnes_api_replay_uses_live_rbac",
    # Manager spawn-env static guard (Task 9) — unit-tier, per-PR.
    "AC-G-noinject": "tests/test_backend_split_guard.py::test_no_real_secret_in_sandbox_spawn_env",
    # Route-auth guard (Task 10, this task) — app-tier, per-PR.
    "AC-G-route-auth": "tests/test_route_auth_guard.py::test_all_routes_authenticated",
}


def _find_test_function(path: Path, name: str) -> ast.FunctionDef | None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _decorator_dotted_name(dec: ast.expr) -> str:
    """Render a decorator expression's dotted call target, e.g. a
    ``@pytest.mark.skipif(...)`` decorator renders as ``pytest.mark.skipif``
    (args are ignored — only the callable identity matters here)."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _is_bare_skip(fn: ast.FunctionDef) -> bool:
    """True if decorated with an unconditional ``@pytest.mark.skip`` (as
    opposed to ``@pytest.mark.skipif``, which is a legitimate manual-gate
    marker used by the e2b-tier tests)."""
    return any(_decorator_dotted_name(dec) == "pytest.mark.skip" for dec in fn.decorator_list)


def test_every_required_ac_has_a_test():
    missing: list[str] = []
    skipped: list[str] = []
    for ac, node in REQUIRED.items():
        path_str, _, name = node.partition("::")
        path = Path(path_str)
        if not path.exists():
            missing.append(f"{ac}: no such file {path_str} (node: {node})")
            continue
        fn = _find_test_function(path, name)
        if fn is None:
            missing.append(f"{ac}: no such test function {node}")
            continue
        if _is_bare_skip(fn):
            skipped.append(f"{ac}: {node} is @pytest.mark.skip-decorated (unconditionally disabled)")

    assert not missing, "Incident-coverage gap(s) — required test node(s) not found:\n  " + "\n  ".join(missing)
    assert not skipped, "Incident-coverage gap(s) — required test node(s) unconditionally skipped:\n  " + "\n  ".join(
        skipped
    )
