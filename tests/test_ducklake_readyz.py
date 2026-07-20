"""Tests for the DuckLake ``/readyz`` check registration (wave-2G Task 5,
finding 4 — review carry-over).

Before this fix, ``app.main``'s lifespan registered the ``"ducklake"``
readiness check (``app.api.health_probes.register_readiness_check``) only
*after* the best-effort reader warm-up (``get_ducklake_read().close()``)
had already succeeded, inside the same ``try``/``except`` block. The
single most likely failure window — the Postgres catalog not yet
reachable at boot — hit that ``except`` branch, which swallowed the
warm-up failure AND skipped registration in the same step: a replica
whose DuckLake attach was actually broken reported ``/readyz`` as fully
ready forever (the check was simply never registered), so the load
balancer kept routing traffic to it.

The fix extracts registration into ``app.main._register_ducklake_readyz_check``,
called UNCONDITIONALLY (before the warm-up's own try/except) whenever the
analytics backend is ``ducklake`` — these tests exercise that function
directly, monkeypatching ``src.ducklake_session.get_ducklake_read`` to
simulate a catalog that is unreachable at boot (and then recovers).
"""

from __future__ import annotations


def _clear_ducklake_check():
    from app.api import health_probes

    health_probes._extra_checks.pop("ducklake", None)


def test_noop_on_legacy_backend(monkeypatch, tmp_path):
    """No ducklake backend active — nothing gets registered, so a legacy
    deployment's /readyz payload is unaffected."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AGNES_ANALYTICS_BACKEND", raising=False)

    import src.analytics_backend as ab

    ab.reset_analytics_backend_cache()
    _clear_ducklake_check()
    try:
        assert ab.analytics_backend() == "legacy"  # sanity: the case under test

        from app.main import _register_ducklake_readyz_check

        _register_ducklake_readyz_check()

        from app.api import health_probes

        assert "ducklake" not in health_probes._extra_checks
    finally:
        _clear_ducklake_check()
        ab.reset_analytics_backend_cache()


def test_catalog_unreachable_at_boot_still_registers_and_reports_not_ready(monkeypatch, tmp_path):
    """Core regression this finding fixes: even when ``get_ducklake_read()``
    raises at boot (simulating a Postgres catalog that isn't reachable
    yet), registration must still happen — the app "still boots" (the
    registration call itself never raises) — and the registered check
    must report ``False`` rather than crashing ``/readyz``. Once the
    catalog becomes reachable, the SAME registered check (no re-register
    needed) must flip to ``True`` on the next periodic poll."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")

    import src.analytics_backend as ab

    ab.reset_analytics_backend_cache()
    _clear_ducklake_check()
    try:

        def _boom():
            raise RuntimeError("catalog unreachable at boot (simulated)")

        monkeypatch.setattr("src.ducklake_session.get_ducklake_read", _boom)

        from app.main import _register_ducklake_readyz_check

        _register_ducklake_readyz_check()  # must not raise -- app still boots

        from app.api import health_probes

        assert "ducklake" in health_probes._extra_checks, (
            "the check must be registered even though get_ducklake_read() is currently broken"
        )
        check_fn = health_probes._extra_checks["ducklake"]
        assert check_fn() is False

        # Catalog recovers -- a later periodic /readyz poll must see it
        # without any re-registration.
        class _FakeCursor:
            def execute(self, *a, **kw):
                return self

            def fetchone(self):
                return (1,)

            def close(self):
                pass

        monkeypatch.setattr("src.ducklake_session.get_ducklake_read", lambda: _FakeCursor())
        assert check_fn() is True
    finally:
        _clear_ducklake_check()
        ab.reset_analytics_backend_cache()


def test_registration_happens_even_when_warmup_would_fail(monkeypatch, tmp_path):
    """Mirrors the actual lifespan ordering: registration is its own
    try/except, separate from (and before) the warm-up's try/except --
    proving the two are decoupled, i.e. a warm-up failure right after
    registration cannot un-register or otherwise disable the check."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")

    import src.analytics_backend as ab

    ab.reset_analytics_backend_cache()
    _clear_ducklake_check()
    try:

        def _boom():
            raise RuntimeError("catalog unreachable (simulated)")

        monkeypatch.setattr("src.ducklake_session.get_ducklake_read", _boom)

        from app.main import _register_ducklake_readyz_check

        # Step 1 (register, unconditional) -- must not raise.
        _register_ducklake_readyz_check()

        # Step 2 (best-effort warm-up, as the lifespan does it) -- fails,
        # but only inside its own try/except; the registration from step
        # 1 must survive regardless.
        try:
            from src.ducklake_session import get_ducklake_read

            get_ducklake_read().close()
        except Exception:
            pass

        from app.api import health_probes

        assert "ducklake" in health_probes._extra_checks
        assert health_probes._extra_checks["ducklake"]() is False
    finally:
        _clear_ducklake_check()
        ab.reset_analytics_backend_cache()
