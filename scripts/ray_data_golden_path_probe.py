"""Run bounded, real-Ray evidence for the application-owned Ray Data recipe."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

# Ray otherwise detects this script's ``uv run`` ancestor and propagates
# the outer editable-project command into each minimal immutable working-dir
# archive. The disposable environment already contains the exact dependency
# set, so workers must use that preinstalled interpreter directly.
os.environ["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] = "0"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from testproject.apps.cluster_tasks.ray_data_job import (  # noqa: E402
    MAX_RESULT_BYTES,
    ArtifactNotAdoptableError,
    OutputChangedError,
    _attempt_paths,
    _completion_result,
    _inspect_output_content,
    _read_completion_manifest,
    validate_adoptable_artifact,
)

if TYPE_CHECKING:
    from django_ray.execution_codec import ExecutionIdentity

MAX_PROBE_SOURCE_FILES = 5_000
MAX_PROBE_SOURCE_BYTES = 32 * 1024 * 1024
MAX_RAY_JOB_EVIDENCE_CANDIDATES = 4_096


def _build_probe_working_dir_archive(destination: Path) -> None:
    """Freeze only the import roots needed by both disposable Ray Jobs."""
    file_count = 0
    byte_count = 0
    with ZipFile(destination, "w") as archive:
        for root_name in ("src", "testproject"):
            source_root = ROOT / root_name
            for source in sorted(source_root.rglob("*"), key=lambda path: path.as_posix()):
                if source.is_symlink():
                    raise AssertionError("probe source archive cannot contain symlinks")
                if not source.is_file():
                    continue
                relative = source.relative_to(ROOT)
                if "__pycache__" in relative.parts or source.suffix in {".pyc", ".pyo"}:
                    continue
                contents = source.read_bytes()
                file_count += 1
                byte_count += len(contents)
                if file_count > MAX_PROBE_SOURCE_FILES or byte_count > MAX_PROBE_SOURCE_BYTES:
                    raise AssertionError("probe source archive exceeded its bounded contract")
                member = ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                member.compress_type = ZIP_DEFLATED
                member.create_system = 3
                member.external_attr = 0o100644 << 16
                archive.writestr(member, contents)
    if file_count == 0:
        raise AssertionError("probe source archive was empty")


def _use_preinstalled_probe_dependencies(django_settings: Any) -> None:
    """Treat the isolated command environment as the disposable Ray node image."""
    config = getattr(django_settings, "DJANGO_RAY", None)
    if not isinstance(config, dict):
        raise AssertionError("probe expected mutable DJANGO_RAY settings")
    profiles = config.get("RUNTIME_ENV_PROFILES")
    if not isinstance(profiles, dict):
        raise AssertionError("probe expected RuntimeEnv profiles")
    project = profiles.get("project")
    ray_data = profiles.get("ray-data")
    if not isinstance(project, dict) or not isinstance(ray_data, dict):
        raise AssertionError("probe expected project and ray-data RuntimeEnv profiles")
    child = ray_data.get("runtime_env")
    if not isinstance(child, dict):
        raise AssertionError("probe expected a composed ray-data RuntimeEnv profile")

    # ``uv run --with ray[data]==...`` supplies the exact dependency set to the
    # local node. Avoid a second Ray-managed virtualenv, which is both redundant
    # and unsupported by pip-less uv Python installations on Windows.
    profiles["project"] = {**project, "pip": []}
    profiles["ray-data"] = {
        **ray_data,
        "runtime_env": {**child, "pip": []},
    }


def _run_probe_worker() -> int:
    """Give the owned worker the same fixed profile as its producer."""
    import django
    from django.conf import settings as django_settings
    from django.core.management import call_command

    django.setup()
    _use_preinstalled_probe_dependencies(django_settings)

    from django_ray.management.commands.django_ray_worker import Command

    class ProbeWorkerCommand(Command):
        def send_heartbeat(self):
            super().send_heartbeat()
            diagnostic = _worker_qualification_diagnostic(self)
            previous = getattr(self, "_probe_diagnostic", None)
            count = getattr(self, "_probe_diagnostic_count", 0)
            if diagnostic != previous and count < 16:
                self._probe_diagnostic = diagnostic
                self._probe_diagnostic_count = count + 1
                self.stdout.write(f"Qualification progress (not authority): {diagnostic}")
                self.stdout.flush()

    call_command(ProbeWorkerCommand(), queue="ray-data", concurrency=1)
    return 0


def _worker_qualification_diagnostic(command) -> str:
    """Read fixed parent-owned scalars; never poll, reap, query DB or contact Ray."""
    try:
        from django_ray.runner.cohort_jobs import JobsCohortAdapterReason, JobsCohortPhase
        from django_ray.runner.cohort_process import CohortProcessPhase, CohortProcessReason

        controller = command._cohort_controller
        if controller is None:
            return '{"controller_present":false}'
        operation = controller.adapter._operation
        running = controller.adapter._supervisor._running
        result = {
            "controller_present": True,
            "adapter_phase": operation.phase.value
            if operation is not None and type(operation.phase) is JobsCohortPhase
            else "idle",
            "adapter_reason": operation.blocked.value
            if operation is not None and type(operation.blocked) is JobsCohortAdapterReason
            else None,
            "helper_phase": running.phase.value
            if running is not None and type(running.phase) is CohortProcessPhase
            else "idle",
            "helper_reason": running.reason.value
            if running is not None and type(running.reason) is CohortProcessReason
            else None,
            "helper_reap_attempted": running is not None and running.reap_attempted is True,
            "helper_reaped": running is not None and running.reaped is True,
        }
        return json.dumps(result, sort_keys=True, separators=(",", ":"))
    except Exception:
        return '{"progress_diagnostic":"unavailable"}'


def _verify_current_completion(execution: Any) -> None:
    """Read the exact resolved claim; this receipt grants no execution authority."""
    from django_ray.execution_codec import ExecutionIdentity, decode_execution_completion
    from django_ray.execution_protocol import ExecutionProtocolRange
    from django_ray.models import RayTaskCohortClaim
    from django_ray.runner.cohort_completion import _digest
    from django_ray.runner.cohort_dispatch import _contract
    from django_ray.target.cohort_claim import CohortRunnerFamily
    from django_ray.target.cohort_claim_storage import _record
    from django_ray.target.cohort_contract import cohort_execution_contract_digest
    from django_ray.target.cohort_transport import decode_cohort_execution_result

    identity = ExecutionIdentity(
        execution.pk,
        execution.task_id,
        execution.attempt_number,
        execution.execution_generation,
    )
    claim = RayTaskCohortClaim.objects.get(
        binding_id=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    record = _record(claim)
    contract = _contract(record)
    if (
        execution.execution_protocol_version != 3
        or record.facts.identity != identity
        or record.facts.binding.runner_family is not CohortRunnerFamily.RAY_JOB
        or record.facts.job_qualification is None
        or record.facts.job_qualification.jobs_endpoint != execution.ray_address
        or claim.disposition != "RESOLVED"
        or claim.resolution_kind != "application_completed"
        or not claim.dispatched_at
        or not claim.resolved_at
        or not execution.finished_at
        or not record.facts.claimed_at
        <= claim.dispatched_at
        <= claim.resolved_at
        <= execution.finished_at
    ):
        raise AssertionError("Ray Data completion lacks a matching resolved current claim")
    result = decode_cohort_execution_result(
        execution.completion_data,
        expected_identity=identity,
        expected_request_digest=record.prepared_request_digest,
        expected_cohort_contract_digest=cohort_execution_contract_digest(contract),
    )
    if result.completion_json is None or claim.resolution_digest != _digest(
        execution.completion_data
    ):
        raise AssertionError("Ray Data completion does not match its resolved claim evidence")
    completion = decode_execution_completion(
        result.completion_json,
        expected_identity=identity,
        expected_execution_protocol_version=3,
        supported_protocols=ExecutionProtocolRange(3, 3),
    ).completion
    if (
        completion.success is not True
        or completion.executor_django_ray_version != contract.expected_django_ray_version
        or execution.executor_django_ray_version != completion.executor_django_ray_version
    ):
        raise AssertionError("Ray Job driver did not persist a successful current completion")


def _path_from_uri(uri: str) -> Path:
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise AssertionError("probe result did not contain a local file URI")
    decoded_path = unquote(parsed.path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", decoded_path):
        decoded_path = decoded_path[1:]
    return Path(decoded_path)


def _read_bounded_rows(
    output_uri: str,
    *,
    expected_rows: int,
    expected_bytes: int,
    expected_sha256: str,
) -> list[dict[str, object]]:
    parquet = importlib.import_module("pyarrow.parquet")

    files, total_bytes, content_sha256 = _inspect_output_content(_path_from_uri(output_uri))
    if not files or len(files) > 8:
        raise AssertionError(f"probe expected between 1 and 8 Parquet files, found {len(files)}")
    if total_bytes != expected_bytes or content_sha256 != expected_sha256:
        raise AssertionError("probe result did not identify its exact bounded Parquet output")
    observed_rows = sum(parquet.ParquetFile(path).metadata.num_rows for path in files)
    if observed_rows != expected_rows:
        raise AssertionError(f"probe expected {expected_rows} rows, found {observed_rows}")

    rows: list[dict[str, object]] = []
    for path in files:
        rows.extend(parquet.read_table(path).to_pylist())
    return sorted(rows, key=lambda row: str(row["record_id"]))


def _assert_metadata_only(value: object) -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            _assert_metadata_only(item)
        return
    if not isinstance(value, dict):
        raise AssertionError(f"non-JSON result value: {type(value).__name__}")
    for key, item in value.items():
        if not isinstance(key, str):
            raise AssertionError("result dictionary contains a non-string key")
        _assert_metadata_only(item)


def _worker_log_tail(path: Path, *, maximum_chars: int = 8_000) -> str:
    try:
        contents = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "<worker log unavailable>"
    return contents[-maximum_chars:]


def _reserved_probe_diagnostics(row, *, lease, configuration_digest, jobs_endpoint) -> dict:
    """Inspect one exact owned reservation; statuses never grant execution authority."""
    from ray.dashboard.modules.job.pydantic_models import JobDetails, JobType

    from django_ray.runtime.cohort_job import (
        decode_probe_job_request,
        probe_job_metadata,
        probe_job_request_digest,
        probe_job_submission_id,
    )
    from django_ray.runtime.cohort_job_entrypoint import (
        CohortJobEntrypointReason,
        CohortProbeJobLaunch,
        probe_job_launch_entrypoint,
    )
    from django_ray.target.attestation import RayRunnerFamily
    from django_ray.target.cohort_job_control import (
        cohort_probe_entrypoint_digest,
        cohort_probe_submitted_runtime_env_digest,
    )
    from django_ray.target.cohort_job_http import fetch_reserved_cohort_job_details

    request = decode_probe_job_request(row["request_json"])
    launch = CohortProbeJobLaunch(
        request,
        probe_job_request_digest(request),
        jobs_endpoint,
        row["submitted_runtime_env_digest"],
        "testproject.settings",
    )
    if (
        request.lease != lease
        or request.runner_family is not RayRunnerFamily.RAY_JOB
        or request.configuration_digest != configuration_digest
        or request.challenge_id != row["challenge_id"]
        or request.challenge_revision != row["challenge_revision"]
        or launch.request_digest != row["request_digest"]
        or probe_job_submission_id(request) != row["submission_id"]
        or row["ray_address"] != jobs_endpoint
        or cohort_probe_entrypoint_digest(probe_job_launch_entrypoint(launch))
        != row["entrypoint_digest"]
    ):
        return {"reservation_binding": False}
    result: dict[str, object] = {
        "reservation_binding": True,
        "receipt_present": row["received_at"] is not None,
    }
    try:
        details = fetch_reserved_cohort_job_details(
            jobs_endpoint, row["submission_id"], timeout_seconds=5.0
        )
        matched = (
            type(details) is JobDetails
            and details.type is JobType.SUBMISSION
            and details.submission_id == row["submission_id"]
            and details.metadata == probe_job_metadata(request)
            and details.entrypoint == probe_job_launch_entrypoint(launch)
            and cohort_probe_submitted_runtime_env_digest(details.runtime_env)
            == row["submitted_runtime_env_digest"]
            and (details.driver_info is None or details.driver_info.id == details.job_id)
        )
        result["job_binding"] = matched
        if matched:
            result["job_status"] = details.status.value
            result["native_id_present"] = details.job_id is not None
            # JobDetails is already byte-bounded. Do not emit its message/logs,
            # paths, IDs, launch carrier, RuntimeEnv or arbitrary error text.
            reasons = {item.value for item in CohortJobEntrypointReason}
            found = (
                set(
                    re.findall(
                        r"^Cohort probe driver refused: ([a-z_]+)\r?$",
                        details.message or "",
                        flags=re.MULTILINE,
                    )
                )
                & reasons
            )
            result["driver_refusal"] = next(iter(found)) if len(found) == 1 else None
    except Exception:
        result["job_read"] = "unavailable"
    return result


def _receipt_publication_diagnostic(row) -> dict:
    """Report finite publication/expiry state without reconstructing eligibility."""
    from django_ray.models import RayTargetPolicyRevision
    from django_ray.runtime.cohort_job import decode_probe_job_request
    from django_ray.target.cohort_job_receipt import decode_cohort_job_receipt

    request = decode_probe_job_request(row["request_json"])
    now = datetime.now(UTC)
    result = {
        "challenge_consumed": row["challenge__consumed_at"] is not None,
        "request_fresh": request.issued_at <= now < request.expires_at,
        "refresh_policy_requested": request.expected_target_policy_id is not None,
    }
    if row["receipt_json"] is not None:
        receipt = decode_cohort_job_receipt(
            row["receipt_json"],
            expected_request=request,
            expected_request_digest=row["request_digest"],
            expected_submission_id=row["submission_id"],
            expected_receipt_digest=row["receipt_digest"],
        )
        proof = receipt.attestation
        result["receipt_fresh"] = proof.observed_at <= now < proof.expires_at
        policy = (
            RayTargetPolicyRevision.objects.filter(target_id=proof.expectation.target_key)
            .order_by("-revision")
            .values("revision", "desired_state")
            .first()
        )
        result["target_policy_present"] = policy is not None
        if policy is not None:
            result["receipt_matches_latest_policy"] = (
                policy["revision"] == proof.expectation.policy_revision
            )
            result["latest_policy_active"] = policy["desired_state"] == "active"
    return result


def _probe_timeout_diagnostics(execution_pk, worker, *, started_after, jobs_endpoint) -> str:
    """Read only this disposable worker's current slot before fixture teardown."""
    try:
        from django_ray.models import RayTargetProbeJobReceipt, TaskWorkerLease
        from django_ray.runtime.cohort_job import CohortProbeJobLease
        from django_ray.target.cohort_intent_storage import read_cohort_intent

        # A live owned subprocess plus its creation window prevents an older
        # same-PID lease from directing this diagnostic to another reservation.
        now = datetime.now(UTC)
        if worker.poll() is not None or not started_after <= now:
            return '{"worker_binding":false}'
        leases = list(
            TaskWorkerLease.objects.filter(
                hostname=socket.gethostname(),
                pid=worker.pid,
                started_at__gte=started_after,
                started_at__lte=now,
                queue_name="ray-data",
            ).values("worker_id", "hostname", "pid", "started_at")[:2]
        )
        if len(leases) != 1:
            return '{"worker_binding":false}'
        lease = CohortProbeJobLease(**leases[0])
        intent = read_cohort_intent(execution_pk)
        rows = list(
            RayTargetProbeJobReceipt.objects.filter(
                challenge__lease_id=lease.worker_id,
                challenge__lease_hostname=lease.hostname,
                challenge__lease_pid=lease.pid,
                challenge__lease_started_at=lease.started_at,
                challenge__configuration_digest=intent.configuration_digest,
                challenge__runner_family="ray_job",
            ).values(
                "challenge_id",
                "challenge_revision",
                "request_json",
                "request_digest",
                "ray_address",
                "submission_id",
                "entrypoint_digest",
                "submitted_runtime_env_digest",
                "received_at",
                "receipt_json",
                "receipt_digest",
                "challenge__consumed_at",
            )[:2]
        )
        result: dict[str, object] = {"worker_binding": True, "reservation_present": len(rows) == 1}
        if len(rows) == 1:
            result.update(
                _reserved_probe_diagnostics(
                    rows[0],
                    lease=lease,
                    configuration_digest=intent.configuration_digest,
                    jobs_endpoint=jobs_endpoint,
                )
            )
            if result.get("reservation_binding") is True:
                try:
                    result.update(_receipt_publication_diagnostic(rows[0]))
                except Exception:
                    result["publication_diagnostic"] = "unavailable"
        return json.dumps(result, sort_keys=True, separators=(",", ":"))
    except Exception:
        # Diagnostics must not replace the original timeout or its finally cleanup.
        return '{"diagnostics":"unavailable"}'


def _wait_for_recovered_execution(
    execution_pk: int,
    worker: subprocess.Popen[str],
    log: Path,
    *,
    started_after: datetime,
    jobs_endpoint: str,
) -> Any:
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskState

    deadline = time.monotonic() + 300
    terminal = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.LOST}
    while time.monotonic() < deadline:
        execution = RayTaskExecution.objects.get(pk=execution_pk)
        if execution.state in terminal:
            attempts = list(
                TaskAttempt.objects.filter(execution_id=execution_pk).order_by("attempt_number")
            )
            if len(attempts) >= 2:
                return execution
            raise AssertionError(
                f"Ray Data task became terminal before framework retry: {execution.state}: "
                f"{execution.error_message}\n{_worker_log_tail(log)}"
            )
        return_code = worker.poll()
        if return_code is not None:
            raise AssertionError(
                f"Ray Job management worker exited with {return_code} before completion:\n"
                f"{_worker_log_tail(log)}"
            )
        time.sleep(0.25)
    diagnostic = _probe_timeout_diagnostics(
        execution_pk, worker, started_after=started_after, jobs_endpoint=jobs_endpoint
    )
    raise AssertionError(
        f"Ray Data recovery task timed out:\n{_worker_log_tail(log)}\n"
        f"Qualification diagnostic (not authority): {diagnostic}"
    )


def _build_rq2_submission_candidates(
    *,
    execution_pk: int,
    task_id: str,
    attempt_protocol_versions: dict[int, int],
    current_generation: int,
) -> dict[str, tuple[ExecutionIdentity, int]]:
    """Build a bounded rq2 ID allowlist from durable execution coordinates."""
    from django_ray.execution_codec import ExecutionIdentity
    from django_ray.ray_job_protocol import (
        STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
        RayJobRequestBindingError,
        coordination_sha256,
    )

    if (
        type(execution_pk) is not int
        or execution_pk <= 0
        or type(task_id) is not str
        or not task_id
        or type(current_generation) is not int
        or current_generation < 0
        or not attempt_protocol_versions
        or any(
            type(attempt) is not int or attempt <= 0 or type(protocol) is not int or protocol <= 0
            for attempt, protocol in attempt_protocol_versions.items()
        )
    ):
        raise AssertionError("Ray Job evidence coordinates were invalid")
    candidate_count = len(attempt_protocol_versions) * (current_generation + 1)
    if candidate_count > MAX_RAY_JOB_EVIDENCE_CANDIDATES:
        raise AssertionError("Ray Job evidence candidate set exceeded its bounded contract")

    candidates: dict[str, tuple[ExecutionIdentity, int]] = {}
    try:
        for attempt_number, protocol_version in sorted(attempt_protocol_versions.items()):
            for generation in range(current_generation + 1):
                identity = ExecutionIdentity(
                    task_execution_pk=execution_pk,
                    task_id=task_id,
                    attempt_number=attempt_number,
                    execution_generation=generation,
                )
                submission_id = (
                    f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}"
                    f"{coordination_sha256(identity)}"
                )
                if submission_id in candidates:
                    raise AssertionError("Ray Job evidence candidate IDs were not unique")
                candidates[submission_id] = (identity, protocol_version)
    except RayJobRequestBindingError as error:
        raise AssertionError("Ray Job evidence coordinates were invalid") from error
    return candidates


def _load_ray_job_submission_evidence(
    *, ray_address: str, execution_pk: int
) -> dict[int, tuple[int, str]]:
    """Read retained terminal Job API metadata without request bytes in JobInfo."""
    from django_ray.models import RayTaskExecution, TaskAttempt
    from django_ray.ray_job_protocol import (
        STRICT_RAY_JOB_SUBMISSION_ID_PREFIX,
        RayJobRequestBindingError,
        RayJobRequestExpectation,
        RayJobRequestReferenceExpectation,
        parse_ray_job_request_metadata,
        validate_ray_job_request_expectation,
        validate_ray_job_request_reference_expectation,
    )
    from django_ray.ray_job_request_storage import (
        RayJobRequestStorageError,
        ray_job_request_reference_content_identity,
    )
    from django_ray.runner.ray_job import (
        _address_pinned_job_client,
        _bounded_control_requests,
    )

    execution = RayTaskExecution.objects.only(
        "pk",
        "task_id",
        "attempt_number",
        "execution_generation",
        "ray_job_request_reference",
    ).get(pk=execution_pk)
    attempt_protocol_versions = dict(
        TaskAttempt.objects.filter(execution_id=execution_pk).values_list(
            "attempt_number",
            "execution_protocol_version",
        )
    )
    rq2_candidates = _build_rq2_submission_candidates(
        execution_pk=int(execution.pk),
        task_id=str(execution.task_id),
        attempt_protocol_versions=attempt_protocol_versions,
        current_generation=int(execution.execution_generation),
    )
    rq1_candidates = {
        f"{STRICT_RAY_JOB_SUBMISSION_ID_PREFIX}{submission_id.rsplit('_', 1)[1]}": candidate
        for submission_id, candidate in rq2_candidates.items()
    }

    client = _address_pinned_job_client(ray_address)
    deadline = time.monotonic() + 30
    observed_statuses: dict[int, str] = {}
    while time.monotonic() < deadline:
        submissions: dict[int, tuple[int, str]] = {}
        statuses: dict[int, str] = {}
        with _bounded_control_requests(client):
            jobs = client.list_jobs()
        for job in jobs:
            submission_id = job.submission_id
            if type(submission_id) is not str:
                continue
            rq2_candidate = rq2_candidates.get(submission_id)
            rq1_candidate = rq1_candidates.get(submission_id)
            if rq2_candidate is None and rq1_candidate is None:
                continue
            try:
                expectation = parse_ray_job_request_metadata(job.metadata, required=True)
            except RayJobRequestBindingError as error:
                raise AssertionError(
                    "candidate Ray Job metadata contained an invalid strict binding"
                ) from error
            candidate = rq2_candidate or rq1_candidate
            assert candidate is not None
            expected_identity, expected_protocol_version = candidate
            try:
                if rq2_candidate is not None:
                    if not isinstance(expectation, RayJobRequestReferenceExpectation):
                        raise AssertionError("rq2 candidate did not advertise rq2 metadata")
                    validation_options: dict[str, object] = {}
                    if (
                        expected_identity.attempt_number == execution.attempt_number
                        and expected_identity.execution_generation == execution.execution_generation
                    ):
                        request_reference = execution.ray_job_request_reference
                        if not request_reference:
                            raise AssertionError(
                                "current rq2 candidate lost its durable request reference"
                            )
                        try:
                            request_digest, request_size = (
                                ray_job_request_reference_content_identity(request_reference)
                            )
                        except RayJobRequestStorageError as error:
                            raise AssertionError(
                                "current rq2 request reference was invalid"
                            ) from error
                        validation_options = {
                            "expected_request_sha256": request_digest,
                            "expected_request_size_bytes": request_size,
                            "request_reference": request_reference,
                        }
                    validate_ray_job_request_reference_expectation(
                        expectation,
                        expected_identity=expected_identity,
                        expected_execution_protocol_version=expected_protocol_version,
                        expected_submission_id=submission_id,
                        **validation_options,
                    )
                else:
                    if not isinstance(expectation, RayJobRequestExpectation):
                        raise AssertionError("rq1 candidate did not advertise rq1 metadata")
                    validate_ray_job_request_expectation(
                        expectation,
                        expected_identity=expected_identity,
                        expected_execution_protocol_version=expected_protocol_version,
                    )
            except RayJobRequestBindingError as error:
                raise AssertionError(
                    "candidate Ray Job binding did not match durable state"
                ) from error

            attempt_number = expected_identity.attempt_number
            execution_generation = expected_identity.execution_generation
            identity = (execution_generation, submission_id)
            previous = submissions.setdefault(attempt_number, identity)
            if previous != identity:
                raise AssertionError(
                    f"attempt {attempt_number} had multiple Ray Job identities: "
                    f"{previous}, {identity}"
                )
            status = getattr(job.status, "value", job.status)
            statuses[attempt_number] = str(status)

        observed_statuses = statuses
        if set(submissions) == {1, 2} and all(
            status in {"STOPPED", "SUCCEEDED", "FAILED"} for status in statuses.values()
        ):
            # The outer process exits 0 after it durably delivers either envelope.
            # TaskAttempt, not Ray's transport status, records the first task failure.
            if statuses != {1: "SUCCEEDED", 2: "SUCCEEDED"}:
                raise AssertionError(
                    f"expected two successful Ray Job transports, found {statuses}"
                )
            return submissions
        time.sleep(0.25)

    raise AssertionError(
        f"Ray Job submissions did not reach retained terminal states: {observed_statuses}"
    )


def _stop_worker(worker: subprocess.Popen[str]) -> None:
    if worker.poll() is not None:
        return
    worker.terminate()
    try:
        worker.wait(timeout=15)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=15)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--worker"]:
        return _run_probe_worker()
    if arguments:
        raise AssertionError("Ray Data probe received unsupported arguments")
    import ray

    fixture = (
        '{"record_id":"a","value":1}\n'
        '{"record_id":"b","value":2}\n'
        '{"record_id":"c","value":3}\n'
        '{"record_id":"d","value":4}\n'
    )
    with tempfile.TemporaryDirectory(prefix="django-ray-data-probe-") as temporary:
        root = Path(temporary)
        input_root = root / "inputs"
        input_root.mkdir()
        output_root = root / "artifacts"
        output_root.mkdir()
        request_storage_root = root / "ray-job-requests"
        request_storage_root.mkdir()
        working_dir_archive = root / "ray-data-probe-working-dir.zip"
        _build_probe_working_dir_archive(working_dir_archive)
        os.environ["DJANGO_SETTINGS_MODULE"] = "testproject.settings"
        os.environ["DJANGO_DEPLOYMENT_MODE"] = "demo"
        os.environ["DATABASE_ENGINE"] = "django.db.backends.sqlite3"
        os.environ["DATABASE_NAME"] = str(root / "probe.sqlite3")
        os.environ["DJANGO_RAY_WORKING_DIR_URI"] = str(working_dir_archive)
        os.environ["DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE"] = "plaintext"
        os.environ["DJANGO_RAY_INPUT_STORAGE_BACKEND"] = "filesystem"
        os.environ["DJANGO_RAY_INPUT_STORAGE_FILESYSTEM_PATH"] = str(request_storage_root)
        os.environ["DJANGO_RAY_DATA_INPUT_ROOT"] = str(input_root)
        os.environ["DJANGO_RAY_DATA_OUTPUT_ROOT"] = str(output_root)
        os.environ["DJANGO_RAY_DATA_DEPLOYMENT_KEY"] = "real-ray-data-probe"
        os.environ["RAY_MAX_RETRIES"] = "2"
        os.environ["RAY_RETRY_DELAY_SECONDS"] = "0"

        import django
        from django.conf import settings as django_settings
        from django.core.management import call_command
        from django.db import connections

        # Do not let Ray's ``auto`` discovery attach this probe to another local
        # development cluster. Start one disposable cluster, then pin every
        # durable submission and worker control request to its exact address.
        os.environ.pop("RAY_ADDRESS", None)
        ray_context = ray.init(
            address="local",
            num_cpus=2,
            include_dashboard=True,
            log_to_driver=False,
        )
        probe_ray_address = str(ray_context.address_info.get("gcs_address", ""))
        if not probe_ray_address:
            ray.shutdown()
            raise AssertionError("local Ray probe did not expose its GCS address")
        os.environ["RAY_ADDRESS"] = probe_ray_address

        try:
            django.setup()
            _use_preinstalled_probe_dependencies(django_settings)
            call_command("migrate", "django_ray", interactive=False, verbosity=0)

            from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
            from django_ray.target.cohort_intent import CohortSelectionPolicy
            from django_ray.target.cohort_intent_storage import read_cohort_intent
            from testproject.apps.cluster_tasks.tasks import (
                RAY_DATA_AFTER_MANIFEST_FAILURE_FIXTURE,
                RAY_DATA_AFTER_MANIFEST_FAILURE_MESSAGE,
                ray_data_batch_score,
            )

            input_path = input_root / "input.jsonl"
            input_path.write_text(fixture, encoding="utf-8")
            enqueue_request = {
                "input_uri": input_path.as_uri(),
                "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                "run_key": "real-ray-data-probe",
                "application_revision": "probe-app-v1",
                "model_revision": "probe-model-v1",
                "scale": 2.0,
                "bias": 1.0,
                "failure_fixture": RAY_DATA_AFTER_MANIFEST_FAILURE_FIXTURE,
            }
        except BaseException:
            ray.shutdown()
            connections.close_all()
            raise
        worker: subprocess.Popen[str] | None = None
        worker_log_path = root / "ray-data-worker.log"
        try:
            task_result = ray_data_batch_score.enqueue(**enqueue_request)
            execution = RayTaskExecution.objects.get(task_id=task_result.id)
            intent = read_cohort_intent(execution.pk)
            if (
                execution.queue_name != "ray-data"
                or execution.runtime_env_profile != "ray-data"
                or execution.execution_protocol_version != 3
                or intent.backend_alias != "ray-data"
                or intent.selection_policy is not CohortSelectionPolicy.JOBS_ONLY
            ):
                raise AssertionError(
                    "Ray Data task was not durably routed to its dedicated profile"
                )
            if execution.ray_target_address != probe_ray_address:
                raise AssertionError("Ray Data task did not snapshot the disposable cluster target")

            connections.close_all()
            worker_environment = dict(os.environ)
            worker_environment["PYTHONUNBUFFERED"] = "1"
            with worker_log_path.open("w", encoding="utf-8") as worker_log:
                worker_started_after = datetime.now(UTC)
                worker = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                    ],
                    cwd=ROOT,
                    env=worker_environment,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                execution = _wait_for_recovered_execution(
                    execution.pk,
                    worker,
                    worker_log_path,
                    started_after=worker_started_after,
                    jobs_endpoint="http://" + str(ray_context.address_info["webui_url"]),
                )
                submissions = _load_ray_job_submission_evidence(
                    ray_address=probe_ray_address,
                    execution_pk=execution.pk,
                )

            if execution.state != TaskState.SUCCEEDED:
                raise AssertionError(
                    f"routed Ray Data task ended as {execution.state}: "
                    f"{execution.error_message}\n{_worker_log_tail(worker_log_path)}"
                )
            attempts = list(
                TaskAttempt.objects.filter(execution=execution).order_by("attempt_number")
            )
            attempt_states = [
                (int(attempt.attempt_number), str(attempt.state)) for attempt in attempts
            ]
            if attempt_states != [(1, TaskState.FAILED), (2, TaskState.SUCCEEDED)]:
                raise AssertionError(
                    f"expected one archived failure then success, found {attempt_states}"
                )
            if any(attempt.execution_protocol_version != 3 for attempt in attempts):
                raise AssertionError("Ray Data retry crossed execution protocols")
            if RAY_DATA_AFTER_MANIFEST_FAILURE_MESSAGE not in str(attempts[0].error_message or ""):
                raise AssertionError("first archived attempt did not retain fixture failure")
            if set(submissions) != {1, 2}:
                raise AssertionError(
                    f"expected two observed Ray Job attempts, found {sorted(submissions)}"
                )
            ray_job_ids = [submissions[number][1] for number in (1, 2)]
            if len(set(ray_job_ids)) != 2 or not all(
                ray_job_id.startswith("raysubmit_") for ray_job_id in ray_job_ids
            ):
                raise AssertionError(f"expected two distinct Ray Job submissions: {ray_job_ids}")
            if execution.ray_job_id != ray_job_ids[1]:
                raise AssertionError("durable success did not retain the second Ray Job identity")
            if execution.result_data is None:
                raise AssertionError("routed Ray Data task did not persist its bounded result")
            second = json.loads(execution.result_data)
            if not isinstance(second, dict):
                raise AssertionError("routed Ray Data task did not return bounded metadata")
            _verify_current_completion(execution)
            if not all(attempt.executor_django_ray_version for attempt in attempts):
                raise AssertionError("archived Ray Job attempts lost executor provenance")

            first_generation = submissions[1][0]
            _, _, first_completion_path = _attempt_paths(
                output_root,
                "real-ray-data-probe",
                "real-ray-data-probe",
                str(execution.task_id),
                execution.pk,
                first_generation,
                1,
            )
            first_manifest, first_completion_bytes = _read_completion_manifest(
                first_completion_path
            )
            first = _completion_result(
                first_manifest,
                manifest_uri=first_completion_path.as_uri(),
                manifest_bytes=first_completion_bytes,
            )
            if first["attempt_number"] != 1 or first["execution_generation"] != first_generation:
                raise AssertionError("failed artifact did not retain its first-attempt fence")
            try:
                validate_adoptable_artifact(
                    first,
                    durable_state=attempts[0].state,
                    output_root_uri=output_root.as_uri(),
                    deployment_key="real-ray-data-probe",
                    task_id=str(execution.task_id),
                    task_execution_pk=execution.pk,
                    execution_generation=first_generation,
                    attempt_number=1,
                )
            except ArtifactNotAdoptableError:
                failed_artifact_rejected = True
            else:
                raise AssertionError("failed durable attempt exposed an adoptable artifact")

            completion_path = _path_from_uri(second["manifest_uri"])
            _, completion_bytes = _read_completion_manifest(completion_path)
            if hashlib.sha256(completion_bytes).hexdigest() != second.get("manifest_sha256"):
                raise AssertionError(
                    "success result did not identify its exact completion manifest"
                )
            completion_mtime = completion_path.stat().st_mtime_ns
            adopted = validate_adoptable_artifact(
                second,
                durable_state=execution.state,
                output_root_uri=output_root.as_uri(),
                deployment_key="real-ray-data-probe",
                task_id=str(execution.task_id),
                task_execution_pk=execution.pk,
                execution_generation=execution.execution_generation,
                attempt_number=execution.attempt_number,
            )
            adopted_again = validate_adoptable_artifact(
                second,
                durable_state=execution.state,
                output_root_uri=output_root.as_uri(),
                deployment_key="real-ray-data-probe",
                task_id=str(execution.task_id),
                task_execution_pk=execution.pk,
                execution_generation=execution.execution_generation,
                attempt_number=execution.attempt_number,
            )
            if _read_completion_manifest(completion_path)[1] != completion_bytes:
                raise AssertionError("repeated artifact adoption changed the completion manifest")
            if completion_path.stat().st_mtime_ns != completion_mtime:
                raise AssertionError("repeated artifact adoption rewrote the completion manifest")
            if first["output_uri"] == second["output_uri"]:
                raise AssertionError("framework retry did not receive a new output namespace")
            try:
                validate_adoptable_artifact(
                    second,
                    durable_state=execution.state,
                    output_root_uri=output_root.as_uri(),
                    deployment_key="real-ray-data-probe",
                    task_id=str(execution.task_id),
                    task_execution_pk=execution.pk,
                    execution_generation=first_generation,
                    attempt_number=1,
                )
            except ArtifactNotAdoptableError:
                stale_fence_rejected = True
            else:
                raise AssertionError("successful artifact crossed an earlier attempt fence")

            for result in (first, second):
                _assert_metadata_only(result)
                encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
                if len(encoded) > MAX_RESULT_BYTES:
                    raise AssertionError("probe result exceeded the bounded result contract")
                if any(key in result for key in ("dataset", "object_ref", "rows", "batches")):
                    raise AssertionError(
                        "probe result leaked distributed data or a framework handle"
                    )

            expected = [
                {"record_id": "a", "value": 1, "score": 3.0},
                {"record_id": "b", "value": 2, "score": 5.0},
                {"record_id": "c", "value": 3, "score": 7.0},
                {"record_id": "d", "value": 4, "score": 9.0},
            ]
            if (
                _read_bounded_rows(
                    first["output_uri"],
                    expected_rows=4,
                    expected_bytes=first["output_bytes"],
                    expected_sha256=first["output_sha256"],
                )
                != expected
            ):
                raise AssertionError("first attempt produced unexpected rows")
            if (
                _read_bounded_rows(
                    second["output_uri"],
                    expected_rows=4,
                    expected_bytes=second["output_bytes"],
                    expected_sha256=second["output_sha256"],
                )
                != expected
            ):
                raise AssertionError("second attempt produced unexpected rows")

            second_output = _path_from_uri(second["output_uri"])
            tampered_file = _inspect_output_content(second_output)[0][0]
            with tampered_file.open("ab") as target:
                target.write(b"tampered")
            try:
                validate_adoptable_artifact(
                    second,
                    durable_state=execution.state,
                    output_root_uri=output_root.as_uri(),
                    deployment_key="real-ray-data-probe",
                    task_id=str(execution.task_id),
                    task_execution_pk=execution.pk,
                    execution_generation=execution.execution_generation,
                    attempt_number=execution.attempt_number,
                )
            except OutputChangedError:
                pass
            else:
                raise AssertionError("artifact adoption accepted tampered Parquet output")

            evidence = {
                "schema_version": 1,
                "outcome": "passed",
                "python_version": platform.python_version(),
                "ray_version": ray.__version__,
                "input_rows": 4,
                "attempts_completed": len(attempts),
                "ray_jobs_submitted": len(submissions),
                "ray_job_transports_succeeded": True,
                "strict_ray_job_request_binding": True,
                "request_reference_transport": True,
                "versioned_completion_envelope": True,
                "execution_protocol_version": 3,
                "current_cohort_claim_correlated": True,
                "executor_provenance_archived": True,
                "preinstalled_ray_data_environment": True,
                "disposable_cluster_target_pinned": True,
                "automatic_retry_recovered": True,
                "failed_artifact_rejected": failed_artifact_rejected,
                "idempotent_artifact_adoption": adopted_again == adopted,
                "stale_attempt_fence_rejected": stale_fence_rejected,
                "new_attempt_namespace_isolated": True,
                "bounded_json_result": True,
                "tampered_output_rejected": True,
                "management_worker_routed": True,
                "ray_job_submission": True,
                "durable_task_succeeded": True,
                "outer_ray_job_context": True,
                "completion_envelope_persisted": True,
                "artifact_adopted_after_success": adopted == json.loads(completion_bytes),
            }
            print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
        finally:
            if worker is not None:
                _stop_worker(worker)
            ray.shutdown()
            connections.close_all()

    if ray.is_initialized():
        raise AssertionError("Ray remained initialized after probe cleanup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
