"""Three fixed WSGI paths and listener events for the owned recycle experiment."""

from __future__ import annotations

import os
import time
from pathlib import Path

# Gunicorn is installed only by the selected, locked Linux sample lane.
from gunicorn.workers.gthread import ThreadWorker  # ty: ignore[unresolved-import]


def root() -> Path:
    return Path(os.environ["DJANGO_RAY_HTTP_RECYCLE_FIXTURE"])


def event(name: str) -> None:
    # One append syscall; fixed event names, no headers, URLs or response data.
    descriptor = os.open(root() / "events", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, f"{name} {os.getpid()}\n".encode("ascii"))
    finally:
        os.close(descriptor)


class ObservedThreadWorker(ThreadWorker):
    """Observe the actual transition without changing dispatch or shutdown."""

    def set_accept_enabled(self, enabled):
        changed = enabled != self._accepting
        super().set_accept_enabled(enabled)
        if changed:
            event("ready" if enabled else "listener-disabled")


def application(environ, start_response):
    path = environ["PATH_INFO"]
    if path == "/hold":
        event("hold-enter")
        deadline = time.monotonic() + 10
        while not (root() / "release").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Owned held request was not released")
            time.sleep(0.01)
    elif path == "/trigger":
        event("trigger-enter")
    elif path == "/probe":
        event("probe-enter")
    else:
        raise ValueError("Unknown owned request")
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
    return [b"ok"]
