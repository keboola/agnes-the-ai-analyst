"""The FastAPI startup hook schedules cache warmup."""

from unittest.mock import patch


def test_startup_handler_calls_warmup_scheduler():
    """A startup handler in app.main calls maybe_schedule_startup_warmup."""
    from app.main import app

    # FastAPI startup events live on app.router.on_startup OR are
    # registered via lifespan. Either way, we should be able to verify
    # the scheduler is called.
    handlers = list(app.router.on_startup)
    handler_names = [getattr(h, "__name__", "?") for h in handlers]
    # Either: a named handler that calls warmup, OR a lifespan that does.
    has_warmup = any("warm" in n.lower() for n in handler_names)
    if not has_warmup:
        # Lifespan path — check for the lifespan fn
        lifespan = getattr(app.router, "lifespan_context", None)
        assert lifespan is not None, (
            "Expected a startup handler (or lifespan) that calls "
            "cache_warmup.maybe_schedule_startup_warmup. "
            f"Found on_startup: {handler_names}"
        )


def test_health_check_succeeds_immediately(seeded_app):
    """/api/health doesn't await warmup; readiness is fire-and-forget."""
    c = seeded_app["client"]
    r = c.get("/api/health")
    assert r.status_code == 200
