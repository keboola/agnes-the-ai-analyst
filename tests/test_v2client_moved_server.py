"""The redirect guard must cover BOTH CLI HTTP clients, not one of them.

#1225 taught `cli/client.py` to stop on a 3xx and name the new address,
because a deployment that changes hostname leaves the old name answering
`308` and the bare status told the user nothing.

But the CLI has a second HTTP client. `cli/v2_client.py` calls the
module-level `httpx.get` / `httpx.post` / … directly, so it never sees
`get_client()`'s event hooks — and it only raises on `>= 400`, so a 3xx
falls straight through to `r.json()` on a redirect's empty body. Ten
command modules route through it (`catalog`, `describe`, `search`,
`collections`, `store`, `my_stack`, `marketplace`, …), and what they
actually print today is:

    Error: internal CLI error (JSONDecodeError).

Measured against the real old hostname, on current `main`. That is worse
than the bare `HTTP 308:` #1225 set out to fix: it names neither the
status, nor the destination, nor a remedy.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import cli.server_moved as server_moved
import cli.v2_client as v2
from cli.v2_client import V2ClientError

ROOT = Path(__file__).resolve().parents[1]


def _redirect(status: int = 308, location: str = "https://new.example/api/catalog") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"Location": location},
        request=httpx.Request("GET", "https://old.example/api/catalog"),
    )


class TestV2ClientStopsOnRedirect:
    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_redirect_raises_instead_of_json_decoding_an_empty_body(self, status):
        with patch("cli.v2_client.get_server_url", return_value="https://old.example"):
            with patch("cli.v2_client.httpx.get", return_value=_redirect(status)):
                with pytest.raises(V2ClientError) as exc:
                    v2.api_get_json("/api/catalog")
        assert exc.value.status_code == status

    def test_message_names_the_destination_and_the_remedy(self):
        with patch("cli.v2_client.get_server_url", return_value="https://old.example"):
            with patch("cli.v2_client.httpx.get", return_value=_redirect()):
                with pytest.raises(V2ClientError) as exc:
                    v2.api_get_json("/api/catalog")
        rendered = str(exc.value)
        assert "https://new.example" in rendered, "does not name where the server moved"
        assert "AGNES_SERVER" in rendered, "does not say how to re-point"

    @pytest.mark.parametrize(
        ("fn", "target", "args"),
        [
            ("api_get_json", "get", ("/api/catalog",)),
            ("api_post_json", "post", ("/api/x", {})),
            ("api_delete", "delete", ("/api/x",)),
            ("api_put_json", "put", ("/api/x", {})),
            ("api_patch_json", "patch", ("/api/x", {})),
        ],
    )
    def test_every_verb_is_covered(self, fn, target, args):
        """One guarded verb is not a guarded client."""
        with patch("cli.v2_client.get_server_url", return_value="https://old.example"):
            with patch(f"cli.v2_client.httpx.{target}", return_value=_redirect()):
                with pytest.raises(V2ClientError):
                    getattr(v2, fn)(*args)

    def test_streaming_download_is_covered_too(self):
        """`api_get_stream` guards inside a `with httpx.stream(...)` block.

        Its check is indented differently from the others, which is exactly
        how a mechanical sweep of the plain verbs left it behind — and it is
        the helper `agnes pull`-style bundle downloads go through, so a
        redirect there writes a redirect body to disk as if it were content.
        """

        class _FakeStream:
            def __init__(self, response):
                self._response = response

            def __enter__(self):
                return self._response

            def __exit__(self, *exc):
                return False

        resp = _redirect()
        resp.iter_bytes = lambda: iter([b""])  # type: ignore[method-assign]

        with patch("cli.v2_client.get_server_url", return_value="https://old.example"):
            with patch("cli.v2_client.httpx.stream", return_value=_FakeStream(resp)):
                with pytest.raises(V2ClientError) as exc:
                    v2.api_get_stream("/api/bundle.zip", "/dev/null")
        assert exc.value.status_code == 308
        assert "https://new.example" in str(exc.value)

    def test_every_httpx_call_site_has_a_guard(self):
        """Derive the call sites from the source instead of listing verbs.

        The first version of this file enumerated five verbs by hand and so
        missed `api_get_stream`. Counting the real call sites is what makes
        a newly added helper fail this test instead of shipping unguarded.
        """
        import ast

        tree = ast.parse((ROOT / "cli" / "v2_client.py").read_text(encoding="utf-8"))

        def _calls(fn, pattern):
            return any(isinstance(n, ast.Call) and pattern(n) for n in ast.walk(fn))

        def _hits_httpx(node):
            f = node.func
            return isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "httpx"

        def _is_guard(node):
            f = node.func
            return isinstance(f, ast.Name) and f.id in {"_raise_for_status", "is_redirect"}

        unguarded = [
            fn.name
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef)
            and fn.name != "_raise_for_status"
            and _calls(fn, _hits_httpx)
            and not _calls(fn, _is_guard)
        ]
        assert not unguarded, f"helpers reach httpx without a redirect guard: {unguarded}"

    def test_success_is_untouched(self):
        ok = httpx.Response(
            200,
            json={"tables": []},
            request=httpx.Request("GET", "https://x/api/catalog"),
        )
        with patch("cli.v2_client.get_server_url", return_value="https://x"):
            with patch("cli.v2_client.httpx.get", return_value=ok):
                assert v2.api_get_json("/api/catalog") == {"tables": []}

    def test_ordinary_errors_still_render_normally(self):
        err = httpx.Response(
            403,
            json={"detail": "Not authorized"},
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "https://x/api/catalog"),
        )
        with patch("cli.v2_client.get_server_url", return_value="https://x"):
            with patch("cli.v2_client.httpx.get", return_value=err):
                with pytest.raises(V2ClientError) as exc:
                    v2.api_get_json("/api/catalog")
        assert exc.value.status_code == 403
        assert "Not authorized" in str(exc.value)


class TestTheRemedyStaysCopyPasteable:
    """The whole point is a command the user can paste. It must survive rendering.

    `cli.error_render` wraps `hint` at 80 columns, so handing it the entire
    explanation reflowed the block and split the new hostname mid-token:

        or set `server: https://agnes-analytics-
        platform.example.com` in your agnes config.yaml

    A hint that cannot be copied is not a remedy. Devin Review on #1266.
    """

    LONG = "https://agnes-analytics-platform.example.com"

    def _rendered(self) -> str:
        from cli.error_render import render_error
        from cli.server_moved import redirect_body

        resp = httpx.Response(
            308,
            headers={"Location": f"{self.LONG}/api/catalog"},
            request=httpx.Request("GET", "https://old-hostname.example.com/api/catalog"),
        )
        return render_error(308, redirect_body(resp, "https://old-hostname.example.com"))

    def test_the_env_var_command_survives_whole(self):
        """The COMMAND, not just the URL — a paste that stops at the hostname
        runs `AGNES_SERVER=...` with nothing after it."""
        lines = self._rendered().splitlines()
        assert any(f"AGNES_SERVER={self.LONG} agnes" in line for line in lines), (
            "the one-off command is broken across lines"
        )

    def test_the_config_remedy_survives_whole(self):
        """The occurrence that actually broke: `server: <url>` in config.yaml."""
        lines = self._rendered().splitlines()
        assert any(f"server: {self.LONG}" in line for line in lines), (
            "the config value is split across lines — it cannot be copied"
        )

    def test_no_line_ends_mid_hostname(self):
        """Catch the split wherever it lands, not only at the two known spots.

        An earlier version of this test counted lines containing the full URL
        and passed on occurrences that were not remedies at all.
        """
        host = self.LONG.removeprefix("https://")
        for line in self._rendered().splitlines():
            stripped = line.rstrip()
            assert not (
                stripped.endswith("-") and host.startswith(stripped.rsplit("/", 1)[-1].rstrip("-"))
            ), f"hostname split at a hyphen: {line!r}"


class TestBothClientsShareOneGuard:
    """A second client is the reason this bug existed; pin the pair."""

    def test_neither_client_open_codes_its_own_redirect_set(self):
        shared = (ROOT / "cli" / "server_moved.py").read_text(encoding="utf-8")
        assert "308" in shared, "the shared module does not define the redirect set"

        for name in ("cli/client.py", "cli/v2_client.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            assert "server_moved" in src, f"{name} does not use the shared redirect guard"

    def test_direct_httpx_call_sites_outside_the_clients_are_guarded_too(self):
        """The guard used to scan `cli/*client*.py` only, and the CLI has
        module-level `httpx` calls elsewhere — the setup-token exchange in
        `cli/commands/init.py` talks to the configured Agnes server directly.
        A moved server answers 3xx there as well. (Devin Review on #1266.)"""
        for path in (ROOT / "cli" / "commands").glob("*.py"):
            src = path.read_text(encoding="utf-8")
            # `httpx.Client(...)` counts too — `agnes auth login` verifies
            # through one, and a client without `follow_redirects` hands the
            # 3xx straight back. (Devin Review on #1266.)
            calls = re.findall(r"(?:^|[^.\w])(?:_?httpx)\.(get|post|put|delete|Client)\(", src)
            if not calls:
                continue
            if "AGNES_SERVER" not in src and "server_url" not in src:
                continue  # not talking to the Agnes server (e.g. a Keboola stack)
            assert "server_moved" in src, (
                f"{path.name} calls the Agnes server with httpx directly but does not consult "
                "the shared redirect diagnosis"
            )

    def test_both_clients_reference_the_shared_helper(self):
        """If a third client appears, this is the check that should be widened."""
        clients = [p for p in (ROOT / "cli").glob("*client*.py")]
        assert {p.name for p in clients} >= {"client.py", "v2_client.py"}
        for path in clients:
            src = path.read_text(encoding="utf-8")
            if not re.search(r"httpx\.(get|post|Client)", src):
                continue
            assert "server_moved" in src, (
                f"{path.name} makes httpx calls but does not consult the shared redirect guard"
            )


class TestTheMessageNamesTheFileToEdit:
    """Devin Review on #1266: "your agnes config.yaml" — which one?

    The pre-refactor message printed the resolved path. Losing it makes the
    remedy a search task, and the path is not guessable: `AGNES_CONFIG_DIR`
    moves it, which is exactly what a second instance on one machine does.
    """

    def test_the_resolved_path_appears_in_the_stderr_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "agnes-cfg"))
        msg = server_moved.moved_server_message(
            308, "https://new.example.com/api/v1/agents", "https://old.example.com"
        )
        assert str(tmp_path / "agnes-cfg" / "config.yaml") in msg

    def test_the_resolved_path_appears_in_the_typed_body(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "agnes-cfg"))
        body = server_moved.redirect_body(
            httpx.Response(
                308,
                headers={"Location": "https://new.example.com/api/v1/agents"},
                request=httpx.Request("GET", "https://old.example.com/api/v1/agents"),
            ),
            "https://old.example.com",
        )
        assert str(tmp_path / "agnes-cfg" / "config.yaml") in body["detail"]["hint"]

    def test_naming_the_path_does_not_create_the_directory(self, monkeypatch, tmp_path):
        """A message must not write to disk — `cli.config._config_dir` mkdirs."""
        target = tmp_path / "never-created"
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(target))
        server_moved.config_file_path()
        assert not target.exists()

    def test_it_points_where_the_config_is_actually_read_from(self, monkeypatch, tmp_path):
        """The duplicated resolution must not drift from `cli.config`."""
        from cli import config as cli_config

        monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "agnes-cfg"))
        assert server_moved.config_file_path() == str(cli_config._config_dir() / "config.yaml")


class TestTheCodeNamesWhatHappened:
    """Devin Review on #1266: a same-origin redirect is not a move.

    A proxy or an in-app redirect answering 3xx has no new address to point
    at, and `code: "server_moved"` sent the reader looking for a hostname
    change that never happened — under a hint that said nothing moved.
    """

    def _body(self, location: str, configured: str = "https://agnes.example.com") -> dict:
        return server_moved.redirect_body(
            httpx.Response(
                308,
                headers={"Location": location},
                request=httpx.Request("GET", f"{configured}/api/v1/agents"),
            ),
            configured,
        )["detail"]

    def test_a_cross_host_move_is_still_server_moved(self):
        detail = self._body("https://new.example.com/api/v1/agents")
        assert detail["code"] == "server_moved"
        assert detail["moved_to"] == "https://new.example.com"

    def test_a_same_origin_redirect_is_not(self):
        detail = self._body("https://agnes.example.com/api/v1/agents/")
        assert detail["code"] == "unexpected_redirect", detail
        assert "moved_to" not in detail

    def test_a_relative_redirect_is_not_either(self):
        detail = self._body("/api/v1/agents/")
        assert detail["code"] == "unexpected_redirect", detail


class TestTheSetupExchangeNamesAFixThatWorks:
    """Devin Review on #1266: `agnes init` reads neither `AGNES_SERVER` nor
    `config.yaml` — it takes the address as an argument, so the generic
    remedy pointed the reader at two things this command ignores."""

    def test_the_fix_is_the_init_flag(self):
        page = (ROOT / "cli" / "commands" / "init.py").read_text(encoding="utf-8")
        assert 'f"agnes init --server-url {moved_to} …"' in page
        assert "AGNES_SERVER=" not in page.split("is_redirect(exchange_resp.status_code)")[1][:1200]

    def test_a_same_origin_redirect_is_not_reported_as_a_move(self):
        page = (ROOT / "cli" / "commands" / "init.py").read_text(encoding="utf-8")
        block = page.split("is_redirect(exchange_resp.status_code)")[1][:1200]
        assert '"unexpected_redirect"' in block


class TestATlsUpgradeIsAMove:
    """Devin Review on #1266: `http://host` → `https://host` keeps the netloc.

    It is the commonest redirect there is — a server that got TLS — and it is
    cross-origin as far as httpx is concerned, so credentials are stripped on
    that hop too. Classifying it as "not a move" left the user with no remedy
    for the one case they can fix in a second.
    """

    def test_the_scheme_change_is_reported_with_the_new_base(self):
        assert (
            server_moved.redirect_target("https://agnes.example.com/api/v1/agents", "http://agnes.example.com")
            == "https://agnes.example.com"
        )

    def test_the_message_offers_the_https_address(self):
        msg = server_moved.moved_server_message(
            308, "https://agnes.example.com/api/v1/agents", "http://agnes.example.com"
        )
        assert "AGNES_SERVER=https://agnes.example.com" in msg

    def test_a_same_scheme_same_host_redirect_is_still_not_a_move(self):
        assert (
            server_moved.redirect_target("http://agnes.example.com/api/v1/agents/", "http://agnes.example.com")
            == ""
        )


def test_a_tls_downgrade_is_not_a_move():
    """Devin Review on #1266: `https` → `http` on the same host is a
    misconfigured proxy, not a relocation. Printing "point your CLI at
    http://…" would talk someone out of TLS on the strength of a redirect
    anyone on the path can forge."""
    assert (
        server_moved.redirect_target("http://agnes.example.com/api/v1/agents", "https://agnes.example.com") == ""
    )
    msg = server_moved.moved_server_message(
        308, "http://agnes.example.com/api/v1/agents", "https://agnes.example.com"
    )
    assert "AGNES_SERVER=http://" not in msg

    # …and a downgrade that ALSO changes host is still a downgrade: handing
    # over a new plaintext address is the thing not to do. (Devin Review.)
    assert server_moved.redirect_target("http://new.example.com/api", "https://old.example.com") == ""
