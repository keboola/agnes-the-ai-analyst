"""Web surfaces for the skill linter (v89, Task 7):

* ``/admin/store/lint`` admin curator page (server-rendered findings + audit)
* owner-visible findings on ``/marketplace/flea/{id}/edit``
* the skill-author chat profile advertising the dry-run/lint step

Reuses the ``web_client`` + ``_make_admin`` fixtures from
``test_store_lint_api`` (fresh app per test, no LLM key → degraded lint).
"""

from __future__ import annotations

from tests.test_store_api import _create_user
from tests.test_store_lint_api import _make_admin, _publish, web_client  # noqa: F401


def _seed_finding(entity_id: str, *, message: str, rule_id: str = "SL002", content_hash: str = "h1") -> str:
    from src.repositories import store_lint_repo

    repo = store_lint_repo()
    run_id = repo.start_run("admin")
    repo.replace_findings(
        entity_id,
        run_id,
        [
            {
                "rule_id": rule_id,
                "severity": "warn",
                "message": message,
                "evidence": {},
                "doc_url": f"/docs/skill-guidelines#{rule_id.lower()}",
            }
        ],
        content_hash,
    )
    repo.finish_run(run_id, linted=1, skipped=0, findings=1)
    return content_hash


class TestAdminLintPage:
    def test_admin_sees_findings_and_audit_button(self, web_client):  # noqa: F811
        _, cookies = _create_user(web_client, "alice@x.com")
        entity_id = _publish(web_client, cookies).json()["id"]
        _seed_finding(entity_id, message="This skill body is unusually large.")

        _, admin_cookies = _make_admin(web_client, "admin@x.com")
        resp = web_client.get("/admin/store/lint", cookies=admin_cookies, headers={"Accept": "text/html"})
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "Audit now" in body
        assert "This skill body is unusually large." in body
        # base_ds chrome actually rendered (the _chrome_ctx regression guard):
        # the shared app header/nav and a real stylesheet href must be present.
        assert "app-header" in body
        assert "app-nav-link" in body
        assert ".css" in body

    def test_dismissed_finding_hidden_by_default(self, web_client):  # noqa: F811
        _, cookies = _create_user(web_client, "bob@x.com")
        entity_id = _publish(web_client, cookies).json()["id"]
        h = _seed_finding(entity_id, message="Dismiss-me finding.", rule_id="SL011")

        from src.repositories import store_lint_repo

        store_lint_repo().dismiss(entity_id, "SL011", "admin@x.com", h)

        _, admin_cookies = _make_admin(web_client, "admin@x.com")
        default_view = web_client.get("/admin/store/lint", cookies=admin_cookies)
        assert "Dismiss-me finding." not in default_view.text
        shown = web_client.get("/admin/store/lint?include_dismissed=true", cookies=admin_cookies)
        assert "Dismiss-me finding." in shown.text

    def test_non_admin_blocked(self, web_client):  # noqa: F811
        _, cookies = _create_user(web_client, "carol@x.com")
        r = web_client.get("/admin/store/lint", cookies=cookies)
        assert r.status_code == 403


class TestOwnerFindings:
    def test_owner_sees_findings_on_edit_page(self, web_client):  # noqa: F811
        _, cookies = _create_user(web_client, "dave@x.com")
        entity_id = _publish(web_client, cookies).json()["id"]
        _seed_finding(entity_id, message="Owner-visible advisory message.", rule_id="SL010")

        resp = web_client.get(
            f"/marketplace/flea/{entity_id}/edit",
            cookies=cookies,
            headers={"Accept": "text/html"},
        )
        assert resp.status_code == 200, resp.text
        assert "Owner-visible advisory message." in resp.text
        assert "/docs/skill-guidelines#sl010" in resp.text


class TestProfileMentionsLint:
    def test_skill_author_profile_advises_dry_run(self):
        from app.chat.profiles import get_profile

        prof = get_profile("skill-author")
        assert prof is not None
        assert "dry-run" in prof.claude_md.lower()
        assert "/docs/skill-guidelines" in prof.claude_md
