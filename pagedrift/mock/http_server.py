"""Threaded HTTP facade constrained to loopback clients and Host headers."""
from __future__ import annotations
import ipaddress
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit
from ..config import loopback_host
from .universe import MockCore

def host_without_port(header_value: str) -> str:
    value = header_value.strip()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else value
    if value.count(":") == 1:
        candidate, port = value.rsplit(":", 1)
        if port.isdigit():
            return candidate
    return value
def _error_body(code: str, message: str) -> bytes:
    return json.dumps(
        {"error": {"code": code, "message": message}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pagedrift-mock/0.1"
    @property
    def core(self) -> MockCore:
        return self.server.core  # type: ignore[attr-defined]
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return
    def _reject(self, status: int, code: str, message: str) -> None:
        body = _error_body(code, message)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
    def _client_is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False
    def do_GET(self) -> None:  # noqa: N802
        client_host = self.client_address[0]
        host_header = self.headers.get("Host", "")
        host_name = host_without_port(host_header)
        if not self._client_is_loopback() or not host_header or not loopback_host(host_name):
            self.core.add_audit_event(
                {
                    "type": "NON_LOOPBACK_REJECTED",
                    "client": client_host,
                    "host_header": host_header,
                    "path": self.path,
                }
            )
            self._reject(403, "NON_LOOPBACK_REJECTED", "only loopback Host/clients are accepted")
            return
        parts = urlsplit(self.path)
        if parts.path == "/meta":
            body = self.core.meta_bytes()
            self._send(200, body)
            return
        if parts.path == "/items":
            query = {
                key: values[0]
                for key, values in parse_qs(parts.query, keep_blank_values=True).items()
                if values
            }
            status, body = self.core.handle_items(query)
            self._send(status, body)
            return
        self._send(404, _error_body("NOT_FOUND", f"unknown path {parts.path!r}"))
    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

class MockHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, address: tuple[str, int], core: MockCore):
        if not loopback_host(address[0]):
            raise ValueError("mock server may bind only to a loopback address")
        self.core = core
        super().__init__(address, _Handler)
    @property
    def bound_host(self) -> str:
        return str(self.server_address[0])
    @property
    def bound_port(self) -> int:
        return int(self.server_address[1])
    def bind_description(self) -> str:
        return f"http://{self.bound_host}:{self.bound_port}"

def start_mock_server(
    core: MockCore, host: str = "127.0.0.1", port: int = 8765
) -> tuple[MockHTTPServer, threading.Thread]:
    server = MockHTTPServer((host, port), core)
    thread = threading.Thread(target=server.serve_forever, name="pagedrift-mock", daemon=True)
    thread.start()
    return server, thread

def stop_mock_server(server: MockHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
