"""Reusable in-memory :class:`src.object_store.ObjectStore` fake (wave 2-H).

Backs every wave-2H test that needs a configured object store without real
``boto3``/network I/O — WF-3's distribution-mirror job tests, and (per the
wave plan) WF-2's manifest signed-URL tests and WF-5's end-to-end contract
test. Living in one shared module means those waves don't each hand-roll
their own stub with slightly different edge-case behavior.

Implements every :class:`~src.object_store.ObjectStore` protocol method
(``presign_get``, ``put_file``, ``head_md5``, ``put_bytes``, ``get_bytes``)
against a plain in-process dict, plus failure-injection flags so callers can
exercise the per-file-failure / fail-open code paths without mocking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}
        self.metadata: Dict[str, Dict[str, str]] = {}
        self.put_file_calls: List[Tuple[str, str, str]] = []
        self.put_bytes_calls: List[Tuple[str, bytes, str]] = []
        self.presign_calls: List[Tuple[str, int]] = []
        # Failure injection — set before exercising the target code path.
        self.fail_head_md5 = False
        self.fail_put_file = False
        self.fail_get_bytes = False

    def presign_get(self, key: str, ttl_s: int = 900) -> str:
        self.presign_calls.append((key, ttl_s))
        return f"https://fake-object-store.example.com/{key}?ttl={ttl_s}"

    def put_file(self, local_path: str | Path, key: str, md5: str) -> None:
        self.put_file_calls.append((str(local_path), key, md5))
        if self.fail_put_file:
            raise RuntimeError("simulated put_file failure")
        self.objects[key] = Path(local_path).read_bytes()
        self.metadata[key] = {"md5": md5}

    def head_md5(self, key: str) -> Optional[str]:
        if self.fail_head_md5:
            raise RuntimeError("simulated head_md5 failure")
        meta = self.metadata.get(key)
        return meta.get("md5") if meta else None

    def put_bytes(self, key: str, data: bytes, md5: str) -> None:
        self.put_bytes_calls.append((key, data, md5))
        self.objects[key] = data
        self.metadata[key] = {"md5": md5}

    def get_bytes(self, key: str) -> Optional[bytes]:
        if self.fail_get_bytes:
            raise RuntimeError("simulated get_bytes failure")
        return self.objects.get(key)
