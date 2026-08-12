"""The `data_source.type` vocabulary must read the same everywhere.

Three places name the allowed values and they had drifted apart:

- `app/api/admin.py` server-config schema → `keboola | bigquery | local | csv`
- `docs/DATA_SOURCES.md`                  → `keboola, bigquery, csv`
- `config/instance.yaml.example`          → `keboola | bigquery | local`

`csv` is an alias for `local` and there is no CSV connector — nothing under
`connectors/` handles it. The docs offered the alias and never mentioned the
canonical name, so an admin following them picked `csv` expecting a
Keboola-style pull and configured an instance with no external source at all.
That is what a first-day admin actually did on a fresh instance.
"""

from __future__ import annotations

import re
from pathlib import Path

CANONICAL = ("keboola", "bigquery", "local")
ALIAS = "csv"


def _server_config_options() -> list[str]:
    """The `options` list the admin server-config schema advertises."""
    src = Path("app/api/admin.py").read_text(encoding="utf-8")
    m = re.search(r'"options":\s*\[([^\]]*)\],\s*\n\s*"default":\s*"local"', src)
    assert m, "data_source.type options list not found in app/api/admin.py"
    return re.findall(r'"([a-z_]+)"', m.group(1))


def test_canonical_values_are_offered_by_the_admin_schema():
    options = _server_config_options()
    for value in CANONICAL:
        assert value in options, f"{value!r} missing from the admin server-config options"


def test_no_csv_connector_exists():
    """If someone ever adds one, this test should fail and the docs' claim
    that `csv` is merely an alias has to be revisited."""
    assert not (Path("connectors") / ALIAS).exists(), (
        "a csv connector now exists — docs/DATA_SOURCES.md still calls `csv` "
        "an alias for `local`; reconcile them"
    )


def test_docs_name_the_canonical_values_not_just_the_alias():
    doc = Path("docs/DATA_SOURCES.md").read_text(encoding="utf-8")
    header = doc.split("## Query Modes")[0]
    # Match the value TABLE rows specifically, not any prose mention — `local`
    # also appears in the sentence explaining the alias, which would let this
    # pass while the table itself lost the row.
    rows = set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", header, re.MULTILINE))
    for value in CANONICAL:
        assert value in rows, (
            f"docs/DATA_SOURCES.md has no table row for `{value}` — a reader "
            "cannot pick a value the documentation does not list"
        )
    assert "alias" in header.lower(), (
        "the docs must say `csv` is an alias for `local`, or a reader picking "
        "it expects a connector that does not exist"
    )


def test_config_example_matches_the_docs():
    example = Path("config/instance.yaml.example").read_text(encoding="utf-8")
    m = re.search(r'type:\s*"keboola"\s*#\s*(.+)', example)
    assert m, "data_source.type comment not found in config/instance.yaml.example"
    listed = re.findall(r"[a-z_]+", m.group(1))
    for value in CANONICAL:
        assert value in listed, (
            f"config/instance.yaml.example does not list {value!r} — the three "
            "places that name this vocabulary must agree"
        )


def test_the_first_time_setup_guide_offers_the_canonical_value():
    """Devin Review on #1263: `CLAUDE.md` still told an agent to ask for
    `csv` — the value this PR's own docs explain has no connector behind it.
    An agent following the setup guide would configure an instance with no
    external source while the operator believed they had picked one."""
    from pathlib import Path

    guide = (Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    line = next(ln for ln in guide.splitlines() if "Data source type" in ln)
    # The canonical value is what the agent should ASK for; the alias may be
    # mentioned, but only where the text says what it means.
    offered = line.split("—", 1)[1].split("(", 1)[0]
    assert "`local`" in offered, line
    assert "`csv`" not in offered, line
