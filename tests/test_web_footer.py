"""Shared page footer (`_footer.html`) contract.

The footer used to render `© {year} {config.INSTANCE_COPYRIGHT or 'AI Harness'}`
in six hand-copied blocks, and `INSTANCE_COPYRIGHT` was hardcoded `""` in
`_config_proxy` — so the documented `instance.copyright` key was inert and
every instance showed the fallback literal, a string no operator chose.

What this suite pins:

1. **The resolver is wired** — `instance.copyright` / `AGNES_INSTANCE_COPYRIGHT`
   actually reaches the template, and empty stays empty (no fallback name).
2. **No invented attribution** — unset credit means the "Deployed by" line is
   absent, not filled with the deployment or product name.
3. **One partial, every chrome** — no page hand-rolls the old copyright line
   again, and the per-chrome footer classes survive the move.
4. **Build provenance moved into the footer** — the fixed bottom-right chip is
   gone from the app chromes and stays only on pre-auth `base_login.html`.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.instance_config import get_instance_copyright

TEMPLATES = Path("app/web/templates")

# Every chrome that renders a footer. Each must go through the partial.
FOOTER_CHROMES = [
    "base.html",
    "base_ds.html",
    "base_index.html",
    "dashboard.html",
    "chat.html",
    "install.html",
]


@pytest.fixture
def no_yaml_config(monkeypatch):
    """Pin the "operator configured nothing" case.

    Tests that assert an *absent* credit have to isolate the YAML layer, not
    just the env var: `get_value` reads the repo's own `config/instance.yaml`
    when that file passes strict validation, so a developer whose local file
    is filled in — and sets `instance.copyright` — would watch these fail
    while CI (which has no instance.yaml at all) passed them. Empty the
    deep-merge cache that `load_instance_config` returns early from, which is
    the state CI runs in anyway."""
    import app.instance_config as ic

    monkeypatch.setattr(ic, "_instance_config", {})
    yield


@pytest.fixture(scope="module")
def web_client(tmp_path_factory):
    """Module-scoped: `create_app()` is expensive enough that one app per test
    trips the 60s per-test timeout under `-n auto` on a cold interpreter.

    Sharing it is safe for what this suite asserts — `_config_proxy()` is
    rebuilt per request precisely so an operator can flip env vars without a
    restart, so the function-scoped `monkeypatch.setenv` calls below still take
    effect on the next request against this same app."""
    tmp_path = tmp_path_factory.mktemp("footer")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATA_DIR", str(tmp_path))
        mp.setenv("TESTING", "1")
        mp.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
        (tmp_path / "state").mkdir()
        (tmp_path / "analytics").mkdir()
        (tmp_path / "extracts").mkdir()
        from src.db import close_system_db

        close_system_db()
        from app.main import create_app

        app = create_app()
        yield TestClient(app)
        close_system_db()


@pytest.fixture(scope="module")
def admin_cookie(web_client):
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin

    password = "AdminPass1!"
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin1",
        email="admin@test.com",
        name="Admin",
        password_hash=PasswordHasher().hash(password),
    )
    grant_admin(conn, "admin1")
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "admin@test.com", "password": password})
    assert resp.status_code == 200, f"Bootstrap failed: {resp.text}"
    return {"access_token": resp.json()["access_token"]}


class TestCopyrightResolver:
    def test_defaults_to_empty_not_a_product_name(self, monkeypatch, no_yaml_config):
        """Empty is the vendor-neutral default — the footer omits the credit
        rather than naming anyone."""
        monkeypatch.delenv("AGNES_INSTANCE_COPYRIGHT", raising=False)
        assert get_instance_copyright() == ""

    def test_yaml_value_reaches_the_resolver(self, monkeypatch):
        """The layer that was broken: a value in `instance.copyright` must
        actually be read. Env unset so the YAML path is what answers."""
        import app.instance_config as ic

        monkeypatch.delenv("AGNES_INSTANCE_COPYRIGHT", raising=False)
        monkeypatch.setattr(ic, "_instance_config", {"instance": {"copyright": "Acme Corp"}})
        assert get_instance_copyright() == "Acme Corp"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_COPYRIGHT", "Acme Corp")
        assert get_instance_copyright() == "Acme Corp"

    def test_whitespace_only_is_empty(self, monkeypatch):
        """A stray-whitespace value must not render "Deployed by  "."""
        monkeypatch.setenv("AGNES_INSTANCE_COPYRIGHT", "   ")
        assert get_instance_copyright() == ""

    def test_registered_in_the_config_surface(self):
        """The knob must appear in /admin/server-config's inventory, or an
        operator can't discover why their footer is bare."""
        from app.api.config_surface import _KNOB_CATALOGUE

        entry = next(k for k in _KNOB_CATALOGUE if k["key"] == "instance_copyright")
        assert entry["resolver"] == "get_instance_copyright"
        assert entry["yaml_path"] == "instance.copyright"
        assert entry["env_var"] == "AGNES_INSTANCE_COPYRIGHT"


class TestRenderedFooter:
    def test_credit_reaches_the_page(self, web_client, admin_cookie, monkeypatch):
        """The regression this whole change exists for: a configured credit
        used to be dropped on the floor by the hardcoded proxy attribute."""
        monkeypatch.setenv("AGNES_INSTANCE_COPYRIGHT", "Acme Corp")
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "Deployed by Acme Corp" in resp.text

    def test_unset_credit_invents_nothing(self, web_client, admin_cookie, monkeypatch, no_yaml_config):
        monkeypatch.delenv("AGNES_INSTANCE_COPYRIGHT", raising=False)
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "Deployed by" not in resp.text
        # The old fallback line must not come back in any form.
        assert "&copy;" not in resp.text
        assert "© 20" not in resp.text

    def test_product_brand_always_renders(self, web_client, admin_cookie, monkeypatch):
        """The left side names the product and is never empty — it's the only
        provenance an operator has on an authed page now that the fixed build
        chip is gone."""
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Foundry AI")
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="site-footer__brand">Foundry AI<' in resp.text

    def test_build_slot_present_for_the_version_fetch(self, web_client, admin_cookie):
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "data-agnes-build" in resp.text
        assert "/api/version" in resp.text


class TestOnePartialEveryChrome:
    @pytest.mark.parametrize("template", FOOTER_CHROMES)
    def test_chrome_includes_the_partial(self, template):
        assert "_footer.html" in (TEMPLATES / template).read_text(), (
            f"{template} must render the footer through the shared partial"
        )

    @pytest.mark.parametrize("template", FOOTER_CHROMES)
    def test_chrome_does_not_hand_roll_the_old_line(self, template):
        body = (TEMPLATES / template).read_text()
        assert "INSTANCE_COPYRIGHT or" not in body, (
            f"{template} re-introduced the fallback-literal footer line; "
            "the partial owns the credit and omits it when unset"
        )

    def test_per_chrome_footer_classes_survived(self):
        """`footer_class` is how the chrome-specific CSS (`.footer`,
        `.cloud-chat-empty-foot`) stays on the element after the move."""
        assert "footer_class = 'footer'" in (TEMPLATES / "dashboard.html").read_text()
        assert "footer_class = 'footer'" in (TEMPLATES / "install.html").read_text()
        assert "footer_class = 'cloud-chat-empty-foot'" in (TEMPLATES / "chat.html").read_text()

    def test_partial_falls_back_when_config_is_absent(self):
        """Some context builders skip `config` entirely; a missing key resolves
        to an empty dict, so the brand needs `default(…, true)` (plain
        `default()` only fires on undefined) or the slot renders as `{}`."""
        body = (TEMPLATES / "_footer.html").read_text()
        assert "config.INSTANCE_BRAND | default('Agnes', true)" in body


class TestBuildChipRetired:
    """The build string lives in the footer now. The fixed z-index-9999 chip
    hovering over content on every page is gone from the app chromes — it
    survives only on pre-auth `base_login.html`, which has no footer."""

    @pytest.mark.parametrize("template", ["base.html", "base_ds.html"])
    def test_app_chromes_dropped_the_fixed_chip(self, template):
        # Match the include, not the name — both files still *mention* the
        # partial in the comment explaining where the build string went.
        assert not re.search(
            r"{%\s*include\s*['\"]_version_badge\.html['\"]",
            (TEMPLATES / template).read_text(),
        ), f"{template} re-added the fixed build chip; the footer carries the build now"

    def test_pre_auth_chrome_keeps_the_chip(self):
        assert re.search(
            r"{%\s*include\s*['\"]_version_badge\.html['\"]",
            (TEMPLATES / "base_login.html").read_text(),
        ), "pre-auth chrome has no footer, so the chip is the only build provenance there"

    def test_chip_not_rendered_on_an_authed_page(self, web_client, admin_cookie):
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "agnes-version-badge" not in resp.text
