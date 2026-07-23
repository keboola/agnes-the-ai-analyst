"""Key-gated real-e2b smoke test (issue #872, the "did a real call still work"
layer on top of the zero-network contract tests in test_e2b_sdk_contract.py).

Skipped unless ``E2B_API_KEY`` is set, so it is a no-op on per-PR CI and runs
only where the secret is present (nightly / pre-release). It exercises the
**real** authenticated e2b round trip that the admin diagnostic uses —
``AsyncSandbox.list(...)`` → ``await paginator.next_items()`` via
``app.chat.readiness.test_e2b_key`` — which is where the SDK contract drift in
#870 actually surfaced (and which no mock can catch).

Run it explicitly with the secret exported:

    E2B_API_KEY=... uv run pytest -m e2b tests/test_e2b_smoke.py

A deeper tier — actually spawning a sandbox via ``AsyncSandbox.create(...,
network={...})`` and killing it — would additionally exercise the egress/network
contract (#872 instance 1); it is intentionally left out here to keep the smoke
cheap and non-flaky. Add it behind this same marker when a disposable template
+ budget are available.
"""

import asyncio
import os

import pytest

pytestmark = pytest.mark.e2b

_HAS_KEY = bool(os.environ.get("E2B_API_KEY", "").strip())


@pytest.mark.skipif(not _HAS_KEY, reason="E2B_API_KEY not set — key-gated smoke")
def test_e2b_key_probe_authenticates_against_real_sdk():
    """`readiness.test_e2b_key` must succeed against the live SDK + a real key.

    This drives the exact `list() -> await next_items()` path — the one the
    unit mocks can't validate — so an SDK bump that changes that contract fails
    here before it reaches production."""
    # Local import: `test_e2b_key` is a `test_`-prefixed name; importing it at
    # module scope would make pytest collect the readiness function itself.
    from app.chat import readiness

    result = asyncio.run(readiness.test_e2b_key())
    assert result.get("ok") is True, f"e2b key probe failed: {result.get('detail')!r}"
