"""A fake Databricks SQL warehouse speaking the Statement Execution API over
real HTTPS, for wire-level tests of the Databricks connector.

Why this exists
---------------
`tests/test_databricks_client.py` drives the client through a duck-typed fake
`requests.Session`, which proves the call *logic* but never exercises the
transport: real HTTP verbs, real polling round-trips, real Arrow IPC bytes off
the wire, and — the security-relevant one — which headers actually leave the
process when fetching a presigned result link.

This module serves a self-signed TLS endpoint implementing the subset of the
API the connector uses:

    POST /api/2.0/sql/statements                      submit (answers PENDING)
    GET  /api/2.0/sql/statements/{id}                 poll   (answers SUCCEEDED)
    GET  /api/2.0/sql/statements/{id}/result/chunks/N chunk link resolution
    GET  /external/{statement_id}/{chunk}             the "presigned" payload

Statements are matched by SQL substring against a caller-supplied routing
table, so a test says "when the SQL mentions information_schema, answer these
rows" without caring how the client got there.

Every request is recorded in `.requests` (path + headers), which is what lets
a test assert that the presigned download carried **no** `Authorization`
header — the workspace token must never reach the storage host.
"""

from __future__ import annotations

import datetime
import io
import json
import ssl
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def make_self_signed_cert(tmp_path: Path) -> Tuple[Path, Path]:
    """Write (cert, key) PEMs for `localhost` into tmp_path; return the paths.

    The cert doubles as the CA bundle the client trusts (via the
    ``REQUESTS_CA_BUNDLE`` env var the fixture sets), so the connector's own
    ``requests`` calls — which never pass ``verify=`` — validate normally
    instead of having TLS verification disabled for the test.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    # Fixed validity window — the codebase forbids Date.now()-style nondeterminism
    # in some contexts, but here the point is simply a cert valid "now" that does
    # not depend on wall-clock drift beyond a wide band.
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "fake-warehouse-cert.pem"
    key_path = tmp_path / "fake-warehouse-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def arrow_stream_bytes(table) -> bytes:
    """Serialize a pyarrow Table to a single Arrow IPC stream."""
    import pyarrow.ipc as ipc

    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@dataclass
class Route:
    """One scripted answer.

    ``match`` is a substring looked for in the submitted SQL. ``columns`` +
    ``rows`` drive INLINE (JSON_ARRAY) answers; ``arrow_table`` drives
    EXTERNAL_LINKS (ARROW_STREAM) answers. ``truncated`` simulates the API
    stopping at ``byte_limit``.
    """

    match: str
    columns: List[str] = field(default_factory=list)
    rows: List[List[Any]] = field(default_factory=list)
    arrow_table: Any = None
    truncated: bool = False
    chunk_rows: int = 1000


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: Dict[str, str]


class FakeWarehouse:
    """Lifecycle wrapper around the TLS server thread."""

    def __init__(self, routes: List[Route], cert_path: Path, key_path: Path):
        self.routes = routes
        self.requests: List[RecordedRequest] = []
        self._statements: Dict[str, Route] = {}
        self._polls: Dict[str, int] = {}
        self._counter = 0
        self._lock = threading.Lock()

        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        self.port = server.server_address[1]
        # `localhost` (not 127.0.0.1) so the cert's SAN matches.
        self.host = f"https://localhost:{self.port}"
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # -- request bookkeeping ------------------------------------------------

    def record(self, method: str, path: str, headers: Dict[str, str]) -> None:
        with self._lock:
            self.requests.append(RecordedRequest(method, path, dict(headers)))

    def requests_for(self, needle: str) -> List[RecordedRequest]:
        return [r for r in self.requests if needle in r.path]

    # -- statement bookkeeping ----------------------------------------------

    def register_statement(self, sql: str) -> Tuple[str, Optional[Route]]:
        with self._lock:
            self._counter += 1
            statement_id = f"st-{self._counter}"
        route = next((r for r in self.routes if r.match in sql), None)
        self._statements[statement_id] = route
        self._polls[statement_id] = 0
        return statement_id, route

    def route_for(self, statement_id: str) -> Optional[Route]:
        return self._statements.get(statement_id)

    def bump_poll(self, statement_id: str) -> int:
        with self._lock:
            self._polls[statement_id] = self._polls.get(statement_id, 0) + 1
            return self._polls[statement_id]


def _make_handler(warehouse: "FakeWarehouse"):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # silence stderr noise in tests
            pass

        # -- helpers --------------------------------------------------------

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _external_link(self, statement_id: str, chunk: int) -> str:
            return f"{warehouse.host}/external/{statement_id}/{chunk}"

        def _succeeded_doc(self, statement_id: str, route: Optional[Route]) -> dict:
            if route is None:
                return {
                    "statement_id": statement_id,
                    "status": {
                        "state": "FAILED",
                        "error": {"error_code": "BAD_REQUEST", "message": "no route matched this SQL"},
                    },
                }
            if route.arrow_table is not None:
                total_rows = route.arrow_table.num_rows
                chunks = max(1, -(-total_rows // route.chunk_rows))
                return {
                    "statement_id": statement_id,
                    "status": {"state": "SUCCEEDED"},
                    "manifest": {
                        "truncated": route.truncated,
                        "total_row_count": total_rows,
                        "total_byte_count": route.arrow_table.nbytes,
                        "total_chunk_count": 0 if route.truncated else chunks,
                        "schema": {
                            "columns": [
                                {"name": n, "type_name": "STRING", "position": i}
                                for i, n in enumerate(route.arrow_table.schema.names)
                            ]
                        },
                    },
                    # Deliberately NOT inlining external_links here: the client
                    # must resolve chunk links via the chunks endpoint, which is
                    # the path a real multi-chunk result takes.
                    "result": {},
                }
            return {
                "statement_id": statement_id,
                "status": {"state": "SUCCEEDED"},
                "manifest": {
                    "truncated": False,
                    "schema": {"columns": [{"name": c} for c in route.columns]},
                },
                "result": {"data_array": [[None if v is None else str(v) for v in row] for row in route.rows]},
            }

        # -- verbs ----------------------------------------------------------

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            warehouse.record("POST", self.path, self.headers)
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path.endswith("/cancel"):
                self._send_json({})
                return
            statement_id, _route = warehouse.register_statement(payload.get("statement", ""))
            # Always answer PENDING first so the client's poll loop is exercised
            # on every statement, exactly as a cold warehouse behaves.
            self._send_json({"statement_id": statement_id, "status": {"state": "PENDING"}})

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            warehouse.record("GET", self.path, self.headers)
            parts = [p for p in self.path.split("/") if p]

            if parts[:1] == ["external"]:
                statement_id, chunk = parts[1], int(parts[2])
                route = warehouse.route_for(statement_id)
                table = route.arrow_table
                start = chunk * route.chunk_rows
                self._send_bytes(arrow_stream_bytes(table.slice(start, route.chunk_rows)))
                return

            # /api/2.0/sql/statements/{id}[/result/chunks/{n}]
            statement_id = parts[4]
            route = warehouse.route_for(statement_id)
            if "chunks" in parts:
                chunk = int(parts[-1])
                self._send_json(
                    {
                        "external_links": [
                            {"chunk_index": chunk, "external_link": self._external_link(statement_id, chunk)}
                        ]
                    }
                )
                return
            warehouse.bump_poll(statement_id)
            self._send_json(self._succeeded_doc(statement_id, route))

    return Handler


def start_fake_warehouse(tmp_path: Path, routes: List[Route], monkeypatch) -> FakeWarehouse:
    """Start a fake warehouse and point `requests` at its cert.

    Sets ``REQUESTS_CA_BUNDLE`` so the connector's plain ``requests`` calls
    (which never pass ``verify=``) trust this endpoint — TLS verification
    stays ON, which is the point.
    """
    cert_path, key_path = make_self_signed_cert(tmp_path)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert_path))
    warehouse = FakeWarehouse(routes, cert_path, key_path)
    return warehouse
