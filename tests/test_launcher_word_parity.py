"""The install guide must name the launcher `agnes init` actually creates.

`home_not_onboarded.html` tells the analyst to type one word to open their
workspace. That word is written by `cli/lib/shortcut.py`, which strips the
workspace folder name to lowercase alphanumerics. The page derived it with
Jinja's `| lower` instead — equivalent only while the folder name is already
alphanumeric, which holds for the brand-derived default but not for an
explicit `AGNES_WORKSPACE_DIR_NAME` override (that value is returned
verbatim). An operator setting "My Team AI" would be told to type
`my team ai` while the shortcut on disk is `myteamai`.
"""

import pytest

from app.instance_config import get_workspace_launcher_word
from cli.lib.shortcut import _launcher_word


@pytest.mark.parametrize(
    "workspace_dir",
    [
        "AcmeIQ",  # brand-derived, already alphanumeric
        "TeamAI",
        "Agnes",
        "My Team AI",  # explicit override with spaces
        "Acme-Data",  # hyphen
        "Data.Analyst",  # dot
        "ACME_2026",  # underscore
        "Team (Prod)",  # parentheses
    ],
)
def test_page_names_the_shortcut_that_gets_installed(workspace_dir, monkeypatch):
    monkeypatch.setenv("AGNES_WORKSPACE_DIR_NAME", workspace_dir)

    assert get_workspace_launcher_word() == _launcher_word(workspace_dir)


def test_launcher_word_is_shell_safe(monkeypatch):
    """Whatever the folder is called, the word must be typeable as a command."""
    monkeypatch.setenv("AGNES_WORKSPACE_DIR_NAME", "My Team (Prod) v2.0")

    word = get_workspace_launcher_word()

    assert word.isalnum(), f"launcher word is not a bare command token: {word!r}"
    assert word == word.lower()


def test_folder_name_keeps_its_formatting(monkeypatch):
    """Sanitizing the *command* must not rewrite the folder the operator chose."""
    monkeypatch.setenv("AGNES_WORKSPACE_DIR_NAME", "My Team AI")

    from app.instance_config import get_workspace_dir_name

    assert get_workspace_dir_name() == "My Team AI"


def test_default_install_is_not_told_to_type_the_cli_name(monkeypatch):
    """The stock instance is the case this got wrong the hardest.

    Brand "Agnes" derives workspace folder `Agnes`, whose launcher would
    shadow the `agnes` CLI — so the CLI installs `agnesai` instead (#783).
    The page used to say `agnes`, which does exist but runs the data CLI and
    prints its help instead of opening the workspace: a wrong instruction
    that looks like it worked.
    """
    monkeypatch.delenv("AGNES_WORKSPACE_DIR_NAME", raising=False)
    monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Agnes")

    assert get_workspace_launcher_word() == "agnesai"
