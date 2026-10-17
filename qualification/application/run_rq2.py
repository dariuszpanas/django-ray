"""Qualify two request-reference Jobs on the existing owned KubeRay generation."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

LAYER = "application_rq2"
BUDGET_SECONDS = 180
WORK_SECONDS = 170
CLEANUP_SECONDS = 5
MAX_OBSERVATION_BYTES = 128 * 1024
QUEUE = "rq2-qualification"
ADDRESS = "http://ray-head:8265"
REQUEST_ROOT = "/rq2/requests"
REFUSAL_LINE = "django-ray task failed: execution request rejected: invalid_versioned"
FACTS = (
    "wrapped_reference_and_registry_bound",
    "carrier_and_metadata_bound",
    "job_info_and_logs_clear",
    "live_driver_and_visible_argv_clear",
    "manager_killed_and_replaced",
    "same_job_attempt_generation_reference",
    "one_submission_and_effect",
    "missing_request_exit_78_before_setup_and_effect",
    "missing_request_terminal_without_retry",
    "owned_manager_stopped",
)
PHASES = frozenset(
    {
        "preflight",
        "django_bootstrap",
        "fixture",
        "client",
        "enqueue",
        "manager_start",
        "initial_effect",
        "initial_inspect",
        "manager_crash",
        "manager_replacement",
        "adoption",
        "completion",
        "completion_cleanup",
        "missing_enqueue",
        "missing_request",
        "missing_observation",
        "missing_preflight",
        "reconciliation_manager",
        "reconciliation",
        "cleanup",
        "receipt",
    }
)
ERROR_TYPES = frozenset(
    {"none", "timeout", "validation", "os_error", "process_error", "unexpected"}
)
MANAGER_STATES = frozenset(
    {
        "not_started",
        "not_observed",
        "running",
        "exited_zero",
        "exited_nonzero",
        "signalled",
        "unavailable",
    }
)
START_FIELDS = {
    "task_state": frozenset(
        {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "CANCELLING", "LOST", "EXPIRED"}
    ),
    "manager_lease": frozenset({"absent", "active", "inactive", "ambiguous"}),
    "claim_owner": frozenset({"unclaimed", "owned_manager", "other"}),
    "request_binding": frozenset({"absent", "present"}),
    "submission_binding": frozenset({"absent", "present"}),
    "task_error": frozenset({"absent", "present"}),
    "task_error_origin": frozenset(
        {
            "absent",
            "other",
            "reservation_failed",
            "submission_failed",
            "prep_invalid_request",
            "prep_resource_limit",
            "prep_configuration",
            "prep_storage_unavailable",
            "prep_integrity_mismatch",
            "prep_registry_mismatch",
            "prep_binding_mismatch",
        }
    ),
    "job_status": frozenset(
        {"not_submitted", "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "STOPPED"}
    ),
    "job_error": frozenset(
        {
            "none",
            "RUNTIME_ENV_SETUP_FAILURE",
            "JOB_SUPERVISOR_ACTOR_START_TIMEOUT",
            "JOB_SUPERVISOR_ACTOR_START_FAILURE",
            "JOB_SUPERVISOR_ACTOR_UNSCHEDULABLE",
            "JOB_SUPERVISOR_ACTOR_UNKNOWN_FAILURE",
            "JOB_SUPERVISOR_ACTOR_DIED",
            "JOB_ENTRYPOINT_COMMAND_START_ERROR",
            "JOB_ENTRYPOINT_COMMAND_ERROR",
        }
    ),
    "driver_exit": frozenset({"not_exited", "zero", "preflight_refusal", "nonzero"}),
}


def _error_type(error: Exception) -> str:
    # Never render the exception, its class name, arguments or nested causes.
    if isinstance(error, TimeoutError | subprocess.TimeoutExpired):
        return "timeout"
    if isinstance(error, ValueError):
        return "validation"
    if isinstance(error, OSError):
        return "os_error"
    if isinstance(error, subprocess.SubprocessError):
        return "process_error"
    return "unexpected"


def _manager_state(process) -> str:
    if process is None:
        return "not_started"
    try:
        code = process.poll()
    except Exception:
        return "unavailable"
    if code is None:
        return "running"
    if type(code) is not int:
        return "unavailable"
    if code == 0:
        return "exited_zero"
    return "signalled" if code < 0 else "exited_nonzero"


class Diagnostics:
    """Fixed labels only; retain the first failure before attempting cleanup."""

    def __init__(self):
        self.work_deadline = time.monotonic() + WORK_SECONDS
        self.phase = "preflight"
        self.failure_phase = "none"
        self.failure_type = "none"
        self.manager_at_failure = "not_observed"
        self.cleanup_failure_type = "none"
        self.manager_after_cleanup = "not_observed"
        self.start_observation = dict.fromkeys(START_FIELDS, "not_observed")

    def starting(self, **fields: str) -> None:
        for key, value in fields.items():
            if key not in START_FIELDS:
                raise ValueError("Unknown startup observation field")
            self.start_observation[key] = value

    def enter(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError("Unknown request-reference observation phase")
        self.phase = phase

    def failed(self, error: Exception, process=None) -> None:
        if self.failure_type == "none":
            self.failure_phase = self.phase
            self.failure_type = _error_type(error)
            self.manager_at_failure = _manager_state(process)

    def cleanup_failed(self, error: Exception, process) -> None:
        self.failed(error, process)
        self.cleanup_failure_type = _error_type(error)
        self.manager_after_cleanup = _manager_state(process)

    def snapshot(self) -> dict:
        # Whitelist at the serialization boundary as well as at assignment.
        fields = {
            "phase": (self.phase, PHASES, "preflight"),
            "failure_phase": (self.failure_phase, PHASES | {"none"}, "preflight"),
            "failure_type": (self.failure_type, ERROR_TYPES, "unexpected"),
            "manager_at_failure": (self.manager_at_failure, MANAGER_STATES, "unavailable"),
            "cleanup_failure_type": (self.cleanup_failure_type, ERROR_TYPES, "unexpected"),
            "manager_after_cleanup": (self.manager_after_cleanup, MANAGER_STATES, "unavailable"),
        }
        result = {
            key: value if type(value) is str and value in allowed else fallback
            for key, (value, allowed, fallback) in fields.items()
        }
        startup = self.start_observation if isinstance(self.start_observation, dict) else {}
        result["start_observation"] = {
            key: value
            if type(value) is str and value in allowed | {"not_observed", "unavailable"}
            else "unavailable"
            for key, allowed in START_FIELDS.items()
            for value in [startup.get(key)]
        }
        return result


def observe_start(pk: int, manager, client, diagnostics: Diagnostics) -> None:
    """Inspect one owned row and at most two leases; retain no IDs or error text."""
    from django.db.models import Case, Value, When

    from django_ray.models import RayTaskExecution, TaskWorkerLease
    from django_ray.runner.errors import (
        RayJobRequestPreparationError,
        RayJobRequestPreparationRejection,
    )
    from django_ray.runner.ray_job import _bounded_control_requests

    # Fetch only a fixed presence category for the potentially unbounded error.
    row = (
        RayTaskExecution.objects.filter(pk=pk)
        .annotate(
            observed_error=Case(
                When(error_message__isnull=True, then=Value("absent")),
                When(error_message="", then=Value("absent")),
                default=Value("present"),
            ),
            observed_error_origin=Case(
                When(error_message__isnull=True, then=Value("absent")),
                When(error_message="", then=Value("absent")),
                *(
                    When(
                        error_message=str(RayJobRequestPreparationError(kind)),
                        then=Value("prep_" + kind.value),
                    )
                    for kind in RayJobRequestPreparationRejection
                ),
                When(
                    error_message__startswith="Failed to reserve Ray Job submission identity: ",
                    then=Value("reservation_failed"),
                ),
                When(
                    error_message__startswith="Failed to submit to Ray: ",
                    then=Value("submission_failed"),
                ),
                default=Value("other"),
            ),
        )
        .values(
            "state",
            "claimed_by_worker",
            "ray_job_id",
            "ray_job_request_reference",
            "observed_error",
            "observed_error_origin",
        )
        .get()
    )
    diagnostics.starting(
        task_state=row["state"],
        claim_owner="other" if row["claimed_by_worker"] else "unclaimed",
        request_binding="present" if row["ray_job_request_reference"] else "absent",
        submission_binding="present" if row["ray_job_id"] else "absent",
        task_error=row["observed_error"],
        task_error_origin=row["observed_error_origin"],
        manager_lease="unavailable",
        job_status="unavailable" if row["ray_job_id"] else "not_submitted",
        job_error="not_observed",
        driver_exit="not_observed",
    )
    leases = list(
        TaskWorkerLease.objects.filter(pid=manager.pid, queue_name=QUEUE).values(
            "worker_id", "is_active"
        )[:2]
    )
    diagnostics.starting(
        manager_lease=(
            "absent"
            if not leases
            else "ambiguous"
            if len(leases) != 1
            else "active"
            if leases[0]["is_active"]
            else "inactive"
        )
    )
    if len(leases) == 1 and row["claimed_by_worker"] == leases[0]["worker_id"]:
        diagnostics.starting(claim_owner="owned_manager")
    if row["ray_job_id"]:
        # The process alarm bounds the entire observation. Each control request
        # additionally retains the production five-second socket timeout.
        try:
            with _bounded_control_requests(client):
                info = client.get_job_info(row["ray_job_id"])
            error = getattr(info, "error_type", None)
            code = getattr(info, "driver_exit_code", None)
            diagnostics.starting(
                job_status=str(info.status),
                job_error="none" if error is None else getattr(error, "value", error),
                driver_exit=(
                    "not_exited"
                    if code is None
                    else "unavailable"
                    if type(code) is not int
                    else "zero"
                    if code == 0
                    else "preflight_refusal"
                    if code == 78
                    else "nonzero"
                ),
            )
        except TimeoutError:
            raise
        except Exception:
            # Reservation can precede remote acceptance. Preserve unavailable,
            # never pretend an unreadable Job is absent or disclose its error.
            pass
    if row["state"] not in {"QUEUED", "RUNNING"}:
        raise ValueError("The owned task terminated before its initial effect")


def validate_receipt(value: object) -> None:
    if (
        not isinstance(value, dict)
        or value.get("generation") not in {"before", "after"}
        or value.get("budget_seconds") != BUDGET_SECONDS
        or type(value.get("budget_seconds")) is not int
        or not isinstance(value.get("assertions"), dict)
        or set(value["assertions"]) != set(FACTS)
        or any(item is not True for item in value["assertions"].values())
    ):
        raise ValueError("Incomplete request-reference qualification receipt")


def identity(row) -> tuple:
    return (
        row.pk,
        row.task_id,
        row.ray_job_id,
        row.attempt_number,
        row.execution_generation,
        row.execution_protocol_version,
        row.ray_job_request_reference,
    )


def one_effect(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("rb") as stream:
        observed = stream.read(2)
    if observed not in {b"", b"x"}:
        raise ValueError("The owned callable produced more than one effect")
    return observed == b"x"


def _raw_json(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    if len(encoded.encode()) > MAX_OBSERVATION_BYTES:
        raise ValueError("Jobs observation exceeds its fixed byte bound")
    return encoded


def inspect_job(row, client, marker: str, *, missing: bool = False):
    """Check public surfaces and persisted binding without returning raw data."""
    from django_ray.execution_codec import ExecutionIdentity
    from django_ray.models import TaskInputPayload
    from django_ray.ray_job_protocol import (
        RayJobRequestReferenceExpectation,
        parse_ray_job_request_metadata,
        validate_ray_job_request_reference_expectation,
    )
    from django_ray.ray_job_request_storage import (
        decode_ray_job_request_locator,
        load_ray_job_request,
        ray_job_request_reference_content_identity,
    )
    from django_ray.runner.ray_job import _bounded_control_requests

    reference = row.ray_job_request_reference
    if not isinstance(reference, str) or not reference.startswith("rq2ref:v1:"):
        raise ValueError("New Jobs request is not a typed reference")
    if len(reference.encode("ascii")) > 500 or row.ray_address != ADDRESS:
        raise ValueError("Jobs request identity escaped the fixture")
    digest, size = ray_job_request_reference_content_identity(reference)
    registry = TaskInputPayload.objects.get(reference=reference)
    if (
        registry.payload_kind != "ray_job_request"
        or registry.state != "ACTIVE"
        or registry.backend != "filesystem"
        or registry.digest != digest
        or registry.size_bytes != size
    ):
        raise ValueError("Jobs request registry differs from its durable reference")
    with _bounded_control_requests(client):
        info = client.get_job_info(row.ray_job_id)
        logs = client.get_job_logs(row.ray_job_id) or ""
        jobs = client.list_jobs()
    if not isinstance(logs, str) or len(logs.encode()) > 65536 or len(jobs) > 64:
        raise ValueError("Jobs public observation exceeds its fixed bound")
    if sum(getattr(job, "submission_id", None) == row.ray_job_id for job in jobs) != 1:
        raise ValueError("The durable execution does not have exactly one Ray Job")
    parts = shlex.split(info.entrypoint)
    if len(parts) != 5 or parts[:4] != [
        "python",
        "-m",
        "django_ray.runtime.entrypoint",
        "--request-ref-b64",
    ]:
        raise ValueError("Jobs entrypoint is not the bounded request-reference carrier")
    locator = decode_ray_job_request_locator(parts[4])
    if locator.reference != reference or locator.filesystem_path != REQUEST_ROOT:
        raise ValueError("Jobs locator differs from its owned persisted request")
    loaded = None if missing else load_ray_job_request(locator)
    expectation = parse_ray_job_request_metadata(info.metadata, required=True)
    if not isinstance(expectation, RayJobRequestReferenceExpectation):
        raise ValueError("Jobs metadata is not a request-reference binding")
    expected_identity = ExecutionIdentity(
        task_execution_pk=row.pk,
        task_id=row.task_id,
        attempt_number=row.attempt_number,
        execution_generation=row.execution_generation,
    )
    validate_ray_job_request_reference_expectation(
        expectation,
        expected_identity=expected_identity,
        expected_execution_protocol_version=row.execution_protocol_version,
        expected_request_sha256=digest,
        expected_request_size_bytes=size,
        expected_submission_id=row.ray_job_id,
        serialized_request=loaded.serialized_request if loaded else None,
        request_reference=reference,
        request_locator=parts[4],
    )
    # Stable submission IDs alone cannot exclude a second Job carrying the
    # same request. Check the bounded namespace-owned inventory by all three
    # independently meaningful identity/content bindings as well.
    for key in (
        "django_ray_coordination_sha256",
        "django_ray_request_sha256",
        "django_ray_request_reference_sha256",
    ):
        if (
            sum(
                (getattr(job, "metadata", None) or {}).get(key) == info.metadata[key]
                for job in jobs
            )
            != 1
        ):
            raise ValueError("The request binding appears in another Ray Job")
    if loaded and (
        loaded.request.identity != expected_identity
        or loaded.request.callable_path != row.callable_path
        or loaded.request.serialized_args != row.args_json
        or loaded.request.serialized_kwargs != row.kwargs_json
    ):
        raise ValueError("Stored request does not match the actual execution")
    payload = info.model_dump() if hasattr(info, "model_dump") else vars(info)
    raw_info = _raw_json(payload)
    forbidden = [row.task_id, row.callable_path, row.args_json, marker]
    if loaded:
        forbidden.append(loaded.serialized_request)
    forbidden += [
        value
        for key, value in os.environ.items()
        if value
        and any(
            word in key.upper()
            for word in ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "ENCRYPTION_KEY")
        )
    ]
    if any(value in surface for value in forbidden for surface in (raw_info, logs)):
        raise ValueError("Jobs public surface contains application data")
    if missing and str(info.status) == "FAILED":
        if info.driver_exit_code != 78:
            raise ValueError("Missing request did not use the preflight refusal exit code")
        fixed = [line for line in logs.splitlines() if line.startswith("django-ray task ")]
        if fixed != [REFUSAL_LINE]:
            raise ValueError("Missing request did not emit the fixed preflight refusal")
    return str(info.status)


def stop_manager(process, *, crash: bool = False) -> None:
    """Stop only our unreaped child; the generation owns its process group."""
    if process.returncode is not None:
        if crash:
            raise ValueError("Manager exited before the deliberate replacement")
        return
    if crash:
        process.kill()
    else:
        process.terminate()
    try:
        code = process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        code = process.wait(timeout=2)
    if crash and code != -signal.SIGKILL:
        raise ValueError("Manager replacement did not observe SIGKILL")


def _start_manager():
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "django",
            "django_ray_worker",
            "--queue",
            QUEUE,
            "--concurrency",
            "1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def missing_submission(row, client, *, generation: str):
    """Prepare with production code, delete only its typed object, submit once."""
    from django.utils import timezone

    from django_ray.ray_job_request_storage import delete_ray_job_request_reference
    from django_ray.runner.ray_job import RayJobRunner, _bounded_control_requests

    runner = RayJobRunner()
    row.state = "RUNNING"
    row.execution_generation = 1
    row.claimed_by_worker = "owned-rq2-missing-request"
    row.started_at = row.last_heartbeat_at = timezone.now()
    handle = runner.submission_handle(row)
    row.ray_job_id, row.ray_address = handle.ray_job_id, handle.ray_address
    row.save(
        update_fields=[
            "state",
            "execution_generation",
            "claimed_by_worker",
            "started_at",
            "last_heartbeat_at",
            "ray_job_id",
            "ray_address",
        ]
    )
    captured = {}

    class CaptureClient:
        def _upload_working_dir_if_needed(self, runtime_env):
            return client._upload_working_dir_if_needed(runtime_env)

        def _upload_py_modules_if_needed(self, runtime_env):
            return client._upload_py_modules_if_needed(runtime_env)

        def submit_job(self, **kwargs):
            captured.update(copy.deepcopy(kwargs))
            return kwargs["submission_id"]

    runner._get_client = lambda _address=None: CaptureClient()
    submitted = runner.submit_durable(row)
    row.refresh_from_db()
    if submitted.ray_job_id != handle.ray_job_id or not row.ray_job_request_reference:
        raise ValueError("Production preparation changed the owned missing request")
    expected_environment = {
        "DJANGO_SETTINGS_MODULE": "testproject.settings_qualification_jobs",
        "DJANGO_RAY_QUALIFICATION_RQ2": "owned-application-job",
        "DJANGO_RAY_QUALIFICATION_RQ2_GENERATION": generation,
        "DJANGO_RAY_QUALIFICATION_RQ2_SETUP_MARKER": "missing",
    }
    environment = captured.get("runtime_env", {}).get("env_vars", {})
    if generation not in {"before", "after"} or any(
        environment.get(key) != value for key, value in expected_environment.items()
    ):
        raise ValueError("The missing-request settings sentinel is not armed")
    delete_ray_job_request_reference(row.ray_job_request_reference)
    with _bounded_control_requests(client):
        if client.submit_job(**captured) != handle.ray_job_id:
            raise ValueError("Ray changed the owned submission identity")


def verify(generation: str, facts: dict, diagnostics: Diagnostics) -> None:
    diagnostics.enter("django_bootstrap")
    import django

    django.setup()
    from django.conf import settings
    from django.db import connection

    from django_ray import __version__
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease
    from django_ray.runner.ray_job import _address_pinned_job_client
    from testproject.qualification_rq2_tasks import controlled

    diagnostics.enter("fixture")
    if connection.vendor != "postgresql" or settings.RQ2_GENERATION != generation:
        raise ValueError("Jobs qualification requires its owned PostgreSQL generation")
    root = settings.RQ2_ROOT
    root.mkdir(exist_ok=False)
    deadline = diagnostics.work_deadline
    manager = None

    def wait(predicate, *, require_manager=True):
        while time.monotonic() < deadline:
            if require_manager and (manager is None or manager.poll() is not None):
                raise ValueError("Owned Jobs manager exited during observation")
            if value := predicate():
                return value
            time.sleep(0.2)
        raise TimeoutError("Jobs qualification exhausted its fixed observation budget")

    def get_row(pk):
        return RayTaskExecution.objects.get(pk=pk)

    def lease(row):
        return TaskWorkerLease.objects.get(worker_id=row.claimed_by_worker)

    def enqueue(case):
        marker = uuid4().hex
        result = controlled.using(
            backend=QUEUE if case == "success" else "rq2-missing", queue_name=QUEUE
        ).enqueue(case, marker)
        return RayTaskExecution.objects.get(task_id=result.id), marker

    try:
        diagnostics.enter("client")
        client = _address_pinned_job_client(ADDRESS)
        diagnostics.enter("enqueue")
        success, success_marker = enqueue("success")
        diagnostics.enter("manager_start")
        manager = _start_manager()
        diagnostics.enter("initial_effect")
        next_observation = 0.0

        def initial_effect():
            nonlocal next_observation
            if one_effect(root / "success-effects"):
                return True
            if time.monotonic() >= next_observation:
                observe_start(success.pk, manager, client, diagnostics)
                next_observation = time.monotonic() + 2
            return False

        wait(initial_effect)
        diagnostics.enter("initial_inspect")
        success = get_row(success.pk)
        original = identity(success)
        owner = lease(success)
        if (
            success.state != "RUNNING"
            or original[3:6] != (1, 1, 1)
            or not owner.is_active
            or owner.pid != manager.pid
            or owner.django_ray_version != __version__
            or not one_effect(root / "success-effects")
            or inspect_job(success, client, success_marker) != "RUNNING"
        ):
            raise ValueError("The owned request did not start once on the current manager")
        facts["wrapped_reference_and_registry_bound"] = True
        facts["carrier_and_metadata_bound"] = True
        facts["job_info_and_logs_clear"] = True
        from testproject.qualification_process import read_receipt

        read_receipt(root / "success-process.json")
        facts["live_driver_and_visible_argv_clear"] = True
        diagnostics.enter("manager_crash")
        stop_manager(manager, crash=True)
        if identity(get_row(success.pk)) != original:
            raise ValueError("Killing the manager changed the durable execution identity")
        diagnostics.enter("manager_replacement")
        manager = _start_manager()
        diagnostics.enter("adoption")
        adopted = wait(
            lambda: (
                RayTaskExecution.objects.filter(pk=success.pk, state="RUNNING")
                .exclude(claimed_by_worker=owner.worker_id)
                .exclude(claimed_by_worker__isnull=True)
                .first()
            )
        )
        replacement = lease(adopted)
        if (
            identity(adopted) != original
            or not replacement.is_active
            or replacement.pid != manager.pid
            or replacement.django_ray_version != __version__
            or inspect_job(adopted, client, success_marker) != "RUNNING"
        ):
            raise ValueError("Replacement manager did not adopt the exact live request")
        facts["manager_killed_and_replaced"] = True
        facts["same_job_attempt_generation_reference"] = True
        diagnostics.enter("completion")
        (root / "success-release").touch(exist_ok=False)
        wait(lambda: get_row(success.pk).state == "SUCCEEDED")
        success = get_row(success.pk)
        if (
            identity(success) != original
            or json.loads(success.result_data) != {"value": 42, "effect": "once"}
            or success.executor_django_ray_version != __version__
            or TaskAttempt.objects.filter(execution=success).count() != 1
            or not one_effect(root / "success-effects")
        ):
            raise ValueError("The adopted request did not complete exactly once")
        wait(lambda: inspect_job(success, client, success_marker) == "SUCCEEDED")
        facts["one_submission_and_effect"] = True
        diagnostics.enter("completion_cleanup")
        stop_manager(manager)
        manager = None
        diagnostics.enter("missing_enqueue")
        missing, marker = enqueue("missing")
        diagnostics.enter("missing_request")
        missing_submission(missing, client, generation=generation)
        missing.refresh_from_db()
        missing_identity = identity(missing)
        diagnostics.enter("missing_observation")
        wait(
            lambda: inspect_job(missing, client, marker, missing=True) == "FAILED",
            require_manager=False,
        )
        diagnostics.enter("missing_preflight")
        if (root / "missing-effects").exists() or (root / "missing-setup").exists():
            raise ValueError("Missing request reached Django setup or the callable")
        facts["missing_request_exit_78_before_setup_and_effect"] = True
        diagnostics.enter("reconciliation_manager")
        manager = _start_manager()
        diagnostics.enter("reconciliation")
        wait(lambda: get_row(missing.pk).state == "FAILED")
        # Observe at least two worker heartbeat periods with retries enabled.
        observed_until = time.monotonic() + 4

        def final_observation():
            row = get_row(missing.pk)
            if (
                identity(row) != missing_identity
                or row.state != "FAILED"
                or row.run_after is not None
                or row.completion_data is not None
                or row.result_data is not None
                or row.result_reference is not None
                or row.error_message
                != "Strict Ray Job terminated without an exact completion envelope"
                or TaskAttempt.objects.filter(execution=row, state="FAILED").count() != 1
                or TaskAttempt.objects.filter(execution=row).count() != 1
                or (root / "missing-effects").exists()
                or (root / "missing-setup").exists()
                or not one_effect(root / "success-effects")
                or inspect_job(row, client, marker, missing=True) != "FAILED"
            ):
                raise ValueError("Missing request was replayed or lost its terminal fence")
            return time.monotonic() >= observed_until

        wait(final_observation)
        facts["missing_request_terminal_without_retry"] = True
    except Exception as error:
        diagnostics.failed(error, manager)
        raise
    finally:
        diagnostics.enter("cleanup")
        # The work alarm can fire inside any bootstrap, database or SDK call.
        # Reset it only for the final shutdown, never for more observations.
        # Work uses the original process deadline, so startup cannot borrow
        # this five-second reserve. The parent retains its 180-second bound.
        signal.alarm(CLEANUP_SECONDS)
        try:
            if manager is not None:
                stop_manager(manager)
            if time.monotonic() > deadline + CLEANUP_SECONDS:
                raise TimeoutError("Owned manager cleanup exceeded its reserved time")
        except Exception as error:
            diagnostics.cleanup_failed(error, manager)
            raise
        diagnostics.manager_after_cleanup = _manager_state(manager)
    facts["owned_manager_stopped"] = True


def _terminated(_signum, _frame):
    raise TimeoutError("Owned request-reference qualification was interrupted")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", required=True, choices=("before", "after"))
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(argv)
    diagnostics = Diagnostics()
    receipt = {
        "schema_version": 1,
        "layer": LAYER,
        "status": "failed",
        "complete_application_gate": False,
        "generation": args.generation,
        "budget_seconds": BUDGET_SECONDS,
        "assertions": dict.fromkeys(FACTS, False),
    }
    if (
        sys.platform != "linux"
        or os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification"
    ):
        diagnostics.failed(ValueError())
        receipt["diagnostics"] = diagnostics.snapshot()
        print(json.dumps(receipt, sort_keys=True), flush=True)
        return 1
    os.environ.update(
        DJANGO_SETTINGS_MODULE="testproject.settings_qualification_jobs",
        DJANGO_RAY_QUALIFICATION_RQ2="owned-application-job",
        DJANGO_RAY_QUALIFICATION_RQ2_GENERATION=args.generation,
    )
    previous = {sig: signal.signal(sig, _terminated) for sig in (signal.SIGTERM, signal.SIGALRM)}
    signal.alarm(WORK_SECONDS)
    try:
        verify(args.generation, receipt["assertions"], diagnostics)
        diagnostics.enter("receipt")
        validate_receipt(receipt)
        receipt["status"] = "passed"
        receipt["diagnostics"] = diagnostics.snapshot()
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        with args.receipt.open("x") as stream:
            stream.write(encoded)
    except Exception as error:
        diagnostics.failed(error)
        receipt["status"] = "failed"
    finally:
        signal.alarm(0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    receipt["diagnostics"] = diagnostics.snapshot()
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
