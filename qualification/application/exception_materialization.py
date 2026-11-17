"""Pure receipt validation and lazy native observation for one fixed exception.

Importing this module loads only the standard library. Native entry points below
are called only inside the already admitted disposable Application processes.
"""

from __future__ import annotations

import html
import json
import math
import os
import shlex
import time
from collections.abc import Callable
from contextlib import contextmanager
from html.parser import HTMLParser
from typing import cast
from uuid import UUID

OBSERVATION_SUBSTAGES = frozenset(
    {"configuration", "client", "enqueue", "recovery", "job_settlement", "presentation", "cleanup"}
)
OBSERVATION_OPERATIONS = frozenset(
    {
        "manager_liveness",
        "terminal_snapshot",
        "jobs_inventory",
        "job_info",
        "job_logs",
        "registry_lookup",
        "request_load",
    }
)

EXPECTED_TYPE = "testproject.qualification_exception_tasks.BrokenMessageFixtureError"
CALLABLE = "testproject.qualification_exception_tasks.broken_message_then_recover"
FALLBACK = "exception message unavailable"
RENDERING_SENTINEL = "qualification exception rendering sentinel"
RESULT = {"recovered": True, "attempt": 2}
SETTINGS = frozenset(
    {"testproject.settings_qualification", "testproject.settings_qualification_jobs"}
)
TRUE_FACTS = (
    "original_failure_traceback_verified",
    "secondary_rendering_text_absent",
    "current_owner_verified",
    "automatic_recovery_observed",
    "current_api_verified",
    "archived_admin_verified",
    "raw_fields_preserved_after_reads",
    "owned_execution_and_attempt_rows_removed",
)
FALSE_FACTS = (
    "complete_application_gate",
    "manual_retry_used",
    "third_attempt_observed",
    "wire_retryable_field_directly_observed",
    "wire_exception_type_field_directly_observed",
    "extra_manager_started_by_observer",
)
JOB_TRUE_FACTS = (
    "typed_request_bindings_verified",
    "per_identity_submission_unique",
    "settled_without_attempt3",
)
JOB_FIELDS = frozenset(
    {
        *JOB_TRUE_FACTS,
        "job_attempt_numbers",
        "job_generation_numbers",
        "transport_statuses",
        "driver_exit_codes",
        "individual_request_GC_claimed",
    }
)
FIELDS = frozenset(
    {
        *TRUE_FACTS,
        *FALSE_FACTS,
        "schema_version",
        "trigger",
        "transport",
        "execution_id",
        "task_id",
        "attempt_numbers",
        "attempt_states",
        "current_attempt",
        "current_generation",
        "execution_protocol_version",
        "result",
        "expected_exception_type",
        "failure_message",
        "manager_versions",
        "executor_versions",
    }
)


def _same(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and actual.keys() == expected.keys()
            and all(_same(actual[key], item) for key, item in expected.items())
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_same(left, right) for left, right in zip(actual, expected, strict=True))
        )
    return actual == expected


def validate_record(value: object, *, transport: str, expected_version: str | None = None) -> None:
    """Require the fixed typed record, without ORM, SDK, task or HTTP imports."""
    fixed = {
        "schema_version": 1,
        "trigger": "broken_exception_message_attempt1",
        "transport": transport,
        "attempt_numbers": [1, 2],
        "attempt_states": ["FAILED", "SUCCEEDED"],
        "current_attempt": 2,
        "current_generation": 2,
        "execution_protocol_version": 1,
        "result": RESULT,
        "expected_exception_type": EXPECTED_TYPE,
        "failure_message": FALLBACK,
        **dict.fromkeys(TRUE_FACTS, True),
        **dict.fromkeys(FALSE_FACTS, False),
    }
    if transport == "rq2_job":
        fixed.update(
            job_attempt_numbers=[1, 2],
            job_generation_numbers=[1, 2],
            transport_statuses=["SUCCEEDED", "SUCCEEDED"],
            driver_exit_codes=[0, 0],
            individual_request_GC_claimed=False,
            **dict.fromkeys(JOB_TRUE_FACTS, True),
        )
    if (
        transport not in {"ray_core", "rq2_job"}
        or type(value) is not dict
        or value.keys() != FIELDS | (JOB_FIELDS if transport == "rq2_job" else frozenset())
        or any(not _same(value[key], expected) for key, expected in fixed.items())
        or type(value["execution_id"]) is not int
        or value["execution_id"] <= 0
    ):
        raise ValueError("Exception materialization evidence is incomplete")
    task_id = value["task_id"]
    try:
        if type(task_id) is not str or len(task_id) != 36:
            raise ValueError
        identity = UUID(task_id)
        if identity.version != 4 or str(identity) != task_id:
            raise ValueError
    except ValueError:
        raise ValueError("Exception materialization identity is invalid") from None
    versions = [value["manager_versions"], value["executor_versions"]]
    for pair in versions:
        if (
            type(pair) is not list
            or len(pair) != 2
            or any(
                type(item) is not str
                or not item
                or len(item) > 128
                or not item.isascii()
                or any(ord(char) <= 32 or ord(char) == 127 for char in item)
                for item in pair
            )
        ):
            raise ValueError("Exception materialization provenance is invalid")
    typed_versions = cast(list[list[str]], versions)
    if len(set(typed_versions[0] + typed_versions[1])) != 1 or (
        expected_version is not None and typed_versions[0][0] != expected_version
    ):
        raise ValueError("Exception materialization provenance does not match")


def validate_distinct_records(records: list[dict]) -> None:
    """Join the four existing Core/Jobs receipts without adding a receipt file."""
    if len(records) != 4:
        raise ValueError("Exception materialization needs both transports and generations")
    for record, transport in zip(records, ("ray_core", "rq2_job") * 2, strict=True):
        validate_record(record, transport=transport)
    if (
        len({value["task_id"] for value in records}) != 4
        or len({value["execution_id"] for value in records}) != 4
    ):
        raise ValueError("Exception materialization identities are not distinct")
    if len({value["executor_versions"][0] for value in records}) != 1:
        raise ValueError("Exception materialization generations differ in package version")


_OBSERVATION_FAILURE_MESSAGES = {
    "deadline": "Exception observation requires its original deadline",
    "configuration": "Exception observation requires unchanged disposable retry settings",
    "terminal_shape": "Exception fixture did not recover in exactly two attempts",
    "attempt_provenance": "Exception attempt provenance or chronology differs",
    "original_failure": "Original exception was not archived with its fixed fallback",
    "success_current": "Successful attempt does not match current durable result",
    "owner": "Exception fixture does not belong to its live candidate manager",
    "admin_settings": "Exception Admin session requires disposable qualification settings",
    "admin_user": "Exception Admin session requires the fixed qualification administrator",
    "admin_session_create": "Exception Admin session was not created",
    "admin_session_cleanup": "Exception Admin session cleanup was not observed",
    "html_attribute": "Exception presentation introduced a diagnostic attribute",
    "html_executable": "Exception presentation introduced executable diagnostic text",
    "http_status": "Exception presentation HTTP status did not match",
    "api_identity": "Exception current API does not match its recovered identity",
    "admin_history": "Exception archived Admin does not show the inert original failure",
    "jobs_inventory": "Exception Jobs inventory exceeded its fixed bound",
    "job_multiplicity": "Exception identity has more than one Job submission",
    "job_carrier": "Exception Job does not use the fixed typed carrier",
    "job_registry_root": "Exception Job request escaped the owned registry",
    "job_registry": "Exception Job registry differs from its typed request",
    "job_metadata_kind": "Exception Job metadata is not a typed request binding",
    "job_request": "Exception Job request does not match its owned execution",
    "job_public_data": "Exception Job public surface exposed application data",
    "job_diagnostic_content": "Exception Job emitted an unexpected application diagnostic",
    "job_binding_duplicate": "Exception request binding appears in another Job",
    "job_transport": "Exception callable failure escaped the strict completion transport",
    "job_exit": "Exception Job did not expose an SDK integer zero exit code",
    "job_diagnostic_count": "Exception Job did not expose its one fixed application diagnostic",
    "job_current_binding": "Exception current row differs from its second submission",
    "job_distinct": "Exception retry reused a submission identity",
    "cleanup_current": "Exception task changed before exact owned cleanup",
    "cleanup_delete": "Exception exact owned cleanup was not observed",
    "manager_missing": "Exception Jobs observation requires the existing live manager",
    "manager_prefix": "Exception Core observation requires its existing manager prefix",
    "manager_exit": "Exception existing Jobs manager exited during observation",
    "terminal_outcome": "Exception fixture terminated outside its intended two attempts",
    "job_settlement": "Exception settled Job identities changed",
    "raw_preservation": "Exception presentation changed durable fields or attempt history",
}
OBSERVATION_FAILURE_CODES = frozenset(
    {
        *_OBSERVATION_FAILURE_MESSAGES,
        "binding_missing",
        "binding_invalid",
        "binding_resource_limit",
        "binding_identity_mismatch",
        "binding_protocol_mismatch",
        "binding_digest_mismatch",
        "binding_transport_mismatch",
        "unclassified",
    }
)


class ObservationError(ValueError):
    """A fixed qualification guard failed; never carry observed values."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in _OBSERVATION_FAILURE_MESSAGES:
            raise ValueError("Invalid exception observation code")
        self.code = code
        super().__init__(_OBSERVATION_FAILURE_MESSAGES[code])


def observation_failure_code(error: Exception) -> str:
    """Project only our own guard code or a trusted fixed binding category."""
    if type(error) is ObservationError:
        code = error.code
        return code if type(code) is str and code in OBSERVATION_FAILURE_CODES else "unclassified"
    from django_ray.ray_job_protocol import (
        RayJobRequestBindingError,
        RayJobRequestBindingRejection,
    )

    if type(error) is RayJobRequestBindingError:
        category = error.classification
        if type(category) is RayJobRequestBindingRejection:
            code = "binding_" + category.value
            return code if code in OBSERVATION_FAILURE_CODES else "unclassified"
    return "unclassified"


def _remaining(deadline: float) -> float:
    if type(deadline) not in {int, float} or not math.isfinite(deadline):
        raise ObservationError("deadline")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Exception observation exhausted its original budget")
    return remaining


def _wait(predicate, *, deadline: float, alive) -> object:
    while True:
        _remaining(deadline)
        alive()
        _remaining(deadline)
        value = predicate()
        _remaining(deadline)
        if value:
            return value
        time.sleep(min(0.2, _remaining(deadline)))


def _configuration(transport: str) -> tuple[dict, str]:
    from django.conf import settings
    from django.db import connection

    from django_ray import __version__
    from django_ray.conf.settings import get_settings
    from django_ray.runner.retry import _match_denylist_entry

    module = os.environ.get("DJANGO_SETTINGS_MODULE")
    expected = (
        "testproject.settings_qualification_jobs"
        if transport == "rq2_job"
        else "testproject.settings_qualification"
    )
    config = get_settings()
    if (
        transport not in {"ray_core", "rq2_job"}
        or module != expected
        or settings.SETTINGS_MODULE != module
        or connection.vendor != "postgresql"
        or type(config["MAX_TASK_ATTEMPTS"]) is not int
        or config["MAX_TASK_ATTEMPTS"] != 3
        or type(config["RETRY_BACKOFF_SECONDS"]) is not int
        or config["RETRY_BACKOFF_SECONDS"] != 5
        or _match_denylist_entry(EXPECTED_TYPE, config["RETRY_EXCEPTION_DENYLIST"]) is not None
        or (
            transport == "rq2_job"
            and (
                os.environ.get("DJANGO_RAY_QUALIFICATION_RQ2") != "owned-application-job"
                or settings.RQ2_GENERATION not in {"before", "after"}
                or settings.RQ2_GENERATION
                != os.environ.get("DJANGO_RAY_QUALIFICATION_RQ2_GENERATION")
            )
        )
    ):
        raise ObservationError("configuration")
    return config, __version__


def _terminal_snapshot(
    pk: int, task_id: str, *, transport: str, version: str, owner, deadline: float
) -> tuple:
    from datetime import timedelta

    from django.utils import timezone

    from django_ray.conf.settings import get_settings
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease

    _remaining(deadline)
    row = RayTaskExecution.objects.get(pk=pk, task_id=task_id)
    _remaining(deadline)
    attempts = list(TaskAttempt.objects.filter(execution_id=pk).order_by("attempt_number")[:3])
    _remaining(deadline)
    now = timezone.now()
    if (
        row.state != "SUCCEEDED"
        or row.attempt_number != 2
        or row.execution_generation != 2
        or row.execution_protocol_version != 1
        or row.callable_path != CALLABLE
        or row.created_with_django_ray_version != version
        or row.runtime_env_profile != ("rq2-qualification" if transport == "rq2_job" else "project")
        or (
            transport == "ray_core"
            and (
                row.ray_job_request_reference is not None
                or type(row.ray_job_id) is not str
                or not 0 < len(row.ray_job_id) <= 255
                or row.ray_address != get_settings()["RAY_ADDRESS"]
                or row.ray_target_address != get_settings()["RAY_ADDRESS"]
            )
        )
        or row.result_reference is not None
        or not isinstance(row.result_data, str)
        or len(row.result_data.encode()) > 256
        or not _same(json.loads(row.result_data), RESULT)
        or row.error_message is not None
        or row.error_traceback is not None
        or len(attempts) != 2
        or [item.attempt_number for item in attempts] != [1, 2]
        or [item.state for item in attempts] != ["FAILED", "SUCCEEDED"]
    ):
        raise ObservationError("terminal_shape")
    failed, succeeded = attempts
    for item in attempts:
        if (
            item.execution_protocol_version != 1
            or item.managed_with_django_ray_version != version
            or item.executor_django_ray_version != version
            or item.started_at is None
            or item.finished_at is None
            or not row.created_at <= item.started_at <= item.finished_at <= now
            or item.created_at < item.finished_at
        ):
            raise ObservationError("attempt_provenance")
    if (
        failed.finished_at > succeeded.started_at
        or failed.error_message != FALLBACK
        or type(failed.error_traceback) is not str
        or not 0 < len(failed.error_traceback.encode()) <= 8192
        or EXPECTED_TYPE not in failed.error_traceback
        or "broken_message_then_recover" not in failed.error_traceback
        or RENDERING_SENTINEL in failed.error_traceback
        or failed.result_data is not None
        or failed.result_reference is not None
    ):
        raise ObservationError("original_failure")
    for field in (
        "state",
        "execution_protocol_version",
        "managed_with_django_ray_version",
        "executor_django_ray_version",
        "started_at",
        "finished_at",
        "result_data",
        "result_reference",
        "error_message",
        "error_traceback",
    ):
        if getattr(succeeded, field) != getattr(row, field):
            raise ObservationError("success_current")
    _remaining(deadline)
    lease = TaskWorkerLease.objects.get(worker_id=row.claimed_by_worker)
    _remaining(deadline)
    observed = timezone.now()
    if (
        not lease.is_active
        or lease.django_ray_version != version
        or lease.started_at > row.started_at
        or not observed - timedelta(seconds=get_settings()["WORKER_LEASE_SECONDS"])
        <= lease.last_heartbeat_at
        <= observed
        or (transport == "ray_core" and not lease.hostname.startswith(owner))
        or (transport == "rq2_job" and lease.pid != owner.pid)
    ):
        raise ObservationError("owner")
    # Archive creation is after terminal persistence, not enqueue time. Do not
    # compare TaskAttempt.created_at <= started_at or invent a stored Job ID.
    return row, attempts


def _raw_fields(pk: int, task_id: str, *, deadline: float) -> tuple:
    from django_ray.models import RayTaskExecution, TaskAttempt

    _remaining(deadline)
    current = RayTaskExecution.objects.values().get(pk=pk, task_id=task_id)
    _remaining(deadline)
    attempts = list(
        TaskAttempt.objects.filter(execution_id=pk).order_by("attempt_number").values()[:3]
    )
    _remaining(deadline)
    return current, attempts


@contextmanager
def _owned_admin_session(*, deadline: float):
    """The Core-only session helper remains unchanged; accept only its Jobs overlay too."""
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.contrib.sessions.backends.db import SessionStore

    module = os.environ.get("DJANGO_SETTINGS_MODULE")
    if (
        module not in SETTINGS
        or settings.SETTINGS_MODULE != module
        or settings.SESSION_ENGINE != "django.contrib.sessions.backends.db"
        or settings.AUTHENTICATION_BACKENDS != ["django.contrib.auth.backends.ModelBackend"]
    ):
        raise ObservationError("admin_settings")
    _remaining(deadline)
    user = get_user_model().objects.get(username="qualification")
    _remaining(deadline)
    if not (user.is_active and user.is_staff and user.is_superuser):
        raise ObservationError("admin_user")
    session = SessionStore()
    try:
        session.set_expiry(900)
        session["_auth_user_id"] = str(user.pk)
        session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        session["_auth_user_hash"] = user.get_session_auth_hash()
        _remaining(deadline)
        session.save()
        _remaining(deadline)
        if not session.session_key:
            raise ObservationError("admin_session_create")
        yield f"{settings.SESSION_COOKIE_NAME}={session.session_key}"
    finally:
        if key := session.session_key:
            session.delete(key)
            if session.exists(key):
                raise ObservationError("admin_session_cleanup")


class _InertText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.executable = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in {"script", "style"}:
            self.executable += 1
        if any(FALLBACK in (value or "") or EXPECTED_TYPE in (value or "") for _, value in attrs):
            raise ObservationError("html_attribute")

    def handle_endtag(self, tag) -> None:
        if tag in {"script", "style"}:
            self.executable = max(0, self.executable - 1)

    def handle_data(self, data) -> None:
        if self.executable and (FALLBACK in data or EXPECTED_TYPE in data):
            raise ObservationError("html_executable")
        self.text.append(data)


def _presentation(row, failed, *, token: str, deadline: float) -> None:
    from qualification.application.http_context import HttpObserver
    from qualification.application.run_api import ApplicationHttp
    from qualification.application.workflow_admin import FIXTURE_ORIGIN

    def read(path, *, headers, status, is_html=False):
        request = ApplicationHttp(
            FIXTURE_ORIGIN,
            request_timeout=min(5, _remaining(deadline)),
            observer=HttpObserver.CORE,
            observer_deadline=deadline,
        )
        actual, raw = request(
            path,
            method="GET",
            headers=headers,
            response_limit=1024 * 1024 if is_html else 64 * 1024,
            required_media_types=frozenset({"text/html" if is_html else "application/json"}),
            required_response_headers=(
                {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
                if status == 200
                else None
            ),
        )
        _remaining(deadline)
        if actual != status:
            raise ObservationError("http_status")
        return raw

    paths = (f"/api/tasks/{row.task_id}", f"/api/executions/{row.pk}")
    for path in paths:
        read(path, headers={}, status=401)
    for index, path in enumerate(paths):
        value = json.loads(read(path, headers={"Authorization": f"Bearer {token}"}, status=200))
        if (
            value.get("task_id") != row.task_id
            or value.get("state") != "SUCCEEDED"
            or not _same(value.get("attempt_number"), 2)
            or not _same(value.get("execution_generation"), 2)
            or not _same(value.get("execution_protocol_version"), 1)
            or value.get("managed_with_django_ray_version") != row.managed_with_django_ray_version
            or value.get("executor_django_ray_version") != row.executor_django_ray_version
            or (
                index == 1
                and (
                    not _same(value.get("id"), row.pk)
                    or value.get("result_data_omission_reason") is not None
                    or not _same(json.loads(value["result_data"]), RESULT)
                    or value.get("error_message") is not None
                )
            )
        ):
            raise ObservationError("api_identity")
    path = f"/admin/django_ray/taskattempt/{failed.pk}/change/"
    read(path, headers={}, status=302, is_html=True)
    with _owned_admin_session(deadline=deadline) as cookie:
        raw = read(path, headers={"Cookie": cookie}, status=200, is_html=True)
        source = raw.decode("utf-8")
        parser = _InertText()
        parser.feed(source)
        parser.close()
        visible = "".join(parser.text)
        if (
            FALLBACK not in visible
            or EXPECTED_TYPE not in visible
            or "broken_message_then_recover" not in visible
            or html.escape(FALLBACK) not in source
            or RENDERING_SENTINEL in source
        ):
            raise ObservationError("admin_history")


def _job_observation(
    row, client, *, deadline: float, operation: Callable[[str | None], None] | None = None
) -> tuple[bool, tuple[str, str]]:
    """Find only two exact identities in a bounded inventory; never count all Jobs as owned."""
    from django_ray.execution_codec import ExecutionIdentity
    from django_ray.models import TaskInputPayload
    from django_ray.ray_job_protocol import (
        RayJobRequestReferenceExpectation,
        coordination_sha256,
        parse_ray_job_request_metadata,
        validate_ray_job_request_reference_expectation,
    )
    from django_ray.ray_job_request_storage import (
        decode_ray_job_request_locator,
        load_ray_job_request,
        ray_job_request_reference_content_identity,
    )
    from django_ray.runner.ray_job import _bounded_control_requests

    identities = [ExecutionIdentity(row.pk, row.task_id, number, number) for number in (1, 2)]
    _remaining(deadline)
    if operation is not None:
        operation("jobs_inventory")
    with _bounded_control_requests(client):
        jobs = client.list_jobs()
    if operation is not None:
        operation(None)
    _remaining(deadline)
    if not isinstance(jobs, list) or len(jobs) > 64:
        raise ObservationError("jobs_inventory")
    selected = []
    for identity in identities:
        digest = coordination_sha256(identity)
        matches = [
            job
            for job in jobs
            if (getattr(job, "metadata", None) or {}).get("django_ray_coordination_sha256")
            == digest
        ]
        if not matches:
            return False, ("", "")
        if len(matches) != 1:
            raise ObservationError("job_multiplicity")
        submission_id = matches[0].submission_id
        _remaining(deadline)
        with _bounded_control_requests(client):
            if operation is not None:
                operation("job_info")
            info = client.get_job_info(submission_id)
            if operation is not None:
                operation(None)
            _remaining(deadline)
            if operation is not None:
                operation("job_logs")
            logs = client.get_job_logs(submission_id) or ""
            if operation is not None:
                operation(None)
        _remaining(deadline)
        parts = shlex.split(info.entrypoint)
        if len(parts) != 5 or parts[:4] != [
            "python",
            "-m",
            "django_ray.runtime.entrypoint",
            "--request-ref-b64",
        ]:
            raise ObservationError("job_carrier")
        locator = decode_ray_job_request_locator(parts[4])
        reference = locator.reference
        if locator.filesystem_path != "/rq2/requests" or len(reference.encode("ascii")) > 500:
            raise ObservationError("job_registry_root")
        content_digest, size = ray_job_request_reference_content_identity(reference)
        if operation is not None:
            operation("registry_lookup")
        registry = TaskInputPayload.objects.get(reference=reference)
        if operation is not None:
            operation(None)
        if (
            registry.payload_kind != "ray_job_request"
            or registry.state != "ACTIVE"
            or registry.backend != "filesystem"
            or registry.digest != content_digest
            or registry.size_bytes != size
        ):
            raise ObservationError("job_registry")
        _remaining(deadline)
        if operation is not None:
            operation("request_load")
        loaded = load_ray_job_request(
            locator, expected_identity=identity, expected_execution_protocol_version=1
        )
        if operation is not None:
            operation(None)
        _remaining(deadline)
        expectation = parse_ray_job_request_metadata(info.metadata, required=True)
        if not isinstance(expectation, RayJobRequestReferenceExpectation):
            raise ObservationError("job_metadata_kind")
        validate_ray_job_request_reference_expectation(
            expectation,
            expected_identity=identity,
            expected_execution_protocol_version=1,
            expected_request_sha256=content_digest,
            expected_request_size_bytes=size,
            expected_submission_id=submission_id,
            serialized_request=loaded.serialized_request,
            request_reference=reference,
            request_locator=parts[4],
        )
        if (
            loaded.request.identity != identity
            or loaded.request.callable_path != CALLABLE
            or loaded.request.serialized_args != row.args_json
            or loaded.request.serialized_kwargs != row.kwargs_json
            or not isinstance(logs, str)
            or len(logs.encode()) > 65536
        ):
            raise ObservationError("job_request")
        # Metadata, logs and request bytes are inspected locally, never exported.
        public = json.dumps(info.model_dump(), sort_keys=True, default=str)
        forbidden = [row.task_id, CALLABLE, loaded.serialized_request, RENDERING_SENTINEL]
        forbidden += [
            value
            for key, value in os.environ.items()
            if value
            and any(
                word in key.upper()
                for word in ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "ENCRYPTION_KEY")
            )
        ]
        if (
            len(public.encode()) > 128 * 1024
            or FALLBACK in public
            or any(value in surface for value in forbidden for surface in (public, logs))
        ):
            raise ObservationError("job_public_data")
        # entrypoint.main emits exactly one fixed application diagnostic, even
        # though both structured outcomes complete the Job transport with zero.
        expected_line = (
            f"django-ray task failed: {FALLBACK}"
            if identity.attempt_number == 1
            else "django-ray task completed successfully"
        )
        lines = logs.splitlines()
        fixed_lines = [line for line in lines if line.startswith("django-ray task ")]
        if any(line != expected_line for line in fixed_lines) or any(
            FALLBACK in line for line in lines if line != expected_line
        ):
            raise ObservationError("job_diagnostic_content")
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
                raise ObservationError("job_binding_duplicate")
        status = str(info.status)
        if status in {"FAILED", "STOPPED"}:
            raise ObservationError("job_transport")
        if status != "SUCCEEDED" or info.driver_exit_code is None:
            return False, ("", "")
        # This is an SDK model observation. The SDK may already have coerced
        # JSON; this is deliberately not a raw-wire scalar-type certificate.
        if type(info.driver_exit_code) is not int or info.driver_exit_code != 0:
            raise ObservationError("job_exit")
        if fixed_lines != [expected_line]:
            raise ObservationError("job_diagnostic_count")
        if identity.attempt_number == 2 and (
            row.ray_job_id != submission_id
            or row.ray_job_request_reference != reference
            or row.ray_address != "http://ray-head:8265"
        ):
            raise ObservationError("job_current_binding")
        selected.append(submission_id)
    if len(set(selected)) != 2:
        raise ObservationError("job_distinct")
    return True, (selected[0], selected[1])


def _remove_settled(pk: int, task_id: str, before: tuple, *, deadline: float) -> None:
    from django.db import transaction

    from django_ray.models import RayTaskExecution, TaskAttempt

    _remaining(deadline)
    with transaction.atomic():
        row = RayTaskExecution.objects.select_for_update().get(pk=pk, task_id=task_id)
        _remaining(deadline)
        if (
            row.state != "SUCCEEDED"
            or row.attempt_number != 2
            or row.execution_generation != 2
            or _raw_fields(pk, task_id, deadline=deadline) != before
        ):
            raise ObservationError("cleanup_current")
        attempt_ids = [item["id"] for item in before[1]]
        _remaining(deadline)
        row.delete()
        if RayTaskExecution.objects.filter(pk=pk, task_id=task_id).exists() or (
            TaskAttempt.objects.filter(pk__in=attempt_ids, execution_id=pk).exists()
        ):
            raise ObservationError("cleanup_delete")
        _remaining(deadline)


def observe(
    *,
    transport: str,
    token: str,
    deadline: float,
    manager=None,
    manager_prefix=None,
    job_client=None,
    progress: Callable[[str], None] | None = None,
    operation: Callable[[str | None], None] | None = None,
) -> dict:
    """Spend the original phase budget; failure leaves unsettled rows to namespace cleanup.

    Synchronous ORM/SDK/HTTP calls are checked after return, not preempted by
    this clock. Existing process/phase supervisors and cleanup remain required.

    The Jobs caller may pass its already-owned pinned client to avoid repeating
    connection/version setup. Every subsequent observation remains unchanged.
    """
    if progress is not None:
        progress("configuration")
    _config, version = _configuration(transport)
    _remaining(deadline)
    from django_ray.models import RayTaskExecution
    from testproject.qualification_exception_tasks import broken_message_then_recover

    if progress is not None:
        progress("client")
    if transport == "rq2_job":
        from django_ray.runner.ray_job import _address_pinned_job_client

        if manager is None or manager.poll() is not None:
            raise ObservationError("manager_missing")
        client = (
            job_client
            if job_client is not None
            else _address_pinned_job_client("http://ray-head:8265")
        )
    elif type(manager_prefix) is not str or not 0 < len(manager_prefix) <= 63:
        raise ObservationError("manager_prefix")

    def alive():
        if transport == "rq2_job" and (manager is None or manager.poll() is not None):
            raise ObservationError("manager_exit")

    if progress is not None:
        progress("enqueue")
    _remaining(deadline)
    alive()
    result = broken_message_then_recover.using(
        backend="rq2-qualification" if transport == "rq2_job" else "default",
        queue_name="rq2-qualification" if transport == "rq2_job" else "default",
    ).enqueue()
    _remaining(deadline)
    row = RayTaskExecution.objects.get(task_id=result.id)
    pk, task_id = row.pk, row.task_id

    def recovered():
        current = RayTaskExecution.objects.get(pk=pk, task_id=task_id)
        if current.attempt_number > 2 or current.state in {
            "CANCELLED",
            "CANCELLING",
            "LOST",
            "EXPIRED",
            "FAILED",
        }:
            raise ObservationError("terminal_outcome")
        return current if current.state == "SUCCEEDED" else None

    if progress is not None:
        progress("recovery")
    _wait(recovered, deadline=deadline, alive=alive)
    row, attempts = _terminal_snapshot(
        pk,
        task_id,
        transport=transport,
        version=version,
        owner=manager if transport == "rq2_job" else manager_prefix,
        deadline=deadline,
    )
    _remaining(deadline)
    if transport == "rq2_job":
        if progress is not None:
            progress("job_settlement")
        selected = None
        settled_until = None

        def settled():
            nonlocal selected, settled_until
            if operation is not None:
                operation("terminal_snapshot")
            current, _ = _terminal_snapshot(
                pk, task_id, transport=transport, version=version, owner=manager, deadline=deadline
            )
            if operation is not None:
                operation(None)
            complete, submissions = _job_observation(
                current, client, deadline=deadline, operation=operation
            )
            if complete:
                if selected is not None and selected != submissions:
                    raise ObservationError("job_settlement")
                selected = submissions
                if settled_until is None:
                    settled_until = time.monotonic() + 4
            return complete and settled_until is not None and time.monotonic() >= settled_until

        def settlement_alive():
            if operation is not None:
                operation("manager_liveness")
            alive()
            if operation is not None:
                operation(None)

        _wait(settled, deadline=deadline, alive=settlement_alive)
    if progress is not None:
        progress("presentation")
    before = _raw_fields(pk, task_id, deadline=deadline)
    _remaining(deadline)
    alive()
    _presentation(row, attempts[0], token=token, deadline=deadline)
    _remaining(deadline)
    alive()
    _terminal_snapshot(
        pk,
        task_id,
        transport=transport,
        version=version,
        owner=manager if transport == "rq2_job" else manager_prefix,
        deadline=deadline,
    )
    if _raw_fields(pk, task_id, deadline=deadline) != before:
        raise ObservationError("raw_preservation")
    alive()
    if progress is not None:
        progress("cleanup")
    _remove_settled(pk, task_id, before, deadline=deadline)
    _remaining(deadline)
    evidence = {
        "schema_version": 1,
        "trigger": "broken_exception_message_attempt1",
        "transport": transport,
        "execution_id": pk,
        "task_id": task_id,
        "attempt_numbers": [1, 2],
        "attempt_states": ["FAILED", "SUCCEEDED"],
        "current_attempt": 2,
        "current_generation": 2,
        "execution_protocol_version": 1,
        "result": dict(RESULT),
        "expected_exception_type": EXPECTED_TYPE,
        "failure_message": FALLBACK,
        "manager_versions": [version, version],
        "executor_versions": [version, version],
        **dict.fromkeys(TRUE_FACTS, True),
        **dict.fromkeys(FALSE_FACTS, False),
    }
    if transport == "rq2_job":
        evidence.update(
            job_attempt_numbers=[1, 2],
            job_generation_numbers=[1, 2],
            transport_statuses=["SUCCEEDED", "SUCCEEDED"],
            driver_exit_codes=[0, 0],
            individual_request_GC_claimed=False,
            **dict.fromkeys(JOB_TRUE_FACTS, True),
        )
    validate_record(evidence, transport=transport, expected_version=version)
    return evidence
