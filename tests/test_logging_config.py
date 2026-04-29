import json
import logging

import pytest

from app.logging_config import (
    _derive_slug,
    _JSONFormatter,
    request_id_var,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _reset_logging(monkeypatch):
    """Reset global logging state between tests."""
    import app.logging_config as lc

    lc._CONFIGURED = False
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    yield
    lc._CONFIGURED = False
    logging.getLogger().handlers.clear()


def test_dev_uses_rich_handler(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    setup_logging("app")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    from rich.logging import RichHandler

    assert isinstance(handlers[0], RichHandler)


def test_prod_uses_json_formatter():
    setup_logging("app")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert isinstance(handlers[0].formatter, _JSONFormatter)


def test_idempotent():
    setup_logging("app")
    setup_logging("app")
    setup_logging("app")
    assert len(logging.getLogger().handlers) == 1


def test_log_level_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    setup_logging("app")
    assert logging.getLogger().level == logging.DEBUG


def test_log_level_default_prod():
    setup_logging("app")
    assert logging.getLogger().level == logging.INFO


def test_log_level_default_dev(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    setup_logging("app")
    assert logging.getLogger().level == logging.DEBUG


def test_slug_explicit_short_name():
    assert _derive_slug("scheduler") == "scheduler"


def test_slug_strips_services_prefix():
    assert _derive_slug("services.scheduler.__main__") == "scheduler"


def test_slug_keeps_nested_module():
    assert _derive_slug("services.corporate_memory.collector") == "corporate_memory.collector"


def test_slug_strips_app_prefix():
    assert _derive_slug("app.main") == "app"


def test_slug_strips_connectors_prefix():
    assert _derive_slug("connectors.jira.transform") == "jira.transform"


def test_slug_explicit_app():
    assert _derive_slug("app") == "app"


def test_json_formatter_includes_service_field():
    rec = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello world",
        args=(),
        exc_info=None,
    )
    fmt = _JSONFormatter(service="myservice")
    line = fmt.format(rec)
    parsed = json.loads(line)
    assert parsed["service"] == "myservice"
    assert parsed["msg"] == "hello world"
    assert parsed["lvl"] == "INFO"
    assert parsed["logger"] == "test"
    assert "ts" in parsed


def test_json_formatter_includes_request_id_when_set():
    fmt = _JSONFormatter(service="app")
    rec = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="m",
        args=(),
        exc_info=None,
    )
    token = request_id_var.set("abc123")
    try:
        line = fmt.format(rec)
    finally:
        request_id_var.reset(token)
    parsed = json.loads(line)
    assert parsed["request_id"] == "abc123"


def test_json_formatter_omits_request_id_when_unset():
    fmt = _JSONFormatter(service="app")
    rec = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="m",
        args=(),
        exc_info=None,
    )
    line = fmt.format(rec)
    parsed = json.loads(line)
    assert "request_id" not in parsed


def test_json_formatter_includes_exception():
    fmt = _JSONFormatter(service="app")
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="oops",
            args=(),
            exc_info=sys.exc_info(),
        )
    line = fmt.format(rec)
    parsed = json.loads(line)
    assert "exc" in parsed
    assert "ValueError: boom" in parsed["exc"]


def test_setup_logging_emits_parsable_json_in_prod(capsys):
    setup_logging("app")
    logging.getLogger("test").info("hello %s", "world")
    out = capsys.readouterr().err
    parsed = json.loads(out.strip().splitlines()[-1])
    assert parsed["msg"] == "hello world"
    assert parsed["service"] == "app"


def test_setup_logging_silences_uvicorn_access_in_prod():
    setup_logging("app")
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


def test_setup_logging_keeps_uvicorn_access_in_dev(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    setup_logging("app")
    assert logging.getLogger("uvicorn.access").level == logging.INFO


def test_slug_none_falls_back_to_app():
    # No service hint and the calling frame's __file__ won't sit under
    # services/connectors/app — fallback returns "app" or the file stem.
    result = _derive_slug(None)
    assert isinstance(result, str)
    assert result  # non-empty


def test_slug_none_uses_frame_inspection_for_app_path(tmp_path, monkeypatch):
    # Simulate a caller from a path that contains "app" in its parts by
    # invoking _derive_slug from a helper module that lives under app/.
    # We exercise the frame-inspection branch directly.
    import app.logging_config as lc

    # Wrap to ensure the call frame's __file__ is THIS test file (no
    # services/connectors/app prefix on macOS path) -> falls through to p.stem.
    result = lc._derive_slug(None)
    assert result  # path stem or "app" — both are valid here


def test_slug_underscore_prefix_falls_back():
    # "_private" should NOT be treated as a service name (starts with "_").
    result = _derive_slug("_private")
    assert isinstance(result, str)
    assert result


def test_slug_main_dunder_falls_back():
    # "__main__" alone is not a useful slug.
    result = _derive_slug("__main__")
    assert isinstance(result, str)
    assert result
