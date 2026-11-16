"""Fixed GET transport with API-only credentials and absolute request shutdown."""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
from urllib.parse import urlsplit

from .bounds import MAX_JSON, MAX_ZIP, BindingError, decode, redirect, require, resolve, route

Timer = threading.Timer


class HeaderReader:
    def __init__(self, stream):
        self.stream, self.bytes, self.lines, self.done = stream, 0, 0, False

    def readline(self, maximum=-1):
        raw = self.stream.readline(
            maximum if self.done else min(maximum, 8193) if maximum >= 0 else 8193
        )
        if not self.done:
            self.bytes += len(raw)
            self.lines += 1
            require(
                len(raw) <= 8192 and self.bytes <= 32768 and self.lines <= 64,
                "transport_response_invalid",
                unavailable=True,
            )
        return raw

    def read(self, count=-1):
        return self.stream.read(count)

    def read1(self, count=-1):
        return self.stream.read1(count)

    def close(self):
        self.stream.close()

    def flush(self):
        self.stream.flush()


class Response(http.client.HTTPResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.fp is not None:
            self.fp = HeaderReader(self.fp)

    def begin(self):
        super().begin()
        if self.fp is not None:
            self.fp.done = True

    def _read_and_discard_trailer(self):
        # Chunk framing and body reads are excluded; trailers share the
        # initial header counters rather than receiving a fresh allowance.
        reader = self.fp
        if reader is not None:
            reader.done = False
        try:
            super()._read_and_discard_trailer()
        finally:
            if reader is not None:
                reader.done = True


class Connection(http.client.HTTPSConnection):
    response_class = Response

    def __init__(self, host, address, timeout, *, deadline, clock):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address, self.active, self.aborted = address, None, threading.Event()
        self.deadline, self.clock = deadline, clock

    def remaining(self):
        remaining = self.deadline - self.clock()
        if self.aborted.is_set() or remaining <= 0:
            raise TimeoutError
        return min(self.timeout, remaining)

    def connect(self):
        self.remaining()
        family, kind, protocol, _, address = self.address
        connection = socket.socket(family, kind, protocol)
        self.active = connection
        try:
            # abort() can fire inside socket() before the handle is published.
            connection.settimeout(self.remaining())
            self.remaining()
            connection.connect(address)
            self.remaining()
            wrapped = self._context.wrap_socket(
                connection, server_hostname=self.host, do_handshake_on_connect=False
            )
            self.active = self.sock = wrapped
            try:
                wrapped.settimeout(self.remaining())
                self.remaining()
                wrapped.do_handshake()
                wrapped.settimeout(self.remaining())
            except BaseException:
                wrapped.close()
                raise
        except BaseException:
            connection.close()
            raise

    def abort(self):
        self.aborted.set()
        if self.active is not None:
            try:
                self.active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.active.close()
            except OSError:
                pass


class Github:
    def __init__(self, budget, token):
        require(
            type(token) is str
            and token.isascii()
            and 0 < len(token) <= 4096
            and not any(c.isspace() for c in token),
            "transport_unavailable",
            unavailable=True,
        )
        self.budget, self._token = budget, token

    def get(self, endpoint, *, binary=False):
        url = route(endpoint)
        require(not binary or endpoint.endswith("/zip"), "transport_route_invalid")
        maximum = MAX_ZIP if binary else MAX_JSON
        deadline = self.budget.clock() + self.budget.remaining()
        for attempt in range(4):
            self.budget.request()
            parsed = urlsplit(url)
            address = resolve(parsed.hostname, self.budget, deadline)
            remaining = min(self.budget.remaining(), deadline - self.budget.clock())
            require(remaining > 0, "verification_budget_exceeded", unavailable=True)
            connection = Connection(
                parsed.hostname, address, remaining, deadline=deadline, clock=self.budget.clock
            )
            expired = threading.Event()

            def abort(expired=expired, connection=connection):
                expired.set()
                connection.abort()

            guard = None
            headers = {
                "User-Agent": "django-ray-witness-binding-v1",
                "Accept": "application/vnd.github+json",
            }
            if parsed.hostname == "api.github.com":
                headers["Authorization"] = "Bearer " + self._token
                headers["X-GitHub-Api-Version"] = "2022-11-28"
            try:
                # TLS context construction and watchdog scheduling consume the
                # same absolute deadline as DNS, redirects and socket I/O.
                remaining = min(self.budget.remaining(), deadline - self.budget.clock())
                require(remaining > 0, "verification_budget_exceeded", unavailable=True)
                guard = Timer(remaining, abort)
                guard.daemon = True
                guard.start()
                require(
                    self.budget.clock() < deadline,
                    "verification_budget_exceeded",
                    unavailable=True,
                )
                connection.request(
                    "GET",
                    parsed.path + ("?" + parsed.query if parsed.query else ""),
                    headers=headers,
                )
                response = connection.getresponse()
                require(
                    self.budget.clock() < deadline, "verification_budget_exceeded", unavailable=True
                )
                require(not expired.is_set(), "verification_budget_exceeded", unavailable=True)
                if response.status in (301, 302, 303, 307, 308):
                    require(
                        binary and attempt < 3, "verification_budget_exceeded", unavailable=True
                    )
                    location = response.getheader("Location")
                    if not isinstance(location, str):
                        raise BindingError("redirect_target_invalid")
                    redirect(location)
                    url = location
                    continue
                if response.status in (404, 410):
                    raise BindingError("original_artifact_unavailable", unavailable=True)
                require(response.status == 200, "transport_response_invalid", unavailable=True)
                length = response.getheader("Content-Length")
                require(
                    length is None
                    or (
                        length.isascii()
                        and length.isdigit()
                        and len(length) <= 10
                        and int(length) <= maximum
                    ),
                    "verification_budget_exceeded",
                    unavailable=True,
                )
                raw = bytearray()
                while True:
                    self.budget.remaining()
                    require(
                        self.budget.clock() < deadline,
                        "verification_budget_exceeded",
                        unavailable=True,
                    )
                    block = response.read1(min(65536, maximum + 1 - len(raw)))
                    require(not expired.is_set(), "verification_budget_exceeded", unavailable=True)
                    if not block:
                        break
                    raw.extend(block)
                    self.budget.received(len(block))
                    require(len(raw) <= maximum, "verification_budget_exceeded", unavailable=True)
                require(
                    length is None or len(raw) == int(length),
                    "transport_response_invalid",
                    unavailable=True,
                )
                return bytes(raw) if binary else decode(bytes(raw))
            except BindingError:
                raise
            except (OSError, http.client.HTTPException, ValueError):
                raise BindingError(
                    "verification_budget_exceeded"
                    if expired.is_set() or self.budget.clock() >= deadline
                    else "transport_unavailable",
                    unavailable=True,
                ) from None
            finally:
                if guard is not None:
                    guard.cancel()
                try:
                    connection.close()
                finally:
                    if guard is not None:
                        guard.join(timeout=1)
                require(
                    guard is None or not guard.is_alive(),
                    "verification_budget_exceeded",
                    unavailable=True,
                )
        raise BindingError("verification_budget_exceeded", unavailable=True)
