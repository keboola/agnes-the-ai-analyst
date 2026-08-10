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

import pytest

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


def test_the_hook_admits_this_instance_own_host():
    """The case that was broken: the sandbox could not reach Agnes at all."""
    mod = _load_hook("https://agnes.example.com")
    assert "agnes.example.com" in mod.ALLOWED_HOSTS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://agnes.example.com", "agnes.example.com"),
        ("http://app:8000", "app"),
        ("app:8000", "app"),  # bare host:port — urlparse yields no hostname without a scheme
        ("https://agnes.example.com/sub/path", "agnes.example.com"),
    ],
)
def test_the_host_is_derived_from_every_spelling_the_env_uses(raw, expected):
    assert expected in _load_hook(raw).ALLOWED_HOSTS


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_an_absent_server_url_leaves_the_allowlist_untouched(raw):
    """Fail closed: no host is better than a wrong one, and the four baked
    entries are what a sandbox needs to function at all."""
    assert _load_hook(raw).ALLOWED_HOSTS == {
        "127.0.0.1",
        "localhost",
        "api.anthropic.com",
        "api.github.com",
    }


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
