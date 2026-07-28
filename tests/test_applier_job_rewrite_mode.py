"""H2-NEW — the applier's python heredocs that rewrite job JSON
preserve mode 0600 after os.replace."""
from __future__ import annotations

from pathlib import Path
import re


def test_update_job_heredoc_chmods_after_replace() -> None:
    """Each ``os.replace(tmp, p)`` in the applier script is followed by
    ``os.chmod(p, 0o600)``. Pre-H2 fix the tmp was created with the
    process umask (0644 under the standard cloud-init umask) and survived
    the rename → job JSON containing target_url became world-readable.
    """
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    # Find every os.replace call (Python form, inside the bash heredocs).
    replace_sites = [
        (i, line) for i, line in enumerate(script.splitlines(), start=1)
        if "os.replace(" in line
    ]
    assert len(replace_sites) >= 2, (
        "expected at least two os.replace() sites inside applier "
        "heredocs (H8 expiry + update_job); script may have been "
        f"restructured. Found: {replace_sites}"
    )
    # For each site, the next ~3 lines must include os.chmod(..., 0o600).
    lines = script.splitlines()
    misses = []
    for lineno, _src in replace_sites:
        window = "\n".join(lines[lineno - 1 : lineno + 5])
        if not re.search(r"os\.chmod\([^)]+0o600", window):
            misses.append(lineno)
    assert not misses, (
        "These os.replace sites are missing an os.chmod(..., 0o600) "
        f"follow-up: lines {misses}\n"
        "Without the chmod, the tmp file's umask-0644 mode survives "
        "the rename and the rewritten job JSON becomes world-readable."
    )
