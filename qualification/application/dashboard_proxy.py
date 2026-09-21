"""Fixed-origin, GET-only proxy for the disposable dashboard browser check."""

from __future__ import annotations

import http.client
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

DASHBOARD_ORIGIN = "http://127.0.0.1:8765"
DASHBOARD_BASE = DASHBOARD_ORIGIN + "/ray"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_CONCURRENT_REQUESTS = 4
LISTEN_PORT = 8765


def upstream_path(target: str) -> str:
    """Strip the fixture prefix without accepting another origin or traversal."""
    if not target.isascii() or len(target) > 4096 or any(ord(c) <= 32 for c in target):
        raise ValueError("Invalid dashboard proxy target")
    parsed = urlsplit(target)
    decoded = unquote(parsed.path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not parsed.path.startswith("/ray/")
        or "\\" in decoded
        or any(part in {".", ".."} for part in decoded.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
    ):
        raise ValueError("Dashboard proxy target escaped its prefix")
    return parsed.path[4:] + ("?" + parsed.query if parsed.query else "")


@contextmanager
def dashboard_proxy():
    """Bind only loopback and proxy one fixed Ray head; forward no credentials.

    The enclosing assertion Job owns the resource ceiling and hard deadline.
    Individual requests have byte/time bounds and a four-request concurrency cap.
    """
    slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
    active = set()
    lock = threading.Lock()

    class Server(ThreadingHTTPServer):
        daemon_threads = False

        def process_request(self, request, client_address):
            request.settimeout(5)
            if not slots.acquire(timeout=5):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                slots.release()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - preserve the stdlib keyword signature
            pass

        def do_GET(self):
            connection = http.client.HTTPConnection("ray-head", 8265, timeout=5)
            try:
                with lock:
                    active.add(connection)
                try:
                    path = upstream_path(self.path)
                except ValueError:
                    self.send_error(400)
                    return
                connection.request("GET", path, headers={"Accept-Encoding": "identity"})
                response = connection.getresponse()
                chunks = []
                size = 0
                deadline = time.monotonic() + 15
                while True:
                    chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                        raise ValueError("Dashboard response exceeded fixture bounds")
                    if not chunk:
                        break
                    chunks.append(chunk)
                self.send_response(response.status)
                content_type = response.getheader("Content-Type", "text/plain")
                self.send_header("Content-Type", content_type.replace("\r", "").replace("\n", ""))
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(chunk)
            except (OSError, ValueError, http.client.HTTPException):
                self.close_connection = True
            finally:
                connection.close()
                with lock:
                    active.discard(connection)

    server = Server(("127.0.0.1", LISTEN_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/ray"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        with lock:
            for connection in tuple(active):
                connection.close()
        if thread.is_alive():
            raise ValueError("Dashboard proxy listener did not stop")
