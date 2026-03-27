"""HTTP client wrapper for CLI — handles auth, retries, streaming."""

from typing import Optional

import httpx

from cli.config import get_server_url, get_token


def get_client(timeout: float = 30.0) -> httpx.Client:
    """Get an authenticated httpx client."""
    token = get_token()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url=get_server_url(),
        headers=headers,
        timeout=timeout,
    )


def api_get(path: str, **kwargs) -> httpx.Response:
    with get_client() as client:
        return client.get(path, **kwargs)


def api_post(path: str, **kwargs) -> httpx.Response:
    with get_client() as client:
        return client.post(path, **kwargs)


def api_delete(path: str, **kwargs) -> httpx.Response:
    with get_client() as client:
        return client.delete(path, **kwargs)


def stream_download(path: str, target_path: str, progress_callback=None) -> int:
    """Stream download a file from the API. Returns bytes written."""
    with get_client(timeout=300.0) as client:
        with client.stream("GET", path) as response:
            response.raise_for_status()
            total = 0
            with open(target_path, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    total += len(chunk)
                    if progress_callback:
                        progress_callback(len(chunk))
            return total
