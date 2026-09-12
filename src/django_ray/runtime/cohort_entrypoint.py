"""Fixed protocol-3 Jobs driver; all application imports follow point guards.

The rq2 locator and independent Jobs metadata bind an opaque request. Before
application entry, the native driver is also corroborated against the physical
submission's address-pinned Jobs record. Injected metadata alone cannot prove
that association. No stdout, exit code or Job status is a not-invoked receipt.
"""

from __future__ import annotations

import json
import os
import sys

from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.ray_job_protocol import (
    RAY_JOB_CONFIG_JSON_ENV_VAR,
    RAY_JOB_REQUEST_REJECTED_EXIT_CODE,
    STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
    RayJobRequestReferenceExpectation,
    _bounded_json_int,
    _build_cohort_job_metadata,
    _duplicate_safe_object,
    is_valid_rq2_ray_job_submission_id,
    load_ray_job_request_expectation,
    validate_ray_job_request_reference_expectation,
)
from django_ray.ray_job_request_storage import (
    _load_ray_job_request,
    decode_ray_job_request_locator,
)
from django_ray.target.cohort_contract import CohortExecutionContract, _digest
from django_ray.target.cohort_intent import _endpoint
from django_ray.target.cohort_transport import (
    PreparedCohortExecution,
    decode_cohort_execution_request,
    decode_cohort_execution_result,
)

_PROTOCOLS = ExecutionProtocolRange(3, 3)


class CohortJobEntrypointError(ValueError):
    def __init__(self):
        super().__init__("Cohort Job execution refused")


def _reject():
    raise CohortJobEntrypointError() from None


def _load_request(locator_json, config_json):
    expectation = load_ray_job_request_expectation(config_json)
    if type(expectation) is not RayJobRequestReferenceExpectation:
        _reject()
    validate_ray_job_request_reference_expectation(
        expectation, expected_execution_protocol_version=3, request_locator=locator_json
    )
    config = json.loads(
        config_json, object_pairs_hook=_duplicate_safe_object, parse_int=_bounded_json_int
    )
    metadata = config["metadata"]
    expected_request_digest = _digest(metadata.get("cohort_request_digest"))
    expected_contract_digest = _digest(metadata.get("cohort_contract_digest"))
    endpoint = _endpoint(metadata.get("cohort_jobs_endpoint"))
    if not endpoint.startswith(("http://", "https://")):
        _reject()
    runtime_digest = _digest(metadata.get("cohort_submitted_runtime_env_digest"))
    submission_id = (
        STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX + expectation.coordination_sha256
    )
    # This is an integrity check only; _corroborate_driver separately compares
    # the actual native Job ID against the independently fetched physical job.
    if metadata.get("job_submission_id") != submission_id:
        _reject()
    locator = decode_ray_job_request_locator(locator_json)
    validate_ray_job_request_reference_expectation(
        expectation,
        expected_request_sha256=locator.digest,
        expected_request_size_bytes=locator.size_bytes,
        request_reference=locator.reference,
        expected_submission_id=submission_id,
    )
    loaded = _load_ray_job_request(
        locator, supported_protocols=_PROTOCOLS, expected_execution_protocol_version=3
    )
    request, contract = decode_cohort_execution_request(
        loaded.serialized_request,
        expected_identity=loaded.request.identity,
        expected_request_digest=expected_request_digest,
        expected_cohort_contract_digest=expected_contract_digest,
    )
    if (
        type(contract) is not CohortExecutionContract
        or request.compiled_graph_submission_transport != "ray-job"
    ):
        _reject()
    validate_ray_job_request_reference_expectation(
        expectation,
        expected_identity=request.identity,
        expected_execution_protocol_version=3,
        expected_request_sha256=loaded.digest,
        expected_request_size_bytes=loaded.size_bytes,
        serialized_request=loaded.serialized_request,
        request_reference=loaded.reference,
    )
    prepared = PreparedCohortExecution(
        request.identity,
        loaded.serialized_request,
        expected_request_digest,
        expected_contract_digest,
    )
    expected_metadata = _build_cohort_job_metadata(prepared, loaded.reference, locator_json)
    expected_metadata.update(
        cohort_jobs_endpoint=endpoint, cohort_submitted_runtime_env_digest=runtime_digest
    )
    # Ray's supervisor injects both defaults before applying submitted metadata.
    # Our fixed submitted mapping contains neither key; require its exact defaults
    # here and keep the API JobDetails comparison submitted-only below.
    if metadata != dict(expected_metadata, job_submission_id=submission_id, job_name=submission_id):
        _reject()
    return prepared, contract, endpoint, submission_id, expected_metadata


def _fetch_execution_job(endpoint, submission_id):
    """Reuse bounded parsing while keeping reserved probe-ID policy unchanged."""
    from django_ray.target.cohort_job_http import _arguments, _Budget, _decode_details, _read_body

    if not is_valid_rq2_ray_job_submission_id(submission_id):
        _reject()
    endpoint, _, timeout = _arguments(endpoint, "django-ray-cohort-probe-" + "0" * 64, 5)
    budget = _Budget(timeout)
    import requests
    from ray.dashboard.modules.dashboard_sdk import SubmissionClient

    client = SubmissionClient(address=endpoint)
    if client._address != endpoint:
        _reject()
    headers = dict(client._headers, **{"Accept-Encoding": "identity"})
    with requests.Session() as session:
        session.trust_env = False
        session.proxies.clear()
        remaining = budget.remaining()
        response = session.get(
            endpoint + "/api/jobs/" + submission_id,
            headers=headers,
            cookies=client._cookies,
            verify=client._verify,
            proxies={},
            stream=True,
            allow_redirects=False,
            timeout=(remaining, remaining),
        )
        try:
            if type(response.status_code) is not int or response.status_code != 200:
                _reject()
            body = _read_body(response, budget)
        finally:
            response.close()
    result = _decode_details(body, submission_id)
    budget.remaining()
    return result


def _corroborate_driver(ray, endpoint, submission_id, metadata, locator):
    import hashlib

    from ray.dashboard.modules.job.pydantic_models import JobStatus, JobType

    from django_ray.target.cohort_job_receipt import is_canonical_native_ray_job_id

    native_id = ray.get_runtime_context().get_job_id()
    if not is_canonical_native_ray_job_id(native_id):
        _reject()
    details = _fetch_execution_job(endpoint, submission_id)
    if (
        details.type is not JobType.SUBMISSION
        or details.status is not JobStatus.RUNNING
        or details.submission_id != submission_id
        or details.job_id != native_id
        or details.driver_info is None
        or details.driver_info.id != native_id
        or details.metadata != metadata
        or details.entrypoint
        != "python -m django_ray.runtime.cohort_entrypoint --request-ref-b64 " + locator
        or type(details.runtime_env) is not dict
    ):
        _reject()
    # Match the submitted record, not the driver's injected env_vars. This is
    # the existing normalized JSON representation, without filesystem planning.
    serialized = json.dumps(
        details.runtime_env, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if (
        "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        != metadata["cohort_submitted_runtime_env_digest"]
    ):
        _reject()


def execute_cohort_task_from_reference(encoded_locator, *, environment=None):
    environment = os.environ if environment is None else environment
    prepared, contract, endpoint, submission_id, metadata = _load_request(
        encoded_locator, environment.get(RAY_JOB_CONFIG_JSON_ENV_VAR)
    )
    from django_ray import __version__

    if __version__ != contract.expected_django_ray_version:
        _reject()
    import ray

    from django_ray.runtime.cohort_job import _gcs_address
    from django_ray.target.cohort_runtime import _local_runtime
    from django_ray.target.probe import RAY_TARGET_PROBE_RAY_VERSION

    package, runtime = _local_runtime(ray)
    if (
        package != contract.expected_django_ray_version
        or runtime != contract.target_expectation.runtime
        or ray.__version__ != RAY_TARGET_PROBE_RAY_VERSION
        or ray.is_initialized() is not False
    ):
        _reject()
    address = _gcs_address(environment)
    try:
        ray.init(address=address, log_to_driver=False)
        _corroborate_driver(ray, endpoint, submission_id, metadata, encoded_locator)
        from django_ray.runtime.cohort_execution import execute_cohort_request

        result = execute_cohort_request(
            prepared.request_json,
            expected_identity=prepared.identity,
            expected_request_digest=prepared.request_digest,
            expected_cohort_contract_digest=prepared.contract_digest,
            ray_job_driver=True,
        )
        return decode_cohort_execution_result(
            result,
            expected_identity=prepared.identity,
            expected_request_digest=prepared.request_digest,
            expected_cohort_contract_digest=prepared.contract_digest,
        )
    finally:
        # An error/hang here remains uncertain to the manager. Neither this
        # local return nor Jobs terminal status proves remote cleanup.
        ray.shutdown()


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if (
        type(arguments) not in {list, tuple}
        or len(arguments) != 2
        or arguments[0] != "--request-ref-b64"
    ):
        return RAY_JOB_REQUEST_REJECTED_EXIT_CODE
    try:
        result = execute_cohort_task_from_reference(arguments[1])
    except Exception:
        print("django-ray cohort Job execution refused", file=sys.stderr)
        return RAY_JOB_REQUEST_REJECTED_EXIT_CODE
    return RAY_JOB_REQUEST_REJECTED_EXIT_CODE if result.refusal is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
