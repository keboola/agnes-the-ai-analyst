from __future__ import annotations

from typing import List


class NativeAdapter:
    """The source already publishes Ossie documents — pass them through."""

    def extract(self, config: dict) -> List[str]:
        return list(config.get("documents") or [])
