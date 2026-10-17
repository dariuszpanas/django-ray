"""Inspect bounded visible argv from inside the fixed, live rq2 driver."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

MAX_PROCESSES = 256
MAX_CMDLINE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
SCAN_SECONDS = 2
REQUEST_ROOT = "/rq2/requests"
FACTS = ("live_driver_bound", "driver_argv_clear", "visible_argv_clear")


def _read(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError("Process observation exceeded its byte bound")
    return value


def _identity(path: Path) -> tuple[int, str]:
    # Linux comm may contain spaces and parentheses. starttime is field 22.
    raw = _read(path / "stat", 4096).decode()
    fields = raw[raw.rfind(")") + 2 :].split()
    if len(fields) < 20 or not fields[19].isdigit():
        raise ValueError("Process observation has no stable identity")
    return int(path.name), fields[19]


def scan_visible_argv(
    *,
    driver_pid: int,
    expected_argv: list[str],
    forbidden: list[str],
    proc_root: Path = Path("/proc"),
) -> dict:
    """No argv leaves this function; absence is meaningful only with the live driver."""
    deadline = time.monotonic() + SCAN_SECONDS
    if not forbidden or any(not isinstance(value, str) or not value for value in forbidden):
        raise ValueError("Process observation has no protected values")
    if len(forbidden) > 128 or sum(len(value.encode()) for value in forbidden) > MAX_TOTAL_BYTES:
        raise ValueError("Process protected values exceed their bound")
    needles = [value.encode() for value in forbidden]
    paths = []
    for path in proc_root.iterdir():
        if time.monotonic() >= deadline:
            raise TimeoutError("Process observation deadline expired")
        if path.name.isascii() and path.name.isdecimal():
            paths.append(path)
            if len(paths) > MAX_PROCESSES:
                raise ValueError("Process observation exceeded its process bound")
    total = 0
    driver_seen = False
    for path in paths:
        if time.monotonic() >= deadline:
            raise TimeoutError("Process observation deadline expired")
        try:
            before = _identity(path)
            argv = _read(path / "cmdline", MAX_CMDLINE_BYTES)
            after = _identity(path)
        except FileNotFoundError:
            # Only a vanished non-driver PID is an allowed concurrent exit.
            if int(path.name) == driver_pid or path.exists():
                raise ValueError("Process observation lost a required identity") from None
            continue
        if before != after:
            raise ValueError("Process identity changed during argv observation")
        total += len(argv)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Process observation exceeded its aggregate bound")
        if any(value in argv for value in needles):
            raise ValueError("Protected process observation failed")
        if int(path.name) == driver_pid:
            if argv != b"\0".join(value.encode() for value in expected_argv) + b"\0":
                raise ValueError("Live driver does not use the bound request carrier")
            driver_seen = True
    if not driver_seen or time.monotonic() >= deadline:
        raise ValueError("Process observation did not retain its live driver")
    return dict.fromkeys(FACTS, True)


def observe_driver(marker: str) -> dict:
    """Bind actual Linux argv to the strict task context and canonical request."""
    from django_ray.execution_codec import ExecutionIdentity
    from django_ray.models import RayTaskExecution
    from django_ray.ray_job_request_storage import (
        decode_ray_job_request_locator,
        load_ray_job_request,
    )
    from django_ray.runtime.context import get_current_task_context

    context = get_current_task_context()
    if context is None or not context.ray_job_driver or not context.strict_execution_request:
        raise ValueError("Process observation requires the live strict Job driver")
    row = RayTaskExecution.objects.get(pk=context.task_pk)
    identity = ExecutionIdentity(row.pk, row.task_id, row.attempt_number, row.execution_generation)
    if (context.task_id, context.attempt_number, context.execution_generation) != (
        row.task_id,
        row.attempt_number,
        row.execution_generation,
    ):
        raise ValueError("Process observation has a stale execution context")
    actual = _read(Path("/proc/self/cmdline"), MAX_CMDLINE_BYTES).split(b"\0")
    if actual[-1:] != [b""]:
        raise ValueError("Live driver argv is incomplete")
    argv = [part.decode() for part in actual[:-1]]
    if len(argv) != 5 or argv[1:4] != ["-m", "django_ray.runtime.entrypoint", "--request-ref-b64"]:
        raise ValueError("Live driver argv has an unexpected carrier")
    locator = decode_ray_job_request_locator(argv[4])
    loaded = load_ray_job_request(locator)
    request = loaded.request
    if (
        locator.filesystem_path != REQUEST_ROOT
        or locator.reference != row.ray_job_request_reference
        or request.identity != identity
        or request.callable_path != row.callable_path
        or request.serialized_args != row.args_json
        or request.serialized_kwargs != row.kwargs_json
    ):
        raise ValueError("Live process carrier differs from its durable request")
    forbidden = [row.task_id, row.callable_path, row.args_json, marker, loaded.serialized_request]
    forbidden += [
        value
        for key, value in os.environ.items()
        if value
        and any(
            word in key.upper()
            for word in ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "ENCRYPTION_KEY")
        )
    ]
    return scan_visible_argv(driver_pid=os.getpid(), expected_argv=argv, forbidden=forbidden)


def validate(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != set(FACTS)
        or any(value[k] is not True for k in FACTS)
    ):
        raise ValueError("Live process observation is incomplete")


def read_receipt(path: Path) -> dict:
    value = json.loads(_read(path, 512))
    validate(value)
    return value
