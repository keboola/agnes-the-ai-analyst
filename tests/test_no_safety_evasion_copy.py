"""Shipped copy must never read as advice for routing around an agent's safety checks.

Agnes is operated almost entirely through an AI agent, so our own prose is
input to that agent. Copy that frames a legitimate practice as *getting past
a detector* is read for what it says, not for what it meant — and a hardened
agent that stops on it is behaving correctly.

This is a repeat: 0.76.4 removed exactly this framing from the web install
prompt (`app/web/setup_instructions.py`, guarded by
`tests/test_setup_instructions.py::test_init_step_has_no_security_judgment_suppression`),
but the same sentences survived in `agnes init --help`, where an analyst ran
into them during onboarding — the agent flagged the `--token-file` help text
as "deliberately written to get an AI agent to route a secret around its own
safety detection" and refused to pick a flag. The mechanism was never the
problem: keeping a PAT out of argv is the repo's own security rule. Only the
justification was written backwards.

So the guard is deliberately surface-agnostic rather than another per-file
assertion — a third copy of the same sentence is the failure mode this file
exists to catch.

Out of scope: `src/_bundled_seed/`, a snapshot synced from the infra-template
repo (`scripts/sync_bundled_seed.sh`). Editing it here is reverted by the next
sync and trips the provenance CI guard, so its copy has to be fixed upstream.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Surfaces whose text this repo authors directly and an agent actually reads:
# the CLI (help strings + the comments beside them), the web install prompt,
# and the workspace template shipped into every analyst's Claude Code session.
_SCAN_ROOTS = (
    "cli",
    "app/web/setup_instructions.py",
    "app/initial_workspace_default",
)

_SCAN_SUFFIXES = {".py", ".md", ".txt", ".json", ".sh", ".tmpl", ".j2", ".html"}

_EVASION_VERB = (
    r"dodg\w*|evad\w*|bypass\w*|sidestep\w*|circumvent\w*|slip past|sneak\w*|get around|work around|avoid\w*"
)
# Deliberately narrow: the *machinery* an agent uses to police itself. Broader
# nouns ("guard", "check", "permission prompt") describe things we legitimately
# pre-approve through sanctioned channels — allow-rules and `autoMode.environment`
# trust, both of which `agnes init` writes on purpose.
_SAFETY_NOUN = r"classifier|detector|safety check|security judgment|security judgement|security protocol"

# The window spans a clause, not a line: the sentence this guard was written
# for put "auto-classifier" and "dodge" ~85 chars apart. Bounded by `.` so a
# match stays inside one sentence.
_EVASION_RE = re.compile(
    rf"(?:{_EVASION_VERB})[^.]{{0,120}}?(?:{_SAFETY_NOUN})|(?:{_SAFETY_NOUN})[^.]{{0,120}}?(?:{_EVASION_VERB})",
    re.IGNORECASE,
)

# Comment markers and the quotes around a wrapped string literal, which sit
# between two halves of one sentence in source but not in what a reader sees.
_PROSE_NOISE_RE = re.compile(r"""^\s*(?:#+|//|\*)\s?|["'`]+""")


def _flatten(text: str) -> tuple[str, list[int]]:
    """Join a file into one prose stream, keeping a char → line-number map.

    Help strings and comments wrap across source lines, so a line-by-line scan
    reads "…trips the auto-classifier" and "…to dodge that" as two unrelated
    lines and matches neither — which is how two of the three sentences that
    prompted this guard would have slipped past it.
    """
    chunks: list[str] = []
    line_of: list[int] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        cleaned = _PROSE_NOISE_RE.sub(" ", line).strip()
        if not cleaned:
            continue
        if chunks:
            chunks.append(" ")
            line_of.append(lineno)
        chunks.append(cleaned)
        line_of.extend([lineno] * len(cleaned))
    return "".join(chunks), line_of


def _scan_shipped_text() -> list[str]:
    offenders: list[str] = []
    for root in _SCAN_ROOTS:
        path = _REPO_ROOT / root
        files = [path] if path.is_file() else sorted(f for f in path.rglob("*") if f.is_file())
        for f in files:
            if f.suffix not in _SCAN_SUFFIXES:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            flat, line_of = _flatten(text)
            for match in _EVASION_RE.finditer(flat):
                lineno = line_of[match.start()]
                offenders.append(f"{f.relative_to(_REPO_ROOT)}:{lineno}: …{match.group(0).strip()}…")
    return offenders


def test_shipped_copy_never_frames_a_practice_as_evading_a_safety_check():
    """No shipped sentence may justify a practice by what detection it avoids.

    State the real reason instead. "Keeps the secret out of argv, where shell
    history and the process table expose it" is the same instruction with a
    justification that survives being read by the agent it instructs.
    """
    offenders = _scan_shipped_text()
    assert not offenders, "Copy framed as evading an agent's safety machinery:\n" + "\n".join(offenders)


def test_init_help_justifies_token_file_by_the_real_exposure():
    """`agnes init --help` must name argv exposure, not a classifier.

    The positive half of the guard above: a future rewrite must not resolve
    the ban by deleting the rationale, leaving an analyst no reason to prefer
    `--token-file` at all.
    """
    from cli.commands.init import init_app

    result = CliRunner().invoke(init_app, ["--help"])
    assert result.exit_code == 0
    help_text = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    # Typer wraps help text, so match on words rather than whole phrases.
    assert "history" in help_text, help_text
    assert "process" in help_text, help_text
    assert "argv" in help_text, help_text
