"""H8-NEW — __rollback + failed-migration branch preserve SOURCE_URL on
instance.yaml revert."""
from __future__ import annotations

from pathlib import Path


def test_rollback_call_passes_source_url() -> None:
    """The __rollback function must call write_instance_yaml with TWO
    arguments — backend AND url. Pre-fix, the call dropped the URL
    via the missing second arg, leaving cloud-source migrations with
    an unusable backend=cloud + no url overlay on the failure path.
    """
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    # Locate the __rollback function body.
    lines = script.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("__rollback()"):
            start = i
            break
    assert start is not None, "__rollback function not found"
    # The body is short — scan the next 30 lines for the
    # write_instance_yaml call.
    body = "\n".join(lines[start : start + 40])
    # H8-NEW: the call must take backend AND url (or be visibly
    # explicit that url='' / cleared on purpose). We assert the
    # SOURCE_URL variable appears in the rollback call line.
    assert "write_instance_yaml" in body, body
    rollback_line = next(
        l for l in body.splitlines()
        if "write_instance_yaml" in l and "$SOURCE_BACKEND" in l
    )
    # Accept both $SOURCE_URL and the safer ${SOURCE_URL:-} default-expansion
    # form — set -euo pipefail would abort on an unset variable without the
    # default, so ${SOURCE_URL:-} is the correct idiom when the trap can fire
    # before the variable is populated.
    assert "SOURCE_URL" in rollback_line, (
        "H8-NEW: __rollback must pass SOURCE_URL as the 2nd arg to "
        "write_instance_yaml; otherwise cloud-source rollback wipes "
        f"the url. Current line:\n  {rollback_line}"
    )


def test_every_source_backend_revert_passes_source_url() -> None:
    """H8-NEW (generalized) — EVERY `write_instance_yaml "$SOURCE_BACKEND"`
    call in the script must also pass SOURCE_URL as the 2nd arg.

    There are two such call sites:
      1. __rollback (ERR-trap path, recovers from heredoc crash mid-migration)
      2. The `else` branch on FINAL_STATUS != "success" (migrator reported
         non-success — orderly post-migrator rollback)

    Both reach the same B4-class outage class without the URL: a
    cloud → side_car failure rewinds instance.yaml to backend=cloud but
    drops the url, and the next app boot crashes with "Postgres URL unset".

    This generalized assertion is forward-compatible: any future
    write_instance_yaml call that reverts to SOURCE_BACKEND will be
    forced to also pass SOURCE_URL.
    """
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    # Find every line that calls write_instance_yaml with $SOURCE_BACKEND.
    # Strip comments — `#` style comments may legitimately mention the
    # earlier signature inside a docstring.
    offending: list[str] = []
    revert_lines: list[str] = []
    for raw in script.splitlines():
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue
        if "write_instance_yaml" not in raw or "$SOURCE_BACKEND" not in raw:
            continue
        revert_lines.append(raw)
        if "SOURCE_URL" not in raw:
            offending.append(raw)
    # Sanity: we expect at least two such revert sites in the post-fix script.
    assert len(revert_lines) >= 2, (
        "expected >=2 `write_instance_yaml \"$SOURCE_BACKEND\"` revert sites; "
        f"found {len(revert_lines)}:\n  " + "\n  ".join(revert_lines)
    )
    assert not offending, (
        "H8-NEW: every `write_instance_yaml \"$SOURCE_BACKEND\"` call must "
        "also pass SOURCE_URL; without the URL, cloud-source rollback wipes "
        "the url and the next boot crashes with 'Postgres URL unset'. "
        "Offending lines:\n  " + "\n  ".join(offending)
    )
