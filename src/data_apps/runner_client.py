from __future__ import annotations

import os
from typing import Any, Optional

import httpx


# Read budget for the cheap calls (status/stop/resume/logs). Short on
# purpose: these do no I/O beyond a local Docker query, so anything slower
# than this really is a wedged sidecar worth reporting as unavailable.
_DEFAULT_TIMEOUT = 60.0

# `up` is the outlier. It is the only call that can trigger a cold image
# pull: the runtime image is ~1.3 GB and on a host that has never run a data
# app the daemon fetches it *inside* this request. Under the 60 s budget the
# pull was cut off mid-stream, docker-py's retried `create` then raised
# ImageNotFound, and the deploy died reporting a missing image that was in
# fact merely still downloading. Pre-pulling (startup script +
# agnes-auto-upgrade) is the real fix; this is the backstop for every path
# that still reaches a cold host, and is tunable because link speed is a
# per-deployment fact.
_UP_TIMEOUT_ENV = "APPS_RUNNER_UP_TIMEOUT"
_UP_TIMEOUT_DEFAULT = 600.0


def _up_timeout() -> float:
    try:
        return float(os.environ.get(_UP_TIMEOUT_ENV, "") or _UP_TIMEOUT_DEFAULT)
    except ValueError:
        return _UP_TIMEOUT_DEFAULT


class RunnerUnavailable(RuntimeError):
    pass


class RunnerError(RuntimeError):
    """Sidecar answered with an error status (4xx/5xx)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"runner {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class RunnerClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self._base = (base_url or os.environ.get("APPS_RUNNER_URL", "http://apps-runner:8600")).rstrip("/")
        self._token = token or os.environ.get("APPS_RUNNER_TOKEN", "")
        self._transport = transport

    def _request(self, method: str, path: str, timeout: float = _DEFAULT_TIMEOUT, **kw: Any) -> dict[str, Any]:
        try:
            with httpx.Client(transport=self._transport, timeout=timeout) as c:
                r = c.request(
                    method,
                    f"{self._base}{path}",
                    headers={"X-Runner-Token": self._token},
                    **kw,
                )
        except httpx.TransportError as exc:
            raise RunnerUnavailable(str(exc)) from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise RunnerError(r.status_code, detail)
        return r.json()

    def up(self, slug: str, spec: dict, config_json: dict) -> dict:
        return self._request(
            "POST",
            f"/apps/{slug}/up",
            timeout=_up_timeout(),
            json={"spec": spec, "config_json": config_json},
        )

    def stop(self, slug: str, mode: str = "recreate") -> dict:
        return self._request("POST", f"/apps/{slug}/stop", json={"mode": mode})

    def resume(self, slug: str) -> dict:
        return self._request("POST", f"/apps/{slug}/resume")

    def status(self, slug: str) -> dict:
        return self._request("GET", f"/apps/{slug}/status")

    def logs(self, slug: str, tail: int = 200) -> str:
        return self._request("GET", f"/apps/{slug}/logs", params={"tail": tail}).get("logs", "")
