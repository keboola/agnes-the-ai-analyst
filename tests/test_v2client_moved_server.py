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


class TestBothClientsShareOneGuard:
    """A second client is the reason this bug existed; pin the pair."""

    def test_neither_client_open_codes_its_own_redirect_set(self):
        shared = (ROOT / "cli" / "server_moved.py").read_text(encoding="utf-8")
        assert "308" in shared, "the shared module does not define the redirect set"

        for name in ("cli/client.py", "cli/v2_client.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            assert "server_moved" in src, f"{name} does not use the shared redirect guard"

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
