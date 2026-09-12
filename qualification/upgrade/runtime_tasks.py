"""Portable real application effects for the released/current runtime rehearsal.

The orchestrator creates the artifact directory and owns the gate. These tasks
never create durable task/claim rows or report upgrade acceptance. Exclusive
markers expose a duplicate invocation instead of making replay look successful.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from django.tasks import task

CASES = frozenset(
    {
        "old-success",
        "old-failure",
        "old-cancel",
        "old-retry",
        "old-gated",
        "current-core",
        "current-jobs",
    }
)
GATED_CASES = frozenset({"old-cancel", "old-gated", "current-jobs"})
GATE_TIMEOUT_SECONDS = 180
MAX_MARKER_BYTES = 4096
MAX_DRIVER_CONFIG_BYTES = 64 * 1024


class FixtureTerminalError(ValueError):
    """The isolated fixture settings deny retries of this deliberate failure."""


def _directory() -> Path:
    raw = os.environ.get("DJANGO_RAY_UPGRADE_ARTIFACT_ROOT", "")
    if not raw or len(raw) > 4096:
        raise FixtureTerminalError("upgrade-artifact-root-unavailable")
    root = Path(raw)
    directory = root / "runtime-effects"
    if (
        not root.is_absolute()
        or not root.is_dir()
        or root.resolve() != root
        or directory.is_symlink()
        or not directory.is_dir()
    ):
        raise FixtureTerminalError("upgrade-artifact-root-unavailable")
    return directory


def _validate_context(context) -> None:
    if (
        context is None
        or type(context.task_id) is not str
        or not 1 <= len(context.task_id) <= 128
        or not context.task_id.isascii()
        or any(ord(character) < 32 for character in context.task_id)
        or any(
            type(value) is not int or not 0 < value < 2**63
            for value in (context.task_pk, context.execution_generation)
        )
        or type(context.attempt_number) is not int
        or not 0 < context.attempt_number <= 2
    ):
        raise FixtureTerminalError("upgrade-native-context-unavailable")


def _native_job_id(ray) -> str:
    try:
        if ray.is_initialized() is not True:
            raise ValueError
        native_id = ray.get_runtime_context().get_job_id()
        if (
            type(native_id) is not str
            or re.fullmatch(r"[0-9a-f]{8}", native_id) is None
            or native_id == "0" * 8
        ):
            raise ValueError
    except Exception:
        raise FixtureTerminalError("upgrade-native-context-unavailable") from None
    return native_id


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _released_endpoint(context) -> str:
    """Bind the released supervisor's injected endpoint and submission identity."""
    address = os.environ.get("RAY_ADDRESS", "")
    config_json = os.environ.get("RAY_JOB_CONFIG_JSON_ENV_VAR", "")
    try:
        if (
            not 1 <= len(address) <= 260
            or re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", address) is None
            or not 1 <= len(config_json.encode("utf-8")) <= MAX_DRIVER_CONFIG_BYTES
        ):
            raise ValueError
        parsed = urlsplit("tcp://" + address)
        if not parsed.hostname or parsed.port is None or not 1 <= parsed.port <= 65535:
            raise ValueError
        config = json.loads(config_json, object_pairs_hook=_unique_object)
        if type(config) is not dict or type(config.get("metadata")) is not dict:
            raise ValueError
        original = {
            "attempt_number": context.attempt_number,
            "execution_generation": context.execution_generation,
            "task_execution_pk": context.task_pk,
            "task_id": context.task_id,
        }
        # Exact v0.4.0 RayJobRunner.submission_id algorithm, independent of the
        # current package's protocol3 request-reference implementation.
        digest = hashlib.sha256(
            json.dumps(original, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()
        submission_id = "raysubmit_django_ray_v1_" + digest
        expected = {
            "job_submission_id": submission_id,
            "job_name": submission_id,
            "django_ray_task_id": str(context.task_pk),
            "django_ray_attempt_number": str(context.attempt_number),
            "django_ray_execution_generation": str(context.execution_generation),
        }
        if any(config["metadata"].get(key) != value for key, value in expected.items()):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise FixtureTerminalError("upgrade-released-driver-unavailable") from None
    return address


def _disconnect_owned(ray, worker, core_worker, native_id) -> None:
    """Local disconnect only; the observer still owns exact Job terminal proof."""
    try:
        if (
            core_worker is None
            or ray._private.worker.global_worker is not worker
            or getattr(worker, "core_worker", None) is not core_worker
            or _native_job_id(ray) != native_id
        ):
            raise ValueError
        ray.shutdown()
        if ray.is_initialized() is not False or getattr(worker, "core_worker", None) is not None:
            raise ValueError
    except Exception:
        raise FixtureTerminalError("upgrade-released-cleanup-unconfirmed") from None


@contextmanager
def _released_connection(ray, context):
    address = _released_endpoint(context)
    worker = ray._private.worker.global_worker
    try:
        if ray.is_initialized() is not False or getattr(worker, "core_worker", None) is not None:
            raise ValueError
    except Exception:
        raise FixtureTerminalError("upgrade-released-driver-unavailable") from None
    core_worker = None
    native_id = None
    try:
        try:
            # Explicit injected GCS only: never auto-discover or create Ray.
            ray.init(address=address, log_to_driver=False)
            if ray._private.worker.global_worker is not worker:
                raise ValueError
            core_worker = getattr(worker, "core_worker", None)
            native_id = _native_job_id(ray)
            if core_worker is None:
                raise ValueError
        except Exception:
            raise FixtureTerminalError("upgrade-released-startup-unconfirmed") from None
        yield native_id
    finally:
        if core_worker is not None and native_id is not None:
            _disconnect_owned(ray, worker, core_worker, native_id)
        else:
            try:
                if (
                    ray.is_initialized() is not False
                    or getattr(worker, "core_worker", None) is not None
                ):
                    raise ValueError
            except Exception:
                # A partially initialized or replaced connection is not ours
                # to reinterpret as cleaned. No application marker was written.
                raise FixtureTerminalError("upgrade-released-cleanup-unconfirmed") from None


def _identity_value(context, native_id, package_version, ray_version) -> dict:
    return {
        "package_version": package_version,
        "ray_version": ray_version,
        "python": platform.python_version(),
        "implementation": platform.python_implementation().lower(),
        "task_pk": context.task_pk,
        "task_id": context.task_id,
        "attempt": context.attempt_number,
        "generation": context.execution_generation,
        "native_job_id": native_id,
        # The released context does not carry a protocol field. Its observer
        # verifies the actual released row rather than guessing this value.
        "context_protocol": getattr(context, "execution_protocol_version", None),
    }


@contextmanager
def _identity(case):
    import ray

    from django_ray import __version__
    from django_ray.runtime.context import get_current_task_context

    context = get_current_task_context()
    _validate_context(context)
    if case.startswith("old-"):
        if (
            __version__ != "0.4.0"
            or getattr(context, "ray_job_driver", None) is not True
            or hasattr(context, "execution_protocol_version")
        ):
            raise FixtureTerminalError("upgrade-released-driver-unavailable")
        with _released_connection(ray, context) as native_id:
            yield _identity_value(context, native_id, __version__, ray.__version__)
    else:
        protocol = getattr(context, "execution_protocol_version", None)
        if __version__ != "0.5.0" or type(protocol) is not int or protocol != 3:
            raise FixtureTerminalError("upgrade-native-context-unavailable")
        # The current fixed entrypoint/guard owns this connection. Never open
        # or close it from the application helper, even on application failure.
        yield _identity_value(context, _native_job_id(ray), __version__, ray.__version__)


def _publish(directory: Path, case: str, phase: str, identity: dict) -> None:
    data = json.dumps(
        {
            "schema": 1,
            "case": case,
            "phase": phase,
            "observed_at": datetime.now(UTC).isoformat(),
            "identity": identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(data) > MAX_MARKER_BYTES:
        raise FixtureTerminalError("upgrade-effect-record-too-large")
    path = directory / f"{case}.{identity['attempt']}.{phase}.json"
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise FixtureTerminalError("upgrade-duplicate-application-effect") from None


@contextmanager
def _begin(case: str):
    if type(case) is not str or case not in CASES:
        raise FixtureTerminalError("unsupported-upgrade-case")
    directory = _directory()
    with _identity(case) as identity:
        _publish(directory, case, "started", identity)
        yield directory, identity


def workflow_increment(value: int) -> int:
    """One importable native leaf, with ordinary best-effort progress."""
    from django_ray.workflows import report_progress

    report_progress(1, 1, message="upgrade-increment-complete")
    return value + 1


def workflow_double(value: int) -> int:
    """The dependent second leaf; it does not own the outer connection."""
    from django_ray.workflows import report_progress

    report_progress(1, 1, message="upgrade-double-complete")
    return value * 2


@task(backend="default", queue_name="upgrade-core")
def value(case: str, payload: str = "42") -> dict:
    """Run two real leaves and retain bounded input/result bytes for backup reads."""
    if (
        type(case) is not str
        or case not in {"old-success", "current-core"}
        or type(payload) is not str
    ):
        raise FixtureTerminalError("unsupported-upgrade-value")
    if len(payload.encode("utf-8")) > 32768:
        raise FixtureTerminalError("upgrade-value-too-large")
    with _begin(case) as (directory, identity):
        from django_ray.workflows import chain, step

        # Keep the original Jobs connection/current guard alive through both
        # leaves. Default reporting preserves genuine legacy progress; no
        # experimental publication mode or synthetic history is enabled here.
        workflow_result = (
            chain(
                step(
                    workflow_increment,
                    django=True,
                    ray_options={"num_cpus": 0.1, "max_retries": 0},
                ),
                step(
                    workflow_double,
                    django=True,
                    ray_options={"num_cpus": 0.1, "max_retries": 0},
                ),
            )
            .with_progress_reporting("full")
            .run(20, use_ray=True)
        )
        if type(workflow_result) is not int or workflow_result != 42:
            raise FixtureTerminalError("upgrade-workflow-result-mismatch")
        _publish(directory, case, "committed", identity)
        return {"identity": identity, "payload": payload, "workflow_result": workflow_result}


@task(backend="default", queue_name="upgrade-core")
def gated_effect(case: str) -> dict:
    """Keep the original native Job alive across one manager replacement."""
    if type(case) is not str or case not in GATED_CASES:
        raise FixtureTerminalError("unsupported-upgrade-gate")
    with _begin(case) as (directory, identity):
        gate = directory / f"{case}.release"
        deadline = time.monotonic() + GATE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                # Only the source-owned orchestrator writes this immutable regular
                # file. Reject special files before opening so a FIFO cannot defeat
                # the polling deadline.
                if not stat.S_ISREG(gate.lstat().st_mode):
                    raise FixtureTerminalError("invalid-upgrade-gate")
                with gate.open("rb") as stream:
                    released = stream.read(9)
            except FileNotFoundError:
                time.sleep(0.1)
                continue
            if released != b"release\n":
                raise FixtureTerminalError("invalid-upgrade-gate")
            if time.monotonic() >= deadline:
                raise FixtureTerminalError("upgrade-gate-timeout")
            _publish(directory, case, "committed", identity)
            return identity
        raise FixtureTerminalError("upgrade-gate-timeout")


@task(backend="default", queue_name="upgrade-core")
def failed() -> None:
    """Produce a genuine terminal failure under the fixture retry denylist."""
    with _begin("old-failure"):
        raise FixtureTerminalError("deliberate-upgrade-application-failure")


@task(backend="default", queue_name="upgrade-core")
def retried() -> dict:
    """Require the old manager to perform one actual failed-to-retry transition."""
    with _begin("old-retry") as (directory, identity):
        if identity["attempt"] == 1:
            raise ValueError("deliberate-upgrade-first-attempt-failure")
        _publish(directory, "old-retry", "committed", identity)
        return identity
