"""Guard: every literal `result="…"` written to the audit log must classify
into a known result class (src/audit_helpers.classify_result). Prevents the
vocabulary drift that made 35% of rows unreachable by the Result filter
('ok' vs 'success', etc. — 2026-07-28 consistency spec)."""

import re
import subprocess
from pathlib import Path

from src.audit_helpers import classify_result

REPO_ROOT = Path(__file__).resolve().parents[1]

# Literals that intentionally classify as 'other' (deliberate outcomes that
# are neither success nor error nor denial). Extend consciously.
# "attempted": written BEFORE the action it describes, so the outcome is not
# knowable yet. Used by `agnes admin metrics import --prune`, where the record
# has to survive an interruption between the audit write and the delete — a
# row claiming success it cannot vouch for would be worse than a vague one.
ALLOWED_OTHER = {"skipped", "attempted"}

_RESULT_RE = re.compile(r'result="([a-z_.0-9]+)"')


def _tracked_py_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "app", "src", "services", "cli", "connectors"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    ).stdout.splitlines()
    return [REPO_ROOT / f for f in out if f.endswith(".py")]


def test_result_literals_classify_into_known_classes():
    offenders: list[str] = []
    for f in _tracked_py_files():
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in _RESULT_RE.finditer(text):
            literal = m.group(1)
            cls = classify_result(literal)
            if cls == "other" and literal not in ALLOWED_OTHER:
                line = text[: m.start()].count("\n") + 1
                offenders.append(f'{f.relative_to(REPO_ROOT)}:{line} result="{literal}"')
    assert not offenders, (
        "audit result literals outside the classification vocabulary "
        "(add to src/audit_helpers.py classes or ALLOWED_OTHER consciously):\n" + "\n".join(offenders)
    )


def test_ok_literal_is_retired():
    """Writers standardize on 'success'; 'ok' remains readable via the
    result_class CASE but must not be written anymore."""
    offenders = []
    for f in _tracked_py_files():
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'result="ok"', text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{f.relative_to(REPO_ROOT)}:{line}")
    assert not offenders, 'retire result="ok" (write "success"):\n' + "\n".join(offenders)
