"""Contract tests against the *installed* e2b SDK (issue #872).

Agnes' chat sandbox has broken in production three times on e2b SDK contract
changes, each invisible to the suite because the unit tests mock the e2b symbols
with a shape that no longer matches reality — the mocks stay green while the real
call raises. These tests assert directly against the installed `e2b` package
(no API key, no network, runs in normal CI) so a future SDK bump that changes a
signature or return shape fails here at `pip install` time instead of silently in
production.

Each assertion is pinned to a concrete past regression:

- `AsyncSandbox.list` becoming/again-being a coroutine (instance 2, #870): the
  admin "test E2B connection" probe does `AsyncSandbox.list(...)` then
  `await paginator.next_items()`. When `list` was awaited as a coroutine it
  raised `TypeError: object AsyncSandboxPaginator can't be used in 'await'`.
- `AsyncSandbox.create` dropping the `network` kwarg / `ALL_TRAFFIC` moving
  (instance 1, egress P0): the provider passes
  `network={"allow_out": ..., "deny_out": [ALL_TRAFFIC]}`.

These are the exact call shapes in `app/chat/readiness.py` and
`app/chat/e2b_provider.py`; keep them in sync if those callsites change.
"""

import inspect

import pytest


def test_asyncsandbox_list_is_a_lazy_paginator_factory_not_a_coroutine():
    """`readiness.test_e2b_key` relies on `AsyncSandbox.list(...)` being a
    synchronous factory returning a paginator whose `next_items()` is awaited.
    If the SDK makes `list` a coroutine again, awaiting the factory (or, here,
    treating it as a paginator) breaks — catch that drift."""
    from e2b import AsyncSandbox

    assert not inspect.iscoroutinefunction(AsyncSandbox.list), (
        "e2b AsyncSandbox.list is a coroutine again — readiness.test_e2b_key "
        "calls it as a sync factory then awaits paginator.next_items(); "
        "awaiting the factory raises TypeError (regression #870)."
    )

    # The factory must not require a network round trip to construct — the auth
    # happens on `await next_items()`. A dummy key is enough to build it.
    paginator = AsyncSandbox.list(api_key="contract-test-key-no-network")
    for attr in ("next_items", "has_next", "next_token"):
        assert hasattr(paginator, attr), (
            f"e2b list() paginator lost `{attr}` — readiness.test_e2b_key "
            f"depends on this shape (regression #870). Got {type(paginator).__name__}."
        )
    assert inspect.iscoroutinefunction(paginator.next_items), (
        "paginator.next_items is no longer awaitable — readiness.test_e2b_key "
        "does `await paginator.next_items()`."
    )


def test_asyncsandbox_create_is_async_and_accepts_network_kwarg():
    """`e2b_provider._spawn` does
    `await AsyncSandbox.create(..., network={"allow_out": ..., "deny_out": [ALL_TRAFFIC]})`.
    Assert `create` is awaitable and still accepts a `network` kwarg (dropped/renamed
    → every spawn 400s, as in the egress P0)."""
    from e2b import AsyncSandbox

    assert inspect.iscoroutinefunction(AsyncSandbox.create), (
        "e2b AsyncSandbox.create is no longer a coroutine — e2b_provider awaits it."
    )
    params = inspect.signature(AsyncSandbox.create).parameters
    assert "network" in params, (
        "e2b AsyncSandbox.create no longer accepts `network` — e2b_provider "
        "passes network={'allow_out': ..., 'deny_out': [ALL_TRAFFIC]} for egress "
        "control (regression: egress P0). Accepted params: "
        f"{list(params)}"
    )


def test_all_traffic_sentinel_is_importable_and_stringy():
    """The provider imports `ALL_TRAFFIC` from `e2b` and puts it in
    `deny_out`. If the symbol is removed/renamed, the import fails at module
    load; assert it stays a plain string CIDR the network dict can carry."""
    from e2b import ALL_TRAFFIC

    assert isinstance(ALL_TRAFFIC, str) and ALL_TRAFFIC, (
        f"e2b ALL_TRAFFIC changed shape ({ALL_TRAFFIC!r}); e2b_provider puts it "
        "verbatim into network['deny_out']."
    )


@pytest.mark.parametrize("symbol", ["create", "connect", "kill", "list"])
def test_asyncsandbox_keeps_the_methods_the_provider_calls(symbol):
    """`e2b_provider` / `readiness` call `AsyncSandbox.{create,connect,kill,list}`.
    A rename would only surface at runtime today — pin the surface here."""
    from e2b import AsyncSandbox

    assert hasattr(AsyncSandbox, symbol), (
        f"e2b AsyncSandbox lost `{symbol}` — app/chat calls it directly."
    )
