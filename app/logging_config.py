"""Centralized logging configuration for FastAPI app and background services.

Each entrypoint (app/main.py, services/*/__main__.py or top-level script)
calls setup_logging(__name__) once. Library modules just do
`logger = logging.getLogger(__name__)` — they NEVER call setup_logging.

Dev (DEBUG=1): rich.logging.RichHandler with color, tracebacks, links.
Prod: stdlib StreamHandler with JSON formatter to stderr.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

_CONFIGURED = False


class _RequestIdFilter(logging.Filter):
    """Inject the current request_id ContextVar into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True


def setup_logging(service: str | None = None, level: str | None = None) -> None:
    """Configure root logger. Idempotent.

    Pass ``__name__`` (preferred) or an explicit short slug like ``"app"``.
    Multiple calls are no-ops.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    debug = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
    lvl = (level or os.environ.get("LOG_LEVEL") or ("DEBUG" if debug else "INFO")).upper()
    slug = _derive_slug(service)

    if debug:
        from rich.console import Console
        from rich.logging import RichHandler

        handler: logging.Handler = RichHandler(
            console=Console(stderr=True, force_terminal=True),
            rich_tracebacks=True,
            tracebacks_show_locals=False,
            show_time=True,
            show_path=True,
            markup=False,
        )
        handler.setFormatter(logging.Formatter("[%(request_id)s] [%(name)s] %(message)s"))
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(_JSONFormatter(service=slug))

    handler.addFilter(_RequestIdFilter())
    logging.basicConfig(level=lvl, handlers=[handler], force=True)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO if debug else logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _CONFIGURED = True


def _derive_slug(service: str | None) -> str:
    """Turn module name (``__name__``) or override into readable service slug.

    Examples:
        _derive_slug("app")                                  -> "app"
        _derive_slug("services.scheduler.__main__")          -> "scheduler"
        _derive_slug("services.corporate_memory.collector")  -> "corporate_memory.collector"
        _derive_slug("connectors.jira.transform")            -> "jira.transform"
    """
    if service and not service.startswith("_") and service != "__main__":
        s = service.removeprefix("services.").removeprefix("connectors.").removeprefix("app.")
        s = s.removesuffix(".__main__").removesuffix(".main")
        if s in ("", "main", "__main__"):
            return "app"
        return s

    try:
        frame = sys._getframe(2)
        path = frame.f_globals.get("__file__")
        if path:
            p = Path(path)
            for top in ("services", "connectors", "app"):
                if top in p.parts:
                    i = p.parts.index(top) + 1
                    rest = p.parts[i:]
                    name = ".".join([*rest[:-1], p.stem])
                    return name.removesuffix(".__main__").removesuffix(".main") or top
            return p.stem
    except Exception:
        pass
    return "app"


class _JSONFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "lvl": record.levelname,
            "logger": record.name,
            "service": self.service,
            "msg": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
