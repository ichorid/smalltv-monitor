"""Minimal HTTP/1.0 JSON server.

HTTP/1.0 with an explicit Content-Length and no chunking: the ESP8266 HTTPClient reads the body
straight off the socket (deserializeJson(http.getStream())) and cannot de-chunk it.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

log = logging.getLogger(__name__)



@dataclass(frozen=True)
class Request:
    path: str
    query: dict[str, str]
    client: str


# (status, body) for JSON, or (status, body, content type). Keys ending in "/" match as prefixes.
Route = Callable[[Request], tuple[int, bytes] | tuple[int, bytes, str]]
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

_NOT_FOUND = b'{"ok":false,"error":"not found"}'
_ERROR = b'{"ok":false,"error":"internal error"}'


class ClientLog:
    """Request count and last-seen time per client IP, so /v1/status shows whether the display is polling."""

    MAX_CLIENTS = 32

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: dict[str, dict[str, object]] = {}

    def record(self, ip: str, path: str) -> None:
        now = time.time()
        with self._lock:
            entry = self._clients.pop(ip, None)  # re-insert so the dict stays ordered by recency
            if entry is None:
                if len(self._clients) >= self.MAX_CLIENTS:
                    del self._clients[next(iter(self._clients))]
                entry = {"requests": 0}
            entry["requests"] = int(entry["requests"]) + 1  # type: ignore[call-overload]
            entry["last_seen"] = now
            entry["last_path"] = path
            self._clients[ip] = entry

    def snapshot(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {ip: dict(entry) for ip, entry in self._clients.items()}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    timeout = 5  # seconds; drops clients that connect and stall

    server: MetricsHTTPServer

    def version_string(self) -> str:
        return "smalltv-monitor"

    def do_GET(self) -> None:
        self._respond(send_body=True)

    def do_HEAD(self) -> None:
        self._respond(send_body=False)

    def _respond(self, send_body: bool) -> None:
        path, _, query = self.path.partition("?")
        if self.server.clients is not None:
            self.server.clients.record(self.client_address[0], path)
        route = self.server.routes.get(path) or next(
            (r for prefix, r in self.server.routes.items() if prefix.endswith("/") and path.startswith(prefix)), None
        )
        content_type = "application/json"
        if route is None:
            status, body = 404, _NOT_FOUND
        else:
            try:
                result = route(Request(path, dict(parse_qsl(query)), self.client_address[0]))
                status, body = result[0], result[1]
                if len(result) == 3:
                    content_type = result[2]
            except Exception:
                log.exception("error rendering %s", self.path)
                status, body = 500, _ERROR
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (stdlib signature)
        log.debug("%s %s", self.address_string(), format % args)


class MetricsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64  # kernel accept backlog; absorbs bursts without extra threads
    MAX_CONCURRENT = 16  # requests beyond this are dropped instead of spawning more threads

    def __init__(
        self,
        address: tuple[str, int],
        routes: Mapping[str, Route],
        allowed: Iterable[Network] | None,
        clients: ClientLog | None = None,
    ) -> None:
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.routes = dict(routes)
        self.clients = clients
        self.allowed = None if allowed is None else list(allowed)
        self._slots = threading.BoundedSemaphore(self.MAX_CONCURRENT)
        self._rejected: set[str] = set()
        super().__init__(address, _Handler)

    def verify_request(self, request: object, client_address: tuple[str, int]) -> bool:  # type: ignore[override]
        if self.allowed is None:
            return True
        ip = ipaddress.ip_address(client_address[0])
        if any(ip in net for net in self.allowed):
            return True
        if client_address[0] not in self._rejected:  # log each refused client once
            self._rejected.add(client_address[0])
            log.warning("refusing client %s (not in allowed networks)", client_address[0])
        return False

    def process_request(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve_in_thread(server: MetricsHTTPServer) -> threading.Thread:
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, name=f"http-{host}:{port}", daemon=True)
    thread.start()
    return thread
