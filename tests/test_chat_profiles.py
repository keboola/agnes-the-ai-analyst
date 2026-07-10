import pytest

from app.chat.profiles import ChatProfile, get_profile

ALL_SLUGS = [
    "data-package-builder",
    "mcp-connect",
    "marketplace-author",
    "corporate-memory",
    "skill-author",
]


def test_known_profile_resolves():
    p = get_profile("data-package-builder")
    assert isinstance(p, ChatProfile)
    assert p.slug == "data-package-builder"
    assert "data package" in p.claude_md.lower()
    assert p.skill_name and p.skill_body
    # persona must steer the agent at the existing admin endpoints
    assert "/api/admin/data-packages" in p.skill_body


def test_skill_author_profile_registered():
    p = get_profile("skill-author")
    assert p is not None
    assert p.skill_name == "agnes-skill-authoring"
    assert "use when" in p.claude_md.lower()  # trigger-quality rule is in the persona
    assert p.skill_body.startswith("---\n")
    assert "/api/store/entities/from-markdown" in p.skill_body


@pytest.mark.parametrize("slug", ALL_SLUGS)
def test_every_profile_is_well_formed(slug):
    p = get_profile(slug)
    assert isinstance(p, ChatProfile)
    assert p.slug == slug
    assert p.claude_md.strip()
    # skill body must be a valid SKILL.md (frontmatter with name + description)
    assert p.skill_body.startswith("---\n")
    assert f"name: {p.skill_name}\n" in p.skill_body
    assert "description:" in p.skill_body
    # persona references the right admin/store endpoint family
    assert "/api/" in p.skill_body


def test_unknown_profile_returns_none():
    assert get_profile("does-not-exist") is None
