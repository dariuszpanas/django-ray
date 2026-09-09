"""Source-owned HTTP assertions shared by host and in-namespace qualification.

Only the Python standard library is required. The caller supplies a bounded HTTP
transport and credential retrieval; this module has no cluster or DRT authority.
A passing API result is one application assertion layer, not the complete gate.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlencode
from uuid import UUID

TASK_FAILURE_STATES = frozenset({"FAILED", "CANCELLED", "LOST", "EXPIRED"})
MAX_OPENAPI_SCHEMA_BYTES = 128_000
EXPECTED_TASK_STATUS_INPUT_MAX_BYTES = 16 * 1024
EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES = 64 * 1024
EXPECTED_EXECUTION_PROTOCOL_VERSION = 1
EXPECTED_EXECUTION_PROVENANCE_MAX_BYTES = 128
EXPECTED_EXECUTION_PROTOCOL_METRIC = "django_ray_tasks_by_execution_protocol_total"
TASK_STATUS_INPUT_OMISSION_REASONS = frozenset(
    {
        None,
        "external_input_not_loaded",
        "stored_input_exceeds_status_limit",
        "malformed_inline_input",
        "encoded_response_limit",
    }
)
TASK_STATUS_BY_STATE = {
    "QUEUED": "READY",
    "RUNNING": "RUNNING",
    "SUCCEEDED": "SUCCESSFUL",
    "FAILED": "FAILED",
    "CANCELLED": "FAILED",
    "CANCELLING": "RUNNING",
    "LOST": "FAILED",
    "EXPIRED": "FAILED",
}
EXPECTED_EXECUTION_PROTOCOL_METRIC_PROTOCOLS = frozenset({"1", "other"})
EXPECTED_EXECUTION_PROTOCOL_METRIC_STATES = frozenset(TASK_STATUS_BY_STATE)
EXECUTION_PROTOCOL_METRIC_SAMPLE_PATTERN = re.compile(
    rf"{EXPECTED_EXECUTION_PROTOCOL_METRIC}"
    r'\{protocol="(1|other)",state="([A-Z]+)"\} '
    r"(?:0|[1-9][0-9]{0,18})\Z"
)
EXPECTED_EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES = 64 * 1024
EXPECTED_EXECUTION_DETAIL_RESPONSE_MAX_BYTES = 256 * 1024


class HttpRequest(Protocol):
    """Transport must enforce requested byte limits and required response headers.

    Omitted limits retain the caller's bounded default. Redirect, timeout, URL
    scope and credential handling also remain the transport's responsibility.
    """

    def __call__(
        self,
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        response_limit: int = ...,
        required_response_headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]: ...


class ApiObservations(Protocol):
    """Mutable progress retained even when a later assertion fails."""

    task_id: str
    task_state: str
    task_result: object
    api_task_status_bounded: bool
    api_bulk_reset_absent: bool
    api_legacy_workflow_node_absent: bool
    api_execution_delete_rejected: bool
    api_legacy_workflow_graph_absent: bool


@dataclass(slots=True)
class ApiEvidence:
    """Observations from this layer; no credentials or response bodies are retained."""

    task_id: str = ""
    task_state: str = ""
    task_result: object = None
    api_task_status_bounded: bool = False
    api_bulk_reset_absent: bool = False
    api_legacy_workflow_node_absent: bool = False
    api_execution_delete_rejected: bool = False
    api_legacy_workflow_graph_absent: bool = False


def _mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return cast("Mapping[str, Any]", value)


def _sequence(value: object, *, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return value


def _json_body(body: bytes, *, endpoint: str) -> Mapping[str, Any]:
    parsed: Any = None
    parsed_ok = False
    try:
        parsed = json.loads(body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        pass
    else:
        parsed_ok = True
    if not parsed_ok:
        # Raise outside the handler: parser exceptions retain the private input.
        raise ValueError(f"{endpoint} did not return valid JSON")
    return _mapping(parsed, field_name=f"{endpoint} response")


def parse_task_result(value: object) -> object:
    """Decode the durable JSON result stored in the sample execution response."""
    if not isinstance(value, str):
        raise ValueError("durable task result_data must be a JSON string")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("durable task result_data is not valid JSON") from error


def validate_task_status_payload(
    payload: Mapping[str, Any],
    *,
    task_id: str,
) -> str:
    """Validate one bounded task-status response and return its durable state."""
    if payload.get("task_id") != task_id:
        raise ValueError("task status polling returned the wrong task")
    state = payload.get("state")
    expected_status = TASK_STATUS_BY_STATE.get(state)
    if expected_status is None or payload.get("status") != expected_status:
        raise ValueError("task status polling returned an inconsistent state/status pair")
    attempt_number = payload.get("attempt_number")
    if type(attempt_number) is not int or attempt_number < 1:
        raise ValueError("task status polling returned an invalid attempt number")
    execution_generation = payload.get("execution_generation")
    if type(execution_generation) is not int or execution_generation < 0:
        raise ValueError("task status polling returned an invalid execution generation")
    if payload.get("input_max_bytes") != EXPECTED_TASK_STATUS_INPUT_MAX_BYTES:
        raise ValueError("task status polling changed its input byte limit")
    if payload.get("response_max_bytes") != EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES:
        raise ValueError("task status polling changed its response byte limit")

    if "input_omission_reason" not in payload:
        raise ValueError("task status polling omitted its input omission reason")
    omission_reason = payload.get("input_omission_reason")
    if omission_reason is not None and (
        not isinstance(omission_reason, str)
        or omission_reason not in TASK_STATUS_INPUT_OMISSION_REASONS
    ):
        raise ValueError("task status polling returned an unknown input omission reason")
    args = payload.get("args")
    kwargs = payload.get("kwargs")
    if omission_reason is None:
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ValueError("task status polling omitted inline input without a reason")
    elif args is not None or kwargs is not None:
        raise ValueError("task status polling mixed input with an omission reason")
    return cast(str, state)


def validate_execution_protocol_visibility(
    payload: Mapping[str, Any],
    *,
    surface: str,
    expected_protocol: int = EXPECTED_EXECUTION_PROTOCOL_VERSION,
    expected_compatible: bool = True,
) -> None:
    """Validate the fixed protocol and bounded provenance API projection."""
    if type(expected_protocol) is not int or expected_protocol < 1:
        raise ValueError("expected execution protocol must be a positive integer")
    if type(expected_compatible) is not bool:
        raise ValueError("expected protocol compatibility must be boolean")
    if type(payload.get("execution_protocol_version")) is not int or (
        payload.get("execution_protocol_version") != expected_protocol
    ):
        raise ValueError(f"{surface} did not report execution protocol version {expected_protocol}")

    state = payload.get("state")
    required_provenance = {
        "created_with_django_ray_version": True,
        "managed_with_django_ray_version": state in {"RUNNING", "SUCCEEDED"},
        "executor_django_ray_version": state == "SUCCEEDED",
    }
    for field_name, required in required_provenance.items():
        value = payload.get(field_name)
        if value is None:
            if required:
                raise ValueError(f"{surface} omitted applicable package provenance")
            continue
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError(f"{surface} returned invalid package provenance")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError(f"{surface} returned invalid package provenance") from error
        if len(encoded) > EXPECTED_EXECUTION_PROVENANCE_MAX_BYTES:
            raise ValueError(f"{surface} returned unbounded package provenance")

    if payload.get("protocol_compatible_worker_available") is not expected_compatible:
        expectation = "has no" if expected_compatible else "unexpectedly has"
        raise ValueError(f"{surface} {expectation} protocol-compatible worker capacity")
    if payload.get("queue_capacity_attested") is not False:
        raise ValueError(f"{surface} unexpectedly attested queue-specific capacity")


def validate_execution_protocol_metrics(body: bytes) -> dict[tuple[str, str], int]:
    """Require the complete fixed-bucket execution-protocol metric family."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("authenticated metrics were not valid UTF-8") from error

    help_line = (
        f"# HELP {EXPECTED_EXECUTION_PROTOCOL_METRIC} "
        "Total tasks by bounded execution-protocol bucket and state"
    )
    type_line = f"# TYPE {EXPECTED_EXECUTION_PROTOCOL_METRIC} gauge"
    lines = text.splitlines()
    if lines.count(help_line) != 1 or lines.count(type_line) != 1:
        raise ValueError("authenticated metrics omitted the bounded protocol metric family")

    samples: dict[tuple[str, str], int] = {}
    for line in lines:
        if not line.startswith(EXPECTED_EXECUTION_PROTOCOL_METRIC):
            continue
        match = EXECUTION_PROTOCOL_METRIC_SAMPLE_PATTERN.fullmatch(line)
        if match is None:
            raise ValueError("authenticated metrics changed the protocol metric label contract")
        key = (match.group(1), match.group(2))
        if key in samples:
            raise ValueError("authenticated metrics duplicated a protocol metric bucket")
        samples[key] = int(line.rsplit(" ", 1)[1])

    expected = {
        (protocol, state)
        for protocol in EXPECTED_EXECUTION_PROTOCOL_METRIC_PROTOCOLS
        for state in EXPECTED_EXECUTION_PROTOCOL_METRIC_STATES
    }
    if set(samples) != expected:
        raise ValueError("authenticated metrics returned incomplete protocol metric buckets")
    return samples


def verify_application_api(
    request: HttpRequest,
    *,
    get_token: Callable[[], str],
    task_timeout: float,
    evidence: ApiObservations | None = None,
) -> ApiObservations:
    """Require protected APIs, durable result five and the safe detail contract.

    The caller supplies a positive finite task timeout and enforces individual
    request deadlines. Existing observations are updated in place so a failed
    run retains its enqueued task identity and completed assertion progress.
    """
    if evidence is None:
        evidence = ApiEvidence()
    unauthenticated = (
        ("/api/enqueue/add/2/3", "POST"),
        ("/api/executions/stats", "GET"),
        ("/api/metrics", "GET"),
        ("/api/executions?limit=1", "GET"),
        ("/api/tasks/00000000-0000-4000-8000-000000000000", "GET"),
    )
    for endpoint, method in unauthenticated:
        status, _ = request(endpoint, method=method)
        if status != 401:
            raise ValueError(f"unauthenticated {endpoint} returned {status}, expected 401")

    status, body = request(
        "/api/openapi.json",
        method="GET",
        response_limit=MAX_OPENAPI_SCHEMA_BYTES,
    )
    if status != 200:
        raise ValueError(f"OpenAPI schema returned {status}, expected 200")
    schema = _json_body(body, endpoint="OpenAPI schema")
    paths = _mapping(schema.get("paths"), field_name="OpenAPI paths")
    execution_path = _mapping(
        paths.get("/api/executions/{execution_id}"),
        field_name="OpenAPI execution detail path",
    )
    if "get" not in execution_path:
        raise ValueError("OpenAPI execution detail path does not advertise GET")
    if "delete" in execution_path:
        raise ValueError("OpenAPI execution detail path advertises unsafe DELETE")
    if "/api/cluster/workflows/{task_id}/graph" in paths:
        raise ValueError("OpenAPI advertises the removed legacy workflow graph")
    evidence.api_legacy_workflow_graph_absent = True
    if "/api/executions/reset" in paths:
        raise ValueError("OpenAPI advertises the removed bulk execution reset")
    evidence.api_bulk_reset_absent = True
    if "/api/cluster/workflows/{task_id}/nodes/{node_id}" in paths:
        raise ValueError("OpenAPI advertises the removed legacy workflow node route")
    evidence.api_legacy_workflow_node_absent = True
    retry_path = _mapping(
        paths.get("/api/executions/{execution_id}/retry"),
        field_name="OpenAPI execution retry path",
    )
    if "post" not in retry_path:
        raise ValueError("OpenAPI execution retry path does not advertise POST")
    node_detail_path = _mapping(
        paths.get("/api/cluster/workflows/{task_id}/node-detail"),
        field_name="OpenAPI workflow node-detail path",
    )
    if "get" not in node_detail_path:
        raise ValueError("OpenAPI workflow node-detail path does not advertise GET")

    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}
    for endpoint in ("/api/executions/stats", "/api/executions?limit=1"):
        status, _ = request(endpoint, method="GET", headers=headers)
        if status != 200:
            raise ValueError(f"authenticated {endpoint} returned {status}, expected 200")
    status, body = request("/api/metrics", method="GET", headers=headers)
    if status != 200:
        raise ValueError(f"authenticated /api/metrics returned {status}, expected 200")
    validate_execution_protocol_metrics(body)

    status, body = request(
        "/api/enqueue/add/2/3",
        method="POST",
        headers=headers,
    )
    if status != 200:
        raise ValueError(f"authenticated add_numbers enqueue returned {status}, expected 200")
    enqueue = _json_body(body, endpoint="add_numbers enqueue")
    task_id = enqueue.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("add_numbers enqueue response has no task_id")
    try:
        parsed_task_id = UUID(task_id)
    except ValueError as error:
        raise ValueError("add_numbers enqueue task_id is not a canonical UUID") from error
    if parsed_task_id.version != 4 or str(parsed_task_id) != task_id:
        raise ValueError("add_numbers enqueue task_id is not a canonical UUIDv4")
    task_id = str(parsed_task_id)
    evidence.task_id = task_id

    deadline = time.monotonic() + task_timeout
    last_state = "missing"
    while True:
        status, body = request(
            f"/api/tasks/{task_id}",
            method="GET",
            headers=headers,
            response_limit=EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES,
            required_response_headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
        if status != 200:
            raise ValueError(f"task status polling returned {status}, expected 200")
        task_status = _json_body(body, endpoint="task status polling")
        last_state = validate_task_status_payload(task_status, task_id=task_id)
        evidence.task_state = last_state
        validate_execution_protocol_visibility(
            task_status,
            surface="task status polling",
        )
        if task_status.get("input_omission_reason") is not None:
            raise ValueError("add_numbers task status unexpectedly omitted inline input")
        if task_status.get("args") != [2, 3] or task_status.get("kwargs") != {}:
            raise ValueError("add_numbers task status changed its exact inline input")
        if last_state == "SUCCEEDED":
            evidence.api_task_status_bounded = True
            break
        if last_state in TASK_FAILURE_STATES:
            raise ValueError(f"add_numbers reached terminal state {last_state}")
        if time.monotonic() >= deadline:
            raise ValueError(
                f"add_numbers did not reach SUCCEEDED within {task_timeout}s "
                f"(last state: {last_state})"
            )
        time.sleep(2)

    execution_query = urlencode({"task_id": task_id, "limit": 1})
    status, body = request(f"/api/executions?{execution_query}", method="GET", headers=headers)
    if status != 200:
        raise ValueError(f"execution lookup returned {status}, expected 200")
    listing = _json_body(body, endpoint="execution lookup")
    tasks = _sequence(listing.get("tasks"), field_name="execution lookup tasks")
    execution = next(
        (
            _mapping(value, field_name="execution")
            for value in tasks
            if isinstance(value, Mapping) and value.get("task_id") == task_id
        ),
        None,
    )
    if execution is None or execution.get("state") != "SUCCEEDED":
        raise ValueError("add_numbers durable execution lookup did not return SUCCEEDED")
    validate_execution_protocol_visibility(
        execution,
        surface="execution lookup",
    )
    execution_id = execution.get("id")
    if not isinstance(execution_id, int) or isinstance(execution_id, bool) or execution_id < 1:
        raise ValueError("add_numbers execution has no positive integer id")
    result = parse_task_result(execution.get("result_data"))
    if result != 5:
        raise ValueError(f"add_numbers durable result is {result!r}, expected 5")
    detail_path = f"/api/executions/{execution_id}"
    status, _ = request(detail_path, method="DELETE", headers=headers)
    if status != 405:
        raise ValueError(f"authenticated execution DELETE returned {status}, expected 405")
    status, body = request(detail_path, method="GET", headers=headers)
    if status != 200:
        raise ValueError(f"execution detail after rejected DELETE returned {status}, expected 200")
    detail = _json_body(body, endpoint="execution detail")
    validate_execution_protocol_visibility(
        detail,
        surface="execution detail",
    )
    detail_result = parse_task_result(detail.get("result_data"))
    if (
        detail.get("id") != execution_id
        or detail.get("task_id") != task_id
        or detail.get("state") != "SUCCEEDED"
        or detail_result != 5
        or detail.get("result_data_omission_reason") is not None
        or detail.get("error_message_omission_reason") is not None
        or detail.get("diagnostic_max_bytes") != EXPECTED_EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
        or detail.get("response_max_bytes") != EXPECTED_EXECUTION_DETAIL_RESPONSE_MAX_BYTES
    ):
        raise ValueError("execution detail changed or lost its bounded projection contract")
    evidence.task_state = last_state
    evidence.task_result = result
    evidence.api_execution_delete_rejected = True

    return evidence
