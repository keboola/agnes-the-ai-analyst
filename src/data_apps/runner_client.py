from __future__ import annotations

import os
from typing import Any, Optional

import httpx


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

    def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            with httpx.Client(transport=self._transport, timeout=60) as c:
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
