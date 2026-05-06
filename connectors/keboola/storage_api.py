"""Lightweight Keboola Storage API client for table export.

The DuckDB Keboola extension was the originally-intended fast path, but on
projects with the `block-shared-snowflake-access` feature flag and on linked
buckets the per-session workspace can't see the bucket schemas at all
(keboola/duckdb-extension#17, fixed upstream in v0.1.6 but not yet in the
community CDN as of 2026-05-06). The `kbcstorage` SDK works but uses
`os.chdir(temp_dir)` to redirect slice downloads, which is process-global —
threaded fan-out races on CWD and slice files land in the wrong directory.

This module talks to Storage API directly and downloads via signed URLs:
- POST /v2/storage/tables/{id}/export-async
- GET  /v2/storage/jobs/{id}  (poll until success/error)
- GET  /v2/storage/files/{id}?federationToken=1
- GET  <signed_url>  (single file or manifest + per-slice URLs for sliced)

No `os.chdir`, no boto3/azure-blob/google-cloud-storage SDK dependencies —
the federation-token detail response includes a signed URL that works for
all three cloud backends. Thread-safe: each call uses an independent
download path under a per-call temp directory.

Storage API reference:
- https://keboola.docs.apiary.io/#reference/tables/asynchronous-table-export
- https://keboola.docs.apiary.io/#reference/jobs
- https://keboola.docs.apiary.io/#reference/files/manage-files/file-detail
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)

# Storage API guarantees export jobs are created small and finish in seconds
# to a few minutes for typical bucket-table sizes; the absolute upper bound
# (very large tables, peak Snowflake load) is the operator's
# storage.jobsParallelism + scan duration. 30 min is a generous ceiling that
# matches what the dashboard's data-preview UI would also wait for.
_DEFAULT_EXPORT_TIMEOUT_SEC = int(os.environ.get("AGNES_KEBOOLA_EXPORT_TIMEOUT_SEC", "1800"))
_DEFAULT_POLL_INTERVAL_SEC = float(os.environ.get("AGNES_KEBOOLA_POLL_INTERVAL_SEC", "2"))

# Per-slice HTTP download timeout — separate from the export-job timeout.
# Sliced exports return a manifest of signed URLs; an individual slice is
# bounded in size by Storage API's slicer (typically ~100 MiB), so a few
# minutes is plenty for one HTTP GET.
_DEFAULT_SLICE_DOWNLOAD_TIMEOUT_SEC = int(
    os.environ.get("AGNES_KEBOOLA_SLICE_TIMEOUT_SEC", "300")
)


@dataclass
class ExportFilter:
    """Structured Keboola Storage API filter spec.

    Mirrors the BQ materialized path's `source_query` SQL string conceptually
    — both let the admin scope an extracted table — but Storage API takes a
    structured filter object rather than free-form SQL. Empty fields all
    map to "no filter" so a default-constructed ExportFilter exports the
    full table.

    Operators per Apiary docs: eq, ne, in, notIn, ge, gt, le, lt.
    """
    where_filters: List[dict] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    changed_since: Optional[str] = None
    changed_until: Optional[str] = None
    limit: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ExportFilter":
        """Parse from `table_registry.source_query` JSON. Tolerates None /
        empty / unknown keys (registry stores admin input that may be sparse)."""
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ValueError(
                f"ExportFilter.from_dict expects a dict, got {type(data).__name__}"
            )
        return cls(
            where_filters=list(data.get("where_filters") or []),
            columns=list(data.get("columns") or []),
            changed_since=data.get("changed_since"),
            changed_until=data.get("changed_until"),
            limit=data.get("limit"),
        )

    def to_export_params(self) -> dict:
        """Serialize for POST body of `/tables/{id}/export-async`.

        whereFilters arrives as a list of `{column, operator, values}` dicts;
        Storage API also accepts a single `whereColumn`/`whereOperator`/
        `whereValues` triple but the multi-filter form is more general.
        """
        params: dict = {}
        if self.where_filters:
            # Validate shape lightly — surface admin typos as ValueError
            # rather than letting them turn into a 400 from Keboola's API
            # without context.
            for i, f in enumerate(self.where_filters):
                if not isinstance(f, dict):
                    raise ValueError(f"where_filters[{i}] must be a dict")
                missing = {"column", "operator", "values"} - set(f.keys())
                if missing:
                    raise ValueError(
                        f"where_filters[{i}] missing fields: {sorted(missing)}"
                    )
                if not isinstance(f["values"], list):
                    raise ValueError(f"where_filters[{i}].values must be a list")
            params["whereFilters"] = self.where_filters
        if self.columns:
            params["columns"] = ",".join(self.columns)
        if self.changed_since:
            params["changedSince"] = self.changed_since
        if self.changed_until:
            params["changedUntil"] = self.changed_until
        if self.limit is not None:
            params["limit"] = int(self.limit)
        return params


class StorageApiError(RuntimeError):
    """Wraps a non-2xx Storage API response with the parsed body for context."""

    def __init__(self, message: str, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class KeboolaStorageClient:
    """Thread-safe Storage API client for table export.

    One instance can be reused across threads — `requests.Session` is
    thread-safe when the underlying `HTTPAdapter`'s pool size is sized for
    concurrent calls. Default `pool_connections=20, pool_maxsize=20`
    accommodates the typical AGNES_KEBOOLA_PARALLELISM=8 plus headroom.
    """

    def __init__(self, *, url: str, token: str, session: Optional[requests.Session] = None):
        if not url or not token:
            raise ValueError("KeboolaStorageClient requires url and token")
        # The DuckDB Keboola extension's ATTACH chokes on a trailing slash
        # (`https://connection.<region>.keboola.com/`); the Storage API
        # tolerates either form, but normalising here keeps URL composition
        # below predictable.
        self.base = url.rstrip("/") + "/v2/storage"
        self.token = token
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=20, pool_maxsize=20
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        self.session = session

    # ---- low-level HTTP helpers -------------------------------------------

    def _headers(self) -> dict:
        return {"X-StorageApi-Token": self.token, "Accept": "application/json"}

    def _get(self, path: str, **kwargs) -> dict:
        url = f"{self.base}{path}"
        resp = self.session.get(url, headers=self._headers(), timeout=30, **kwargs)
        return self._parse(resp, "GET", url)

    def _post(self, path: str, *, data: Optional[dict] = None) -> dict:
        url = f"{self.base}{path}"
        resp = self.session.post(
            url, headers=self._headers(), data=data, timeout=30
        )
        return self._parse(resp, "POST", url)

    def _parse(self, resp: requests.Response, method: str, url: str) -> dict:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        if resp.status_code >= 400:
            # Redact the token if it accidentally surfaces in an error body.
            # The Storage API doesn't echo it, but third-party proxies in
            # front of customer instances sometimes do.
            redacted = self._redact(body)
            raise StorageApiError(
                f"{method} {url} -> HTTP {resp.status_code}: {redacted}",
                status=resp.status_code,
                body=body,
            )
        if not isinstance(body, dict):
            raise StorageApiError(
                f"{method} {url} -> unexpected non-JSON response: {str(body)[:200]}",
                status=resp.status_code,
                body=body,
            )
        return body

    def _redact(self, body: Any) -> str:
        s = str(body)
        if self.token and self.token in s:
            s = s.replace(self.token, "<redacted-storage-token>")
        return s[:500]

    # ---- export-async + job polling ---------------------------------------

    def export_table_async(self, table_id: str, params: dict) -> dict:
        """POST /v2/storage/tables/{table_id}/export-async — kicks off the
        async export and returns the job resource. Caller polls `job.id`
        via `wait_for_job` to find the file id when status='success'."""
        return self._post(f"/tables/{table_id}/export-async", data=params)

    def wait_for_job(
        self,
        job_id: int,
        *,
        timeout: float = _DEFAULT_EXPORT_TIMEOUT_SEC,
        poll_interval: float = _DEFAULT_POLL_INTERVAL_SEC,
    ) -> dict:
        """Block until the async job reaches a terminal state. Returns the
        job dict on success; raises `StorageApiError` on failure or timeout.

        The poll interval starts small and backs off slightly so a chain of
        ~10 fast polls covers a sub-30 s job without flogging the API, while
        a 30-min job ends up at a steady cadence after a few minutes.
        """
        deadline = time.monotonic() + timeout
        interval = poll_interval
        while time.monotonic() < deadline:
            job = self._get(f"/jobs/{job_id}")
            status = job.get("status")
            if status == "success":
                return job
            if status == "error":
                raise StorageApiError(
                    f"Storage API job {job_id} reported error: "
                    f"{job.get('error') or job}",
                    body=job,
                )
            time.sleep(interval)
            # Exponential backoff bounded at 10 s — a multi-minute Snowflake
            # scan does not benefit from sub-second polls. 1.5 multiplier
            # reaches 10 s after ~9 polls (~30 s wall-clock) and stays there.
            interval = min(interval * 1.5, 10.0)
        raise StorageApiError(
            f"Storage API job {job_id} did not finish within {timeout}s"
        )

    # ---- file detail + signed-URL download --------------------------------

    def file_detail(self, file_id: int) -> dict:
        """GET /v2/storage/files/{file_id}?federationToken=1 — returns the
        file metadata plus a presigned URL (`url`) usable directly via HTTP
        without any cloud SDK. For sliced exports the `url` resolves to a
        manifest JSON listing the per-slice signed URLs."""
        return self._get(f"/files/{file_id}", params={"federationToken": 1})

    def download_file(self, file_info: dict, dest_path: Path) -> Path:
        """Download a Storage API file (single or sliced) to `dest_path`.

        Single-file: stream the signed URL directly, gunzipping if
        Content-Encoding is gzip OR if the URL's `name` ends in `.gz` (the
        Storage API exporter compresses CSVs by default).

        Sliced: GET the manifest JSON, then HTTP GET each slice's signed
        URL serially into `dest_path` (concatenated). Slices are independent
        files, so even a 1 GB sliced export is a sequence of bounded
        downloads — bounded memory regardless of total result size.
        """
        url = file_info.get("url")
        if not url:
            raise StorageApiError(
                f"file detail missing 'url': {self._redact(file_info)}",
                body=file_info,
            )

        is_sliced = bool(file_info.get("isSliced"))
        is_gzipped = bool(
            file_info.get("name", "").endswith(".gz")
            or file_info.get("isEncrypted") is False  # not encrypted ≠ not gzipped, but defensive
        )

        dest_path.parent.mkdir(parents=True, exist_ok=True)

        if is_sliced:
            self._download_sliced(url, dest_path)
        else:
            self._download_single(url, dest_path, gunzip_on_read=is_gzipped)
        return dest_path

    def _download_single(self, url: str, dest_path: Path, *, gunzip_on_read: bool) -> None:
        """Stream a single signed URL into `dest_path`, transparently
        gunzipping if the file name suggests it's a `.gz`. We don't trust
        the response Content-Encoding header alone — Storage API often
        serves through proxies that transparently decompress and rewrite
        the header, so the `name`-ends-in-`.gz` heuristic is more reliable
        in practice."""
        with self.session.get(url, stream=True, timeout=_DEFAULT_SLICE_DOWNLOAD_TIMEOUT_SEC) as r:
            r.raise_for_status()
            tmp = dest_path.with_suffix(dest_path.suffix + ".part")
            try:
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            fh.write(chunk)
                if gunzip_on_read:
                    self._gunzip_in_place(tmp, dest_path)
                    tmp.unlink(missing_ok=True)
                else:
                    tmp.replace(dest_path)
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)

    def _download_sliced(self, manifest_url: str, dest_path: Path) -> None:
        """Sliced exports: the file detail's `url` points at a JSON manifest
        whose `entries[].url` are signed per-slice URLs. Download each slice
        and concatenate into `dest_path`. The first slice contains the CSV
        header (Storage API guarantees stable header positioning)."""
        m = self.session.get(
            manifest_url, timeout=_DEFAULT_SLICE_DOWNLOAD_TIMEOUT_SEC
        )
        m.raise_for_status()
        manifest = m.json()
        entries = manifest.get("entries") or []
        if not entries:
            raise StorageApiError(
                f"sliced manifest had no entries: {str(manifest)[:200]}",
                body=manifest,
            )

        with tempfile.TemporaryDirectory(prefix="kbc-slice-") as tmpdir:
            slice_paths: List[Path] = []
            for i, entry in enumerate(entries):
                surl = entry.get("url")
                if not surl:
                    raise StorageApiError(
                        f"slice {i} missing 'url': {str(entry)[:200]}",
                        body=entry,
                    )
                sp = Path(tmpdir) / f"slice-{i:05d}"
                # Slices may individually be gzipped — same heuristic as
                # single-file: if the slice URL's path ends in `.gz`, gunzip
                # after download.
                gz = ".gz" in surl.split("?")[0].rsplit("/", 1)[-1]
                self._download_single(surl, sp, gunzip_on_read=gz)
                slice_paths.append(sp)

            # Concat. Sliced CSV exports include the header in slice 0 only
            # (Storage API contract); subsequent slices are header-less.
            with open(dest_path, "wb") as out:
                for sp in slice_paths:
                    with open(sp, "rb") as fh:
                        shutil.copyfileobj(fh, out, length=64 * 1024)

    @staticmethod
    def _gunzip_in_place(src: Path, dest: Path) -> None:
        with gzip.open(src, "rb") as gz, open(dest, "wb") as out:
            shutil.copyfileobj(gz, out, length=64 * 1024)

    # ---- high-level: export to local CSV ----------------------------------

    def export_table_to_csv(
        self,
        table_id: str,
        dest_csv: Path,
        *,
        export_filter: Optional[ExportFilter] = None,
        export_timeout: float = _DEFAULT_EXPORT_TIMEOUT_SEC,
    ) -> dict:
        """End-to-end: export-async → poll → download to local CSV.

        Returns a small stats dict so callers can log / record provenance:
            {"job_id": int, "file_id": int, "rows": int|None, "bytes": int}
        """
        params = (export_filter or ExportFilter()).to_export_params()
        job_resp = self.export_table_async(table_id, params)
        job_id = job_resp.get("id")
        if not job_id:
            raise StorageApiError(
                f"export-async response missing job id: {self._redact(job_resp)}",
                body=job_resp,
            )

        job = self.wait_for_job(job_id, timeout=export_timeout)
        results = job.get("results") or {}
        file_id = (results.get("file") or {}).get("id") or results.get("fileId")
        if not file_id:
            raise StorageApiError(
                f"job {job_id} succeeded but had no result file: "
                f"{self._redact(job)}",
                body=job,
            )

        file_info = self.file_detail(file_id)
        self.download_file(file_info, dest_csv)
        size = dest_csv.stat().st_size if dest_csv.exists() else 0
        return {
            "job_id": int(job_id),
            "file_id": int(file_id),
            "rows": (results.get("totalRowsCount")
                     or results.get("rowsCount")
                     or job.get("totalRowsCount")),
            "bytes": size,
        }
