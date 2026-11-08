"""Owned manager processes and a loopback-only Jobs API outage boundary."""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_OUTPUT = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep fixture authentication on the explicitly owned dashboard endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Manager:
    def __init__(self, name, *, recovery_only=False):
        self.output = bytearray()
        self.overflow = False
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-P",
                "-m",
                "qualification.latency.manager",
                name,
                "recovery-only" if recovery_only else "fast",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        assert self.process.stdout is not None
        with self.process.stdout as stream:
            while chunk := stream.read(4096):
                remaining = MAX_OUTPUT - len(self.output)
                self.output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.overflow = True
                    self.process.kill()
                    return

    def stop(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise RuntimeError("manager did not shut down") from None
        finally:
            self.reader.join(timeout=5)
        if self.process.returncode != 0 or self.overflow or self.reader.is_alive():
            print(bytes(self.output).decode(errors="replace"), flush=True)
            raise RuntimeError("manager process failed")


class JobsProxy:
    def __init__(self, upstream):
        parsed = urllib.parse.urlsplit(upstream)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.port == 0
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("qualification proxy requires its owned loopback Ray dashboard")
        upstream = upstream.rstrip("/")
        self.unavailable = threading.Event()
        self.requests = []
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                pass

            def forward(self):
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1024 * 1024 or len(owner.requests) >= 512:
                    self.send_error(413)
                    return
                body = self.rfile.read(length) if length else None
                if owner.unavailable.is_set():
                    status, data = 503, b'{"error":"qualification outage"}'
                else:
                    headers = {"Content-Type": "application/json"}
                    for name in ("Authorization", "X-Ray-Authorization"):
                        value = self.headers.get(name)
                        if value is not None:
                            headers[name] = value
                    request = urllib.request.Request(
                        upstream + self.path,
                        data=body,
                        method=self.command,
                        headers=headers,
                    )
                    try:
                        try:
                            response = owner.opener.open(request, timeout=5)
                        except urllib.error.HTTPError as error:
                            response = error
                        with response:
                            status, data = response.status, response.read(1024 * 1024 + 1)
                    except (urllib.error.URLError, TimeoutError, ConnectionError):
                        status, data = 502, b'{"error":"qualification upstream unavailable"}'
                    if len(data) > 1024 * 1024:
                        status, data = 502, b"{}"
                    if (
                        not isinstance(status, int)
                        or isinstance(status, bool)
                        or not 100 <= status <= 599
                    ):
                        status, data = 502, b"{}"
                    if not 200 <= status < 300:
                        data = b'{"error":"qualification upstream request failed"}'
                owner.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "status": status,
                        "at_ns": time.monotonic_ns(),
                    }
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    return

            do_GET = forward  # noqa: N815 - stdlib handler names
            do_POST = forward  # noqa: N815 - stdlib handler names
            do_DELETE = forward  # noqa: N815 - stdlib handler names

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def failure_diagnostics(self):
        """Retain bounded HTTP status counts without headers, bodies or error text."""
        statuses = {}
        for request in self.requests[:512]:
            status = str(request["status"])
            statuses[status] = statuses.get(status, 0) + 1
        return {"observed_requests": sum(statuses.values()), "http_status_counts": statuses}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Jobs proxy did not shut down")
