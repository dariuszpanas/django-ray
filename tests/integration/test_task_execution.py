"""Exercise direct task entrypoints and the serial local-Ray boundary."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest
import ray

from tests.local_ray import init_local_ray

# Get project root
PROJECT_ROOT = Path(__file__).parent.parent.parent
_COHORT_NATIVE_TIMEOUT_SECONDS = 120
_COHORT_NATIVE_SETTINGS = "cohort_native_fixture.settings"
_COHORT_NATIVE_ENDPOINT = "http://127.0.0.1:8265"
_COHORT_NATIVE_SETTINGS_SOURCE = """
import os
from pathlib import Path

SECRET_KEY = "isolated-native-cohort-test-only"
INSTALLED_APPS = ["django_ray"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.environ["COHORT_PROBE_DB"], "OPTIONS": {"timeout": 5}}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
DJANGO_RAY = {"RAY_ADDRESS": "auto", "WORKER_LEASE_SECONDS": 180, "WORKER_HEARTBEAT_SECONDS": 5}

if os.environ.get("COHORT_PROBE_ROLE") == "driver":
    import ray
    assert ray.is_initialized() is False, "Django loaded before owned Ray cleanup"
    Path(os.environ["COHORT_PROBE_BOOTSTRAP_MARKER"]).write_text("after-cleanup", encoding="utf-8")
"""


def _native_cohort_manager(case: str, directory: str) -> None:
    """An isolated manager harness; never reuse pytest's in-memory database."""
    import hashlib
    from dataclasses import asdict
    from datetime import UTC, datetime

    import django
    from django.core.management import call_command
    from django.db import connections
    from ray.dashboard.modules.job.common import JobStatus
    from ray.job_submission import JobSubmissionClient
    from ray.runtime_env import RuntimeEnv

    if case == "application":
        _native_cohort_application_manager(Path(directory))
        return
    assert case in {"success", "wrong-package"}
    assert not ray.is_initialized()
    django.setup()
    call_command("migrate", verbosity=0, interactive=False)

    from django_ray import __version__
    from django_ray.models import (
        RayTarget,
        RayTargetProbeChallenge,
        RayTargetProbeJobReceipt,
        RayWorkerTargetCapability,
        TaskWorkerLease,
    )
    from django_ray.runner.leasing import WorkerLeaseIdentity
    from django_ray.runtime.cohort_job import (
        CohortProbeJobLease,
        CohortProbeJobRequest,
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
        cohort_probe_submitted_runtime_env_digest,
        inspect_reserved_cohort_job,
    )
    from django_ray.target.cohort_job_http import fetch_reserved_cohort_job_details
    from django_ray.target.cohort_job_receipt_storage import (
        read_cohort_job_reservation,
        reserve_cohort_job_probe,
    )
    from django_ray.target.cohort_probe import derive_cohort_target_key
    from django_ray.target.cohort_probe_challenges import issue_ray_target_probe_challenge
    from django_ray.target.cohort_runtime import _local_runtime

    base = Path(directory)
    package, runtime = _local_runtime(ray)
    expected_package = package if case == "success" else "0.0.0"
    # These are current-checkout paths in one admitted Linux host. This fixture
    # proves Jobs/bootstrap integration, not an installed-wheel/source-byte seal.
    spec = RuntimeEnv(
        env_vars={
            "DJANGO_SETTINGS_MODULE": _COHORT_NATIVE_SETTINGS,
            "PYTHONPATH": os.environ["PYTHONPATH"],
            "COHORT_PROBE_DB": os.environ["COHORT_PROBE_DB"],
            "COHORT_PROBE_BOOTSTRAP_MARKER": os.environ["COHORT_PROBE_BOOTSTRAP_MARKER"],
            "COHORT_PROBE_ROLE": "driver",
        }
    ).to_dict()
    configuration = {
        "endpoint": _COHORT_NATIVE_ENDPOINT,
        "package": expected_package,
        "runtime": asdict(runtime),
        "runtime_env_digest": cohort_probe_submitted_runtime_env_digest(spec),
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    now = datetime.now(UTC)
    lease = TaskWorkerLease.objects.create(
        worker_id="native-cohort-manager",
        hostname="native-cohort-host",
        pid=os.getpid(),
        started_at=now,
        last_heartbeat_at=now,
        capability_schema_version=1,
        django_ray_version=__version__,
        min_supported_execution_protocol_version=3,
        max_supported_execution_protocol_version=3,
        legacy_admission_token=None,
    )
    identity = WorkerLeaseIdentity(lease.pk, lease.hostname, lease.pid, lease.started_at)
    issued = issue_ray_target_probe_challenge(
        identity,
        digest,
        runner_family=RayRunnerFamily.RAY_JOB,
        now=datetime.now(UTC),
        ttl_seconds=120,
    )
    request = CohortProbeJobRequest(
        challenge_id=issued.receipt.challenge_id,
        challenge_revision=issued.receipt.revision,
        lease=CohortProbeJobLease(
            identity.worker_id, identity.hostname, identity.pid, identity.started_at
        ),
        configuration_digest=digest,
        target_key=None,
        runner_family=RayRunnerFamily.RAY_JOB,
        expected_package_version=expected_package,
        expected_runtime=runtime,
        expected_cluster_session=None,
        expected_target_policy_id=None,
        policy_revision=1,
        issued_at=issued.receipt.issued_at,
        expires_at=issued.receipt.expires_at,
    )
    launch = CohortProbeJobLaunch(
        request=request,
        request_digest=probe_job_request_digest(request),
        jobs_endpoint=_COHORT_NATIVE_ENDPOINT,
        submitted_runtime_env_digest=cohort_probe_submitted_runtime_env_digest(spec),
        django_settings_module=_COHORT_NATIVE_SETTINGS,
    )
    command = probe_job_launch_entrypoint(launch)
    reserved = reserve_cohort_job_probe(
        identity,
        request,
        nonce=issued.nonce,
        jobs_endpoint=_COHORT_NATIVE_ENDPOINT,
        entrypoint=command,
        submitted_runtime_env=spec,
    )
    assert reserved.changed
    assert all(
        not connection.in_atomic_block and connection.get_autocommit()
        for connection in connections.all(initialized_only=True)
    )
    row = RayTargetProbeJobReceipt.objects.get(pk=request.challenge_id)
    assert row.receipt_json is None
    submission_id = probe_job_submission_id(request)
    # Parent cleanup owns this known handle even if submission loses its reply.
    # The raw consumption nonce is never persisted in a file or remote carrier.
    (base / "submission-id").write_text(submission_id, encoding="ascii")
    diagnostic_path = base / "job-diagnostic.json"
    diagnostic_path.write_text(
        json.dumps({"phase": "reserved", "job_state": "unknown", "driver_refusal": "unavailable"}),
        encoding="utf-8",
    )
    client = JobSubmissionClient(_COHORT_NATIVE_ENDPOINT)
    submitted = client.submit_job(
        submission_id=submission_id,
        entrypoint=command,
        runtime_env=spec,
        metadata=probe_job_metadata(request),
    )
    assert submitted == submission_id
    deadline = time.monotonic() + 90
    observed = None
    while time.monotonic() < deadline:
        observed = fetch_reserved_cohort_job_details(
            _COHORT_NATIVE_ENDPOINT,
            submission_id,
            timeout_seconds=min(5.0, max(0.001, deadline - time.monotonic())),
        )
        refusal = next(
            (
                reason.value
                for reason in CohortJobEntrypointReason
                if f"Cohort probe driver refused: {reason.value}" in (observed.message or "")
            ),
            "unavailable",
        )
        diagnostic_path.write_text(
            json.dumps(
                {
                    "phase": "observed",
                    "job_state": observed.status.value,
                    "driver_refusal": refusal,
                    "native_driver_recorded": observed.job_id is not None,
                    "bootstrap_marker": (base / "bootstrap-marker").exists(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        if observed.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED}:
            break
        time.sleep(0.1)
    assert observed is not None
    marker = base / "bootstrap-marker"
    row.refresh_from_db()
    pending = RayTargetProbeChallenge.objects.get(pk=request.challenge_id).consumed_at is None
    no_eligibility = (
        not RayTarget.objects.exists() and not RayWorkerTargetCapability.objects.exists()
    )
    if case == "success":
        assert observed.status is JobStatus.SUCCEEDED, observed.status
        assert marker.read_text(encoding="utf-8") == "after-cleanup"
        assert row.receipt_json is not None
        snapshot = read_cohort_job_reservation(
            identity, request, jobs_endpoint=_COHORT_NATIVE_ENDPOINT
        )
        assert snapshot is not None
        inspected = inspect_reserved_cohort_job(snapshot)
        assert inspected is not None
        assert inspected.receipt.native_job_id == observed.job_id
        expectation = inspected.receipt.attestation.expectation
        assert request.target_key is None
        assert expectation.target_key == derive_cohort_target_key(
            RayRunnerFamily.RAY_JOB, expectation.cluster_session
        )
        result = {
            "bootstrap_after_cleanup": True,
            "pending_receipt": True,
            "inspected_succeeded_native_id": True,
            "challenge_unconsumed": pending,
            "no_eligibility": no_eligibility,
        }
    else:
        assert observed.status is JobStatus.FAILED, observed.status
        # This negative assertion identifies the intended refusal; logs are
        # never used as provenance for the positive inspection above.
        assert observed.driver_exit_code == 1
        assert "Cohort probe driver refused: runtime_mismatch" in (observed.message or "")
        assert observed.job_id is None
        assert not marker.exists()
        assert row.receipt_json is None and row.receipt_digest is None and row.received_at is None
        assert (
            read_cohort_job_reservation(identity, request, jobs_endpoint=_COHORT_NATIVE_ENDPOINT)
            is None
        )
        result = {
            "runtime_mismatch_refusal": True,
            "no_django_bootstrap": True,
            "no_native_driver": True,
            "no_receipt": True,
            "challenge_unconsumed": pending,
            "no_eligibility": no_eligibility,
        }
    assert not ray.is_initialized()
    connections.close_all()
    (base / "result.json").write_text(json.dumps(result, sort_keys=True), encoding="utf-8")


def _native_cohort_application_diagnostic(
    details, *, submission_id, command, metadata, spec, marker
):
    """Fixed observations only; this does not authenticate an application result."""
    from ray.dashboard.modules.job.pydantic_models import JobStatus, JobType

    return {
        "phase": "application-observed",
        "job_state": details.status.value if type(details.status) is JobStatus else "unknown",
        "submission_matches": (
            details.type is JobType.SUBMISSION and details.submission_id == submission_id
        ),
        "native_driver_matches": (
            details.job_id is not None
            and details.driver_info is not None
            and details.driver_info.id == details.job_id
        ),
        "entrypoint_matches": details.entrypoint == command,
        "metadata_matches": details.metadata == metadata,
        "runtime_env_matches": details.runtime_env == spec,
        "application_marker": marker.exists(),
        "fixed_entrypoint_refusal": "django-ray cohort Job execution refused"
        in (details.message or "")[:4096],
    }


def _native_cohort_application_manager(base: Path) -> None:
    """Exercise the real rq2 CLI without inventing manager/DB claim eligibility."""
    import hashlib

    import django
    from django.core.management import call_command
    from django.db import connections
    from ray.dashboard.modules.job.pydantic_models import JobStatus, JobType
    from ray.job_submission import JobSubmissionClient
    from ray.runtime_env import RuntimeEnv

    from django_ray.execution_protocol import ExecutionProtocolRange
    from django_ray.ray_job_protocol import (
        STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
        _build_cohort_job_metadata,
        coordination_sha256,
    )
    from django_ray.ray_job_request_storage import _prepare_ray_job_request
    from django_ray.runtime.cohort_entrypoint import _fetch_execution_job
    from django_ray.target.cohort_contract import decode_cohort_execution_contract
    from tests.native_cohort import prepared_execution

    assert ray.is_initialized() is False
    django.setup()
    call_command("migrate", verbosity=0, interactive=False)
    contract = decode_cohort_execution_contract(
        (base / "contract.json").read_text(encoding="utf-8")
    )
    marker = base / "application-marker.json"
    prepared = prepared_execution(
        contract,
        "tests.native_cohort.async_cli_marker",
        json.dumps([str(marker)]),
        transport="ray-job",
    )
    stored = _prepare_ray_job_request(
        prepared.request_json,
        {
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(base / "requests"),
        },
        supported_protocols=ExecutionProtocolRange(3, 3),
    )
    submission_id = STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX + coordination_sha256(
        prepared.identity
    )
    command = (
        "python -m django_ray.runtime.cohort_entrypoint --request-ref-b64 " + stored.encoded_locator
    )
    spec = RuntimeEnv(
        py_executable=sys.executable,
        env_vars={
            "DJANGO_SETTINGS_MODULE": _COHORT_NATIVE_SETTINGS,
            "PYTHONPATH": os.environ["PYTHONPATH"],
            "COHORT_PROBE_DB": os.environ["COHORT_PROBE_DB"],
            "COHORT_PROBE_ROLE": "application",
            "RAY_ENABLE_UV_RUN_RUNTIME_ENV": "0",
        },
    ).to_dict()
    metadata = _build_cohort_job_metadata(prepared, stored.reference, stored.encoded_locator)
    metadata.update(
        cohort_jobs_endpoint=_COHORT_NATIVE_ENDPOINT,
        cohort_submitted_runtime_env_digest="sha256:"
        + hashlib.sha256(
            json.dumps(
                spec,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest(),
    )
    # The outer parent owns this exact submission before a possibly lost ACK.
    (base / "submission-id").write_text(submission_id, encoding="ascii")
    assert (
        JobSubmissionClient(_COHORT_NATIVE_ENDPOINT).submit_job(
            submission_id=submission_id,
            entrypoint=command,
            runtime_env=spec,
            metadata=metadata,
        )
        == submission_id
    )
    deadline = time.monotonic() + 90
    details = None
    while time.monotonic() < deadline:
        details = _fetch_execution_job(_COHORT_NATIVE_ENDPOINT, submission_id)
        (base / "job-diagnostic.json").write_text(
            json.dumps(
                _native_cohort_application_diagnostic(
                    details,
                    submission_id=submission_id,
                    command=command,
                    metadata=metadata,
                    spec=spec,
                    marker=marker,
                ),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        if details.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED}:
            break
        time.sleep(0.1)
    assert details is not None and details.status is JobStatus.SUCCEEDED
    assert details.type is JobType.SUBMISSION and details.submission_id == submission_id
    assert details.driver_info is not None and details.driver_info.id == details.job_id
    assert (
        details.entrypoint == command
        and details.metadata == metadata
        and details.runtime_env == spec
    )
    with marker.open(encoding="utf-8") as source:
        effect = json.loads(source.read(2048))
    assert effect == {
        "value": 42,
        "protocol": 3,
        "task_id": prepared.identity.task_id,
        "native_job_id": details.job_id,
        "awaited": True,
    }
    assert ray.is_initialized() is False
    connections.close_all()
    (base / "result.json").write_text(
        json.dumps(
            {
                "awaited_application": True,
                "current_context": True,
                "physical_native_id": True,
                "submitted_controls": True,
                "no_manager_ray_connection": True,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _native_cohort_environment(tmp_path: Path) -> dict[str, str]:
    """Prepare one source-owned settings package without connecting to a resource."""
    package = tmp_path / "cohort_native_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "settings.py").write_text(_COHORT_NATIVE_SETTINGS_SOURCE, encoding="utf-8")
    environment = os.environ.copy()
    # Ray's high-level submission client lets these override even an explicit
    # endpoint. This fixture's submission and stop must stay on its owned host.
    for name in (
        "RAY_API_SERVER_ADDRESS",
        "RAY_ADDRESS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": _COHORT_NATIVE_SETTINGS,
            "PYTHONPATH": os.pathsep.join(
                (str(tmp_path), str(PROJECT_ROOT / "src"), str(PROJECT_ROOT))
            ),
            "COHORT_PROBE_DB": str(tmp_path / "probe.sqlite3"),
            "COHORT_PROBE_BOOTSTRAP_MARKER": str(tmp_path / "bootstrap-marker"),
            "COHORT_PROBE_ROLE": "manager",
        }
    )
    return environment


def _native_cohort_stop_owned_job(endpoint: str, submission_id: str) -> None:
    """A stop acknowledgment is asynchronous; observe terminal before returning."""
    from ray.dashboard.modules.job.common import JobStatus
    from ray.job_submission import JobSubmissionClient

    from django_ray.ray_job_protocol import is_valid_rq2_ray_job_submission_id
    from django_ray.runtime.cohort_entrypoint import _fetch_execution_job
    from django_ray.target.cohort_job_http import fetch_reserved_cohort_job_details

    deadline = time.monotonic() + 10
    client = JobSubmissionClient(endpoint)
    client.stop_job(submission_id)
    while time.monotonic() < deadline:
        details = (
            _fetch_execution_job(endpoint, submission_id)
            if is_valid_rq2_ray_job_submission_id(submission_id)
            else fetch_reserved_cohort_job_details(
                endpoint,
                submission_id,
                timeout_seconds=min(2.0, max(0.001, deadline - time.monotonic())),
            )
        )
        if details.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED}:
            return
        time.sleep(0.1)
    raise AssertionError("Exact owned cohort Job did not reach terminal state")


def _native_cohort_case(tmp_path: Path, case: str) -> dict[str, bool]:
    """Bound the manager separately from the exact owned remote Job's cleanup."""
    environment = _native_cohort_environment(tmp_path)
    manager_script = """
import json
import sys
try:
    from tests.integration.test_task_execution import _native_cohort_manager
    _native_cohort_manager(sys.argv[1], sys.argv[2])
except Exception as error:
    name = type(error).__name__
    if not name.isascii() or not name.isidentifier() or len(name) > 64:
        name = 'unknown'
    print(json.dumps({'phase': 'manager-failed', 'error_type': name}), flush=True)
    raise SystemExit(1)
"""
    log_path = tmp_path / "manager.log"
    manager_timed_out = False
    cleanup_failed = False
    try:
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-c", manager_script, case, str(tmp_path)],
                cwd=PROJECT_ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                process.wait(timeout=_COHORT_NATIVE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                manager_timed_out = True
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
    finally:
        owned = tmp_path / "submission-id"
        if owned.exists():
            with owned.open("r", encoding="ascii") as handle_file:
                handle = handle_file.read(128)
            from django_ray.ray_job_protocol import is_valid_rq2_ray_job_submission_id
            from django_ray.target.cohort_job_http import _arguments

            _arguments(
                _COHORT_NATIVE_ENDPOINT,
                "django-ray-cohort-probe-" + "0" * 64
                if is_valid_rq2_ray_job_submission_id(handle)
                else handle,
                5.0,
            )
            cleanup_script = (
                "import sys; from tests.integration.test_task_execution "
                "import _native_cohort_stop_owned_job; "
                "_native_cohort_stop_owned_job(sys.argv[1], sys.argv[2])"
            )
            with (tmp_path / "cleanup.log").open("wb") as cleanup_log:
                try:
                    cleanup = subprocess.run(
                        [sys.executable, "-c", cleanup_script, _COHORT_NATIVE_ENDPOINT, handle],
                        cwd=PROJECT_ROOT,
                        env=environment,
                        stdout=cleanup_log,
                        stderr=subprocess.STDOUT,
                        timeout=15,
                    )
                    cleanup_failed = cleanup.returncode != 0
                except subprocess.TimeoutExpired:
                    cleanup_failed = True
    with log_path.open("rb") as log:
        log.seek(0, os.SEEK_END)
        log.seek(max(0, log.tell() - 8192))
        diagnostic = log.read(8192).decode("utf-8", errors="replace")
    if (tmp_path / "job-diagnostic.json").exists():
        with (tmp_path / "job-diagnostic.json").open("r", encoding="utf-8") as job_diagnostic:
            diagnostic += job_diagnostic.read(2048)
    assert not cleanup_failed, "Exact owned Job cleanup failed. " + diagnostic
    assert not manager_timed_out, "Native cohort manager exceeded its deadline. " + diagnostic
    assert process.returncode == 0, diagnostic
    with (tmp_path / "result.json").open("r", encoding="utf-8") as result_file:
        return json.loads(result_file.read(2048))


def _ray_worker_execution_metadata_probe() -> dict[str, object]:
    """Read django-ray execution metadata inside a real Ray worker."""
    import sys

    from django_ray.runtime.remote import _ray_execution_metadata

    return {
        "ray_loaded_before_probe": "ray" in sys.modules,
        "metadata": _ray_execution_metadata(),
    }


@pytest.fixture(scope="module")
def ray_cluster():
    """Start the required local Ray cluster for the serial real-Ray lane."""
    if ray.is_initialized():
        raise RuntimeError("Required local Ray fixture found an initialized runtime")
    try:
        init_local_ray(include_dashboard=True)
    except Exception as exc:
        raise RuntimeError("Required local Ray startup failed") from exc
    try:
        yield
    finally:
        # The serial Linux test runner supplies the enclosing execution deadline.
        ray.shutdown(wait_for_processes=True)


@pytest.fixture
def django_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide entrypoint import state and restore it after every test."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")

    # Ensure paths are set up
    src_path = str(PROJECT_ROOT / "src")
    root_path = str(PROJECT_ROOT)

    if src_path not in sys.path:
        monkeypatch.syspath_prepend(src_path)
    if root_path not in sys.path:
        monkeypatch.syspath_prepend(root_path)


class TestEntrypointExecution:
    """Test the task entrypoint execution directly."""

    def test_execute_simple_task(self, django_settings_env):
        """Test executing a simple task through the entrypoint."""
        from django_ray.runtime.entrypoint import execute_task

        result_json = execute_task(
            callable_path="testproject.tasks.add_numbers",
            serialized_args="[2, 3]",
            serialized_kwargs="{}",
        )

        result = json.loads(result_json)
        assert result["success"] is True
        assert result["result"] == 5
        assert result["error"] is None

    def test_execute_task_with_kwargs(self, django_settings_env):
        """Test executing a task with keyword arguments."""
        from django_ray.runtime.entrypoint import execute_task

        result_json = execute_task(
            callable_path="testproject.tasks.echo_task",
            serialized_args='["hello"]',
            serialized_kwargs='{"key": "value"}',
        )

        result = json.loads(result_json)
        assert result["success"] is True
        assert result["result"]["args"] == ["hello"]
        assert result["result"]["kwargs"] == {"key": "value"}

    def test_execute_async_task_value_and_event_loop_cleanup(self, django_settings_env):
        """The entrypoint awaits a coroutine and closes its per-task event loop."""
        from django_ray.runtime.entrypoint import execute_task

        result = json.loads(
            execute_task(
                callable_path="testproject.tasks.async_add_numbers",
                serialized_args="[20, 22]",
                serialized_kwargs="{}",
            )
        )

        assert result["success"] is True
        assert result["result"] == 42
        assert result["exception_type"] is None
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()

    def test_execute_async_task_preserves_underlying_exception(self, django_settings_env):
        """Retry classification sees the coroutine's exception rather than a wrapper."""
        from django_ray.runtime.entrypoint import execute_task

        result = json.loads(
            execute_task(
                callable_path="testproject.tasks.async_failing_task",
                serialized_args="[]",
                serialized_kwargs="{}",
            )
        )

        assert result["success"] is False
        assert result["error"] == "Async task requested a retryable failure"
        assert result["exception_type"] == "builtins.ValueError"
        assert result["retryable"] is True

    def test_async_parent_cancellation_cleans_up_pending_child(
        self,
        django_settings_env,
        monkeypatch,
    ):
        """Closing the cancelled task loop awaits pending-child cleanup."""
        from django_ray.runtime import import_utils
        from django_ray.runtime.entrypoint import execute_task

        events: list[str] = []

        async def pending_child() -> None:
            events.append("child-started")
            try:
                await asyncio.Event().wait()
            finally:
                events.append("child-cleaned")

        async def cancelled_parent() -> None:
            asyncio.create_task(pending_child())
            await asyncio.sleep(0)
            raise asyncio.CancelledError

        monkeypatch.setattr(import_utils, "import_callable", lambda _path: cancelled_parent)

        with pytest.raises(asyncio.CancelledError):
            execute_task("testproject.tasks.cancelled_parent", "[]", "{}")

        assert events == ["child-started", "child-cleaned"]
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()

    @pytest.mark.django_db(transaction=True)
    def test_execute_async_task_preserves_context_and_supports_async_orm(
        self,
        django_settings_env,
    ):
        """ContextVars cross awaits and Django's async ORM can read durable state."""
        from django_ray.models import RayTaskExecution, TaskState
        from django_ray.runtime.entrypoint import execute_task

        execution = RayTaskExecution.objects.create(
            task_id="async-context-entrypoint-001",
            callable_path="testproject.tasks.async_context_probe",
            state=TaskState.RUNNING,
        )

        result = json.loads(
            execute_task(
                callable_path=execution.callable_path,
                serialized_args='["entrypoint"]',
                serialized_kwargs='{"load_execution": true}',
                task_execution_pk=execution.pk,
                ray_job_driver=False,
            )
        )

        assert result["success"] is True
        assert result["result"] == {
            "value": "entrypoint",
            "execution_id_before": execution.pk,
            "execution_id_after": execution.pk,
            "ray_job_driver_before": False,
            "ray_job_driver_after": False,
            "task_id": execution.task_id,
            "active_task_count": 1,
            "loop_running": True,
        }

    def test_execute_failing_task(self, django_settings_env):
        """Test that failing tasks return error information."""
        from django_ray.runtime.entrypoint import execute_task

        result_json = execute_task(
            callable_path="testproject.tasks.failing_task",
            serialized_args="[]",
            serialized_kwargs="{}",
        )

        result = json.loads(result_json)
        assert result["success"] is False
        assert "This task is designed to fail" in result["error"]
        assert result["traceback"] is not None

    def test_execute_nonexistent_task(self, django_settings_env):
        """Test executing a task that doesn't exist."""
        from django_ray.runtime.entrypoint import execute_task

        result_json = execute_task(
            callable_path="testproject.tasks.nonexistent_task",
            serialized_args="[]",
            serialized_kwargs="{}",
        )

        result = json.loads(result_json)
        assert result["success"] is False
        assert "nonexistent_task" in result["error"]


@pytest.mark.real_ray
class TestRayRemoteExecution:
    """Test executing tasks as Ray remote functions."""

    def test_cohort_probe_job_persists_pending_receipt_after_cleanup(self, ray_cluster, tmp_path):
        assert _native_cohort_case(tmp_path, "success") == {
            "bootstrap_after_cleanup": True,
            "pending_receipt": True,
            "inspected_succeeded_native_id": True,
            "challenge_unconsumed": True,
            "no_eligibility": True,
        }

    def test_cohort_probe_job_package_mismatch_precedes_django(self, ray_cluster, tmp_path):
        assert _native_cohort_case(tmp_path, "wrong-package") == {
            "runtime_mismatch_refusal": True,
            "no_django_bootstrap": True,
            "no_native_driver": True,
            "no_receipt": True,
            "challenge_unconsumed": True,
            "no_eligibility": True,
        }

    def test_ray_worker_reports_execution_metadata(self, ray_cluster):
        """Ray workers preload the module required by the metadata fast path."""
        probe = ray.remote(_ray_worker_execution_metadata_probe)

        result = ray.get(probe.remote())

        assert result["ray_loaded_before_probe"] is True
        metadata = result["metadata"]
        assert isinstance(metadata, dict)
        assert set(metadata) == {
            "assigned_resources",
            "ray_job_id",
            "ray_node_id",
            "ray_task_id",
            "ray_worker_id",
        }
        assert all(
            metadata[key] for key in ("ray_job_id", "ray_node_id", "ray_task_id", "ray_worker_id")
        )
        assert metadata["assigned_resources"].get("CPU") == 1.0

    def test_ray_remote_task(self, django_settings_env, ray_cluster):
        """Test running a task via Ray remote."""

        @ray.remote
        def remote_add(a: int, b: int) -> int:
            # Setup Django before importing tasks with @task decorator
            import os

            import django

            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testproject.settings")
            django.setup()

            # Import inside the remote function
            from testproject.tasks import add_numbers

            # add_numbers is a Django Task object, use .call() to execute
            return add_numbers.call(a, b)

        result = ray.get(remote_add.remote(10, 20))
        assert result == 30

    def test_ray_remote_entrypoint(self, django_settings_env, ray_cluster):
        """Test running the entrypoint via Ray remote."""

        @ray.remote
        def remote_execute(callable_path: str, args: str, kwargs: str) -> str:
            import os
            import sys

            # Set up environment
            os.environ["DJANGO_SETTINGS_MODULE"] = "testproject.settings"

            # Add paths
            project_root = Path(__file__).parent.parent.parent
            src_path = str(project_root / "src")
            root_path = str(project_root)

            if src_path not in sys.path:
                sys.path.insert(0, src_path)
            if root_path not in sys.path:
                sys.path.insert(0, root_path)

            from django_ray.runtime.entrypoint import execute_task

            return execute_task(callable_path, args, kwargs)

        result_json = ray.get(
            remote_execute.remote(
                "testproject.tasks.multiply_numbers",
                "[7, 6]",
                "{}",
            )
        )

        result = json.loads(result_json)
        assert result["success"] is True
        assert result["result"] == 42

    def test_ray_core_runs_async_task_through_package_entrypoint(
        self,
        django_settings_env,
        ray_cluster,
    ):
        """The actual current outer guard preserves awaited task context and errors."""
        from dataclasses import replace

        from django_ray.execution_codec import ExecutionIdentity
        from django_ray.runtime.remote import execute_cohort_django_task_remote
        from tests.native_cohort import (
            decode_completion,
            observed_contract,
            prepared_execution,
            remote_expectations,
        )

        contract = observed_contract(ray, ExecutionIdentity(4242, "async-core", 1, 1))
        remote_entrypoint: Any = ray.remote(cast(Any, execute_cohort_django_task_remote)).options(
            runtime_env={"env_vars": {"DJANGO_SETTINGS_MODULE": "testproject.settings"}}
        )
        prepared = prepared_execution(
            contract, "tests.native_cohort.async_context_probe", '["ray-core"]'
        )
        result = decode_completion(
            ray.get(
                remote_entrypoint.remote(
                    prepared.request_json,
                    **remote_expectations(prepared),
                ),
                timeout=30,
            ),
            prepared,
        )
        assert result.success is True
        assert result.result == {
            "value": "ray-core",
            "execution_id_before": 4242,
            "execution_id_after": 4242,
            "ray_job_driver_before": False,
            "ray_job_driver_after": False,
            "task_id": "async-core",
            "active_task_count": 1,
            "loop_running": True,
        }
        failed_contract = replace(
            contract, identity=ExecutionIdentity(4243, "async-core-fail", 1, 1)
        )
        failed = prepared_execution(failed_contract, "testproject.tasks.async_failing_task", "[]")
        failure = decode_completion(
            ray.get(
                remote_entrypoint.remote(
                    failed.request_json,
                    **remote_expectations(failed),
                ),
                timeout=30,
            ),
            failed,
        )
        assert failure.success is False
        assert failure.error == "Async task requested a retryable failure"
        assert failure.exception_type == "builtins.ValueError"

    def test_ray_core_runs_one_strict_versioned_request(
        self,
        django_settings_env,
        ray_cluster,
    ):
        """A real worker validates a current outer contract and completion identity."""
        from django_ray.execution_codec import ExecutionIdentity
        from django_ray.runtime.remote import execute_cohort_django_task_remote
        from tests.native_cohort import (
            decode_completion,
            observed_contract,
            prepared_execution,
            remote_expectations,
        )

        identity = ExecutionIdentity(4244, "strict-ray-core-request", 2, 3)
        prepared = prepared_execution(
            observed_contract(ray, identity),
            "testproject.tasks.add_numbers",
            "[20,22]",
        )
        remote_entrypoint: Any = ray.remote(cast(Any, execute_cohort_django_task_remote)).options(
            runtime_env={"env_vars": {"DJANGO_SETTINGS_MODULE": "testproject.settings"}}
        )
        completion = decode_completion(
            ray.get(
                remote_entrypoint.remote(
                    prepared.request_json,
                    **remote_expectations(prepared),
                ),
                timeout=30,
            ),
            prepared,
        )
        assert completion.success is True and completion.result == 42
        assert completion.identity == identity
        assert completion.execution_protocol_version == 3
        from django_ray import __version__

        assert completion.executor_django_ray_version == __version__

    def test_ray_core_current_guard_refuses_wrong_runtime_before_application(
        self,
        django_settings_env,
        ray_cluster,
    ):
        """A canonical but incompatible contract yields an owned pre-invocation refusal."""
        from dataclasses import replace

        from django_ray.execution_codec import ExecutionIdentity
        from django_ray.runtime.remote import execute_cohort_django_task_remote
        from django_ray.target.attestation import (
            build_ray_cluster_attestation,
            build_ray_node_observation,
            ray_target_expectation_digest,
        )
        from django_ray.target.cohort_transport import decode_cohort_execution_result
        from tests.native_cohort import observed_contract, prepared_execution, remote_expectations

        actual = observed_contract(ray, ExecutionIdentity(4246, "runtime-mismatch", 1, 1))
        runtime = actual.target_expectation.runtime
        wrong = replace(runtime, python_patch=runtime.python_patch + 1)
        expectation = replace(actual.target_expectation, runtime=wrong)
        evidence = actual.claim_attestation
        incompatible = build_ray_cluster_attestation(
            expectation=expectation,
            boundary=evidence.boundary,
            nodes=tuple(
                build_ray_node_observation(
                    node_id=node.node_id,
                    cluster_session=expectation.cluster_session,
                    runtime=wrong,
                )
                for node in evidence.nodes
            ),
            observed_at=evidence.observed_at,
            expires_at=evidence.expires_at,
        )
        contract = replace(
            actual,
            target_expectation=expectation,
            target_expectation_digest=ray_target_expectation_digest(expectation),
            claim_attestation=incompatible,
            claim_attestation_digest=incompatible.attestation_digest,
        )
        prepared = prepared_execution(
            contract, "tests.native_cohort.application_must_not_import", "[]"
        )
        remote_entrypoint: Any = ray.remote(cast(Any, execute_cohort_django_task_remote))
        encoded = ray.get(
            remote_entrypoint.remote(
                prepared.request_json,
                **remote_expectations(prepared),
            ),
            timeout=30,
        )
        rejected = decode_cohort_execution_result(
            encoded,
            expected_identity=prepared.identity,
            expected_request_digest=prepared.request_digest,
            expected_cohort_contract_digest=prepared.contract_digest,
        )
        assert rejected.refusal == "runtime_mismatch"
        assert rejected.application_invoked is False
        assert rejected.completion_json is None

    def test_ray_job_runs_async_task_through_cli_entrypoint(
        self,
        django_settings_env,
        ray_cluster,
        tmp_path,
    ):
        """Prove current rq2 transport, guarded context, and an awaited application effect.

        This marker/physical-Job proof does not verify durable completion.success,
        its serialized result, or manager consumption. Those remain part of the
        real qualified Jobs release evidence.
        """
        from django_ray.execution_codec import ExecutionIdentity
        from django_ray.target.attestation import RayRunnerFamily
        from django_ray.target.cohort_contract import encode_cohort_execution_contract
        from tests.native_cohort import observed_contract

        contract = observed_contract(
            ray,
            ExecutionIdentity(4250, "native-current-cli", 1, 1),
            family=RayRunnerFamily.RAY_JOB,
        )
        (tmp_path / "contract.json").write_text(
            encode_cohort_execution_contract(contract),
            encoding="utf-8",
        )
        assert _native_cohort_case(tmp_path, "application") == {
            "awaited_application": True,
            "current_context": True,
            "physical_native_id": True,
            "submitted_controls": True,
            "no_manager_ray_connection": True,
        }


def test_native_async_probe_reads_bound_context_without_database_access():
    from django_ray.runtime.context import durable_task_execution
    from tests.native_cohort import async_context_probe

    with durable_task_execution(
        4242,
        task_id="async-core",
        execution_protocol_version=3,
        attempt_number=1,
        execution_generation=1,
        ray_job_driver=False,
        strict_execution_request=True,
    ):
        assert asyncio.run(async_context_probe("ray-core")) == {
            "value": "ray-core",
            "execution_id_before": 4242,
            "execution_id_after": 4242,
            "ray_job_driver_before": False,
            "ray_job_driver_after": False,
            "task_id": "async-core",
            "active_task_count": 1,
            "loop_running": True,
        }


@pytest.mark.parametrize("matches", [False, True])
def test_native_application_diagnostics_keep_provider_content_private(tmp_path, matches):
    from types import SimpleNamespace

    from ray.dashboard.modules.job.pydantic_models import JobStatus, JobType

    private = "private-provider-value"
    marker = tmp_path / "marker"
    if matches:
        marker.write_text("application effect", encoding="utf-8")
    details = SimpleNamespace(
        status=JobStatus.SUCCEEDED if matches else JobStatus.FAILED,
        type=JobType.SUBMISSION,
        submission_id="owned" if matches else private,
        job_id="01000000",
        driver_info=SimpleNamespace(id="01000000" if matches else private),
        entrypoint="fixed" if matches else private,
        metadata={"expected": True} if matches else {"secret": private},
        runtime_env={} if matches else {"env_vars": {"TOKEN": private}},
        message=f"{private}: django-ray cohort Job execution refused",
    )
    result = _native_cohort_application_diagnostic(
        details,
        submission_id="owned",
        command="fixed",
        metadata={"expected": True},
        spec={},
        marker=marker,
    )
    assert result == {
        "phase": "application-observed",
        "job_state": "SUCCEEDED" if matches else "FAILED",
        "submission_matches": matches,
        "native_driver_matches": matches,
        "entrypoint_matches": matches,
        "metadata_matches": matches,
        "runtime_env_matches": matches,
        "application_marker": matches,
        "fixed_entrypoint_refusal": True,
    }
    assert private not in json.dumps(result) and "TOKEN" not in json.dumps(result)
    details.message = "x" * 4096 + "django-ray cohort Job execution refused"
    assert (
        _native_cohort_application_diagnostic(
            details, submission_id="owned", command="fixed", metadata={}, spec={}, marker=marker
        )["fixed_entrypoint_refusal"]
        is False
    )


def test_generic_inline_cli_refuses_before_application(tmp_path):
    """The obsolete inline CLI never becomes an alternate current entry."""
    marker = tmp_path / "must-not-exist"
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "callable_path": "tests.native_cohort.async_cli_marker",
                "serialized_args": json.dumps([str(marker)]),
                "serialized_kwargs": "{}",
            }
        ).encode()
    ).decode("ascii")
    result = subprocess.run(
        [sys.executable, "-m", "django_ray.runtime.entrypoint", "--payload-b64", payload],
        cwd=PROJECT_ROOT,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert b"execution request rejected" in result.stderr.lower()


@pytest.mark.django_db
class TestModelIntegration:
    """Test task execution with Django models."""

    @pytest.fixture
    def task_execution(self, django_settings_env):
        """Create a RayTaskExecution model instance."""
        import django
        from django.apps import apps

        if not apps.ready:
            django.setup()

        from django_ray.models import RayTaskExecution, TaskState

        task = RayTaskExecution.objects.create(
            task_id="test-task-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
        )
        yield task
        task.delete()

    def test_create_task_execution(self, task_execution):
        """Test creating a task execution record."""
        from django_ray.models import TaskState

        assert task_execution.pk is not None
        assert task_execution.state == TaskState.QUEUED
        assert task_execution.attempt_number == 1

    def test_update_task_state(self, task_execution):
        """Current rows require a real ledger claim before RUNNING."""
        from django.db import IntegrityError, transaction

        from django_ray.models import TaskState

        task_execution.state = TaskState.RUNNING
        with pytest.raises(IntegrityError), transaction.atomic():
            task_execution.save()
        task_execution.refresh_from_db()
        assert task_execution.state == TaskState.QUEUED
        assert task_execution.execution_protocol_version == 3
