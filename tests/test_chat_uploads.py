"""Tests for POST /api/chat/uploads — chat workspace file upload endpoint.

TDD suite: written before the implementation. Covers:
  - Happy path: data file, image, document
  - register_as_table=true: workspace-local table registration
  - Oversize rejection (413)
  - Path-traversal rejection (400)
  - Unauthenticated rejection (401/403)
  - Invalid kind rejection (400)
  - Invalid content type rejection (415)
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user

TEST_USER = {"id": "user_upload_tester", "email": "upload@test.com", "is_admin": False}


def _make_app(*, data_dir: Path) -> FastAPI:
    """Minimal FastAPI app with the chat uploads router."""
    os.environ["DATA_DIR"] = str(data_dir)

    # Import AFTER setting DATA_DIR so path helpers pick it up.
    from app.api.chat_uploads import router as chat_uploads_router

    app = FastAPI()
    app.include_router(chat_uploads_router)
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def client(data_dir: Path) -> TestClient:
    app = _make_app(data_dir=data_dir)
    return TestClient(app)


@pytest.fixture
def unauthed_client(data_dir: Path) -> TestClient:
    """Client with no auth override — real dependency."""
    os.environ["DATA_DIR"] = str(data_dir)
    from app.api.chat_uploads import router as chat_uploads_router

    app = FastAPI()
    app.include_router(chat_uploads_router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Happy path — data file
# ---------------------------------------------------------------------------


def test_upload_csv_data_file(client: TestClient, data_dir: Path) -> None:
    """CSV uploaded with kind=data lands under uploads/ in user workspace."""
    csv_content = b"id,name\n1,Alice\n2,Bob\n"
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "data"},
        files={"file": ("test_data.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workspace_path"].endswith("test_data.csv")
    assert body["filename"] == "test_data.csv"
    assert body["size_bytes"] == len(csv_content)
    assert body["kind"] == "data"
    assert body.get("table_name") is None  # not registered

    # File actually exists on disk
    from app.chat.workdir import _safe_email_dir

    email_slug = _safe_email_dir(TEST_USER["email"])
    dest = data_dir / "users" / email_slug / "workspace" / "uploads" / "test_data.csv"
    assert dest.exists()
    assert dest.read_bytes() == csv_content


def test_upload_image(client: TestClient, data_dir: Path) -> None:
    """PNG uploaded with kind=image lands in uploads/."""
    png_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10  # minimal fake PNG
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "image"},
        files={"file": ("chart.png", io.BytesIO(png_content), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "image"
    assert body["filename"] == "chart.png"


def test_upload_document_pdf(client: TestClient, data_dir: Path) -> None:
    """PDF uploaded with kind=document lands in uploads/."""
    pdf_content = b"%PDF-1.4\n"
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "document"},
        files={"file": ("report.pdf", io.BytesIO(pdf_content), "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "document"
    assert body["filename"] == "report.pdf"


# ---------------------------------------------------------------------------
# register_as_table
# ---------------------------------------------------------------------------


def test_register_as_table_csv(client: TestClient, data_dir: Path) -> None:
    """CSV + register_as_table=true creates workspace-local extract.duckdb entry."""
    csv_content = b"id,value\n1,100\n2,200\n"
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "data", "register_as_table": "true", "table_name": "my_upload"},
        files={"file": ("sales.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["table_name"] == "my_upload"

    from app.chat.workdir import _safe_email_dir

    email_slug = _safe_email_dir(TEST_USER["email"])
    ws = data_dir / "users" / email_slug / "workspace"
    # extract.duckdb must exist in the workspace uploads extract area
    extract_db = ws / "uploads" / "extract.duckdb"
    assert extract_db.exists(), f"expected extract.duckdb at {extract_db}"


def test_register_as_table_auto_name(client: TestClient, data_dir: Path) -> None:
    """register_as_table=true without a table_name derives name from filename."""
    csv_content = b"x\n1\n2\n"
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "data", "register_as_table": "true"},
        files={"file": ("my_data.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Should derive "my_data" from filename (stem, sanitized)
    assert body["table_name"] == "my_data"


def test_register_as_table_requires_data_kind(client: TestClient) -> None:
    """register_as_table=true on a non-data kind is rejected with 400."""
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "image", "register_as_table": "true"},
        files={"file": ("photo.png", io.BytesIO(b"fake"), "image/png")},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "register_as_table" in body.get("detail", "")


# ---------------------------------------------------------------------------
# Rejection cases
# ---------------------------------------------------------------------------


def test_oversize_rejected(client: TestClient) -> None:
    """Files exceeding the per-file cap return 413."""
    from app.api.chat_uploads import MAX_CHAT_UPLOAD_BYTES

    # One byte over the cap
    oversized = b"x" * (MAX_CHAT_UPLOAD_BYTES + 1)
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "data"},
        files={"file": ("big.csv", io.BytesIO(oversized), "text/csv")},
    )
    assert resp.status_code == 413


def test_path_traversal_rejected(client: TestClient) -> None:
    """Filenames with directory separators or traversal sequences are rejected."""
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "document"},
        files={
            "file": (
                "../../../etc/passwd",
                io.BytesIO(b"evil"),
                "text/plain",
            )
        },
    )
    assert resp.status_code == 400
    assert "filename" in resp.json().get("detail", "").lower()


def test_dotdot_in_name_rejected(client: TestClient) -> None:
    """Double-dot in filename is rejected."""
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "document"},
        files={"file": ("some..file.txt", io.BytesIO(b"x"), "text/plain")},
    )
    assert resp.status_code == 400


def test_invalid_kind_rejected(client: TestClient) -> None:
    """Unknown kind value is rejected with 422."""
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "skill"},
        files={"file": ("foo.csv", io.BytesIO(b"a"), "text/csv")},
    )
    assert resp.status_code == 422


def test_unauthenticated_rejected(unauthed_client: TestClient) -> None:
    """Request without a bearer token is rejected."""
    resp = unauthed_client.post(
        "/api/chat/uploads",
        data={"kind": "data"},
        files={"file": ("foo.csv", io.BytesIO(b"x"), "text/csv")},
    )
    assert resp.status_code in (401, 403)


def test_unsupported_content_type_rejected(client: TestClient) -> None:
    """Content types not on the allow-list return 415."""
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "document"},
        files={"file": ("prog.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
    )
    assert resp.status_code == 415


# ---------------------------------------------------------------------------
# Response shape completeness
# ---------------------------------------------------------------------------


def test_response_includes_hint(client: TestClient) -> None:
    """Response always includes a hint pointing to next steps."""
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "data"},
        files={"file": ("sample.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "hint" in body, f"expected 'hint' field in response, got: {list(body.keys())}"


def test_empty_filename_rejected(client: TestClient) -> None:
    """Empty or missing filename is rejected (400 from our guard or 422 from FastAPI validation)."""
    resp = client.post(
        "/api/chat/uploads",
        data={"kind": "data"},
        files={"file": ("", io.BytesIO(b"x"), "text/csv")},
    )
    assert resp.status_code in (400, 422), f"expected 4xx, got {resp.status_code}: {resp.text}"
