"""One Linux exec helper with private bounded JSON and retained group ownership.

This is local process supervision, never evidence that a Ray Job is stopped.
The parent polls; it does not wait or communicate with the subprocess. Keep the
leader unreaped until group cleanup so its numeric PID cannot be reused while
we may still signal its process group. Uncertain ownership stays quarantined.
"""

from __future__ import annotations

import errno
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NoReturn

COHORT_PROCESS_REQUEST_MAX_BYTES = 3 * 1024 * 1024
COHORT_PROCESS_RESPONSE_MAX_BYTES = 256 * 1024
COHORT_PROCESS_SCHEMA_VERSION = 1
_REQUEST_SCHEMA = "django-ray.cohort-helper-request"
_RESPONSE_SCHEMA = "django-ray.cohort-helper-response"
_HELPER_MODULE = "django_ray.runner.cohort_job_helper"
_GROUP_SCAN_LIMIT = 4096
_STAT_MAX_BYTES = 4096
_GRACE_SECONDS = 2.0
_JSON_MAX_NODES = 8192
_JSON_MAX_DEPTH = 32
_COMMANDS = frozenset({"prepare", "submit", "inspect", "stop", "discover-client", "inspect-driver"})


class CohortProcessReason(StrEnum):
    INVALID = "invalid"
    PLATFORM_UNSUPPORTED = "platform_unsupported"
    WRONG_OWNER = "wrong_owner"
    BUSY = "busy"
    STALE = "stale"
    SPAWN_FAILED = "spawn_failed"
    DEADLINE = "deadline"
    CANCELLED = "cancelled"
    CLOCK_FAILED = "clock_failed"
    INVALID_RESPONSE = "invalid_response"
    HELPER_FAILED = "helper_failed"
    OWNERSHIP_UNCERTAIN = "ownership_uncertain"
    LOCAL_CLEANUP_FAILED = "local_cleanup_failed"


class CohortProcessError(RuntimeError):
    def __init__(self, reason: CohortProcessReason):
        self.reason = reason
        super().__init__(f"Cohort helper refused: {reason.value}")


class CohortProcessPhase(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    TERMINATING = "terminating"
    KILLING = "killing"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class CohortProcessRequest:
    operation_id: str = field(repr=False)
    payload: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class CohortProcessOperation:
    operation_id: str = field(repr=False)
    deadline: float = field(repr=False)


@dataclass(frozen=True, slots=True)
class CohortProcessCompletion:
    """Local completion only; the payload cannot establish remote cleanup."""

    operation_id: str = field(repr=False)
    reason: CohortProcessReason | None
    response: dict[str, Any] | None = field(default=None, repr=False)


def _refuse(reason=CohortProcessReason.INVALID) -> NoReturn:
    raise CohortProcessError(reason) from None


def _operation_id(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        _refuse()
    return value


def _strict_value(value, maximum):
    nodes = 0
    string_bytes = 0

    def visit(item, depth=0):
        nonlocal nodes, string_bytes
        nodes += 1
        if depth > _JSON_MAX_DEPTH or nodes > _JSON_MAX_NODES:
            _refuse()
        if type(item) is str:
            if len(item) > maximum - string_bytes:
                _refuse()
            string_bytes += len(item.encode("utf-8"))
            if string_bytes > maximum:
                _refuse()
            return
        if item is None or type(item) is bool:
            return
        if type(item) is int and item.bit_length() <= maximum * 4:
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) in (list, dict) and len(item) <= _JSON_MAX_NODES:
            if type(item) is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        _refuse()
                    visit(key, depth + 1)
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
            return
        _refuse()

    visit(value)


def _wire_structure(encoded):
    """Refuse excessive JSON object expansion before invoking the decoder."""
    nodes, depth = 0, 0
    quoted, escaped, primitive = False, False, False
    for char in encoded:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
            continue
        if char in (32, 9, 10, 13, 44, 58):
            primitive = False
        elif char in (125, 93):
            depth -= 1
            primitive = False
        elif char == 34:
            nodes += 1
            quoted = True
            primitive = False
        elif char in (123, 91):
            nodes += 1
            depth += 1
            primitive = False
        elif not primitive:
            nodes += 1
            primitive = True
        if nodes > _JSON_MAX_NODES or depth > _JSON_MAX_DEPTH + 1:
            _refuse()


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            _refuse()
        value[key] = item
    return value


def _encode(value, maximum):
    try:
        _strict_value(value, maximum)
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > maximum:
            _refuse()
        return encoded
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _refuse()


def _decode(encoded, maximum):
    try:
        if type(encoded) is not bytes or len(encoded) > maximum:
            _refuse()
        _wire_structure(encoded)
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda value: _refuse(),
        )
        _strict_value(value, maximum)
        return value
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _refuse()


def _envelope(value, schema, keys):
    if (
        type(value) is not dict
        or set(value) != keys
        or value["schema"] != schema
        or type(value["schema_version"]) is not int
        or value["schema_version"] != COHORT_PROCESS_SCHEMA_VERSION
    ):
        _refuse()
    _operation_id(value["operation_id"])
    if type(value["payload"]) is not dict:
        _refuse()


def encode_cohort_process_request(operation_id: str, payload: dict[str, Any]) -> bytes:
    if (
        type(payload) is not dict
        or set(payload) != {"command", "arguments"}
        or type(payload["command"]) is not str
        or payload["command"] not in _COMMANDS
        or type(payload["arguments"]) is not dict
    ):
        _refuse()
    return _encode(
        {
            "schema": _REQUEST_SCHEMA,
            "schema_version": COHORT_PROCESS_SCHEMA_VERSION,
            "operation_id": _operation_id(operation_id),
            "payload": payload,
        },
        COHORT_PROCESS_REQUEST_MAX_BYTES,
    )


def decode_cohort_process_request(encoded: bytes) -> CohortProcessRequest:
    value = _decode(encoded, COHORT_PROCESS_REQUEST_MAX_BYTES)
    _envelope(value, _REQUEST_SCHEMA, {"schema", "schema_version", "operation_id", "payload"})
    encode_cohort_process_request(value["operation_id"], value["payload"])
    return CohortProcessRequest(value["operation_id"], value["payload"])


def encode_cohort_process_response(
    operation_id: str, *, outcome: str, payload: dict[str, Any]
) -> bytes:
    if type(outcome) is not str or outcome not in {"ok", "failed"} or type(payload) is not dict:
        _refuse()
    return _encode(
        {
            "schema": _RESPONSE_SCHEMA,
            "schema_version": COHORT_PROCESS_SCHEMA_VERSION,
            "operation_id": _operation_id(operation_id),
            "outcome": outcome,
            "payload": payload,
        },
        COHORT_PROCESS_RESPONSE_MAX_BYTES,
    )


def _decode_response(encoded, operation_id):
    value = _decode(encoded, COHORT_PROCESS_RESPONSE_MAX_BYTES)
    _envelope(
        value, _RESPONSE_SCHEMA, {"schema", "schema_version", "operation_id", "outcome", "payload"}
    )
    if value["operation_id"] != operation_id or value["outcome"] not in ("ok", "failed"):
        _refuse()
    return value


def _directory_fd(directory: str) -> int:
    if type(directory) is not str or not os.path.isabs(directory):
        _refuse()
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            _refuse()
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_file(fd, name, maximum):
    stream = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = os.fstat(stream)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            _refuse()
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(stream, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(stream)
        if len(encoded) > maximum or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            _refuse()
        return encoded
    finally:
        os.close(stream)


def _write_file(fd, name, encoded):
    stream = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd
    )
    try:
        view = memoryview(encoded)
        while view:
            count = os.write(stream, view)
            if count <= 0:
                _refuse()
            view = view[count:]
        os.fsync(stream)
    finally:
        os.close(stream)


def read_cohort_process_request(directory: str) -> CohortProcessRequest:
    """Helper-side read without imports of Django, Ray or application code."""
    try:
        fd = _directory_fd(directory)
        try:
            return decode_cohort_process_request(
                _read_file(fd, "request.json", COHORT_PROCESS_REQUEST_MAX_BYTES)
            )
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError):
        _refuse()


def write_cohort_process_response(
    directory: str, *, operation_id: str, outcome: str, payload: dict[str, Any]
) -> None:
    """Helper-side private, atomic response; never write diagnostics to stdio."""
    encoded = encode_cohort_process_response(operation_id, outcome=outcome, payload=payload)
    try:
        fd = _directory_fd(directory)
        try:
            _write_file(fd, "response.tmp", encoded)
            os.replace("response.tmp", "response.json", src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError):
        _refuse()


def _other_group_members(pgid: int) -> bool | None:
    """Bounded Linux metadata scan; uncertainty never authorizes PID release."""
    try:
        with os.scandir("/proc") as entries:
            for count, entry in enumerate(entries):
                if count >= _GROUP_SCAN_LIMIT:
                    return None
                if not entry.name.isdecimal() or int(entry.name) == pgid:
                    continue
                try:
                    fd = os.open(
                        f"/proc/{entry.name}/stat", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                    )
                    try:
                        raw = os.read(fd, _STAT_MAX_BYTES + 1)
                    finally:
                        os.close(fd)
                except FileNotFoundError:
                    continue
                if len(raw) > _STAT_MAX_BYTES:
                    return None
                fields = raw.rsplit(b")", 1)[1].split()
                if int(fields[2]) == pgid:
                    return True
        return False
    except (OSError, ValueError, IndexError):
        return None


def _group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


@dataclass(slots=True)
class _Running:
    ticket: CohortProcessOperation
    pid: int
    process: Any = field(repr=False)
    directory: str = field(repr=False)
    directory_fd: int = field(repr=False)
    reason: CohortProcessReason | None = None
    phase: CohortProcessPhase = CohortProcessPhase.RUNNING
    term_at: float | None = None
    kill_at: float | None = None
    reap_attempted: bool = False
    reaped: bool = False
    returncode: int | None = None


class CohortProcessSupervisor:
    """A single owner polls one exact local helper and retains uncertain cleanup."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic):
        self._owner = threading.current_thread()
        self._monotonic = monotonic
        self._last_clock: float | None = None
        self._running: _Running | None = None

    def _owned(self, ticket=None):
        if threading.current_thread() is not self._owner:
            _refuse(CohortProcessReason.WRONG_OWNER)
        if ticket is not None and (self._running is None or self._running.ticket is not ticket):
            _refuse(CohortProcessReason.STALE)

    def _clock(self):
        try:
            now = self._monotonic()
            if (
                type(now) not in (int, float)
                or not math.isfinite(now)
                or now < 0
                or self._last_clock is not None
                and now < self._last_clock
            ):
                _refuse(CohortProcessReason.CLOCK_FAILED)
            self._last_clock = float(now)
            return float(now)
        except Exception:
            _refuse(CohortProcessReason.CLOCK_FAILED)

    @property
    def outstanding(self):
        self._owned()
        return self._running.ticket if self._running else None

    @property
    def phase(self):
        self._owned()
        return self._running.phase if self._running else CohortProcessPhase.IDLE

    def start(self, payload: dict[str, Any], *, timeout_seconds: float) -> CohortProcessOperation:
        self._owned()
        if self._running is not None:
            _refuse(CohortProcessReason.BUSY)
        if sys.platform != "linux":
            _refuse(CohortProcessReason.PLATFORM_UNSUPPORTED)
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 600
        ):
            _refuse()
        operation_id = uuid.uuid4().hex
        encoded = encode_cohort_process_request(operation_id, payload)
        now = self._clock()
        directory, fd = None, None
        try:
            directory = tempfile.mkdtemp(prefix="django-ray-cohort-")
            fd = _directory_fd(directory)
            _write_file(fd, "request.json", encoded)
            process = subprocess.Popen(
                [sys.executable, "-m", _HELPER_MODULE, directory],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            if type(process.pid) is not int or process.pid <= 1:
                _refuse(CohortProcessReason.SPAWN_FAILED)
            ticket = CohortProcessOperation(operation_id, now + float(timeout_seconds))
            self._running = _Running(ticket, process.pid, process, directory, fd)
            return ticket
        except Exception:
            if fd is not None:
                try:
                    os.unlink("request.json", dir_fd=fd)
                except OSError:
                    pass
                os.close(fd)
            if directory is not None:
                try:
                    os.rmdir(directory)
                except OSError:
                    pass
            _refuse(CohortProcessReason.SPAWN_FAILED)

    def _signal(self, running, sig):
        if running.reap_attempted:
            running.phase = CohortProcessPhase.QUARANTINED
            return False
        try:
            # Another component may have reaped this child. Never signal a
            # numeric group after losing the unreaped direct-child identity.
            exited = os.waitid(os.P_PID, running.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is not None and exited.si_pid != running.pid:
                raise ChildProcessError(errno.ECHILD, "unowned child")
        except OSError as error:
            if error.errno == errno.ECHILD:
                running.reap_attempted = True
            running.phase = CohortProcessPhase.QUARANTINED
            return False
        try:
            os.killpg(running.pid, sig)
            return True
        except ProcessLookupError:
            return True
        except OSError:
            running.phase = CohortProcessPhase.QUARANTINED
            return False

    def _stop(self, running, reason, now):
        if running.reason is None:
            running.reason = reason
        if running.term_at is None:
            if self._signal(running, signal.SIGTERM):
                running.term_at = now
                running.phase = CohortProcessPhase.TERMINATING
        elif running.kill_at is None and now - running.term_at >= _GRACE_SECONDS:
            if self._signal(running, signal.SIGKILL):
                running.kill_at = now
                running.phase = CohortProcessPhase.KILLING
        elif running.kill_at is not None and now - running.kill_at >= _GRACE_SECONDS:
            running.phase = CohortProcessPhase.QUARANTINED

    def cancel(self, ticket: CohortProcessOperation):
        self._owned(ticket)
        running = self._running
        assert running is not None
        try:
            now = self._clock()
        except CohortProcessError:
            now = self._last_clock or 0.0
        self._stop(running, CohortProcessReason.CANCELLED, now)

    def _discard_directory(self, running):
        try:
            original = os.fstat(running.directory_fd)
            current = os.stat(running.directory, follow_symlinks=False)
            if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                return False
            for name in ("request.json", "response.json", "response.tmp"):
                try:
                    os.unlink(name, dir_fd=running.directory_fd)
                except FileNotFoundError:
                    pass
            os.rmdir(running.directory)
            os.close(running.directory_fd)
            return True
        except OSError:
            return False

    def poll(self, ticket: CohortProcessOperation) -> CohortProcessCompletion | None:
        self._owned(ticket)
        running = self._running
        assert running is not None
        try:
            now = self._clock()
        except CohortProcessError:
            now = self._last_clock or 0.0
            self._stop(running, CohortProcessReason.CLOCK_FAILED, now)
        if now >= ticket.deadline:
            self._stop(running, CohortProcessReason.DEADLINE, now)
        elif running.reason is not None:
            self._stop(running, running.reason, now)
        if not running.reap_attempted:
            try:
                exited = os.waitid(os.P_PID, running.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except OSError as error:
                if error.errno == errno.EINTR:
                    return None
                running.reason = running.reason or CohortProcessReason.OWNERSHIP_UNCERTAIN
                running.phase = CohortProcessPhase.QUARANTINED
                running.reap_attempted = True
                return None
            if exited is None:
                return None
            if exited.si_pid != running.pid:
                running.reason = running.reason or CohortProcessReason.OWNERSHIP_UNCERTAIN
                running.phase = CohortProcessPhase.QUARANTINED
                running.reap_attempted = True
                return None
            others = _other_group_members(running.pid)
            if others is None:
                running.reason = running.reason or CohortProcessReason.OWNERSHIP_UNCERTAIN
                running.phase = CohortProcessPhase.QUARANTINED
                return None
            if others:
                return None
            running.reap_attempted = True
            try:
                running.returncode = running.process.poll()
            except Exception:
                running.reason = running.reason or CohortProcessReason.OWNERSHIP_UNCERTAIN
                running.phase = CohortProcessPhase.QUARANTINED
                return None
            running.reaped = running.returncode is not None
        if not running.reaped or _group_exists(running.pid):
            running.reason = running.reason or CohortProcessReason.OWNERSHIP_UNCERTAIN
            running.phase = CohortProcessPhase.QUARANTINED
            return None
        response = None
        if running.reason is None:
            if running.returncode != 0:
                running.reason = CohortProcessReason.HELPER_FAILED
            else:
                try:
                    envelope = _decode_response(
                        _read_file(
                            running.directory_fd, "response.json", COHORT_PROCESS_RESPONSE_MAX_BYTES
                        ),
                        ticket.operation_id,
                    )
                    if envelope["outcome"] != "ok":
                        running.reason = CohortProcessReason.HELPER_FAILED
                    else:
                        response = envelope["payload"]
                except (CohortProcessError, OSError, ValueError, TypeError):
                    running.reason = CohortProcessReason.INVALID_RESPONSE
        if not self._discard_directory(running):
            running.reason = running.reason or CohortProcessReason.LOCAL_CLEANUP_FAILED
            running.phase = CohortProcessPhase.QUARANTINED
            return None
        # Bounded file decoding and local cleanup still consume the parent's
        # deadline. A response that becomes late during either is no success.
        try:
            if self._clock() >= ticket.deadline:
                running.reason = running.reason or CohortProcessReason.DEADLINE
        except CohortProcessError:
            running.reason = running.reason or CohortProcessReason.CLOCK_FAILED
        if running.reason is not None:
            response = None
        completion = CohortProcessCompletion(ticket.operation_id, running.reason, response)
        self._running = None
        return completion
