"""Plain-language store errors on the /skills builder (AGT-1).

`app/api/store.py` rejects a duplicate name with a *bare string* detail
(``HTTPException(status_code=409, detail="conflict_owner_name")``) — a pinned
contract, see ``tests/test_store_api.py``. The sibling authoring surfaces
(`store_upload.html`, `store_edit.html`) map those machine codes to sentences;
`skills.html` was the one template that did not, so the builder showed the raw
`conflict_owner_name` token to the user.

These tests lock the mapping in place and keep the wording aligned across the
three templates — the same rejection must not grow three different phrasings.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _skills_page(seeded_app) -> str:
    resp = seeded_app["client"].get("/skills", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200, resp.text
    return resp.text


class TestSkillsBuilderErrorMessages:
    def test_conflict_owner_name_has_a_plain_language_message(self, seeded_app):
        text = _skills_page(seeded_app)
        assert "ERROR_MESSAGES" in text, "skills.html needs the shared code→sentence map"
        # The code is the map KEY; a human sentence must sit next to it.
        assert "conflict_owner_name:" in text or "conflict_owner_name'" in text
        assert "already have" in text.lower()

    def test_covers_the_same_codes_as_the_sibling_templates(self, seeded_app):
        """Same rejection, same vocabulary across the three authoring surfaces."""
        text = _skills_page(seeded_app)
        for code in (
            "conflict_owner_name",
            "conflict_global_suffix",
            "invalid_name_format",
            "title_required",
            "title_too_long",
            "tagline_too_long",
        ):
            assert code in text, f"{code} has no plain-language mapping in skills.html"

    def test_bare_string_details_are_humanized_not_echoed(self, seeded_app):
        """`errMessage` must consult the map before falling back to the raw token.

        The 409 arrives as a bare string, which used to hit the
        `typeof detail === 'string'` branch and get returned verbatim.
        """
        text = _skills_page(seeded_app)
        assert "humanizeError" in text
        # The raw-echo shortcut must be gone: a string detail is looked up first.
        assert "if (typeof detail === 'string') return detail;" not in text
