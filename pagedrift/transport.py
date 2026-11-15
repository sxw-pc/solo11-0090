"""In-process and loopback HTTP transports with an explicit egress guard."""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode, urlsplit
from .config import loopback_host
from .models import TransportResponse
from .mock.universe import MockCore

class TransportError(RuntimeError):
    pass

class NonLoopbackError(TransportError):
    pass

def require_loopback_url(url: str) -> None:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not loopback_host(host):
        raise NonLoopbackError(f"refusing non-loopback URL: {url}")

class InProcessTransport:
    transport_kind = "inprocess"
    def __init__(self, core: MockCore):
        self.core = core
    def meta(self) -> dict[str, Any]:
        raw = self.core.meta_bytes()
        return json.loads(raw.decode("utf-8"))
    def fetch(self, query: dict[str, Any]) -> TransportResponse:
        status, body = self.core.handle_items(dict(query))
        return TransportResponse(status=status, body=body, elapsed_ms=0)

class HttpTransport:
    transport_kind = "http"
    def __init__(
        self,
        base_url: str,
        *,
        items_path: str = "/items",
        meta_path: str = "/meta",
        timeout: float = 5.0,
    ):
        require_loopback_url(base_url)
        parts = urlsplit(base_url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.items_path = items_path if items_path.startswith("/") else "/" + items_path
        self.meta_path = meta_path if meta_path.startswith("/") else "/" + meta_path
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def _get(self, path: str, query: dict[str, Any] | None = None) -> TransportResponse:
        url = self.origin + path
        if query:
            url += "?" + urlencode(query)
        require_loopback_url(url)
        started = time.perf_counter_ns()
        request = urllib.request.Request(url, headers={"User-Agent": "pagedrift-local/0.1"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = int(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"loopback HTTP request failed: {exc}") from exc
        elapsed = (time.perf_counter_ns() - started) // 1_000_000
        return TransportResponse(status=status, body=body, elapsed_ms=int(elapsed))
    def meta(self) -> dict[str, Any]:
        response = self._get(self.meta_path)
        if response.status != 200:
            raise TransportError(f"meta endpoint returned HTTP {response.status}")
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(f"meta endpoint returned invalid JSON: {exc}") from exc
    def fetch(self, query: dict[str, Any]) -> TransportResponse:
        return self._get(self.items_path, query)
