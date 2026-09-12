"""Dormant process-side operations for one owned fixed probe Job.

The parent reserves before submit, owns the external deadline and authenticates
publication. These operations never initialize Django/Ray or receive the nonce.
RuntimeEnv JSON is private IPC material, never a diagnostic. Profile checksums
bind trusted preparation; they do not authenticate source bytes or hooks which
run before the fixed driver. Ordinary task RuntimeEnv behavior is unchanged.

Ray Client address discovery is deliberately unavailable here: Ray 2.58's SDK
resolver initializes a client driver. An owned, supervised discovery/cleanup
adapter is required before activating that existing ordinary Jobs route.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Never

from django_ray.execution_codec import _preparse_json_scan, _unique_object, _validate_json_tree
from django_ray.runtime.cohort_job import (
    decode_probe_job_request,
    probe_job_metadata,
    probe_job_submission_id,
)
from django_ray.runtime.cohort_job_entrypoint import (
    decode_probe_job_launch,
    probe_job_launch_entrypoint,
)
from django_ray.target.attestation import (
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    encode_ray_target_expectation,
)
from django_ray.target.cohort_contract import _digest, _package_version, _timestamp
from django_ray.target.cohort_intent import _endpoint
from django_ray.target.cohort_job_control import (
    COHORT_PROBE_RUNTIME_ENV_MAX_BYTES,
    CohortJobReservationSnapshot,
    cohort_probe_entrypoint_digest,
    cohort_probe_submitted_runtime_env_digest,
)
from django_ray.target.cohort_job_http import (
    _arguments,
    _Budget,
    _read_body,
    fetch_reserved_cohort_job_details,
)
from django_ray.target.cohort_runtime import _local_runtime
from django_ray.target.probe import RAY_TARGET_PROBE_RAY_VERSION

COHORT_JOB_COMMAND_MAX_BYTES = 3 * 1024 * 1024
COHORT_JOB_RESULT_MAX_BYTES = 256 * 1024
_PROFILE_MAX_BYTES = COHORT_PROBE_RUNTIME_ENV_MAX_BYTES
_RECEIPT_MAX_BYTES = 1_064_960
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_PACKAGE = re.compile(r"/api/packages/gcs/[A-Za-z0-9_][A-Za-z0-9_.+-]{0,511}\.(?:zip|whl)")
_DUMMY_HANDLE = "django-ray-cohort-probe-" + "0" * 64


class CohortJobProcessControlReason(StrEnum):
    INVALID = "invalid"
    RESOURCE_LIMIT = "resource_limit"
    LOCAL_RUNTIME_MISMATCH = "local_runtime_mismatch"
    UNSUPPORTED_ADDRESS = "unsupported_address"
    PREPARATION_FAILED = "preparation_failed"
    SUBMISSION_UNCONFIRMED = "submission_unconfirmed"
    INSPECTION_FAILED = "inspection_failed"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"
    RESPONSE_MISMATCH = "response_mismatch"


class CohortJobProcessControlError(RuntimeError):
    def __init__(self, reason: CohortJobProcessControlReason):
        self.reason = reason
        super().__init__(f"Cohort Job control refused: {reason.value}")


def _reject(reason: CohortJobProcessControlReason) -> Never:
    raise CohortJobProcessControlError(reason) from None


def _canonical(value: object, *, maximum: int) -> str:
    _validate_json_tree(
        value,
        allow_nonfinite=False,
        allow_nul=False,
        max_depth=16,
        max_nodes=8192,
        max_string_bytes=_RECEIPT_MAX_BYTES,
    )
    serialized = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    if len(serialized.encode("utf-8")) > maximum:
        _reject(CohortJobProcessControlReason.RESOURCE_LIMIT)
    return serialized


def _json(serialized: object, *, maximum: int, canonical: bool = True) -> dict:
    if type(serialized) is not str or len(serialized.encode("utf-8")) > maximum:
        _reject(CohortJobProcessControlReason.INVALID)
    _preparse_json_scan(serialized, max_depth=16, max_nodes=8192)
    value = json.loads(serialized, object_pairs_hook=_unique_object)
    if type(value) is not dict:
        _reject(CohortJobProcessControlReason.INVALID)
    retained = _canonical(value, maximum=maximum)
    if canonical and retained != serialized:
        _reject(CohortJobProcessControlReason.INVALID)
    return value


def _shape(payload: object, keys: set[str]):
    if type(payload) is not dict or set(payload) != keys:
        _reject(CohortJobProcessControlReason.INVALID)
    _canonical(payload, maximum=COHORT_JOB_COMMAND_MAX_BYTES)


def _runtime(version: object, value: object):
    try:
        _package_version(version)
        if type(value) is RayRuntimeVersion:
            runtime = value
        elif type(value) is dict and set(value) == {
            item.name for item in fields(RayRuntimeVersion)
        }:
            runtime = RayRuntimeVersion(**value)
        else:
            raise ValueError
        encode_ray_target_expectation(
            RayTargetExpectation(
                "validation", RayRunnerFamily.RAY_JOB, "session_validation", 1, runtime
            )
        )
        import ray

        package, actual = _local_runtime(ray)
        if (
            package != version
            or actual != runtime
            or ray.__version__ != RAY_TARGET_PROBE_RAY_VERSION
        ):
            raise ValueError
    except Exception:
        _reject(CohortJobProcessControlReason.LOCAL_RUNTIME_MISMATCH)


def _submission_window(request):
    if not request.issued_at <= datetime.now(UTC) < request.expires_at:
        _reject(CohortJobProcessControlReason.INVALID)


def _settings(spec: dict, module: object, *, require_present: bool):
    if type(module) is not str or len(module) > 255 or _MODULE.fullmatch(module) is None:
        _reject(CohortJobProcessControlReason.INVALID)
    environment = spec.get("env_vars")
    if environment is None:
        environment = {}
    if type(environment) is not dict or any(
        type(key) is not str or type(value) is not str for key, value in environment.items()
    ):
        _reject(CohortJobProcessControlReason.INVALID)
    actual = environment.get("DJANGO_SETTINGS_MODULE")
    if (require_present and actual is None) or (actual is not None and actual != module):
        _reject(CohortJobProcessControlReason.INVALID)
    spec["env_vars"] = dict(environment, DJANGO_SETTINGS_MODULE=module)


def _resolve_endpoint(declared: str) -> str:
    declared = _endpoint(declared)
    if declared.startswith(("http://", "https://")):
        return _arguments(declared, _DUMMY_HANDLE, 5)[0]
    if "://" in declared or declared == "local":
        _reject(CohortJobProcessControlReason.UNSUPPORTED_ADDRESS)
    import ray._raylet as raylet
    from ray._private import ray_constants, services

    if declared == "auto":
        addresses = services.find_gcs_addresses()
        bootstrap = services.find_bootstrap_address(None)
        if len(addresses) > 1 and bootstrap is not None:
            declared = bootstrap
        elif len(addresses) == 1:
            declared = next(iter(addresses))
        elif bootstrap is not None:
            declared = bootstrap
        else:
            _reject(CohortJobProcessControlReason.PREPARATION_FAILED)
    # A concrete address avoids canonicalizer RAY_ADDRESS environment precedence.
    concrete = services.canonicalize_bootstrap_address(declared)
    if concrete is None:
        _reject(CohortJobProcessControlReason.UNSUPPORTED_ADDRESS)
    client = raylet.GcsClient(address=concrete)
    value = client.internal_kv_get(
        ray_constants.DASHBOARD_ADDRESS, namespace=ray_constants.KV_NAMESPACE_DASHBOARD, timeout=5
    )
    if type(value) is not bytes or not 0 < len(value) <= 4096:
        _reject(CohortJobProcessControlReason.PREPARATION_FAILED)
    return _arguments("http://" + value.decode("ascii"), _DUMMY_HANDLE, 5)[0]


class _Response:
    """Only bounded successful JSON/empty package responses reach SDK helpers."""

    def __init__(self, status: int, body: bytes):
        self.status_code = status
        self._body = body

    def __repr__(self):
        return "<bounded cohort Job response>"

    def json(self):
        return _json(self._body.decode("utf-8"), maximum=128 * 1024, canonical=False)


def _client(endpoint: str, *, operation: str, submission_id: str = _DUMMY_HANDLE):
    import requests
    from ray.dashboard.modules.dashboard_sdk import SubmissionClient

    pinned_endpoint = _arguments(endpoint, submission_id, 5)[0]

    class BoundedClient(SubmissionClient):
        def _raise_error(self, r):
            _reject(CohortJobProcessControlReason.RESPONSE_MISMATCH)

        def _do_request(self, method, endpoint, *, data=None, json_data=None, **kwargs):
            path = endpoint
            allowed = (
                (
                    operation == "prepare"
                    and method in {"GET", "PUT"}
                    and type(path) is str
                    and _PACKAGE.fullmatch(path) is not None
                    and json_data is None
                    and (
                        (method == "GET" and data is None)
                        or (method == "PUT" and type(data) is bytes)
                    )
                )
                or (
                    operation == "submit"
                    and method == "POST"
                    and path == "/api/jobs/"
                    and data is None
                    and type(json_data) is dict
                )
                or (
                    operation == "stop"
                    and method == "POST"
                    and path == f"/api/jobs/{submission_id}/stop"
                    and data is None
                    and json_data is None
                )
            )
            if not allowed or kwargs:
                _reject(CohortJobProcessControlReason.INVALID)
            budget = _Budget(5)
            headers = dict(self._headers)
            headers["Accept-Encoding"] = "identity"
            with requests.Session() as session:
                session.trust_env = False
                session.proxies.clear()
                remaining = budget.remaining()
                response = session.request(
                    method,
                    pinned_endpoint + path,
                    data=data,
                    json=json_data,
                    headers=headers,
                    cookies=self._cookies,
                    verify=self._verify,
                    proxies={},
                    stream=True,
                    allow_redirects=False,
                    timeout=(remaining, remaining),
                )
                try:
                    budget.remaining()
                    if type(response.status_code) is not int or (
                        response.status_code != 200
                        and not (
                            operation == "prepare"
                            and method == "GET"
                            and response.status_code == 404
                        )
                    ):
                        _reject(CohortJobProcessControlReason.RESPONSE_MISMATCH)
                    body = _read_body(response, budget)
                    budget.remaining()
                    return _Response(response.status_code, body)
                finally:
                    response.close()

    client = BoundedClient(address=pinned_endpoint)
    if client._address != pinned_endpoint:
        _reject(CohortJobProcessControlReason.INVALID)
    return client


def prepare(arguments: dict) -> dict:
    _shape(
        arguments,
        {
            "ray_address",
            "control_runtime_env_json",
            "source_control_profile_digest",
            "django_settings_module",
            "expected_package_version",
            "expected_runtime",
        },
    )
    spec = _json(arguments["control_runtime_env_json"], maximum=_PROFILE_MAX_BYTES)
    source = _digest(arguments["source_control_profile_digest"])
    if not secrets.compare_digest(source, cohort_probe_submitted_runtime_env_digest(spec)):
        _reject(CohortJobProcessControlReason.INVALID)
    _settings(spec, arguments["django_settings_module"], require_present=False)
    _runtime(arguments["expected_package_version"], arguments["expected_runtime"])
    endpoint = _resolve_endpoint(arguments["ray_address"])
    client = _client(endpoint, operation="prepare")
    client._upload_working_dir_if_needed(spec)
    client._upload_py_modules_if_needed(spec)
    from ray.runtime_env import RuntimeEnv
    from ray.runtime_env.runtime_env import _validate_no_local_paths

    prepared = RuntimeEnv(**spec)
    _validate_no_local_paths(prepared)
    submitted = prepared.to_dict()
    _settings(submitted, arguments["django_settings_module"], require_present=True)
    serialized = _canonical(submitted, maximum=_PROFILE_MAX_BYTES)
    return {
        "jobs_endpoint": endpoint,
        "submitted_runtime_env_json": serialized,
        "source_control_profile_digest": source,
        "submitted_runtime_env_digest": cohort_probe_submitted_runtime_env_digest(submitted),
    }


def submit(arguments: dict) -> dict:
    _shape(arguments, {"launch_json", "submitted_runtime_env_json"})
    launch = decode_probe_job_launch(arguments["launch_json"])
    spec = _json(arguments["submitted_runtime_env_json"], maximum=_PROFILE_MAX_BYTES)
    _settings(spec, launch.django_settings_module, require_present=True)
    if not secrets.compare_digest(
        launch.submitted_runtime_env_digest, cohort_probe_submitted_runtime_env_digest(spec)
    ):
        _reject(CohortJobProcessControlReason.INVALID)
    request = launch.request
    _submission_window(request)
    _runtime(request.expected_package_version, request.expected_runtime)
    from ray.runtime_env import RuntimeEnv
    from ray.runtime_env.runtime_env import _validate_no_local_paths

    normalized = RuntimeEnv(**spec)
    _validate_no_local_paths(normalized)
    if (
        _canonical(normalized.to_dict(), maximum=_PROFILE_MAX_BYTES)
        != arguments["submitted_runtime_env_json"]
    ):
        _reject(CohortJobProcessControlReason.INVALID)
    submission_id = probe_job_submission_id(request)
    client = _client(launch.jobs_endpoint, operation="submit", submission_id=submission_id)
    _submission_window(request)
    response = client._do_request(
        "POST",
        "/api/jobs/",
        json_data={
            "entrypoint": probe_job_launch_entrypoint(launch),
            "submission_id": submission_id,
            "runtime_env": spec,
            "metadata": probe_job_metadata(request),
        },
    ).json()
    if set(response) != {"job_id", "submission_id"} or any(
        type(value) is not str or value != submission_id for value in response.values()
    ):
        _reject(CohortJobProcessControlReason.RESPONSE_MISMATCH)
    return {"submitted": True, "submission_id": submission_id}


def inspect(arguments: dict) -> dict:
    _shape(
        arguments,
        {
            "request_json",
            "request_digest",
            "jobs_endpoint",
            "entrypoint_digest",
            "submitted_runtime_env_digest",
            "receipt_json",
            "receipt_digest",
        },
    )
    request = decode_probe_job_request(arguments["request_json"])
    _runtime(request.expected_package_version, request.expected_runtime)
    from django_ray.target.cohort_job_control import inspect_detached_cohort_job

    snapshot = CohortJobReservationSnapshot(
        request=request, **{key: value for key, value in arguments.items() if key != "request_json"}
    )
    inspected = inspect_detached_cohort_job(snapshot)
    if inspected is None:
        return {"pending": True}
    return {"pending": False, "inspected_at": _timestamp(inspected.inspected_at)}


def _owned_details(launch):
    from ray.dashboard.modules.job.pydantic_models import JobDetails, JobType

    request = launch.request
    details = fetch_reserved_cohort_job_details(
        launch.jobs_endpoint, probe_job_submission_id(request)
    )
    if (
        type(details) is not JobDetails
        or details.type is not JobType.SUBMISSION
        or details.submission_id != probe_job_submission_id(request)
        or details.metadata != probe_job_metadata(request)
        or cohort_probe_entrypoint_digest(details.entrypoint)
        != cohort_probe_entrypoint_digest(probe_job_launch_entrypoint(launch))
        or cohort_probe_submitted_runtime_env_digest(details.runtime_env)
        != launch.submitted_runtime_env_digest
    ):
        _reject(CohortJobProcessControlReason.RESPONSE_MISMATCH)
    if details.job_id is not None and (
        type(details.job_id) is not str
        or re.fullmatch(r"[0-9a-f]{8}", details.job_id) is None
        or details.job_id == "ffffffff"
    ):
        _reject(CohortJobProcessControlReason.RESPONSE_MISMATCH)
    if details.driver_info is not None and details.driver_info.id != details.job_id:
        _reject(CohortJobProcessControlReason.RESPONSE_MISMATCH)
    return details


def stop(arguments: dict) -> dict:
    _shape(arguments, {"launch_json"})
    launch = decode_probe_job_launch(arguments["launch_json"])
    _runtime(launch.request.expected_package_version, launch.request.expected_runtime)
    from ray.dashboard.modules.job.common import JobStatus

    terminal = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED}
    before = _owned_details(launch)
    if before.status in terminal:
        return {"terminal": True}
    if before.status not in {JobStatus.PENDING, JobStatus.RUNNING}:
        _reject(CohortJobProcessControlReason.RESPONSE_MISMATCH)
    submission_id = probe_job_submission_id(launch.request)
    response = (
        _client(launch.jobs_endpoint, operation="stop", submission_id=submission_id)
        ._do_request("POST", f"/api/jobs/{submission_id}/stop")
        .json()
    )
    if set(response) != {"stopped"} or type(response["stopped"]) is not bool:
        _reject(CohortJobProcessControlReason.RESPONSE_MISMATCH)
    after = _owned_details(launch)
    if before.job_id is not None and after.job_id != before.job_id:
        _reject(CohortJobProcessControlReason.RESPONSE_MISMATCH)
    return {"terminal": after.status in terminal}


def discover_client(arguments: dict) -> dict:
    from django_ray.runner.cohort_client_discovery import discover_client_jobs_endpoint

    return discover_client_jobs_endpoint(arguments)


def inspect_driver(arguments: dict) -> dict:
    from django_ray.runner.cohort_client_discovery import inspect_client_driver

    return inspect_client_driver(arguments)


def execute_cohort_job_control(command: str, payload: dict) -> dict:
    """Dispatch one bounded command; process supervision belongs to the parent."""
    functions = {
        "prepare": prepare,
        "submit": submit,
        "inspect": inspect,
        "stop": stop,
        "discover-client": discover_client,
        "inspect-driver": inspect_driver,
    }
    failures = {
        "prepare": CohortJobProcessControlReason.PREPARATION_FAILED,
        "submit": CohortJobProcessControlReason.SUBMISSION_UNCONFIRMED,
        "inspect": CohortJobProcessControlReason.INSPECTION_FAILED,
        "stop": CohortJobProcessControlReason.CLEANUP_UNCONFIRMED,
        "discover-client": CohortJobProcessControlReason.PREPARATION_FAILED,
        "inspect-driver": CohortJobProcessControlReason.CLEANUP_UNCONFIRMED,
    }
    if type(command) is not str or command not in functions:
        _reject(CohortJobProcessControlReason.INVALID)
    try:
        result = functions[command](payload)
        _canonical(result, maximum=COHORT_JOB_RESULT_MAX_BYTES)
        return result
    except CohortJobProcessControlError:
        raise
    except Exception:
        _reject(failures[command])
