"""Prometheus metrics and a combined /metrics + /health server for AMPA.

This module exposes three metrics required by the observability work-item and
provides a small WSGI server that serves both `/metrics` and `/health` on the
same port. Tests may start the server via `start_metrics_server`.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional, Tuple

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from wsgiref.simple_server import make_server, WSGIServer, WSGIRequestHandler

# Registry-local metrics so they do not clash with external collectors during
# tests or when the package is imported multiple times.
registry = CollectorRegistry()

ampa_heartbeat_sent_total = Counter(
    "ampa_heartbeat_sent_total",
    "Total number of successful AMPA heartbeat sends",
    registry=registry,
)
ampa_heartbeat_failure_total = Counter(
    "ampa_heartbeat_failure_total",
    "Total number of failed AMPA heartbeat sends",
    registry=registry,
)
ampa_last_heartbeat_timestamp_seconds = Gauge(
    "ampa_last_heartbeat_timestamp_seconds",
    "Last successful heartbeat time as epoch seconds",
    registry=registry,
)


def _wsgi_app(environ, start_response):
    path = environ.get("PATH_INFO", "")
    if path == "/metrics":
        data = generate_latest(registry)
        start_response("200 OK", [("Content-Type", CONTENT_TYPE_LATEST)])
        return [data]

    if path == "/health":
        # Fatal misconfiguration = missing AMPA_DISCORD_WEBHOOK
        webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
        if webhook and webhook.strip():
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"OK"]
        start_response("503 Service Unavailable", [("Content-Type", "text/plain")])
        return [b"misconfigured"]

    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found"]


class _ThreadedWSGIServer(threading.Thread):
    def __init__(self, host: str, port: int):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self._server: Optional[Tuple[WSGIServer, int]] = None

    def run(self) -> None:  # pragma: no cover - exercised in integration tests
        httpd = make_server(self.host, self.port, _wsgi_app)
        # communicate the chosen port back to the thread owner via attribute
        self._server = (httpd, httpd.server_port)
        try:
            httpd.serve_forever()
        finally:
            try:
                httpd.server_close()
            except Exception:
                pass

    def get_port(self) -> Optional[int]:
        if self._server:
            return self._server[1]
        return None


def start_metrics_server(
    host: str = "127.0.0.1", port: int = 8000
) -> Tuple[_ThreadedWSGIServer, int]:
    """Start the combined metrics+health server in a background thread.

    Returns the thread object and the bound port (useful when port=0 was passed).
    """
    thr = _ThreadedWSGIServer(host, port)
    thr.start()

    # Wait for server to be created and bound
    for _ in range(50):
        p = thr.get_port()
        if p:
            return thr, p
        time.sleep(0.01)
    # Last-ditch: return whatever we have
    return thr, port


__all__ = [
    "ampa_heartbeat_sent_total",
    "ampa_heartbeat_failure_total",
    "ampa_last_heartbeat_timestamp_seconds",
    "start_metrics_server",
]
