"""Owned manager processes and a loopback-only Jobs API outage boundary."""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_OUTPUT = 64 * 1024


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
        if not upstream.startswith("http://127.0.0.1:"):
            raise ValueError("qualification proxy requires its owned loopback Ray dashboard")
        self.unavailable = threading.Event()
        self.requests = []
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
                    request = urllib.request.Request(
                        upstream + self.path,
                        data=body,
                        method=self.command,
                        headers={"Content-Type": "application/json"},
                    )
                    try:
                        response = urllib.request.urlopen(request, timeout=5)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        status, data = response.status, response.read(1024 * 1024 + 1)
                    if len(data) > 1024 * 1024:
                        status, data = 502, b"{}"
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
                self.wfile.write(data)

            do_GET = forward  # noqa: N815 - stdlib handler names
            do_POST = forward  # noqa: N815 - stdlib handler names
            do_DELETE = forward  # noqa: N815 - stdlib handler names

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Jobs proxy did not shut down")
