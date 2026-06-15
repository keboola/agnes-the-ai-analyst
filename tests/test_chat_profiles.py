from app.chat.profiles import ChatProfile, get_profile


def test_known_profile_resolves():
    p = get_profile("data-package-builder")
    assert isinstance(p, ChatProfile)
    assert p.slug == "data-package-builder"
    assert "data package" in p.claude_md.lower()
    assert p.skill_name and p.skill_body
    # persona must steer the agent at the existing admin endpoints
    assert "/api/admin/data-packages" in p.skill_body


def test_unknown_profile_returns_none():
    assert get_profile("does-not-exist") is None
