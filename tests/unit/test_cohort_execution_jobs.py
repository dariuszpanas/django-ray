"""Physical rq2 Jobs association and pre-Django protocol-3 point checks."""

import builtins
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import ray
from ray.dashboard.modules.job.pydantic_models import DriverInfo, JobDetails, JobStatus, JobType

from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.ray_job_protocol import (
    RAY_JOB_CONFIG_JSON_ENV_VAR,
    STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
    _build_cohort_job_metadata,
    coordination_sha256,
)
from django_ray.ray_job_request_storage import _prepare_ray_job_request
from django_ray.runtime import cohort_entrypoint as entrypoint
from django_ray.target.attestation import RayRunnerFamily
from django_ray.target.cohort_contract import (
    cohort_execution_contract_digest,
    encode_cohort_execution_contract,
)
from django_ray.target.cohort_transport import (
    CohortExecutionResult,
    PreparedCohortExecution,
    cohort_execution_request_digest,
    encode_cohort_execution_request,
    encode_cohort_execution_result,
)
from tests.unit.test_cohort_execution import completed
from tests.unit.test_cohort_job_http import transport as transport  # noqa: F401
from tests.unit.test_cohort_runtime import _contract
from tests.unit.test_execution_codec import _inline_request


@pytest.fixture
def job_request(tmp_path):
    contract = _contract(family=RayRunnerFamily.RAY_JOB)
    request = replace(
        _inline_request(contract.identity),
        execution_protocol_version=3,
        compiled_graph_submission_transport="ray-job",
        cohort_contract_json=encode_cohort_execution_contract(contract),
    )
    prepared = PreparedCohortExecution(
        contract.identity,
        encode_cohort_execution_request(request),
        cohort_execution_request_digest(request),
        cohort_execution_contract_digest(contract),
    )
    stored = _prepare_ray_job_request(
        prepared.request_json,
        {"INPUT_STORAGE_BACKEND": "filesystem", "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path)},
        supported_protocols=ExecutionProtocolRange(3, 3),
    )
    metadata = _build_cohort_job_metadata(prepared, stored.reference, stored.encoded_locator)
    endpoint = "http://ray.example.test:8265/prefix"
    submission_id = STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX + coordination_sha256(
        contract.identity
    )
    metadata.update(
        cohort_jobs_endpoint=endpoint,
        cohort_submitted_runtime_env_digest="sha256:" + hashlib.sha256(b"{}").hexdigest(),
    )
    config = {"metadata": dict(metadata, job_submission_id=submission_id, job_name=submission_id)}
    return SimpleNamespace(
        contract=contract,
        prepared=prepared,
        stored=stored,
        metadata=metadata,
        endpoint=endpoint,
        submission_id=submission_id,
        config=config,
    )


def details(job):
    assert JobDetails is not None and DriverInfo is not None
    return JobDetails(
        type=JobType.SUBMISSION,
        submission_id=job.submission_id,
        job_id="01000000",
        status=JobStatus.RUNNING,
        metadata=job.metadata,
        runtime_env={},
        entrypoint="python -m django_ray.runtime.cohort_entrypoint --request-ref-b64 "
        + job.stored.encoded_locator,
        driver_info=DriverInfo(id="01000000", node_ip_address="10.0.0.1", pid="123"),
    )


def test_reference_and_independent_metadata_validate_without_application_import(
    job_request, monkeypatch
):
    job = job_request
    original = builtins.__import__

    def poison(name, *args, **kwargs):
        if name == "django" or name.startswith(("django.", "testproject.")):
            pytest.fail("rq2 preflight imported application")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", poison)
    loaded = entrypoint._load_request(job.stored.encoded_locator, json.dumps(job.config))
    assert loaded == (job.prepared, job.contract, job.endpoint, job.submission_id, job.metadata)


@pytest.mark.parametrize("job_name", [None, "expected", "substituted"])
def test_ray_supervisor_injected_defaults_and_user_override(job_request, monkeypatch, job_name):
    """Exercise Ray's actual metadata composition without creating a GCS client."""
    from ray.dashboard.modules.job import job_supervisor

    job = job_request
    submitted = dict(job.metadata)
    if job_name is not None:
        submitted["job_name"] = job.submission_id if job_name == "expected" else "other-name"
    original = dict(submitted)
    monkeypatch.setattr(job_supervisor, "GcsClient", lambda **_kwargs: object())
    monkeypatch.setattr(job_supervisor, "JobInfoStorageClient", lambda *_args: object())
    monkeypatch.setattr(job_supervisor, "JobLogStorageClient", lambda: object())
    monkeypatch.setattr(job_supervisor.JobSupervisor, "_configure_logger", lambda _self: None)
    monkeypatch.setattr(
        ray, "init", lambda **_kwargs: pytest.fail("Metadata check initialized Ray")
    )
    supervisor = job_supervisor.JobSupervisor(
        job.submission_id, "unused", submitted, "unused:6379", "unused"
    )
    assert submitted == original
    assert supervisor._metadata == {
        "job_submission_id": job.submission_id,
        "job_name": job.submission_id,
        **submitted,
    }
    config = json.dumps({"metadata": supervisor._metadata})
    if job_name == "substituted":
        # Ray permits this override, but our fixed prepared metadata does not.
        with pytest.raises(entrypoint.CohortJobEntrypointError):
            entrypoint._load_request(job.stored.encoded_locator, config)
    else:
        loaded = entrypoint._load_request(job.stored.encoded_locator, config)
        assert loaded == (job.prepared, job.contract, job.endpoint, job.submission_id, job.metadata)
        assert "job_name" not in loaded[-1] and "job_submission_id" not in loaded[-1]


def test_missing_injected_job_name_is_refused(job_request):
    job = job_request
    del job.config["metadata"]["job_name"]
    with pytest.raises(entrypoint.CohortJobEntrypointError):
        entrypoint._load_request(job.stored.encoded_locator, json.dumps(job.config))


@pytest.mark.parametrize(
    "field",
    [
        "cohort_request_digest",
        "cohort_contract_digest",
        "cohort_jobs_endpoint",
        "job_submission_id",
        "job_name",
        "extra",
    ],
)
def test_crossed_metadata_never_initializes_ray(job_request, monkeypatch, field):
    job = job_request
    if field.endswith("digest"):
        job.config["metadata"][field] = "sha256:" + "f" * 64
    elif field == "cohort_jobs_endpoint":
        job.config["metadata"][field] = "ray://ray.example.test:10001"
    else:
        job.config["metadata"][field] = "crossed"
    monkeypatch.setattr(
        ray, "init", lambda **_kwargs: pytest.fail("Crossed request initialized Ray")
    )
    with pytest.raises(ValueError):
        entrypoint.execute_cohort_task_from_reference(
            job.stored.encoded_locator,
            environment={
                RAY_JOB_CONFIG_JSON_ENV_VAR: json.dumps(job.config),
                "RAY_ADDRESS": "ray:6379",
            },
        )


@pytest.mark.parametrize(
    "change",
    [
        "physical_job",
        "driver_id",
        "submission",
        "type",
        "status",
        "command",
        "environment",
        "metadata",
        "injected_job_name",
        "injected_job_submission_id",
    ],
)
def test_driver_binding_requires_actual_jobs_record_not_injected_submission_id(
    job_request, monkeypatch, change
):
    job = job_request
    record = details(job)
    if change == "physical_job":
        record.job_id = "02000000"
    elif change == "driver_id":
        record.driver_info.id = "02000000"
    elif change == "submission":
        record.submission_id = "other"
    elif change == "type":
        record.type = JobType.DRIVER
    elif change == "status":
        record.status = JobStatus.SUCCEEDED
    elif change == "command":
        record.entrypoint = "python application.py"
    elif change == "environment":
        record.runtime_env = {"env_vars": {"SECRET": "private"}}
    elif change.startswith("injected_"):
        record.metadata = dict(
            job.metadata, **{change.removeprefix("injected_"): job.submission_id}
        )
    else:
        record.metadata = dict(job.metadata, other="private")
    monkeypatch.setattr(entrypoint, "_fetch_execution_job", lambda *_args: record)
    fake = SimpleNamespace(
        get_runtime_context=lambda: SimpleNamespace(get_job_id=lambda: "01000000")
    )
    with pytest.raises(entrypoint.CohortJobEntrypointError) as error:
        entrypoint._corroborate_driver(
            fake, job.endpoint, job.submission_id, job.metadata, job.stored.encoded_locator
        )
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "address", ["auto", "local", "ray://ray:10001", "https://ray:8265", "host:6379/path"]
)
def test_driver_uses_only_jobs_injected_gcs_address(job_request, monkeypatch, address):
    job = job_request
    monkeypatch.setattr(ray, "is_initialized", lambda: False)
    monkeypatch.setattr(
        ray, "init", lambda **_kwargs: pytest.fail("Unbound driver address connected")
    )
    with pytest.raises(ValueError):
        entrypoint.execute_cohort_task_from_reference(
            job.stored.encoded_locator,
            environment={
                RAY_JOB_CONFIG_JSON_ENV_VAR: json.dumps(job.config),
                "RAY_ADDRESS": address,
            },
        )


def test_job_execution_orders_physical_association_then_guard_and_owned_shutdown(
    job_request, monkeypatch
):
    from django_ray.runtime import cohort_execution

    job = job_request
    calls = []
    monkeypatch.setattr(ray, "is_initialized", lambda: False)
    monkeypatch.setattr(ray, "init", lambda **kwargs: calls.append(("init", kwargs)))
    monkeypatch.setattr(ray, "shutdown", lambda: calls.append("shutdown"))
    monkeypatch.setattr(
        ray, "get_runtime_context", lambda: SimpleNamespace(get_job_id=lambda: "01000000")
    )

    def fetch(endpoint, handle):
        assert endpoint == job.endpoint and handle == job.submission_id
        calls.append("physical_record")
        return details(job)

    monkeypatch.setattr(entrypoint, "_fetch_execution_job", fetch)

    def execute(serialized, **kwargs):
        assert calls[-1] == "physical_record"
        assert serialized == job.prepared.request_json and kwargs["ray_job_driver"] is True
        calls.append("guarded_application")
        return encode_cohort_execution_result(
            CohortExecutionResult(
                job.prepared.identity,
                job.prepared.request_digest,
                job.prepared.contract_digest,
                completion_json=completed(job.prepared),
            )
        )

    monkeypatch.setattr(cohort_execution, "execute_cohort_request", execute)
    result = entrypoint.execute_cohort_task_from_reference(
        job.stored.encoded_locator,
        environment={
            RAY_JOB_CONFIG_JSON_ENV_VAR: json.dumps(job.config),
            "RAY_ADDRESS": "ray:6379",
        },
    )
    assert result.completion_json == completed(job.prepared)
    assert calls == [
        ("init", {"address": "ray:6379", "log_to_driver": False}),
        "physical_record",
        "guarded_application",
        "shutdown",
    ]


def test_main_never_prints_untrusted_exception_or_completion(monkeypatch, capsys):
    def fail(*_args):
        raise RuntimeError("private-token")

    monkeypatch.setattr(entrypoint, "execute_cohort_task_from_reference", fail)
    assert entrypoint.main(["--request-ref-b64", "opaque"]) == 78
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "django-ray cohort Job execution refused\n"


def test_jobs_runner_uses_exact_qualified_endpoint_and_binds_uploaded_controls(
    settings, tmp_path, monkeypatch
):
    from django_ray.runner.ray_job import RayJobRunner
    from django_ray.target.cohort_transport import prepare_cohort_execution
    from tests.unit.test_ray_core_runner import _task_execution
    from tests.unit.test_ray_job import FakeJobClient

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    claim = _contract(family=RayRunnerFamily.RAY_JOB)
    task = _task_execution(
        claim.identity.task_execution_pk,
        task_id=claim.identity.task_id,
        attempt_number=claim.identity.attempt_number,
        execution_generation=claim.identity.execution_generation,
        execution_protocol_version=3,
        callable_path="testproject.tasks.add_numbers",
        ray_target_address="ray://unrelated:10001",
        claimed_by_worker="owned-worker",
    )
    prepared = prepare_cohort_execution(task, contract=claim, transport="ray-job")
    endpoint = "https://qualified.example.test:8265/prefix"
    client = FakeJobClient()
    calls = []
    runner = RayJobRunner()

    def attach(stored, *, task_execution, submission_handle, supported_protocols, restore_purged):
        assert restore_purged is False
        assert task_execution is task and supported_protocols == ExecutionProtocolRange(3, 3)
        assert stored.request.execution_protocol_version == 3
        assert submission_handle.ray_address == endpoint
        calls.append("attached")
        return stored.reference

    monkeypatch.setattr(
        "django_ray.ray_job_request_storage._register_and_attach_ray_job_request", attach
    )

    def get_client(address):
        assert address == endpoint and calls == ["attached"]
        return client

    monkeypatch.setattr(runner, "_get_client", get_client)
    handle = runner.cohort_submission_handle(task, jobs_endpoint=endpoint)
    assert (
        runner.submit_cohort_task(task, prepared=prepared, jobs_endpoint=endpoint).ray_job_id
        == handle.ray_job_id
    )
    assert len(client.submissions) == 1
    submitted = client.submissions[0]
    assert submitted["entrypoint"].startswith(
        "python -m django_ray.runtime.cohort_entrypoint --request-ref-b64 "
    )
    metadata = submitted["metadata"]
    assert isinstance(metadata, dict)
    assert "job_submission_id" not in metadata
    assert "job_name" not in metadata
    assert metadata["cohort_jobs_endpoint"] == endpoint
    assert metadata["cohort_request_digest"] == prepared.request_digest
    assert metadata["cohort_contract_digest"] == prepared.contract_digest
    assert (
        metadata["cohort_submitted_runtime_env_digest"]
        == "sha256:" + hashlib.sha256(b"{}").hexdigest()
    )


@pytest.mark.parametrize("change", ["local-content", "trust-identity"])
def test_jobs_snapshot_rejects_drift_from_owned_prepared_plan_before_storage(
    settings, tmp_path, monkeypatch, change
):
    from django_ray.runner.ray_job import RayJobRunner
    from django_ray.runtime.runtime_env import normalize_runtime_env
    from django_ray.target.cohort_transport import prepare_cohort_execution
    from django_ray.workflow.plans import WorkflowPlanMismatchError
    from tests.unit.test_ray_core_runner import _task_execution

    source_path = tmp_path / "application"
    source_path.mkdir()
    content = source_path / "owned.py"
    content.write_text("VALUE = 1\n")
    runtime = normalize_runtime_env({"working_dir": str(source_path)})
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PLAN_TRUST_IDENTITY": {"environment_revision": "v1"},
    }
    contract = _contract(family=RayRunnerFamily.RAY_JOB)
    task = _task_execution(
        contract.identity.task_execution_pk,
        task_id=contract.identity.task_id,
        attempt_number=contract.identity.attempt_number,
        execution_generation=contract.identity.execution_generation,
        execution_protocol_version=3,
        callable_path="testproject.tasks.add_numbers",
        claimed_by_worker="owned-worker",
        runtime_env_hash=runtime.digest,
        runtime_env_json=runtime.serialized,
        runtime_env_profile=runtime.profile,
    )
    prepared = prepare_cohort_execution(task, contract=contract, transport="ray-job")
    if change == "local-content":
        content.write_text("VALUE = 2\n")
    else:
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "WORKFLOW_PLAN_TRUST_IDENTITY": {"environment_revision": "v2"},
        }
    runner = RayJobRunner()
    source = runner._capture_cohort_submission(
        task,
        prepared=prepared,
        handle=runner.cohort_submission_handle(task, jobs_endpoint="http://ray.example:8265"),
    )
    monkeypatch.setattr(
        "django_ray.ray_job_request_storage._prepare_ray_job_request",
        lambda *_args, **_kwargs: pytest.fail("drifted request reached storage"),
    )
    with pytest.raises(WorkflowPlanMismatchError), runner._prepare_cohort_submission(source):
        pytest.fail("drifted request became attachable")


def test_execution_http_is_address_pinned_bounded_and_keeps_probe_ids_separate(
    job_request, transport
):
    job = job_request
    transport.body = details(job).model_dump_json().encode()
    observed = entrypoint._fetch_execution_job(job.endpoint, job.submission_id)
    assert observed.job_id == "01000000" and observed.submission_id == job.submission_id
    assert transport.requests[0][0] == job.endpoint + "/api/jobs/" + job.submission_id
    options = transport.requests[0][1]
    assert options["allow_redirects"] is False and options["stream"] is True
    assert options["proxies"] == {} and options["headers"]["Accept-Encoding"] == "identity"
    assert transport.closed_responses == transport.closed_sessions == 1
    with pytest.raises(entrypoint.CohortJobEntrypointError):
        entrypoint._fetch_execution_job(job.endpoint, "django-ray-cohort-probe-" + "a" * 64)
    assert len(transport.requests) == 1


def test_execution_http_refuses_crossed_submission_after_bounded_parse(job_request, transport):
    from django_ray.target.cohort_job_http import CohortJobHttpError

    job = job_request
    record = details(job)
    record.submission_id = "raysubmit_django_ray_rq2_" + "f" * 64
    transport.body = record.model_dump_json().encode()
    with pytest.raises(CohortJobHttpError):
        entrypoint._fetch_execution_job(job.endpoint, job.submission_id)
