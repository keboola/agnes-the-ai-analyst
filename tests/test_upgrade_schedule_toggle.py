"""Static contract for the per-instance auto-upgrade cadence override.

Pins the three-part infra contract (same pattern as
test_startup_studio_toggle.py) so a rename or dropped template argument
can't silently break the override:

* variables.tf declares upgrade_schedule on BOTH prod_instance and
  dev_instances, defaulting to the historical "*/5 * * * *" so no caller
  is affected unless they explicitly override;
* main.tf forwards each.value.upgrade_schedule into templatefile(...);
* startup-script.sh.tpl's cron install line is built from the templated
  value, not a hardcoded "*/5 * * * *" string.
"""

import re
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")


def test_variables_tf_declares_upgrade_schedule_on_both_instance_types():
    body = (MODULE / "variables.tf").read_text()
    pattern = re.compile(r'upgrade_schedule\s*=\s*optional\(string,\s*"\*/5 \* \* \* \*"\)')
    occurrences = pattern.findall(body)
    assert len(occurrences) == 2, (
        f'expected upgrade_schedule = optional(string, "*/5 * * * *") on '
        f"BOTH prod_instance and dev_instances object types, found "
        f"{len(occurrences)} occurrence(s)"
    )


def test_main_tf_forwards_upgrade_schedule():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r"upgrade_schedule\s*=\s*each\.value\.upgrade_schedule", body), (
        "main.tf must forward each.value.upgrade_schedule into the startup-script templatefile() call"
    )


def test_tpl_cron_line_uses_templated_schedule_not_hardcoded():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert 'UPGRADE_SCHEDULE="${upgrade_schedule}"' in body, (
        "startup-script.sh.tpl must capture the templated upgrade_schedule "
        "value, matching the existing UPGRADE_MODE pattern"
    )
    assert 'CRON_LINE="$UPGRADE_SCHEDULE /usr/local/bin/agnes-auto-upgrade.sh' in body, (
        "the installed crontab line must be built from $UPGRADE_SCHEDULE, not a literal cadence string"
    )
    assert '"*/5 * * * * /usr/local/bin/agnes-auto-upgrade.sh' not in body, (
        "no hardcoded */5 cron line may remain once the variable is wired in"
    )
