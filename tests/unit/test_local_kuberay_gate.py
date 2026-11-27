"""Unit tests for local KubeRay gate protocol, overlay, HTTP guard, and encryption contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from urllib.request import ProxyHandler

import pytest
import yaml

from scripts import local_kuberay_gate as gate_module
from scripts.local_kuberay_auth import GRAFANA_AUTH_RECEIPT_KEYS, RAY_AUTH_RECEIPT_KEYS
from scripts.local_kuberay_gate import (
    APP_DEPLOYMENTS,
    DOCKER_CONTEXT_ALLOWLISTS,
    EXPECTED_NAMESPACE,
    EXPECTED_POLL_ATTEMPT_ERROR_MAX_BYTES,
    MAX_HTTP_RESPONSE_BYTES,
    MAX_OUTPUT_CHARACTERS,
    RAY_CLUSTER_NAME,
    RUNTIME_ENV_STORAGE_PROBE_MARKER,
    SETUP_JOB,
    CommandError,
    CommandResult,
    LocalKubeRayGate,
    Redactor,
    RejectRedirects,
    Runner,
    build_local_http_opener,
    configure_overlay_copy,
    create_source_build_context,
    expected_ray_topology,
    inspect_docker_context_allowlists,
    inspect_kubeconfig_snapshot,
    inspect_ray_job_request_storage_overlay,
    inspect_rendered_resources,
    inspect_runtime_env_encryption_overlay,
    inspect_runtime_env_encryption_secret_data,
    load_rendered_resources,
    normalize_ray_topology,
    parse_ray_image_python_version,
    ray_runtime_image_reference,
    register_kubeconfig_secrets,
    secret_data_sha256,
    source_bound_tag,
    split_apply_resources,
    validate_bounded_poll_projection,
    validate_execution_protocol_metrics,
    validate_execution_protocol_visibility,
    validate_local_context,
    validate_local_docker_endpoint,
    validate_local_http_url,
    validate_namespace,
    validate_protocol_v2_rejection,
    validate_protocol_v2_target_execution,
    validate_runtime_env_encryption_envelope,
    validate_task_status_payload,
    validate_terminal_diagnostic_text,
)
from tests.unit._local_kuberay_gate_helpers import (
    ROOT,
    SOURCE_TREE,
    TAG,
    TASK_ID,
    TOKEN68,
    _bounded_poll_metadata,
    _config,
    _container,
    _deployment,
    _execution_protocol_metrics,
    _kubeconfig_payload,
    _percent_hex_case_variants,
    _protocol_v2_rejection_payload,
    _protocol_v2_target_execution_payload,
    _ray_cluster,
    _resources,
    _runtime_env_encryption_resources,
    _runtime_env_envelope,
    _task_status_payload,
)


def test_expired_is_a_terminal_failure_in_all_gate_task_vocabularies() -> None:
    assert "EXPIRED" in gate_module.TASK_FAILURE_STATES
    assert "EXPIRED" in gate_module.WORKFLOW_PROGRESS_TASK_STATES
    assert gate_module.WORKFLOW_PROGRESS_FAILURE_STATES == gate_module.TASK_FAILURE_STATES


def test_terminal_diagnostic_gate_accepts_inert_lines_and_rejects_controls() -> None:
    diagnostic = 'ray::task()\nFile "task.py"\tline 1'

    assert validate_terminal_diagnostic_text(diagnostic, field_name="traceback") == diagnostic
    for unsafe in ("\x1b[31mfailed", "progress\rfailed", "bad\x00text", "bad\x9b31mtext"):
        with pytest.raises(ValueError, match="terminal control"):
            validate_terminal_diagnostic_text(unsafe, field_name="traceback")


@pytest.mark.parametrize(
    "omission_reason",
    [
        None,
        "external_input_not_loaded",
        "stored_input_exceeds_status_limit",
        "malformed_inline_input",
        "encoded_response_limit",
    ],
)
def test_task_status_gate_accepts_the_fixed_omission_vocabulary(
    omission_reason: str | None,
) -> None:
    payload = _task_status_payload(omission_reason=omission_reason)

    assert validate_task_status_payload(payload, task_id=TASK_ID) == "SUCCEEDED"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("task_id", "different-task", "wrong task"),
        ("status", "SUCCEEDED", "state/status pair"),
        ("attempt_number", True, "attempt number"),
        ("execution_generation", -1, "execution generation"),
        ("input_max_bytes", 1, "input byte limit"),
        ("response_max_bytes", 1, "response byte limit"),
        ("input_omission_reason", "invented", "omission reason"),
    ],
)
def test_task_status_gate_rejects_contract_drift(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _task_status_payload()
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        validate_task_status_payload(payload, task_id=TASK_ID)


def test_protocol_visibility_gate_accepts_queued_and_succeeded_lifecycle_fields() -> None:
    succeeded = _task_status_payload()
    queued = _task_status_payload(state="QUEUED")
    queued["managed_with_django_ray_version"] = None
    queued["executor_django_ray_version"] = None

    validate_execution_protocol_visibility(succeeded, surface="task status")
    validate_execution_protocol_visibility(queued, surface="task status")


def test_protocol_visibility_gate_accepts_unsupported_protocol_without_a_worker() -> None:
    queued = _task_status_payload(state="QUEUED")
    queued["execution_protocol_version"] = 2
    queued["managed_with_django_ray_version"] = None
    queued["executor_django_ray_version"] = None
    queued["protocol_compatible_worker_available"] = False

    validate_execution_protocol_visibility(
        queued,
        surface="unsupported task status",
        expected_protocol=2,
        expected_compatible=False,
    )

    queued["protocol_compatible_worker_available"] = True
    with pytest.raises(ValueError, match="compatible worker"):
        validate_execution_protocol_visibility(
            queued,
            surface="unsupported task status",
            expected_protocol=2,
            expected_compatible=False,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("execution_protocol_version", 2, "protocol version"),
        ("created_with_django_ray_version", None, "package provenance"),
        ("managed_with_django_ray_version", "", "package provenance"),
        ("executor_django_ray_version", "x" * 129, "unbounded"),
        ("protocol_compatible_worker_available", False, "compatible worker"),
        ("queue_capacity_attested", True, "queue-specific"),
    ],
)
def test_protocol_visibility_gate_rejects_contract_drift(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _task_status_payload()
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        validate_execution_protocol_visibility(payload, surface="task status")


def test_protocol_metrics_gate_requires_every_fixed_bucket() -> None:
    body = _execution_protocol_metrics()

    samples = validate_execution_protocol_metrics(body)
    assert samples[("1", "QUEUED")] == 0
    assert samples[("other", "QUEUED")] == 0

    unsupported = _execution_protocol_metrics(unsupported_queued=1)
    assert validate_execution_protocol_metrics(unsupported)[("other", "QUEUED")] == 1

    missing = body.replace(
        b'django_ray_tasks_by_execution_protocol_total{protocol="other",state="LOST"} 0\n',
        b"",
    )
    with pytest.raises(ValueError, match="incomplete protocol metric buckets"):
        validate_execution_protocol_metrics(missing)

    unbounded = body.replace(b'protocol="other"', b'protocol="99"', 1)
    with pytest.raises(ValueError, match="label contract"):
        validate_execution_protocol_metrics(unbounded)


def test_protocol_v2_rejection_gate_requires_the_exact_preinvocation_result() -> None:
    validate_protocol_v2_rejection(_protocol_v2_rejection_payload())


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("classification", "protocol_mismatch"),
        ("success", True),
        ("retryable", True),
        ("traceback_absent", False),
        ("result_absent", False),
        ("result_reference_absent", False),
        ("exception_type", "ValueError"),
        ("transport_version", 2),
        ("input_reference_absent", False),
        ("application_marker_present", True),
    ],
)
def test_protocol_v2_rejection_gate_rejects_effect_or_transport_drift(
    field: str,
    replacement: object,
) -> None:
    payload = _protocol_v2_rejection_payload()
    payload[field] = replacement

    with pytest.raises(ValueError, match="fixed pre-invocation rejection"):
        validate_protocol_v2_rejection(payload)


def test_protocol_v2_rejection_gate_rejects_unbounded_extra_fields() -> None:
    payload = _protocol_v2_rejection_payload()
    payload["private_marker"] = "must-not-be-retained"

    with pytest.raises(ValueError, match="fixed pre-invocation rejection"):
        validate_protocol_v2_rejection(payload)


def test_protocol_v2_target_execution_gate_requires_both_authenticated_results() -> None:
    validate_protocol_v2_target_execution(_protocol_v2_target_execution_payload())


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("exact_result_kind", "compatibility_rejection"),
        ("exact_application_invoked", False),
        ("exact_application_success", False),
        ("exact_application_result", 4),
        ("exact_observed_proof_bound", False),
        ("mismatch_result_kind", "completion"),
        ("mismatch_reason", "membership_mismatch"),
        ("mismatch_application_invoked", True),
        ("mismatch_marker_present", True),
        ("mismatch_observed_proof_bound", False),
    ],
)
def test_protocol_v2_target_execution_gate_rejects_incomplete_or_unsafe_evidence(
    field: str,
    replacement: object,
) -> None:
    payload = _protocol_v2_target_execution_payload()
    payload[field] = replacement

    with pytest.raises(ValueError, match="proof was incomplete or unsafe"):
        validate_protocol_v2_target_execution(payload)


def test_protocol_v2_target_execution_gate_rejects_extra_identity() -> None:
    payload = _protocol_v2_target_execution_payload()
    payload["target_key"] = "must-not-be-emitted"

    with pytest.raises(ValueError, match="proof was incomplete or unsafe"):
        validate_protocol_v2_target_execution(payload)


@pytest.mark.parametrize(
    "result_reason",
    [
        None,
        "external_result_not_loaded",
        "stored_result_exceeds_poll_limit",
        "malformed_inline_result",
        "encoded_response_limit",
    ],
)
def test_bounded_poll_gate_accepts_the_fixed_result_omission_vocabulary(
    result_reason: str | None,
) -> None:
    payload = {
        "task_id": TASK_ID,
        "result": None,
        "error": None,
        **_bounded_poll_metadata(),
    }
    payload["result_omission_reason"] = result_reason

    validate_bounded_poll_projection(payload, surface="workflow poll", task_id=TASK_ID)


@pytest.mark.parametrize(
    "error_reason",
    [None, "stored_error_exceeds_poll_limit", "encoded_response_limit"],
)
def test_bounded_poll_gate_accepts_the_fixed_error_omission_vocabulary(
    error_reason: str | None,
) -> None:
    payload = {
        "task_id": TASK_ID,
        "result": None,
        "error": None,
        **_bounded_poll_metadata(),
    }
    payload["error_omission_reason"] = error_reason

    validate_bounded_poll_projection(payload, surface="RuntimeEnv poll", task_id=TASK_ID)


@pytest.mark.parametrize(
    "attempt_reason",
    [None, "stored_error_exceeds_attempt_limit", "encoded_response_limit"],
)
def test_bounded_poll_gate_accepts_recovery_attempt_omission_vocabulary(
    attempt_reason: str | None,
) -> None:
    payload = {
        "task_id": TASK_ID,
        "result": None,
        "error": None,
        "attempts": [{"error": None, "error_omission_reason": attempt_reason}],
        "attempt_error_max_bytes": EXPECTED_POLL_ATTEMPT_ERROR_MAX_BYTES,
        **_bounded_poll_metadata(),
    }

    validate_bounded_poll_projection(payload, surface="recovery poll", task_id=TASK_ID)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"diagnostic_max_bytes": 1}, "diagnostic byte limit"),
        ({"response_max_bytes": 1}, "response byte limit"),
        ({"result_omission_reason": "invented"}, "result omission reason"),
        ({"error_omission_reason": "invented"}, "error omission reason"),
        (
            {"result": {"value": 1}, "result_omission_reason": "encoded_response_limit"},
            "mixed a result",
        ),
        (
            {"error": "failed", "error_omission_reason": "encoded_response_limit"},
            "mixed an error",
        ),
    ],
)
def test_bounded_poll_gate_rejects_projection_drift(
    mutation: dict[str, object],
    message: str,
) -> None:
    payload = {
        "task_id": TASK_ID,
        "result": None,
        "error": None,
        **_bounded_poll_metadata(),
        **mutation,
    }

    with pytest.raises(ValueError, match=message):
        validate_bounded_poll_projection(payload, surface="workflow poll", task_id=TASK_ID)


def test_bounded_poll_gate_rejects_recovery_attempt_limit_drift() -> None:
    payload = {
        "task_id": TASK_ID,
        "result": None,
        "error": None,
        "attempts": [{"error": None, "error_omission_reason": None}],
        "attempt_error_max_bytes": 1,
        **_bounded_poll_metadata(),
    }

    with pytest.raises(ValueError, match="attempt error byte limit"):
        validate_bounded_poll_projection(payload, surface="recovery poll", task_id=TASK_ID)


def test_poll_projection_evidence_requires_workflow_and_runtime_env_families() -> None:
    gate = LocalKubeRayGate(_config())
    payload = {
        "task_id": TASK_ID,
        "result": None,
        "error": None,
        **_bounded_poll_metadata(),
    }

    gate._observe_bounded_poll_projection(
        payload,
        family="runtime_env",
        surface="RuntimeEnv poll",
        task_id=TASK_ID,
    )
    assert gate.evidence.api_workflow_runtime_polls_bounded is False

    gate._observe_bounded_poll_projection(
        payload,
        family="workflow",
        surface="workflow poll",
        task_id=TASK_ID,
    )
    assert gate.evidence.api_workflow_runtime_polls_bounded is True


def test_namespace_guard_accepts_only_the_dedicated_local_namespace() -> None:
    validate_namespace(EXPECTED_NAMESPACE)

    with pytest.raises(ValueError, match="exactly 'django-ray'"):
        validate_namespace("default")


@pytest.mark.parametrize(
    ("context", "server"),
    [
        ("docker-desktop", "https://kubernetes.docker.internal:6443"),
        ("kind-django-ray", "https://127.0.0.1:57321"),
    ],
)
def test_context_guard_accepts_named_local_clusters(context: str, server: str) -> None:
    validate_local_context(current=context, expected=context, server_url=server)


@pytest.mark.parametrize(
    ("current", "expected", "server", "message"),
    [
        ("production", "docker-desktop", "https://127.0.0.1:6443", "active Kubernetes"),
        ("production", "production", "https://127.0.0.1:6443", "context must be"),
        (
            "kind-local",
            "kind-local",
            "https://api.production.example.com:6443",
            "non-local Kubernetes API",
        ),
    ],
)
def test_context_guard_fails_closed(current: str, expected: str, server: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_local_context(current=current, expected=expected, server_url=server)


def test_flattened_kubeconfig_snapshot_binds_one_local_context() -> None:
    payload = _kubeconfig_payload()

    assert (
        inspect_kubeconfig_snapshot(payload, expected_context="docker-desktop")
        == "https://kubernetes.docker.internal:6443"
    )

    drifted = _kubeconfig_payload(server="https://production.example.com:6443")
    with pytest.raises(ValueError, match="non-local Kubernetes API"):
        inspect_kubeconfig_snapshot(drifted, expected_context="docker-desktop")

    proxied = _kubeconfig_payload()
    clusters = cast(list[dict[str, Any]], proxied["clusters"])
    cluster = cast(dict[str, Any], clusters[0]["cluster"])
    cluster["proxy-url"] = "https://production.example.invalid"
    with pytest.raises(ValueError, match="must not route through a proxy URL"):
        inspect_kubeconfig_snapshot(proxied, expected_context="docker-desktop")


def test_kubeconfig_static_auth_and_exec_credentials_are_registered_before_use() -> None:
    secrets = {
        "token": "kube-static-token-marker",
        "password": "kube-password-marker",
        "client-key-data": base64.b64encode(b"private-key-marker").decode(),
        "auth": "auth-provider-token-marker",
        "exec_arg": "exec-argument-token-marker",
        "exec_inline": "exec-inline-secret-marker",
        "exec_env": "exec-environment-token-marker",
    }
    payload = _kubeconfig_payload()
    users = cast(list[dict[str, Any]], payload["users"])
    users[0]["user"] = {
        "token": secrets["token"],
        "password": secrets["password"],
        "client-key-data": secrets["client-key-data"],
        "auth-provider": {
            "name": "oidc",
            "config": {"id-token": secrets["auth"], "issuer": "https://issuer.invalid"},
        },
        "exec": {
            "command": "credential-helper",
            "args": [
                "get",
                "--token",
                secrets["exec_arg"],
                f"--client-secret={secrets['exec_inline']}",
            ],
            "env": [{"name": "ACCESS_TOKEN", "value": secrets["exec_env"]}],
        },
    }
    redactor = Redactor()

    register_kubeconfig_secrets(payload, redactor=redactor)

    serialized = redactor.clean(" ".join(secrets.values()) + " private-key-marker")
    assert "marker" not in serialized
    assert serialized.count("[REDACTED]") == len(secrets) + 1


def test_kubeconfig_credentials_redact_every_percent_escape_hex_case() -> None:
    payload = _kubeconfig_payload()
    users = cast(list[dict[str, Any]], payload["users"])
    users[0]["user"] = {"password": TOKEN68}
    redactor = Redactor()

    register_kubeconfig_secrets(payload, redactor=redactor)

    encoded = quote(TOKEN68, safe="")
    variants = _percent_hex_case_variants(encoded)
    assert len(variants) > 1
    assert all(redactor.clean(variant) == "[REDACTED]" for variant in variants)
    unrelated = quote(TOKEN68.swapcase(), safe="")
    assert redactor.clean(unrelated) == unrelated


def test_cli_defaults_use_gate_owned_direct_nodeports() -> None:
    args = gate_module._parser().parse_args(
        [
            "--context",
            "docker-desktop",
            "--namespace",
            EXPECTED_NAMESPACE,
            "--ray-restart",
            "required",
        ]
    )

    assert args.web_url == "http://localhost:30080"
    assert args.prometheus_url == "http://localhost:30090"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:30080",
        "http://django-ray.localhost:30080",
        "https://127.0.0.1:30443",
    ],
)
def test_http_guard_accepts_only_local_urls(url: str) -> None:
    validate_local_http_url(url, option="--web-url")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://token@localhost:30080",
        "http://localhost:30080/?token=secret",
        "file:///tmp/socket",
    ],
)
def test_http_guard_rejects_token_exfiltration_paths(url: str) -> None:
    with pytest.raises(ValueError):
        validate_local_http_url(url, option="--web-url")


def test_http_guard_never_echoes_rejected_credentials_or_query_values() -> None:
    marker = "URL-CREDENTIAL-MARKER"
    for url in (
        f"http://{marker}@evil.example:30080",
        f"http://evil.example:30080/?token={marker}",
    ):
        with pytest.raises(ValueError) as captured:
            validate_local_http_url(url, option="--web-url")
        assert marker not in str(captured.value)
        assert url not in str(captured.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "npipe:////./pipe/dockerDesktopLinuxEngine",
        "unix:///var/run/docker.sock",
        "tcp://127.0.0.1:2375",
    ],
)
def test_docker_guard_accepts_only_local_daemons(endpoint: str) -> None:
    validate_local_docker_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "ssh://builder@example.com",
        "tcp://docker.example.com:2376",
        "https://10.20.30.40:2376",
        "npipe:////remote-host/pipe/docker_engine",
        "unix://remote-host/var/run/docker.sock",
    ],
)
def test_docker_guard_rejects_remote_daemons(endpoint: str) -> None:
    with pytest.raises(ValueError, match="must be local"):
        validate_local_docker_endpoint(endpoint)


def test_docker_guard_does_not_echo_rejected_credentials() -> None:
    marker = "password-marker"

    with pytest.raises(ValueError, match="credentials") as captured:
        validate_local_docker_endpoint(f"tcp://user:{marker}@localhost:2375")

    assert marker not in str(captured.value)


def test_local_http_opener_disables_proxies_and_rejects_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "")
    opener = build_local_http_opener()
    redirects = next(handler for handler in opener.handlers if isinstance(handler, RejectRedirects))

    assert not any(isinstance(handler, ProxyHandler) for handler in opener.handlers)
    assert (
        redirects.redirect_request(
            None,  # type: ignore[arg-type]
            None,
            302,
            "Found",
            {},
            "https://example.invalid/token-sink",
        )
        is None
    )


def test_real_http_path_allows_internal_queries_without_crossing_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "local-token-that-must-never-be-printed-123456"

    class Response:
        def __init__(
            self,
            status: int,
            body: object,
            *,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.status = status
            self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.headers = headers or {}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return self.body[:limit]

    class ScriptedOpener:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str, bool]] = []

        def open(self, request: Any, *, timeout: float) -> Response:
            assert timeout == 10
            authenticated = request.get_header("Authorization") == f"Bearer {token}"
            self.requests.append((request.full_url, request.get_method(), authenticated))
            path = request.full_url.removeprefix("http://django-ray.localhost:30080")
            if path == "/api/openapi.json":
                return Response(
                    200,
                    {
                        "paths": {
                            "/api/executions/{execution_id}": {
                                "get": {"operationId": "get_execution"}
                            },
                            "/api/executions/{execution_id}/retry": {"post": {}},
                            "/api/cluster/workflows/{task_id}/node-detail": {"get": {}},
                        },
                        "components": {
                            "schemas": {
                                "bounded-large-schema": {"description": "x" * MAX_OUTPUT_CHARACTERS}
                            }
                        },
                    },
                )
            if not authenticated:
                return Response(401, {})
            if path == "/api/enqueue/add/2/3":
                return Response(200, {"task_id": TASK_ID})
            if path == "/api/metrics":
                return Response(200, _execution_protocol_metrics())
            if path == f"/api/tasks/{TASK_ID}":
                return Response(
                    200,
                    _task_status_payload(),
                    headers={
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            if path == f"/api/executions?task_id={TASK_ID}&limit=1":
                return Response(
                    200,
                    {
                        "tasks": [
                            {
                                "id": 17,
                                "task_id": TASK_ID,
                                "state": "SUCCEEDED",
                                "result_data": "5",
                                "execution_protocol_version": 1,
                                "created_with_django_ray_version": "0.5.0-producer",
                                "managed_with_django_ray_version": "0.5.0-manager",
                                "executor_django_ray_version": "0.5.0-executor",
                                "protocol_compatible_worker_available": True,
                                "queue_capacity_attested": False,
                            }
                        ]
                    },
                )
            if path == "/api/executions/17" and request.get_method() == "DELETE":
                return Response(405, {})
            if path == "/api/executions/17":
                return Response(
                    200,
                    {
                        "id": 17,
                        "task_id": TASK_ID,
                        "state": "SUCCEEDED",
                        "result_data": "5",
                        "execution_protocol_version": 1,
                        "created_with_django_ray_version": "0.5.0-producer",
                        "managed_with_django_ray_version": "0.5.0-manager",
                        "executor_django_ray_version": "0.5.0-executor",
                        "protocol_compatible_worker_available": True,
                        "queue_capacity_attested": False,
                        "result_data_omission_reason": None,
                        "error_message_omission_reason": None,
                        "diagnostic_max_bytes": 65_536,
                        "response_max_bytes": 262_144,
                    },
                )
            return Response(200, {})

    opener = ScriptedOpener()
    gate = LocalKubeRayGate(_config())
    gate.http_opener = cast(Any, opener)
    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    gate._verify_api()

    assert (
        f"http://django-ray.localhost:30080/api/executions?task_id={TASK_ID}&limit=1",
        "GET",
        True,
    ) in opener.requests
    assert gate.evidence.task_state == "SUCCEEDED"
    assert gate.evidence.task_result == 5
    assert gate.evidence.api_task_status_bounded is True
    assert gate.evidence.api_bulk_reset_absent is True
    assert gate.evidence.api_legacy_workflow_node_absent is True
    assert gate.evidence.api_execution_delete_rejected is True
    assert gate.evidence.api_legacy_workflow_graph_absent is True


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Cache-Control": "public", "X-Content-Type-Options": "nosniff"},
        {"Cache-Control": "no-store", "X-Content-Type-Options": "sniff"},
    ],
)
def test_http_reader_rejects_missing_or_unsafe_required_headers(
    headers: dict[str, str],
) -> None:
    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return b"{}"[:limit]

    response = Response()
    response.headers = headers  # type: ignore[attr-defined]

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            return response

    gate = LocalKubeRayGate(_config())
    gate.http_opener = cast(Any, Opener())

    with pytest.raises(ValueError, match="header"):
        gate._http(
            "/api/tasks/task-id",
            method="GET",
            required_response_headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


def test_http_reader_accepts_workflow_pages_larger_than_diagnostic_output() -> None:
    payload = {
        "schema": "django-ray.workflow-progress-page",
        "items": [{"message": "x" * (MAX_OUTPUT_CHARACTERS + 1_000)}],
    }
    encoded = json.dumps(payload).encode()
    read_limits: list[int] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            read_limits.append(limit)
            return encoded[:limit]

    class Opener:
        def open(self, request: Any, *, timeout: float) -> Response:
            assert request.full_url.endswith("/api/cluster/workflows/task/nodes?limit=64")
            assert timeout == 10
            return Response()

    assert MAX_OUTPUT_CHARACTERS < len(encoded) < MAX_HTTP_RESPONSE_BYTES
    gate = LocalKubeRayGate(_config())
    gate.http_opener = cast(Any, Opener())

    status, body = gate._http(
        "/api/cluster/workflows/task/nodes?limit=64",
        method="GET",
    )

    assert status == 200
    assert body == encoded
    assert json.loads(body) == payload
    assert read_limits == [MAX_HTTP_RESPONSE_BYTES + 1]


@pytest.mark.parametrize(
    "path",
    [
        "http://localhost:30080/api/executions?limit=1",
        "http://django-ray.localhost:30081/api/executions?limit=1",
        "https://django-ray.localhost:30080/api/executions?limit=1",
        "http://user@django-ray.localhost:30080/api/executions?limit=1",
        "http://django-ray.localhost:30080/api/executions#fragment",
    ],
)
def test_real_http_path_rejects_local_credential_boundary_changes(path: str) -> None:
    gate = LocalKubeRayGate(_config())

    with pytest.raises(ValueError):
        gate._http(path, method="GET", headers={"Authorization": "Bearer marker"})


def test_runner_timeout_is_bounded_and_redacts_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "timeout-token-that-must-be-redacted"
    redactor = Redactor()
    redactor.register(token)
    runner = Runner(redactor=redactor, timeout_seconds=7)

    def time_out(*args: object, **kwargs: object) -> None:
        assert kwargs["timeout"] == 3
        raise subprocess.TimeoutExpired(
            cmd=["fake"],
            timeout=3,
            output=f"partial {token}",
            stderr=f"error {token}",
        )

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(CommandError, match="timed out after 3s") as captured:
        runner.run(["fake"], cwd=ROOT, timeout=3)

    assert token not in str(captured.value)
    assert str(captured.value).count("[REDACTED]") == 2
    assert isinstance(captured.value.__cause__, subprocess.TimeoutExpired)


def test_runner_sensitive_failures_never_expose_unregistered_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded_secret = base64.b64encode(b"A" * 40).decode()

    def time_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            cmd=["kubectl"],
            timeout=3,
            output=f'{{"data":{{"DJANGO_API_TOKEN":"{encoded_secret}"}}}}',
            stderr=encoded_secret,
        )

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(CommandError, match="sensitive command output suppressed") as captured:
        Runner(timeout_seconds=3).run(
            ["kubectl", "get", "secret"],
            cwd=ROOT,
            sensitive_output=True,
        )

    assert encoded_secret not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_runner_sensitive_nonzero_failures_never_expose_unregistered_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "unregistered-secret-from-stderr"
    encoded_secret = base64.b64encode(secret.encode()).decode()

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["kubectl"],
            returncode=1,
            stdout=f"plaintext={secret}",
            stderr=f"encoded={encoded_secret}",
        )

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(CommandError, match="sensitive command output suppressed") as captured:
        Runner().run(
            ["kubectl", "get", "secret"],
            cwd=ROOT,
            sensitive_output=True,
        )

    assert secret not in str(captured.value)
    assert encoded_secret not in str(captured.value)


def test_private_json_parsers_drop_raw_payload_from_exception_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_value = "private-json-value-that-must-not-enter-the-exception-graph"
    malformed = f'{{"private":"{private_value}"'
    gate = LocalKubeRayGate(_config())

    with pytest.raises(ValueError, match="did not return valid JSON") as api_error:
        gate._json_body(malformed.encode(), endpoint="private API")

    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(malformed, "", 0),
    )
    with pytest.raises(ValueError, match="valid private JSON") as shell_error:
        gate._sensitive_django_shell("print('private')", field_name="private shell")
    with pytest.raises(
        ValueError, match="Secret/django-ray-secret is not valid JSON"
    ) as secret_error:
        gate._secret_data()

    for error in (api_error.value, shell_error.value, secret_error.value):
        assert private_value not in str(error)
        assert error.__cause__ is None
        assert error.__context__ is None


@pytest.mark.parametrize("ray_authorized", [False, True])
def test_sensitive_django_shell_accepts_one_json_object_among_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    ray_authorized: bool,
) -> None:
    gate = LocalKubeRayGate(_config())
    observed: dict[str, str] = {}

    class FakeUUID:
        hex = "a" * 32

    monkeypatch.setattr(gate_module, "uuid4", lambda: FakeUUID())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        expected_container = "django-ray-worker" if ray_authorized else "django-web"
        assert args[:4] == ("exec", f"deployment/{expected_container}", "-c", expected_container)
        assert kwargs["sensitive_output"] is True
        observed["script"] = args[-1]
        return CommandResult(
            "SIGTERM handler is not set because this is not the main thread.\n"
            '{"complete":true}\n'
            f"django_ray_private_json_complete_v1_{'a' * 32}\n"
            "Ray Client connection closed.\n",
            "",
            0,
        )

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    assert gate._sensitive_django_shell(
        "print('private')", field_name="private shell", ray_authorized=ray_authorized
    ) == {"complete": True}
    assert observed["script"].endswith(f"print('django_ray_private_json_complete_v1_{'a' * 32}')\n")


@pytest.mark.parametrize("invalid_surface", [None, "ray", "grafana"])
def test_authentication_gate_uses_existing_credential_owners_and_requires_complete_receipts(
    monkeypatch: pytest.MonkeyPatch, invalid_surface: str | None
) -> None:
    gate = LocalKubeRayGate(_config())
    gate._ray_cluster_uid = "verified-cluster"
    calls: list[tuple[str, str]] = []
    identity_checks: list[bool] = []
    readiness_checks: list[bool] = []
    monkeypatch.setattr(gate, "_verify_ray_identity", lambda: identity_checks.append(True))
    head = {"metadata": {"name": "verified-ray-head"}}
    monkeypatch.setattr(gate, "_ray_pods", lambda **kwargs: ("verified-cluster", [head]))
    monkeypatch.setattr(gate, "_rendered_ray_pod_contract", lambda pod: ("head", None))

    def kubectl(*args: str, **kwargs: Any) -> CommandResult:
        assert args == ("rollout", "status", "deployment/grafana", "--timeout=300s")
        assert kwargs == {"timeout": gate._rollout_command_timeout()}
        assert calls == [("deployment/django-ray-worker", "django-ray-worker")]
        readiness_checks.append(True)
        return CommandResult("", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    def probe(script: str, *, resource: str, container: str) -> Mapping[str, Any]:
        compile(script, "<authentication-probe>", "exec")
        calls.append((resource, container))
        surface = "ray" if container == "django-ray-worker" else "grafana"
        if surface == "grafana":
            assert readiness_checks == [True]
        keys = RAY_AUTH_RECEIPT_KEYS if surface == "ray" else GRAFANA_AUTH_RECEIPT_KEYS
        receipt: dict[str, object] = {"schema_version": 1, **dict.fromkeys(keys, True)}
        if surface == invalid_surface:
            receipt.pop(sorted(keys)[0])
        return receipt

    monkeypatch.setattr(gate, "_authentication_probe", probe)
    if invalid_surface is None:
        gate._verify_authentication()
    else:
        with pytest.raises(ValueError, match="every required boundary"):
            gate._verify_authentication()
    assert calls[0] == ("deployment/django-ray-worker", "django-ray-worker")
    assert gate.evidence.ray_auth_boundary_verified is (invalid_surface != "ray")
    assert gate.evidence.grafana_auth_boundary_verified is (invalid_surface is None)
    if invalid_surface != "ray":
        assert calls[1] == ("pod/verified-ray-head", "dashboard-importer")
    assert readiness_checks == ([] if invalid_surface == "ray" else [True])
    assert len(identity_checks) == (2 if invalid_surface is None else 1)


def test_grafana_rollout_timeout_prevents_authentication_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config(rollout_timeout=7))
    gate._ray_cluster_uid = "verified-cluster"
    gate.evidence.grafana_auth_boundary_verified = True
    monkeypatch.setattr(gate, "_verify_ray_identity", lambda: None)
    head = {"metadata": {"name": "verified-ray-head"}}
    monkeypatch.setattr(gate, "_ray_pods", lambda **kwargs: ("verified-cluster", [head]))
    monkeypatch.setattr(gate, "_rendered_ray_pod_contract", lambda pod: ("head", None))
    probes: list[str] = []

    def probe(script: str, *, resource: str, container: str) -> Mapping[str, Any]:
        probes.append(container)
        assert container == "django-ray-worker"
        return {"schema_version": 1, **dict.fromkeys(RAY_AUTH_RECEIPT_KEYS, True)}

    def kubectl(*args: str, **kwargs: Any) -> CommandResult:
        assert args == ("rollout", "status", "deployment/grafana", "--timeout=7s")
        assert kwargs == {"timeout": gate._rollout_command_timeout()}
        raise TimeoutError("Grafana rollout deadline expired")

    monkeypatch.setattr(gate, "_authentication_probe", probe)
    monkeypatch.setattr(gate, "_kubectl", kubectl)
    with pytest.raises(TimeoutError, match="Grafana rollout deadline expired"):
        gate._verify_authentication()
    assert probes == ["django-ray-worker"]
    assert gate.evidence.ray_auth_boundary_verified is True
    assert gate.evidence.grafana_auth_boundary_verified is False


@pytest.mark.parametrize("complete", [False, True])
def test_authentication_probe_is_fresh_bounded_private_and_requires_completion(
    monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: Any) -> CommandResult:
        assert args[:7] == (
            "exec",
            "deployment/django-ray-worker",
            "-c",
            "django-ray-worker",
            "--",
            "python",
            "-c",
        )
        assert kwargs == {"sensitive_output": True, "timeout": max(90, gate.config.command_timeout)}
        marker = re.search(r"print\('([^']+)'\)\n$", args[-1])
        assert marker is not None
        payload = '{"schema_version":1}\n'
        if complete:
            payload += marker.group(1) + "\n"
        return CommandResult(payload, "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    if complete:
        assert gate._authentication_probe(
            "print('{}')", resource="deployment/django-ray-worker", container="django-ray-worker"
        ) == {"schema_version": 1}
    else:
        with pytest.raises(ValueError, match="valid private JSON"):
            gate._authentication_probe(
                "print('{}')",
                resource="deployment/django-ray-worker",
                container="django-ray-worker",
            )


def test_sensitive_django_shell_rejects_ambiguous_json_object_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())

    class FakeUUID:
        hex = "b" * 32

    monkeypatch.setattr(gate_module, "uuid4", lambda: FakeUUID())
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(
            '{"diagnostic":true}\n{"complete":true}\n'
            f"django_ray_private_json_complete_v1_{'b' * 32}\n",
            "",
            0,
        ),
    )

    with pytest.raises(ValueError, match="valid private JSON") as captured:
        gate._sensitive_django_shell("print('private')", field_name="private shell")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_sensitive_django_shell_requires_controlled_completion_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult('{"complete":true}\n', "", 0),
    )

    with pytest.raises(ValueError, match="valid private JSON"):
        gate._sensitive_django_shell("print('private')", field_name="private shell")


def test_private_kubeconfig_json_failures_drop_raw_payload_from_exception_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_value = "private-kubeconfig-value-that-must-not-enter-the-exception-graph"
    malformed = f'{{"private":"{private_value}"'

    creation_gate = LocalKubeRayGate(_config())
    creation_gate.temp_root = tmp_path
    monkeypatch.setattr(
        creation_gate.runner,
        "run",
        lambda *args, **kwargs: CommandResult(malformed, "", 0),
    )
    with pytest.raises(ValueError, match="flattened kubeconfig") as creation_error:
        creation_gate._create_kubeconfig_snapshot(current_context=_config().context)

    snapshot = tmp_path / "malformed-kubeconfig.json"
    snapshot.write_text(malformed, encoding="utf-8")
    verification_gate = LocalKubeRayGate(_config())
    verification_gate.kubeconfig_path = snapshot
    verification_gate._kubeconfig_digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="no longer valid JSON") as verification_error:
        verification_gate._verify_kubeconfig_snapshot()

    for error in (creation_error.value, verification_error.value):
        assert private_value not in str(error)
        assert error.__cause__ is None
        assert error.__context__ is None


def test_runner_redacts_before_bounding_failure_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "0123456789abcdefghijKLMNOPQRSTuvwxyzABCD"
    redactor = Redactor()
    redactor.register(token)

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["fake"],
            returncode=1,
            stdout=("p" * 100) + token + ("z" * (MAX_OUTPUT_CHARACTERS - 20)),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(CommandError) as captured:
        Runner(redactor=redactor).run(["fake"], cwd=ROOT)

    assert token[20:] not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)


def test_source_build_context_exports_only_the_committed_tree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Gate Test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "gate@example.invalid"],
        cwd=repository,
        check=True,
    )
    for name in ("Dockerfile", "Dockerfile.ray", "pyproject.toml", "uv.lock"):
        (repository / name).write_text(f"tracked {name}\n", encoding="utf-8")
    for name, patterns in DOCKER_CONTEXT_ALLOWLISTS.items():
        (repository / name).write_text("\n".join(patterns) + "\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".env\n", encoding="utf-8")
    (repository / ".env").write_text("DJANGO_API_TOKEN=secret\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "add",
            "Dockerfile",
            "Dockerfile.ray",
            "Dockerfile.dockerignore",
            "Dockerfile.ray.dockerignore",
            "pyproject.toml",
            "uv.lock",
            ".gitignore",
        ],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "test: seed archive"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source_tree = subprocess.run(
        ["git", "rev-parse", f"{commit}^{{tree}}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "Dockerfile").write_text("working tree drift\n", encoding="utf-8")
    export_root = tmp_path / "export"
    export_root.mkdir()

    context = create_source_build_context(
        runner=Runner(),
        root=repository,
        temporary_root=export_root,
        commit=commit,
        source_tree=source_tree,
    )

    assert (context / "Dockerfile").is_file()
    assert (context / "Dockerfile").read_text(encoding="utf-8") == "tracked Dockerfile\n"
    assert not (context / ".env").exists()
    assert not (context / ".git").exists()


def test_released_v040_handoff_pin_is_immutable() -> None:
    assert gate_module.RELEASED_V040_TAG == "v0.4.0"
    assert gate_module.RELEASED_V040_COMMIT == "95ee5dfe95b1c1bed95ff28c4fcb5fcdc491e485"
    assert gate_module.RELEASED_V040_SOURCE_TREE == "6ce02dfe51832db6227cc886bcced62399167f8b"


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        (("f" * 40,), "release commit"),
        ((gate_module.RELEASED_V040_COMMIT, "f" * 40), "reviewed source tree"),
        (
            (
                gate_module.RELEASED_V040_COMMIT,
                gate_module.RELEASED_V040_SOURCE_TREE,
                "",
                "commit",
            ),
            "annotated tag",
        ),
        (
            (
                gate_module.RELEASED_V040_COMMIT,
                gate_module.RELEASED_V040_SOURCE_TREE,
                "",
                "tag",
                "f" * 40,
            ),
            "pinned release commit",
        ),
    ],
)
def test_released_v040_source_identity_fails_closed_on_pin_or_present_tag_drift(
    responses: tuple[str, ...],
    message: str,
) -> None:
    class ReleaseIdentityRunner:
        def __init__(self) -> None:
            self.redactor = Redactor()
            self.calls: list[tuple[str, ...]] = []
            self.responses = iter(responses)

        def run(self, args: list[str], **_kwargs: object) -> CommandResult:
            self.calls.append(tuple(args))
            return CommandResult(next(self.responses), "", 0)

    runner = ReleaseIdentityRunner()
    gate = LocalKubeRayGate(_config(), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=message):
        gate._verify_released_v040_source_identity()

    assert runner.calls[0] == (
        "git",
        "rev-parse",
        "--verify",
        f"{gate_module.RELEASED_V040_COMMIT}^{{commit}}",
    )


@pytest.mark.parametrize(
    ("tag_returncode", "error"),
    [(1, None), (2, "could not verify the optional local")],
)
def test_released_v040_source_identity_handles_optional_tag_lookup(
    tag_returncode: int,
    error: str | None,
) -> None:
    class ReleaseIdentityRunner:
        def __init__(self) -> None:
            self.redactor = Redactor()
            self.calls: list[tuple[tuple[str, ...], bool]] = []
            self.responses = iter(
                (
                    CommandResult(gate_module.RELEASED_V040_COMMIT, "", 0),
                    CommandResult(gate_module.RELEASED_V040_SOURCE_TREE, "", 0),
                    CommandResult("", "", tag_returncode),
                )
            )

        def run(self, args: list[str], *, check: bool = True, **_kwargs: object) -> CommandResult:
            self.calls.append((tuple(args), check))
            return next(self.responses)

    runner = ReleaseIdentityRunner()
    gate = LocalKubeRayGate(_config(), runner=runner)  # type: ignore[arg-type]

    if error is None:
        gate._verify_released_v040_source_identity()
    else:
        with pytest.raises(ValueError, match=error):
            gate._verify_released_v040_source_identity()

    tag_ref = f"refs/tags/{gate_module.RELEASED_V040_TAG}"
    assert runner.calls == [
        (
            (
                "git",
                "rev-parse",
                "--verify",
                f"{gate_module.RELEASED_V040_COMMIT}^{{commit}}",
            ),
            True,
        ),
        (
            (
                "git",
                "rev-parse",
                "--verify",
                f"{gate_module.RELEASED_V040_COMMIT}^{{tree}}",
            ),
            True,
        ),
        (("git", "show-ref", "--verify", "--quiet", tag_ref), False),
    ]


def test_released_v040_source_identity_verifies_present_annotated_tag() -> None:
    class ReleaseIdentityRunner:
        def __init__(self) -> None:
            self.redactor = Redactor()
            self.calls: list[tuple[tuple[str, ...], bool]] = []
            self.responses = iter(
                (
                    CommandResult(gate_module.RELEASED_V040_COMMIT, "", 0),
                    CommandResult(gate_module.RELEASED_V040_SOURCE_TREE, "", 0),
                    CommandResult("", "", 0),
                    CommandResult("tag", "", 0),
                    CommandResult(gate_module.RELEASED_V040_COMMIT, "", 0),
                )
            )

        def run(self, args: list[str], *, check: bool = True, **_kwargs: object) -> CommandResult:
            self.calls.append((tuple(args), check))
            return next(self.responses)

    runner = ReleaseIdentityRunner()
    gate = LocalKubeRayGate(_config(), runner=runner)  # type: ignore[arg-type]

    gate._verify_released_v040_source_identity()

    tag_ref = f"refs/tags/{gate_module.RELEASED_V040_TAG}"
    assert runner.calls == [
        (
            (
                "git",
                "rev-parse",
                "--verify",
                f"{gate_module.RELEASED_V040_COMMIT}^{{commit}}",
            ),
            True,
        ),
        (
            (
                "git",
                "rev-parse",
                "--verify",
                f"{gate_module.RELEASED_V040_COMMIT}^{{tree}}",
            ),
            True,
        ),
        (("git", "show-ref", "--verify", "--quiet", tag_ref), False),
        (("git", "cat-file", "-t", tag_ref), True),
        (("git", "rev-parse", "--verify", f"{tag_ref}^{{commit}}"), True),
    ]


def test_docker_context_policies_fail_closed_without_exact_specific_allowlists(
    tmp_path: Path,
) -> None:
    for name, patterns in DOCKER_CONTEXT_ALLOWLISTS.items():
        (tmp_path / name).write_text("\n".join(patterns) + "\n", encoding="utf-8")

    inspect_docker_context_allowlists(tmp_path)

    (tmp_path / "Dockerfile.dockerignore").unlink()
    with pytest.raises(ValueError, match="missing required Docker context policy"):
        inspect_docker_context_allowlists(tmp_path)

    for omitted_pattern in (
        "**/.env",
        "**/.env.*",
        "**/*.sqlite3",
        "**/*.sqlite3-*",
    ):
        unsafe_patterns = tuple(
            pattern
            for pattern in DOCKER_CONTEXT_ALLOWLISTS["Dockerfile.dockerignore"]
            if pattern != omitted_pattern
        )
        (tmp_path / "Dockerfile.dockerignore").write_text(
            "\n".join(unsafe_patterns) + "\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="reviewed deny-by-default allowlist"):
            inspect_docker_context_allowlists(tmp_path)


def test_historical_docker_context_policy_is_bound_to_exact_released_source(
    tmp_path: Path,
) -> None:
    historical = gate_module.RELEASED_V040_DOCKER_CONTEXT_ALLOWLISTS
    for name, patterns in historical.items():
        (tmp_path / name).write_text("\n".join(patterns) + "\n", encoding="utf-8")

    inspect_docker_context_allowlists(tmp_path, expected_allowlists=historical)
    assert (
        gate_module._docker_context_allowlists_for_source(
            commit=gate_module.RELEASED_V040_COMMIT,
            source_tree=gate_module.RELEASED_V040_SOURCE_TREE,
        )
        is historical
    )
    assert (
        gate_module._docker_context_allowlists_for_source(
            commit="f" * 40,
            source_tree=gate_module.RELEASED_V040_SOURCE_TREE,
        )
        is DOCKER_CONTEXT_ALLOWLISTS
    )
    with pytest.raises(ValueError, match="unexpected source tree"):
        gate_module._docker_context_allowlists_for_source(
            commit=gate_module.RELEASED_V040_COMMIT,
            source_tree="f" * 40,
        )
    with pytest.raises(ValueError, match="reviewed deny-by-default allowlist"):
        inspect_docker_context_allowlists(tmp_path)

    historical_path = tmp_path / "Dockerfile.dockerignore"
    historical_path.write_text(
        historical_path.read_text(encoding="utf-8") + "**/unexpected\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reviewed deny-by-default allowlist"):
        inspect_docker_context_allowlists(tmp_path, expected_allowlists=historical)


def test_source_bound_tag_contains_tree_time_and_uniqueness() -> None:
    tag = source_bound_tag(
        SOURCE_TREE,
        now=datetime(2026, 7, 21, 12, 34, 56, tzinfo=UTC),
        nonce="deadbeef",
    )

    assert tag == TAG
    assert re.fullmatch(r"local-gate-tree-[0-9a-f]{12}-\d{14}-[0-9a-f]{8}", tag)


def test_runtime_env_encryption_envelope_contract_is_exact_and_canonical() -> None:
    serialized, nonce, ciphertext = _runtime_env_envelope()

    assert validate_runtime_env_encryption_envelope(serialized) == (nonce, ciphertext)

    noncanonical = json.dumps(json.loads(serialized))
    with pytest.raises(ValueError, match="not canonical"):
        validate_runtime_env_encryption_envelope(noncanonical)

    exposed = serialized.replace(ciphertext, RUNTIME_ENV_STORAGE_PROBE_MARKER)
    with pytest.raises(ValueError, match="plaintext probe marker"):
        validate_runtime_env_encryption_envelope(exposed)


def test_runtime_env_encryption_envelope_parse_failure_has_no_private_cause() -> None:
    private_value = "raw-envelope-value-that-must-not-enter-the-exception-graph"

    with pytest.raises(ValueError, match="not valid JSON") as captured:
        validate_runtime_env_encryption_envelope(f'{{"ciphertext":"{private_value}"')

    assert private_value not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_secret_data_digest_covers_every_value_without_order_dependence() -> None:
    original = {"DJANGO_API_TOKEN": "dG9rZW4=", "DJANGO_SECRET_KEY": "c2VjcmV0"}
    reordered = dict(reversed(tuple(original.items())))

    assert secret_data_sha256(original) == secret_data_sha256(reordered)
    assert secret_data_sha256(original) != secret_data_sha256(
        {**original, "DJANGO_SECRET_KEY": "Y2hhbmdlZA=="}
    )


def test_overlay_copy_replaces_tags_without_editing_repository(tmp_path: Path) -> None:
    source = ROOT / "k8s"
    original = (source / "overlays/kuberay-kind/kustomization.yaml").read_text(encoding="utf-8")

    overlay = configure_overlay_copy(
        source_k8s=source,
        destination_k8s=tmp_path / "k8s",
        tag=TAG,
    )

    copied = yaml.safe_load((overlay / "kustomization.yaml").read_text(encoding="utf-8"))
    assert copied["images"] == [
        {"name": "django-ray", "newName": "django-ray", "newTag": TAG},
        {"name": "django-ray-worker", "newName": "django-ray-worker", "newTag": TAG},
    ]
    assert (source / "overlays/kuberay-kind/kustomization.yaml").read_text(
        encoding="utf-8"
    ) == original


@lru_cache(maxsize=1)
def _render_real_kuberay_overlay_yaml() -> str | None:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        return None
    with tempfile.TemporaryDirectory(prefix="kuberay-overlay-") as temp_dir:
        overlay = configure_overlay_copy(
            source_k8s=ROOT / "k8s",
            destination_k8s=Path(temp_dir) / "k8s",
            tag=TAG,
        )
        return subprocess.run(
            [kubectl, "kustomize", str(overlay)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout


def test_real_kuberay_overlay_is_namespace_scoped_and_source_bound() -> None:
    rendered = _render_real_kuberay_overlay_yaml()
    if rendered is None:
        pytest.skip("kubectl is required to render the KubeRay overlay")

    resources = load_rendered_resources(rendered)
    inspect_rendered_resources(resources, namespace=EXPECTED_NAMESPACE, tag=TAG)
    inspect_runtime_env_encryption_overlay(resources)
    inspect_ray_job_request_storage_overlay(resources)

    assert all(
        resource["kind"] == "Namespace"
        or resource.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE
        for resource in resources
    )
    assert not any(resource["kind"].startswith("ClusterRole") for resource in resources)
    assert not any(resource["kind"] == "Secret" for resource in resources)

    ray_cluster = next(resource for resource in resources if resource.get("kind") == "RayCluster")
    head = ray_cluster["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]
    payload_mount = next(
        mount for mount in head["volumeMounts"] if mount["name"] == "payload-storage"
    )
    payload_mount.pop("readOnly")
    with pytest.raises(ValueError, match="must be read-only"):
        inspect_ray_job_request_storage_overlay(resources)


def test_runtime_env_encryption_overlay_is_scoped_to_application_containers() -> None:
    resources = _runtime_env_encryption_resources()

    inspect_runtime_env_encryption_overlay(resources)

    target = next(
        resource
        for resource in resources
        if resource.get("kind") == "Deployment"
        and resource.get("metadata", {}).get("name") == "django-ray-worker-sync"
    )
    target["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "plaintext"
    with pytest.raises(ValueError, match="exactly on django-web"):
        inspect_runtime_env_encryption_overlay(resources)


@pytest.mark.parametrize("location", ["configmap", "secret", "ray", "unknown-envfrom"])
def test_runtime_env_encryption_overlay_rejects_shared_or_ray_selectors(location: str) -> None:
    resources = _runtime_env_encryption_resources()
    if location == "configmap":
        configmap = next(resource for resource in resources if resource.get("kind") == "ConfigMap")
        configmap["data"] = {
            "DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE": "encrypted",
        }
    elif location == "secret":
        resources.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "injected-runtime-env",
                    "namespace": EXPECTED_NAMESPACE,
                },
                "data": {"DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE": "ZW5jcnlwdGVk"},
            }
        )
    elif location == "unknown-envfrom":
        target = next(
            resource
            for resource in resources
            if resource.get("kind") == "Deployment"
            and resource.get("metadata", {}).get("name") == "django-ray-worker-sync"
        )
        target["spec"]["template"]["spec"]["containers"][0].setdefault("envFrom", []).append(
            {"secretRef": {"name": "external-runtime-env"}}
        )
    else:
        ray_cluster = next(
            resource for resource in resources if resource.get("kind") == "RayCluster"
        )
        ray_cluster["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["env"] = [
            {
                "name": "DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE",
                "value": "encrypted",
            }
        ]

    with pytest.raises(ValueError):
        inspect_runtime_env_encryption_overlay(resources)


def test_preserved_secret_rejects_runtime_env_selector_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "DJANGO_API_TOKEN": base64.b64encode(TOKEN68.encode()).decode(),
        "DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE": "ZW5jcnlwdGVk",
    }

    with pytest.raises(ValueError, match="must not contain"):
        inspect_runtime_env_encryption_secret_data(data)

    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(gate, "_secret_data", lambda: data)
    with pytest.raises(ValueError, match="must not contain"):
        gate._secret_token()


def test_real_kuberay_overlay_pins_exact_lean_ray_topology() -> None:
    rendered = _render_real_kuberay_overlay_yaml()
    if rendered is None:
        pytest.skip("kubectl is required to render the KubeRay overlay")
    ray_cluster = next(
        resource
        for resource in load_rendered_resources(rendered)
        if resource.get("kind") == "RayCluster"
    )

    assert expected_ray_topology(ray_cluster) == (1, 2)

    ray_cluster["spec"]["workerGroupSpecs"][0]["minReplicas"] = 3
    with pytest.raises(ValueError, match="must pin minReplicas"):
        expected_ray_topology(ray_cluster)


def test_parse_ray_image_python_version_accepts_one_canonical_python_312_line() -> None:
    assert parse_ray_image_python_version("3.12.12\n") == "3.12.12"


@pytest.mark.parametrize(
    "stdout",
    (
        "",
        "3.12\n",
        "3.12.012\n",
        "3.12.12rc1\n",
        "3.13.0\n",
        "warning: stale image\n3.12.12\n",
        "3.12.12\nwarning: stale image\n",
        " 3.12.12\n",
    ),
)
def test_parse_ray_image_python_version_rejects_noise_wrong_minor_and_noncanonical_output(
    stdout: str,
) -> None:
    with pytest.raises(ValueError):
        parse_ray_image_python_version(stdout)


def test_ray_runtime_image_reference_requires_one_shared_named_ray_image() -> None:
    rendered = cast(dict[str, Any], _ray_cluster())
    topology = normalize_ray_topology(rendered)

    assert ray_runtime_image_reference(topology) == "rayproject/ray:2.60.0-py312"

    rendered["spec"]["workerGroupSpecs"][0]["template"]["spec"]["containers"][0]["image"] = (
        "rayproject/ray:2.60.0-py312-drift"
    )
    with pytest.raises(ValueError):
        ray_runtime_image_reference(normalize_ray_topology(rendered))


@pytest.mark.parametrize(
    "image",
    (
        "rayproject/ray:latest",
        "rayproject/ray",
        " rayproject/ray:2.60.0-py312",
        "-rayproject/ray:2.60.0-py312",
        "rayproject/ray:2.60.0-py312-\N{SNOWMAN}",
        "r" * (gate_module.MAX_RAY_IMAGE_REFERENCE_CHARACTERS + 1) + ":tag",
    ),
)
def test_ray_runtime_image_reference_rejects_unbounded_or_unpinned_references(
    image: str,
) -> None:
    rendered = cast(dict[str, Any], _ray_cluster())
    rendered["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["image"] = image
    rendered["spec"]["workerGroupSpecs"][0]["template"]["spec"]["containers"][0]["image"] = image

    with pytest.raises(ValueError):
        ray_runtime_image_reference(normalize_ray_topology(rendered))


@pytest.mark.parametrize("component", ("head", "worker"))
def test_ray_runtime_image_reference_rejects_a_missing_named_ray_container(
    component: str,
) -> None:
    rendered = cast(dict[str, Any], _ray_cluster())
    if component == "head":
        container = rendered["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]
    else:
        container = rendered["spec"]["workerGroupSpecs"][0]["template"]["spec"]["containers"][0]
    container["name"] = f"not-the-ray-{component}"

    with pytest.raises(ValueError):
        ray_runtime_image_reference(normalize_ray_topology(rendered))


def test_ray_topology_normalization_rejects_equal_count_structural_drift() -> None:
    rendered = _ray_cluster(workers=4)
    live = json.loads(json.dumps(rendered))
    original_group = live["spec"]["workerGroupSpecs"][0]
    first = dict(original_group, groupName="first", minReplicas=2, replicas=2, maxReplicas=2)
    second = dict(original_group, groupName="second", minReplicas=2, replicas=2, maxReplicas=2)
    live["spec"]["workerGroupSpecs"] = [first, second]

    assert expected_ray_topology(rendered) == expected_ray_topology(live) == (1, 4)
    assert normalize_ray_topology(rendered) != normalize_ray_topology(live)

    image_drift = json.loads(json.dumps(rendered))
    image_drift["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["image"] = (
        "rayproject/ray:stale"
    )
    assert normalize_ray_topology(rendered) != normalize_ray_topology(image_drift)


def test_live_ray_topology_must_match_the_exact_rendered_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = _ray_cluster(uid="cluster-owner", workers=4)
    live = json.loads(json.dumps(rendered))
    original_group = live["spec"]["workerGroupSpecs"][0]
    live["spec"]["workerGroupSpecs"] = [
        dict(original_group, groupName="first", minReplicas=2, replicas=2, maxReplicas=2),
        dict(original_group, groupName="second", minReplicas=2, replicas=2, maxReplicas=2),
    ]
    gate = LocalKubeRayGate(_config())
    gate.rendered_ray_topology = normalize_ray_topology(rendered)

    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(json.dumps(live), "", 0),
    )

    with pytest.raises(ValueError, match="exact rendered head/worker contract"):
        gate._ray_pods(allow_empty=True)


def test_prometheus_rbac_is_namespaced_and_has_no_node_permission() -> None:
    resources = list(
        yaml.safe_load_all((ROOT / "k8s/base/monitoring.yaml").read_text(encoding="utf-8"))
    )
    role = next(resource for resource in resources if resource.get("kind") == "Role")
    binding = next(resource for resource in resources if resource.get("kind") == "RoleBinding")

    assert role["metadata"] == {"name": "prometheus-django-ray", "namespace": EXPECTED_NAMESPACE}
    assert binding["metadata"]["namespace"] == EXPECTED_NAMESPACE
    assert binding["roleRef"]["kind"] == "Role"
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}
    ]


def test_monitoring_imports_dashboards_from_the_running_ray_release() -> None:
    """Do not ship stale generated snapshots beside the runtime dashboard importer."""
    monitoring = (ROOT / "k8s/base/monitoring.yaml").read_text(encoding="utf-8")

    assert 'DASHBOARDS_PATH = "/tmp/ray/session_latest/metrics/grafana/dashboards"' in monitoring
    assert 'pattern = os.path.join(DASHBOARDS_PATH, "*_dashboard.json")' in monitoring
    assert not (ROOT / "k8s/base/ray_default_dashboard.json").exists()
    assert not (ROOT / "k8s/base/ray_data_dashboard.json").exists()


def test_render_guard_rejects_cluster_scoped_and_cross_namespace_resources() -> None:
    cluster_scoped = _resources()
    cluster_scoped.append(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "unexpected", "namespace": EXPECTED_NAMESPACE},
        }
    )
    with pytest.raises(ValueError, match="not in the guarded inventory"):
        inspect_rendered_resources(cluster_scoped, namespace=EXPECTED_NAMESPACE, tag=TAG)

    cross_namespace = _resources()
    cast_deployment = next(
        resource
        for resource in cross_namespace
        if resource.get("kind") == "Deployment"
        and resource.get("metadata", {}).get("name") == "django-web"  # type: ignore[union-attr]
    )
    cast_deployment["metadata"]["namespace"] = "default"  # type: ignore[index]
    with pytest.raises(ValueError, match="targets namespace 'default'"):
        inspect_rendered_resources(cross_namespace, namespace=EXPECTED_NAMESPACE, tag=TAG)


def test_unknown_workloads_have_no_pre_setup_apply_phase() -> None:
    resources = _resources()
    unexpected = _deployment("unexpected-workload", image="busybox:1.36")
    resources.append(unexpected)

    with pytest.raises(ValueError, match="not in the guarded inventory"):
        inspect_rendered_resources(resources, namespace=EXPECTED_NAMESPACE, tag=TAG)
    with pytest.raises(ValueError, match="has no guarded apply phase"):
        split_apply_resources(resources)  # type: ignore[arg-type]


def test_render_guard_rejects_floating_application_images() -> None:
    resources = _resources()
    deployment = next(
        resource
        for resource in resources
        if resource.get("kind") == "Deployment"
        and resource.get("metadata", {}).get("name") == "django-web"  # type: ignore[union-attr]
    )
    deployment["spec"] = {
        "template": {"spec": {"containers": [_container("django-web", "django-ray:latest")]}}
    }

    with pytest.raises(ValueError, match="floating tag"):
        inspect_rendered_resources(resources, namespace=EXPECTED_NAMESPACE, tag=TAG)


def test_setup_job_is_separated_from_secret_free_apply_stream() -> None:
    resources = _resources()

    prerequisites, setup, workloads = split_apply_resources(resources)  # type: ignore[arg-type]

    assert not any(resource.get("kind") == "Secret" for resource in resources)
    assert setup["metadata"]["name"] == SETUP_JOB
    assert all(resource.get("kind") != "Job" for resource in prerequisites)
    assert not any(resource.get("kind") == "Secret" for resource in prerequisites)
    assert {resource["metadata"]["name"] for resource in workloads} == {
        *APP_DEPLOYMENTS,
        RAY_CLUSTER_NAME,
    }
    assert not any(
        resource.get("kind") in {"Deployment", "RayCluster"}
        and resource.get("metadata", {}).get("name") in {*APP_DEPLOYMENTS, RAY_CLUSTER_NAME}
        for resource in prerequisites
    )


def test_rendered_bootstrap_secret_is_rejected_from_every_apply_phase() -> None:
    resources = _resources()
    resources.append(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "django-ray-secret",
                "namespace": EXPECTED_NAMESPACE,
            },
        }
    )

    with pytest.raises(ValueError, match="not in the guarded inventory"):
        inspect_rendered_resources(resources, namespace=EXPECTED_NAMESPACE, tag=TAG)
    with pytest.raises(ValueError, match="has no guarded apply phase"):
        split_apply_resources(resources)  # type: ignore[arg-type]


def test_full_gate_removes_co_resident_policy_before_applying_prerequisites(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.temp_root = tmp_path
    gate.resources = _resources()
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str, **_kwargs: object) -> CommandResult:
        calls.append(args)
        return CommandResult("", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    gate._apply_overlay()

    assert calls[0] == (
        "delete",
        *gate_module.CO_RESIDENT_APPLICATION_POLICY_OBJECTS,
        "--ignore-not-found=true",
        "--wait=true",
    )
    apply_index = next(index for index, call in enumerate(calls) if call[0] == "apply")
    assert apply_index > 0


def test_request_reference_payload_storage_has_exact_writer_reader_boundaries() -> None:
    claim = yaml.safe_load((ROOT / "k8s/base/payload-storage.yaml").read_text(encoding="utf-8"))
    assert claim["kind"] == "PersistentVolumeClaim"
    assert claim["metadata"]["name"] == "payload-storage-pvc"
    assert claim["spec"]["accessModes"] == ["ReadWriteMany"]

    kustomization = yaml.safe_load(
        (ROOT / "k8s/base/kustomization.yaml").read_text(encoding="utf-8")
    )
    assert "payload-storage.yaml" in kustomization["resources"]
    config = yaml.safe_load((ROOT / "k8s/base/configmap.yaml").read_text(encoding="utf-8"))
    assert config["data"]["DJANGO_RAY_INPUT_STORAGE_BACKEND"] == "filesystem"
    assert config["data"]["DJANGO_RAY_INPUT_STORAGE_FILESYSTEM_PATH"] == "/payload-storage/inputs"

    for manifest_name, deployment_name in (
        ("django-web.yaml", "django-web"),
        ("django-ray-worker.yaml", "django-ray-worker"),
    ):
        resources = list(
            yaml.safe_load_all((ROOT / f"k8s/base/{manifest_name}").read_text(encoding="utf-8"))
        )
        deployment = next(
            resource
            for resource in resources
            if resource.get("kind") == "Deployment"
            and resource["metadata"]["name"] == deployment_name
        )
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        payload_mount = next(
            mount for mount in container["volumeMounts"] if mount["name"] == "payload-storage"
        )
        assert payload_mount == {
            "name": "payload-storage",
            "mountPath": "/payload-storage",
        }
        payload_volume = next(
            volume for volume in pod["volumes"] if volume["name"] == "payload-storage"
        )
        assert payload_volume["persistentVolumeClaim"]["claimName"] == "payload-storage-pvc"

    static_ray = list(
        yaml.safe_load_all((ROOT / "k8s/base/ray-cluster.yaml").read_text(encoding="utf-8"))
    )
    for deployment_name in ("ray-head", "ray-worker"):
        deployment = next(
            resource
            for resource in static_ray
            if resource.get("kind") == "Deployment"
            and resource["metadata"]["name"] == deployment_name
        )
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        payload_mount = next(
            mount for mount in container["volumeMounts"] if mount["name"] == "payload-storage"
        )
        assert payload_mount["readOnly"] is True

    kuberay = next(
        resource
        for resource in yaml.safe_load_all(
            (ROOT / "k8s/overlays/kuberay-kind/ray-cluster-kuberay.yaml").read_text(
                encoding="utf-8"
            )
        )
        if resource.get("kind") == "RayCluster"
    )
    ray_pod_specs = [
        kuberay["spec"]["headGroupSpec"]["template"]["spec"],
        kuberay["spec"]["workerGroupSpecs"][0]["template"]["spec"],
    ]
    for pod in ray_pod_specs:
        container = pod["containers"][0]
        payload_mount = next(
            mount for mount in container["volumeMounts"] if mount["name"] == "payload-storage"
        )
        assert payload_mount == {
            "name": "payload-storage",
            "mountPath": "/payload-storage",
            "readOnly": True,
        }
        payload_volume = next(
            volume for volume in pod["volumes"] if volume["name"] == "payload-storage"
        )
        assert payload_volume["persistentVolumeClaim"]["claimName"] == "payload-storage-pvc"

    ray_job_manager = yaml.safe_load(
        (ROOT / "k8s/overlays/kuberay-kind/worker-ray-job.yaml").read_text(encoding="utf-8")
    )
    assert ray_job_manager["metadata"]["name"] == "django-ray-worker-ray-job"
    ray_job_pod = ray_job_manager["spec"]["template"]["spec"]
    ray_job_container = ray_job_pod["containers"][0]
    assert ray_job_container["command"] == [
        "python",
        "testproject/manage.py",
        "django_ray_worker",
    ]
    assert ray_job_container["args"] == [
        "--queue",
        "ray-data",
        "--concurrency",
        "1",
    ]
    assert not {"--sync", "--local", "--cluster"} & set(ray_job_container["args"])
    ray_job_payload_mount = next(
        mount for mount in ray_job_container["volumeMounts"] if mount["name"] == "payload-storage"
    )
    assert ray_job_payload_mount == {
        "name": "payload-storage",
        "mountPath": "/payload-storage",
    }
    ray_job_payload_volume = next(
        volume for volume in ray_job_pod["volumes"] if volume["name"] == "payload-storage"
    )
    assert ray_job_payload_volume["persistentVolumeClaim"]["claimName"] == ("payload-storage-pvc")
    kuberay_kustomization = yaml.safe_load(
        (ROOT / "k8s/overlays/kuberay-kind/kustomization.yaml").read_text(encoding="utf-8")
    )
    assert "worker-ray-job.yaml" in kuberay_kustomization["resources"]

    settings_source = (ROOT / "testproject/settings.py").read_text(encoding="utf-8")
    assert '"DJANGO_RAY_INPUT_STORAGE_BACKEND"' in settings_source
    assert '"DJANGO_RAY_INPUT_STORAGE_FILESYSTEM_PATH"' in settings_source

    tls_overlay = (ROOT / "k8s/overlays/dev-tls/kustomization.yaml").read_text(encoding="utf-8")
    assert tls_overlay.count("path: /spec/template/spec/containers/0/volumeMounts/-") == 3
    assert tls_overlay.count("path: /spec/template/spec/volumes/-") == 3
