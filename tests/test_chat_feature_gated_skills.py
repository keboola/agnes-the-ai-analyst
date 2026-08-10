"""A skill for a feature this instance does not have is worse than no skill.

Observed on a live instance with data apps switched off: asked for a chart,
the agent loaded ``agnes-data-apps-extras``, called ``data_apps_list``, and got
back ``404 data_apps_disabled``. Two costs, and the second is the one that
matters — a wasted round trip, and a skill that had already pointed the agent
at *building a hosted dashboard* for what was a one-off plot. The workspace
tree shipped every skill it had and consulted nothing about the instance it was
being built for.

Pruning happens after the template copy rather than by filtering the copy,
because the workspace is converged repeatedly: an operator who turns the
feature off later has to see the skill leave, and one who turns it back on has
to see it return.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.chat.workdir import _FEATURE_GATED_SKILLS, _prune_disabled_feature_skills


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    skills = tmp_path / ".claude" / "skills"
    (skills / "agnes-data-apps-extras").mkdir(parents=True)
    (skills / "agnes-data-apps-extras" / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    # An ungated skill, to prove the prune is surgical rather than a wipe.
    (skills / "some-other-skill").mkdir(parents=True)
    (skills / "some-other-skill" / "SKILL.md").write_text("---\nname: y\n---\n", encoding="utf-8")
    return tmp_path


def test_the_data_apps_skill_goes_when_the_feature_is_off(workspace, monkeypatch):
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "false")
    _prune_disabled_feature_skills(workspace)
    skills = workspace / ".claude" / "skills"
    assert not (skills / "agnes-data-apps-extras").exists()
    assert (skills / "some-other-skill").exists(), "only the gated skill may be pruned"


def test_the_skill_stays_when_the_feature_is_on(workspace, monkeypatch):
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "true")
    _prune_disabled_feature_skills(workspace)
    assert (workspace / ".claude" / "skills" / "agnes-data-apps-extras").exists()


def test_pruning_is_idempotent_and_reversible(workspace, monkeypatch):
    """Convergence runs repeatedly. Off twice must not error; back on must
    restore — the bundled tree is the source, this only ever subtracts."""
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "false")
    _prune_disabled_feature_skills(workspace)
    _prune_disabled_feature_skills(workspace)  # no raise on an already-pruned tree
    skills = workspace / ".claude" / "skills"
    assert not (skills / "agnes-data-apps-extras").exists()
    # The next template copy puts it back; flipping the flag must then keep it.
    (skills / "agnes-data-apps-extras").mkdir()
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "true")
    _prune_disabled_feature_skills(workspace)
    assert (skills / "agnes-data-apps-extras").exists()


def test_a_workspace_without_skills_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "false")
    _prune_disabled_feature_skills(tmp_path)  # must not raise


def test_every_gated_skill_is_actually_bundled():
    """A typo in the registry key would silently gate nothing at all — the
    prune would look like it ran and the skill would ship anyway."""
    bundled = Path("app/initial_workspace_default/.claude/skills")
    for skill_name in _FEATURE_GATED_SKILLS:
        assert (bundled / skill_name).is_dir(), (
            f"{skill_name} is registered as feature-gated but is not a bundled skill — the gate would be a no-op"
        )
