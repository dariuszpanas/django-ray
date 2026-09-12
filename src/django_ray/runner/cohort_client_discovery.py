"""Private process-side Ray Client discovery; never cluster qualification.

Ray 2.58 ClientBuilder creates a native DRIVER. The parent must supervise this
entire operation, retain ambiguous outcomes and independently inspect its exact
driver after disconnect before preparing any Jobs work. Native DRIVER identifiers
are never submission identifiers and are never sent to the Jobs stop endpoint.

Connect, context reads and disconnect have native calls without hard deadlines.
Local budgets reject late results; only the parent's process supervisor enforces
the external deadline. A lost response cannot be reconstructed as cleanup proof.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Never
from urllib.parse import urlsplit

from django_ray.runner.cohort_job_control import _canonical, _json, _runtime, _shape
from django_ray.target.cohort_contract import _digest, _parse_timestamp, _timestamp
from django_ray.target.cohort_intent import _endpoint
from django_ray.target.cohort_job_http import _arguments, _Budget, _read_body
from django_ray.target.cohort_job_receipt import is_canonical_native_ray_job_id
from django_ray.target.probe import RAY_TARGET_PROBE_RAY_VERSION

_MAX_BYTES = 16 * 1024
_DUMMY_SUBMISSION_ID = "django-ray-cohort-probe-" + "0" * 64
_ID = re.compile(r"[0-9a-f]{32}")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}")
_REQUEST_KEYS = {
    "discovery_id",
    "configuration_digest",
    "ray_address",
    "package_version",
    "runtime",
    "issued_at",
    "expires_at",
}
_OBSERVATION_KEYS = {
    "schema_version",
    "request",
    "jobs_endpoint",
    "native_job_id",
    "cluster_session",
    "namespace",
    "driver_node_ip_address",
    "driver_pid",
    "driver_start_time",
    "driver_entrypoint_digest",
    "observed_at",
    "disconnect_returned",
}
# In tagged Ray 2.58 _apply_uv_hook_for_client returns immediately when
# py_executable is explicit. The specific server's separate ray.init call also
# needs its UV hook disabled: that hook scans every uv ancestor and otherwise
# replaces JobConfig even when the Client supplied an explicit executable.
# RuntimeEnvContext installs env_vars before exec'ing the server interpreter.
# This fixed profile never permits inherited source upload or an altered driver.
_RUNTIME_ENV = {
    "py_executable": "python",
    "env_vars": {"RAY_ENABLE_UV_RUN_RUNTIME_ENV": "0"},
}


class ClientDiscoveryReason(StrEnum):
    INVALID = "invalid"
    UNSUPPORTED_CONTEXT = "unsupported_context"
    DISCOVERY_UNCONFIRMED = "discovery_unconfirmed"
    RESPONSE_MISMATCH = "response_mismatch"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"
    EXPIRED = "expired"


class ClientDiscoveryError(RuntimeError):
    def __init__(self, reason: ClientDiscoveryReason):
        self.reason = reason
        super().__init__(f"Cohort Client discovery refused: {reason.value}")


def _reject(reason: ClientDiscoveryReason) -> Never:
    raise ClientDiscoveryError(reason) from None


def _now():
    return datetime.now(UTC)


def _request(value):
    _shape(value, _REQUEST_KEYS)
    _canonical(value, maximum=_MAX_BYTES)
    if type(value["discovery_id"]) is not str or _ID.fullmatch(value["discovery_id"]) is None:
        _reject(ClientDiscoveryReason.INVALID)
    _digest(value["configuration_digest"])
    address = _endpoint(value["ray_address"])
    parsed = urlsplit(address)
    if (
        parsed.scheme != "ray"
        or not address.startswith("ray://")
        or parsed.path
        or not parsed.hostname
        or parsed.port is None
        or any(marker in address for marker in ("?", "#", "%", "\\"))
    ):
        _reject(ClientDiscoveryReason.INVALID)
    issued, expires = _parse_timestamp(value["issued_at"]), _parse_timestamp(value["expires_at"])
    if not timedelta(0) < expires - issued <= timedelta(seconds=300):
        _reject(ClientDiscoveryReason.INVALID)
    _runtime(value["package_version"], value["runtime"])
    return issued, expires


def validate_client_discovery_request(request):
    """Validate the parent's finite request before it starts an owned helper."""
    try:
        _request(request)
        return request
    except ClientDiscoveryError:
        raise
    except Exception:
        _reject(ClientDiscoveryReason.INVALID)


def _namespace(request):
    return "django-ray-cohort-discovery-" + request["discovery_id"]


def _metadata(request):
    return {
        "django_ray_cohort_discovery_kind": "client-jobs-v1",
        "django_ray_cohort_discovery_id": request["discovery_id"],
        "django_ray_cohort_discovery_configuration": request["configuration_digest"],
    }


def _entrypoint_digest(value):
    if type(value) is not str or not 0 < len(value.encode("utf-8")) <= 4096 or "\0" in value:
        _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _native_id(value):
    if not is_canonical_native_ray_job_id(value):
        _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
    return value


def _dashboard_endpoint(value):
    if type(value) is not str or not value:
        _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
    if "://" not in value:
        value = "http://" + value
    return _arguments(value, _DUMMY_SUBMISSION_ID, 5)[0]


def _driver_details(endpoint, native_id, *, timeout_seconds=5.0):
    """Strict native DRIVER GET; reserved SUBMISSION validation is unchanged."""
    endpoint, _unused, timeout = _arguments(endpoint, _DUMMY_SUBMISSION_ID, timeout_seconds)
    _native_id(native_id)
    budget = _Budget(timeout)
    import ray
    import requests
    from ray.dashboard.modules.dashboard_sdk import SubmissionClient
    from ray.dashboard.modules.job.pydantic_models import JobDetails, JobType

    if ray.__version__ != RAY_TARGET_PROBE_RAY_VERSION:
        _reject(ClientDiscoveryReason.UNSUPPORTED_CONTEXT)
    # This constructor loads the authorized helper's auth/TLS configuration;
    # it neither initializes Ray nor resolves another dashboard address.
    client = SubmissionClient(address=endpoint)
    if client._address != endpoint:
        _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
    headers = dict(client._headers, **{"Accept-Encoding": "identity"})
    with requests.Session() as session:
        session.trust_env = False
        session.proxies.clear()
        remaining = budget.remaining()
        response = session.get(
            endpoint + "/api/jobs/" + native_id,
            headers=headers,
            cookies=client._cookies,
            verify=client._verify,
            proxies={},
            stream=True,
            allow_redirects=False,
            timeout=(remaining, remaining),
        )
        try:
            budget.remaining()
            if type(response.status_code) is not int or response.status_code != 200:
                _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
            body = _read_body(response, budget)
        finally:
            response.close()
    payload = _json(body.decode("utf-8"), maximum=128 * 1024, canonical=False)
    if set(payload) - set(JobDetails.model_fields):
        _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
    details = JobDetails.model_validate_json(json.dumps(payload), strict=True)
    if (
        details.type is not JobType.DRIVER
        or details.job_id != native_id
        or details.submission_id is not None
        or details.driver_info is None
        or details.driver_info.id != native_id
    ):
        _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
    budget.remaining()
    return details


def _corroborate(details, request, *, native_id):
    from ray.dashboard.modules.job.pydantic_models import JobDetails, JobType

    if (
        type(details) is not JobDetails
        or details.type is not JobType.DRIVER
        or details.job_id != native_id
        or details.submission_id is not None
        or details.driver_info is None
        or details.driver_info.id != native_id
        or details.metadata != _metadata(request)
        or details.runtime_env != _RUNTIME_ENV
        or type(details.start_time) is not int
        or not 0 < details.start_time < 1 << 63
    ):
        _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
    pid = details.driver_info.pid
    if type(pid) is not str or re.fullmatch(r"[1-9][0-9]{0,9}", pid) is None:
        _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
    ipaddress.ip_address(details.driver_info.node_ip_address)
    return {
        "driver_node_ip_address": details.driver_info.node_ip_address,
        "driver_pid": pid,
        "driver_start_time": details.start_time,
        "driver_entrypoint_digest": _entrypoint_digest(details.entrypoint),
    }


def validate_client_discovery_observation(request, observation):
    """Correlate a trusted owned-helper response; not standalone authentication."""
    try:
        issued, expires = _request(request)
        _shape(observation, _OBSERVATION_KEYS)
        if (
            type(observation["schema_version"]) is not int
            or observation["schema_version"] != 1
            or _canonical(observation["request"], maximum=_MAX_BYTES)
            != _canonical(request, maximum=_MAX_BYTES)
            or observation["disconnect_returned"] is not True
            or observation["namespace"] != _namespace(request)
            or type(observation["cluster_session"]) is not str
            or _SESSION.fullmatch(observation["cluster_session"]) is None
            or observation["jobs_endpoint"] != _dashboard_endpoint(observation["jobs_endpoint"])
            or type(observation["driver_pid"]) is not str
            or re.fullmatch(r"[1-9][0-9]{0,9}", observation["driver_pid"]) is None
            or type(observation["driver_start_time"]) is not int
            or not 0 < observation["driver_start_time"] < 1 << 63
            or not issued <= _parse_timestamp(observation["observed_at"]) < expires
        ):
            _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
        _native_id(observation["native_job_id"])
        ipaddress.ip_address(observation["driver_node_ip_address"])
        _digest(observation["driver_entrypoint_digest"])
        _canonical(observation, maximum=_MAX_BYTES)
        return observation
    except ClientDiscoveryError:
        raise
    except Exception:
        _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)


def discover_client_jobs_endpoint(request: dict) -> dict:
    """Create one owned Client DRIVER and corroborate it while still connected."""
    context = None
    try:
        issued, expires = _request(request)
        now = _now()
        if not issued <= now < expires:
            _reject(ClientDiscoveryReason.EXPIRED)
        budget = _Budget(min(30.0, (expires - now).total_seconds()))
        import ray
        import ray.util.client as client
        from ray.client_builder import ClientBuilder
        from ray.dashboard.modules.job.common import JobStatus
        from ray.job_config import JobConfig

        if (
            ray.is_initialized() is not False
            or client.ray.is_connected() is not False
            or client.ray.is_default() is not True
            or type(client.num_connected_contexts()) is not int
            or client.num_connected_contexts() != 0
        ):
            _reject(ClientDiscoveryReason.UNSUPPORTED_CONTEXT)
        builder = ClientBuilder(request["ray_address"][len("ray://") :])
        builder._job_config = JobConfig(
            runtime_env=deepcopy(_RUNTIME_ENV),
            metadata=_metadata(request),
            ray_namespace=_namespace(request),
        )
        builder._deprecation_warn_enabled = False
        builder._init_args(allow_multiple=True, logging_level="ERROR", log_to_driver=False)
        context = builder.connect()
        budget.remaining()
        with context:
            runtime_context = ray.get_runtime_context()
            native_id = _native_id(runtime_context.get_job_id())
            session = runtime_context.get_session_name()
            if (
                context.ray_version != RAY_TARGET_PROBE_RAY_VERSION
                or context.python_version
                != ".".join(
                    str(request["runtime"][part])
                    for part in ("python_major", "python_minor", "python_patch")
                )
                or runtime_context.namespace != _namespace(request)
            ):
                _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
            endpoint = _dashboard_endpoint(context.dashboard_url)
            details = _driver_details(
                endpoint, native_id, timeout_seconds=min(5, budget.remaining())
            )
            identity = _corroborate(details, request, native_id=native_id)
            if details.status is not JobStatus.RUNNING:
                _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
            observed = _now()
            if not now <= observed < expires:
                _reject(ClientDiscoveryReason.EXPIRED)
            observation = {
                "schema_version": 1,
                "request": request,
                "jobs_endpoint": endpoint,
                "native_job_id": native_id,
                "cluster_session": session,
                "namespace": _namespace(request),
                **identity,
                "observed_at": _timestamp(observed),
                "disconnect_returned": True,
            }
        # __exit__ restores the default context but does not close allow_multiple
        # clients; disconnect explicitly selects and closes this owned context.
        owned_context = context
        context = None
        owned_context.disconnect()
        budget.remaining()
        if not observed <= _now() < expires:
            _reject(ClientDiscoveryReason.EXPIRED)
        return validate_client_discovery_observation(request, observation)
    except ClientDiscoveryError:
        raise
    except Exception:
        _reject(ClientDiscoveryReason.DISCOVERY_UNCONFIRMED)
    finally:
        if context is not None:
            # Best effort only. A returned/raised/hung close never proves that
            # the native DRIVER is dead; the parent retains this operation.
            try:
                context.disconnect()
            except Exception:
                pass


def inspect_client_driver(arguments: dict) -> dict:
    """Read the exact correlated DRIVER; SUCCEEDED means Ray's is_dead=True.

    Cleanup may be confirmed after discovery's creation deadline. This performs
    one five-second bounded read, no reconnect, retries, stop or new driver.
    """
    try:
        _shape(arguments, {"request", "observation"})
        request, observation = arguments["request"], arguments["observation"]
        validate_client_discovery_observation(request, observation)
        from ray.dashboard.modules.job.common import JobStatus

        began = _parse_timestamp(_timestamp(_now()))
        if began < _parse_timestamp(observation["observed_at"]):
            _reject(ClientDiscoveryReason.EXPIRED)
        details = _driver_details(observation["jobs_endpoint"], observation["native_job_id"])
        identity = _corroborate(details, request, native_id=observation["native_job_id"])
        if any(
            observation[key] != value for key, value in identity.items()
        ) or details.status not in {JobStatus.RUNNING, JobStatus.SUCCEEDED}:
            _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
        now = _parse_timestamp(_timestamp(_now()))
        if now < began:
            _reject(ClientDiscoveryReason.EXPIRED)
        result = {
            "schema_version": 1,
            "request": request,
            "observation": observation,
            "terminal": details.status is JobStatus.SUCCEEDED,
            "inspected_at": _timestamp(now),
        }
        return validate_client_driver_inspection(request, observation, result)
    except ClientDiscoveryError:
        raise
    except Exception:
        _reject(ClientDiscoveryReason.CLEANUP_UNCONFIRMED)


def validate_client_driver_inspection(request, observation, result):
    """Validate correlation in an independently held owned-helper response."""
    try:
        validate_client_discovery_observation(request, observation)
        _shape(result, {"schema_version", "request", "observation", "terminal", "inspected_at"})
        if (
            type(result["schema_version"]) is not int
            or result["schema_version"] != 1
            or type(result["terminal"]) is not bool
            or _canonical(result["request"], maximum=_MAX_BYTES)
            != _canonical(request, maximum=_MAX_BYTES)
            or _canonical(result["observation"], maximum=_MAX_BYTES)
            != _canonical(observation, maximum=_MAX_BYTES)
            or _parse_timestamp(result["inspected_at"])
            < _parse_timestamp(observation["observed_at"])
        ):
            _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
        _canonical(result, maximum=_MAX_BYTES * 2)
        return result
    except ClientDiscoveryError:
        raise
    except Exception:
        _reject(ClientDiscoveryReason.RESPONSE_MISMATCH)
