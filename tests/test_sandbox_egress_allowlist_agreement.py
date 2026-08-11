"""The VM egress policy and the in-sandbox hook must allow the same hosts.

Two independent allowlists decide whether a sandbox may reach a host:

- ``app/chat/e2b_provider.py::_effective_allow_out`` — the VM-level policy,
  which derives the Agnes host from ``agnes_server_url()``;
- ``ALLOWED_HOSTS`` in the bundled ``.claude/hooks/pre_tool_use.py`` — checked
  first, inside the sandbox, before anything reaches the network.

The provider's docstring says its default "mirrors the bundled PreToolUse
hook's ALLOWED_HOSTS so the VM-level policy and the hook agree". It did not.
The provider added the Agnes host; the hook's set was four hard-coded entries
and could not contain it, because the host differs per deployment and that file
ships verbatim. The stricter of two layers wins, so every in-sandbox request to
Agnes was refused by the hook — on a network that would have allowed it.

Watched live on a running instance: asked to build a data app, the agent
obtained a git credential from the API and then failed to clone the app's repo
by name, by hostname and by IP, including one attempt with the sandbox bypass.
Authoring a data app from chat was impossible for that reason alone. What made
it hard to see is that the MCP tools kept working the whole time — they reach
the server through the local relay on 127.0.0.1, which was allowed.

So the agreement is now asserted rather than claimed in a comment.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path


HOOK = Path("app/initial_workspace_default/.claude/hooks/pre_tool_use.py")
PROVIDER = Path("app/chat/e2b_provider.py")


def _load_hook(agnes_server: str | None):
    """Import the bundled hook with a chosen AGNES_SERVER, as the sandbox does."""
    prev = os.environ.get("AGNES_SERVER")
    if agnes_server is None:
        os.environ.pop("AGNES_SERVER", None)
    else:
        os.environ["AGNES_SERVER"] = agnes_server
    try:
        spec = importlib.util.spec_from_file_location("bundled_hook", HOOK)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if prev is None:
            os.environ.pop("AGNES_SERVER", None)
        else:
            os.environ["AGNES_SERVER"] = prev


def test_the_two_allowlists_agree_on_the_baked_entries():
    """The provider's fallback list and the hook's baked set are written out
    separately in two files. Pin them equal — this is the drift that made an
    entire feature unusable, and neither file's tests could see the other."""
    provider_src = PROVIDER.read_text(encoding="utf-8")
    block = re.search(
        r"return \[host, (.*?)\]",
        provider_src,
        re.DOTALL,
    )
    assert block, "_effective_allow_out's fallback list moved — re-point this guard"
    provider_hosts = set(re.findall(r'"([^"]+)"', block.group(1)))
    hook_hosts = _load_hook(None).ALLOWED_HOSTS
    assert provider_hosts == hook_hosts, (
        f"VM policy allows {sorted(provider_hosts)} but the in-sandbox hook allows "
        f"{sorted(hook_hosts)} — the stricter one wins, silently"
    )


def test_the_provider_still_derives_the_agnes_host():
    """The other half of the agreement: if the provider ever stops adding the
    host, the hook admitting it would be a hole rather than a fix."""
    provider_src = PROVIDER.read_text(encoding="utf-8")
    assert "return [host, " in provider_src, (
        "_effective_allow_out no longer leads its fallback with the derived Agnes host"
    )


# ── userinfo in the URL ──────────────────────────────────────────────────────


def _decide(mod, cmd: str):
    d = mod._decide({"tool_name": "Bash", "tool_input": {"command": cmd}})
    out = d.get("hookSpecificOutput", d)
    return out.get("permissionDecision", "allow"), out.get("permissionDecisionReason") or ""


def test_basic_auth_credentials_are_not_read_as_the_hostname():
    """`split(":")[0]` over the whole authority read the basic-auth USERNAME as
    the host. Every data-app clone URL is `http://agnes:<jwt>@<host>/...`, so
    the hook decided on `agnes` — refusing a request to a host it never
    actually looked at, and reporting "Outbound network to 'agnes'" to a
    reader who had no such host anywhere.

    The relay on 127.0.0.1 is the in-sandbox case that matters: it IS
    allowlisted, and a credentialed URL pointed at it was refused anyway.
    """
    mod = _load_hook(None)
    decision, reason = _decide(mod, "git clone http://agnes:JWT@127.0.0.1:34025/data-apps.git/x")
    assert decision == "allow", reason


def test_an_allowed_name_in_the_userinfo_does_not_smuggle_a_denied_host():
    """Allow side, and the reason this is a security fix rather than a
    convenience one: `http://api.github.com:pw@evil.example/` extracted
    `api.github.com` and was ALLOWED while the request went to `evil.example`.
    Anyone who could get a command run in the sandbox could reach any host by
    putting an allowlisted name in the userinfo."""
    mod = _load_hook("https://agnes.example.com")
    decision, reason = _decide(mod, "curl http://api.github.com:pw@evil.example/x")
    assert decision == "deny"
    assert "evil.example" in reason, f"the real host must be named in the refusal: {reason!r}"


def test_ordinary_urls_still_decide_the_same_way():
    mod = _load_hook("https://agnes.example.com")
    assert _decide(mod, "curl https://api.github.com/repos")[0] == "allow"
    assert _decide(mod, "curl https://evil.example/x")[0] == "deny"
