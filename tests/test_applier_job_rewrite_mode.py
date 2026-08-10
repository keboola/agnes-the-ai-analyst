"""H2-NEW — the applier's python heredocs that rewrite job JSON leave the
file 0600, and never leave it observable at anything else.

The job JSON carries ``target_url`` and error messages that can quote a
database url, so it must not land world-readable. The original fix chmodded
the destination *after* ``os.replace``, which closes the leak but leaves a
window: between the rename and the chmod the real path exists at the process
umask (0644 under the standard cloud-init umask), and anything watching that
path can read it. Chmodding the TEMP file before the rename removes the window
entirely — ``os.replace`` is atomic and carries the temp's mode — so that is
what this now pins.

The same ordering is asserted across every writer of ``instance.yaml`` in
``tests/test_startup_instance_yaml_perms.py``; this file stays focused on the
two job-file writers, which is where the H2 finding originally landed.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_job_rewrites_chmod_the_temp_before_the_rename() -> None:
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    lines = script.splitlines()
    replace_sites = [(i, ln) for i, ln in enumerate(lines, start=1) if "os.replace(" in ln]
    assert len(replace_sites) >= 2, (
        "expected at least two os.replace() sites inside applier heredocs "
        f"(H8 expiry + update_job); script may have been restructured. Found: {replace_sites}"
    )

    misses = []
    for lineno, _src in replace_sites:
        # Look BEHIND the rename, not ahead of it.
        window = "\n".join(lines[max(0, lineno - 7) : lineno - 1])
        if not re.search(r"os\.chmod\(\s*tmp\s*,[^)]*0o600", window):
            misses.append(lineno)

    assert not misses, (
        f"These os.replace sites have no os.chmod(tmp, 0o600) before them: lines {misses}\n"
        "Chmodding the destination afterwards still leaves the real path readable "
        "at the umask default for the window between the two calls."
    )


def test_no_writer_chmods_the_destination_after_the_rename() -> None:
    """The superseded shape, pinned so it cannot come back.

    ``os.chmod(p, ...)`` right after ``os.replace(tmp, p)`` is the pattern this
    file used to require. It is not wrong so much as late, and having both
    shapes in one script is how the window survives a future edit.
    """
    script = Path("scripts/ops/agnes-state-applier.sh").read_text()
    lines = script.splitlines()
    offenders = [
        i
        for i, ln in enumerate(lines, start=1)
        if "os.replace(" in ln and re.search(r"os\.chmod\(\s*(?!tmp\b)\w+\s*,[^)]*0o600", "\n".join(lines[i : i + 3]))
    ]
    assert not offenders, (
        f"chmod of the destination after the rename at lines {offenders} — chmod the temp before os.replace instead"
    )
