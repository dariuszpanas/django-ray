"""Unit tests for the local KubeRay application gate."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
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
    EXPECTED_PROBE_HOST,
    EXPECTED_PROBE_PATH,
    MAX_HTTP_RESPONSE_BYTES,
    MAX_OUTPUT_CHARACTERS,
    RAY_CLUSTER_LABEL,
    RAY_CLUSTER_NAME,
    RUNTIME_ENV_REQUIRED_MEMBER,
    RUNTIME_ENV_STORAGE_PROBE_MARKER,
    SETUP_JOB,
    TASK_MANAGER_DEPLOYMENTS,
    CommandError,
    CommandResult,
    DeploymentContract,
    GateError,
    LocalKubeRayGate,
    PodImageContract,
    Redactor,
    RejectRedirects,
    Runner,
    build_local_http_opener,
    configure_overlay_copy,
    create_source_build_context,
    expected_ray_topology,
    inspect_docker_context_allowlists,
    inspect_kubeconfig_snapshot,
    inspect_probe_contract,
    inspect_ray_job_request_storage_overlay,
    inspect_rendered_resources,
    inspect_runtime_env_encryption_overlay,
    inspect_runtime_env_encryption_secret_data,
    inspect_setup_log,
    load_rendered_resources,
    normalize_ray_topology,
    normalize_runtime_image_id,
    parse_docker_image_inspect,
    parse_ray_image_python_version,
    parse_runtime_archive_probe,
    parse_task_result,
    pod_image_contract,
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
    APP_TAG,
    COMMIT,
    IMAGE_ID,
    PRIOR_RELEASED_V040_HOSTNAME,
    PRIOR_RELEASED_V040_TAG,
    PRIOR_RELEASED_V040_WORKER_ID,
    PROTOCOL_V1_SURVIVAL_MARKER,
    PROTOCOL_V1_SURVIVAL_TASK_ID,
    PROTOCOL_V2_POISON,
    ROOT,
    SOURCE_TREE,
    TAG,
    TASK_ID,
    TOKEN68,
    _application_inventory_fixture,
    _application_pod,
    _application_replicaset,
    _bounded_poll_metadata,
    _config,
    _container,
    _deployment,
    _execution_protocol_metrics,
    _kubeconfig_payload,
    _live_application_deployment,
    _old_terminating_application_generation,
    _percent_hex_case_variants,
    _protocol_v2_rejection_payload,
    _protocol_v2_target_execution_payload,
    _ray_cluster,
    _ray_pod,
    _resources,
    _runtime_env_encryption_resources,
    _runtime_env_envelope,
    _set_ray_topology,
    _setup_pod,
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


def test_released_v040_manager_manifest_is_ephemeral_exact_and_protocol_honest() -> None:
    gate = LocalKubeRayGate(_config())
    gate.evidence.released_v040_image_tag = (
        f"{gate_module.RELEASED_V040_IMAGE_REPOSITORY}:test-source"
    )

    manifest = gate._released_v040_manager_manifest()

    assert gate_module.RELEASED_V040_MANAGER_DEPLOYMENT not in APP_DEPLOYMENTS
    assert manifest["apiVersion"] == "apps/v1"
    assert manifest["kind"] == "Deployment"
    assert manifest["metadata"] == {
        "name": gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
        "namespace": EXPECTED_NAMESPACE,
        "labels": {
            "app": "django-ray",
            "component": "worker",
            "queues": gate_module.RAY_JOB_GATE_QUEUE,
            "runner": "ray-job-v040",
        },
    }
    spec = manifest["spec"]
    assert spec["replicas"] == 1
    assert spec["selector"]["matchLabels"] == spec["template"]["metadata"]["labels"]
    pod_spec = spec["template"]["spec"]
    container = pod_spec["containers"][0]
    assert container["name"] == gate_module.RELEASED_V040_MANAGER_CONTAINER
    assert container["image"] == gate.evidence.released_v040_image_tag
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["command"] == [
        "python",
        "testproject/manage.py",
        "django_ray_worker",
    ]
    assert container["args"] == [
        "--queue",
        gate_module.RAY_JOB_GATE_QUEUE,
        "--concurrency",
        "1",
    ]
    assert container["envFrom"] == [
        {"configMapRef": {"name": "django-ray-config"}},
    ]
    assert {entry["name"]: entry["value"] for entry in container["env"] if "value" in entry} == {
        "RAY_ADDRESS": "ray://ray-head-svc:10001",
        "RAY_AUTH_MODE": "token",
        "DJANGO_API_ENABLED": "false",
        "RAY_MAX_RETRIES": "1",
        **gate_module.RUNTIME_ENV_ENCRYPTION_ENV,
    }
    references = {
        entry["name"]: entry["valueFrom"]["secretKeyRef"]
        for entry in container["env"]
        if "valueFrom" in entry
    }
    assert set(references) == {
        "DJANGO_SECRET_KEY",
        "DATABASE_USER",
        "DATABASE_PASSWORD",
        "RAY_AUTH_TOKEN",
    }
    assert references["RAY_AUTH_TOKEN"]["name"] == "django-ray-auth"
    assert container["readinessProbe"]["successThreshold"] == 1
    assert container["livenessProbe"]["successThreshold"] == 1
    serialized = json.dumps(manifest, sort_keys=True)
    assert "min_supported_execution_protocol_version" not in serialized
    assert "max_supported_execution_protocol_version" not in serialized
    assert "capability_schema_version" not in serialized


def test_released_v040_manager_create_and_delete_are_exact_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.temp_root = tmp_path
    gate.evidence.released_v040_image_tag = PRIOR_RELEASED_V040_TAG
    gate.evidence.released_v040_image_id = IMAGE_ID
    manifest = gate._released_v040_manager_manifest()
    deployment = json.loads(json.dumps(manifest))
    deployment["status"] = {"readyReplicas": 1}
    pod_spec = deployment["spec"]["template"]["spec"]
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "django-ray-worker-ray-job-v040-test",
            "namespace": EXPECTED_NAMESPACE,
            "uid": "released-v040-pod-uid",
        },
        "spec": pod_spec,
        "status": {
            "initContainerStatuses": [
                {
                    "name": entry["name"],
                    "image": entry["image"],
                    "imageID": f"sha256:{'c' * 64}",
                }
                for entry in pod_spec["initContainers"]
            ],
            "containerStatuses": [
                {
                    "name": gate_module.RELEASED_V040_MANAGER_CONTAINER,
                    "image": gate.evidence.released_v040_image_tag,
                    "imageID": IMAGE_ID,
                    "ready": True,
                }
            ],
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str, **_kwargs: object) -> CommandResult:
        calls.append(args)
        if args[:3] == (
            "get",
            "deployment",
            gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
        ):
            if args[-2:] == ("-o", "json"):
                return CommandResult(json.dumps(deployment), "", 0)
            return CommandResult("", "", 0)
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": [pod]}), "", 0)
        if args[:2] == ("get", "pods,replicasets"):
            return CommandResult(json.dumps({"items": []}), "", 0)
        return CommandResult("", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        gate,
        "_docker",
        lambda *_args, **_kwargs: CommandResult(_released_v040_image_inspect(), "", 0),
    )

    gate._apply_released_v040_manager()
    gate._delete_released_v040_manager()

    manifest_path = tmp_path / "released-v040-manager.yaml"
    assert yaml.safe_load(manifest_path.read_text(encoding="utf-8")) == manifest
    assert calls[0] == ("create", "-f", str(manifest_path))
    assert calls[1][:3] == (
        "rollout",
        "status",
        f"deployment/{gate_module.RELEASED_V040_MANAGER_DEPLOYMENT}",
    )
    assert calls[-3][:3] == (
        "delete",
        "deployment",
        gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
    )
    assert "--ignore-not-found=true" in calls[-3]
    assert "--wait=true" in calls[-3]
    assert calls[-2] == (
        "get",
        "deployment",
        gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
        "--ignore-not-found",
        "-o",
        "name",
    )
    assert calls[-1][:2] == ("get", "pods,replicasets")
    assert calls[-1][-2:] == ("-o", "json")


def _released_v040_recovery_deployment(gate: LocalKubeRayGate) -> dict[str, Any]:
    return gate._released_v040_manager_manifest(
        image_tag=PRIOR_RELEASED_V040_TAG,
        hostname=PRIOR_RELEASED_V040_HOSTNAME,
    )


def _released_v040_image_inspect(
    *,
    commit: str = gate_module.RELEASED_V040_COMMIT,
    source_tree: str = gate_module.RELEASED_V040_SOURCE_TREE,
) -> str:
    return json.dumps(
        [
            {
                "Id": IMAGE_ID,
                "RepoTags": [PRIOR_RELEASED_V040_TAG],
                "Config": {
                    "Labels": {
                        "org.opencontainers.image.revision": commit,
                        "org.opencontainers.image.source-tree": source_tree,
                    }
                },
            }
        ]
    )


def test_released_v040_predelete_accepts_live_defaulted_probe_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    deployment = _released_v040_recovery_deployment(gate)
    main = deployment["spec"]["template"]["spec"]["containers"][0]
    main["readinessProbe"]["successThreshold"] = 1
    main["livenessProbe"]["successThreshold"] = 1
    assert main["readinessProbe"]["successThreshold"] == 1
    assert main["livenessProbe"]["successThreshold"] == 1
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str, **_kwargs: object) -> CommandResult:
        calls.append(args)
        if args[:3] == (
            "get",
            "deployment",
            gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
        ):
            if args[-2:] == ("-o", "json"):
                return CommandResult(json.dumps(deployment), "", 0)
            return CommandResult("", "", 0)
        if args[:2] == ("get", "pods,replicasets"):
            return CommandResult(json.dumps({"items": []}), "", 0)
        return CommandResult("", "", 0)

    gate._released_v040_hostname = PRIOR_RELEASED_V040_HOSTNAME
    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        gate,
        "_docker",
        lambda *_args, **_kwargs: CommandResult(_released_v040_image_inspect(), "", 0),
    )

    gate._delete_released_v040_manager()

    assert calls[0][-2:] == ("-o", "json")
    assert calls[1][:3] == (
        "delete",
        "deployment",
        gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
    )
    assert sum(call[0] == "delete" for call in calls) == 1


def _released_v040_recovery_lease(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "worker_id": PRIOR_RELEASED_V040_WORKER_ID,
        "hostname": PRIOR_RELEASED_V040_HOSTNAME,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "capability_schema_version": 0,
        "django_ray_version": None,
        "min_supported_execution_protocol_version": None,
        "max_supported_execution_protocol_version": None,
        "is_active": False,
        "last_heartbeat_at": "2026-08-13T00:00:00+00:00",
        "stopped_at": "2026-08-13T00:01:00+00:00",
        "heartbeat_live": False,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("foreign_after_create_failure", [False, True])
def test_released_v040_ordinary_finally_never_deletes_unowned_same_name_after_create_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    foreign_after_create_failure: bool,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.temp_root = tmp_path
    gate.evidence.released_v040_image_tag = (
        f"{gate_module.RELEASED_V040_IMAGE_REPOSITORY}:released-v040-{TAG}"
    )
    gate.evidence.released_v040_image_id = IMAGE_ID
    foreign_deployment = _released_v040_recovery_deployment(gate)
    create_failed = False
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str, **_kwargs: object) -> CommandResult:
        nonlocal create_failed
        calls.append(args)
        if args[:3] == (
            "get",
            "deployment",
            gate_module.RELEASED_V040_MANAGER_DEPLOYMENT,
        ):
            if args[-2:] == ("-o", "json"):
                payload = (
                    json.dumps(foreign_deployment)
                    if create_failed and foreign_after_create_failure
                    else ""
                )
                return CommandResult(payload, "", 0)
            return CommandResult("", "", 0)
        if args[:2] == ("get", "pods,replicasets"):
            return CommandResult(json.dumps({"items": []}), "", 0)
        if args[0] == "create":
            create_failed = True
            raise RuntimeError("released manager create failed")
        if args[0] == "delete":
            pytest.fail("ordinary cleanup must not delete a Deployment it did not create")
        return CommandResult("", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        gate,
        "_observe_released_v040_reserved_leases",
        lambda: {"count": 0, "rows": []},
    )
    monkeypatch.setattr(
        gate,
        "_docker",
        lambda *_args, **_kwargs: CommandResult(
            _released_v040_image_inspect(commit="f" * 40),
            "",
            0,
        ),
    )

    gate._recover_released_v040_startup_residue()
    with pytest.raises(RuntimeError, match="released manager create failed"):
        gate._apply_released_v040_manager()

    if foreign_after_create_failure:
        with pytest.raises(ValueError):
            gate._delete_released_v040_manager()
    else:
        gate._delete_released_v040_manager()

    assert sum(call[0] == "create" for call in calls) == 1
    assert not any(call[0] == "delete" for call in calls)


def test_released_v040_startup_recovery_accepts_only_a_pinned_prior_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.evidence.released_v040_image_tag = (
        f"{gate_module.RELEASED_V040_IMAGE_REPOSITORY}:released-v040-{TAG}"
    )
    assert gate.evidence.released_v040_image_tag != PRIOR_RELEASED_V040_TAG
    deployment = _released_v040_recovery_deployment(gate)
    docker_calls: list[tuple[str, ...]] = []
    cleanup_events: list[str] = []

    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **_kwargs: CommandResult(json.dumps(deployment), "", 0),
    )

    def docker(*args: str, **_kwargs: object) -> CommandResult:
        docker_calls.append(args)
        return CommandResult(_released_v040_image_inspect(), "", 0)

    monkeypatch.setattr(gate, "_docker", docker)
    monkeypatch.setattr(
        gate,
        "_observe_released_v040_reserved_leases",
        lambda: {"count": 0, "rows": []},
    )

    def delete_manager() -> None:
        assert gate._released_v040_hostname == PRIOR_RELEASED_V040_HOSTNAME
        cleanup_events.append("deployment")

    def delete_lease() -> None:
        assert gate._released_v040_hostname == PRIOR_RELEASED_V040_HOSTNAME
        gate._released_v040_worker_id = PRIOR_RELEASED_V040_WORKER_ID
        cleanup_events.append("lease")

    monkeypatch.setattr(gate, "_delete_released_v040_manager", delete_manager)
    monkeypatch.setattr(gate, "_delete_released_v040_worker_lease", delete_lease)

    gate._recover_released_v040_startup_residue()

    assert docker_calls == [("image", "inspect", PRIOR_RELEASED_V040_TAG)]
    assert cleanup_events == ["deployment", "lease"]
    assert gate._released_v040_hostname is None
    assert gate._released_v040_worker_id is None


@pytest.mark.parametrize(
    "residue",
    [
        {"count": 2, "rows": [_released_v040_recovery_lease()] * 2},
        {
            "count": 1,
            "rows": [_released_v040_recovery_lease(hostname=f"dr-v040-{'9' * 32}")],
        },
    ],
)
def test_released_v040_prior_deployment_refuses_ambiguous_or_foreign_lease_without_deletion(
    monkeypatch: pytest.MonkeyPatch,
    residue: Mapping[str, Any],
) -> None:
    gate = LocalKubeRayGate(_config())
    deployment = _released_v040_recovery_deployment(gate)
    cleanup_events: list[str] = []
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **_kwargs: CommandResult(json.dumps(deployment), "", 0),
    )
    monkeypatch.setattr(
        gate,
        "_docker",
        lambda *_args, **_kwargs: CommandResult(_released_v040_image_inspect(), "", 0),
    )
    monkeypatch.setattr(gate, "_observe_released_v040_reserved_leases", lambda: residue)
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_manager",
        lambda: cleanup_events.append("deployment"),
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_worker_lease",
        lambda: cleanup_events.append("lease"),
    )

    with pytest.raises(ValueError, match="ambiguous|foreign"):
        gate._recover_released_v040_startup_residue()

    assert cleanup_events == []


@pytest.mark.parametrize(
    "drift",
    ["image-tag", "docker-revision", "docker-tree", "container-contract"],
)
def test_released_v040_startup_recovery_refuses_foreign_deployment_without_deletion(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    deployment = _released_v040_recovery_deployment(gate)
    inspect_payload = _released_v040_image_inspect()
    if drift == "image-tag":
        deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "foreign.example/django-ray:v0.4.0"
        )
    elif drift == "docker-revision":
        inspect_payload = _released_v040_image_inspect(commit="f" * 40)
    elif drift == "docker-tree":
        inspect_payload = _released_v040_image_inspect(source_tree="f" * 40)
    else:
        deployment["spec"]["template"]["spec"]["containers"][0]["args"][-1] = "2"
    cleanup_events: list[str] = []
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **_kwargs: CommandResult(json.dumps(deployment), "", 0),
    )
    monkeypatch.setattr(
        gate,
        "_docker",
        lambda *_args, **_kwargs: CommandResult(inspect_payload, "", 0),
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_manager",
        lambda: cleanup_events.append("deployment"),
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_worker_lease",
        lambda: cleanup_events.append("lease"),
    )

    with pytest.raises(ValueError):
        gate._recover_released_v040_startup_residue()

    assert cleanup_events == []


def test_released_v040_reserved_lease_observer_is_prefix_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    payload = {"count": 1, "rows": [_released_v040_recovery_lease()]}
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<released-v040-reserved-leases>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return payload

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._observe_released_v040_reserved_leases() == payload
    assert observed["field_name"] == "released v0.4.0 reserved lease recovery observation"
    assert 'hostname__startswith="dr-v040-"' in observed["script"]
    assert '"hostname", "worker_id"' in observed["script"]
    assert ")[:2]" in observed["script"]
    assert "get_lease_duration" in observed["script"]
    assert 'row["heartbeat_live"]' in observed["script"]


def test_released_v040_startup_recovery_accepts_one_expired_exact_lease_and_resets_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gate = LocalKubeRayGate(_config())
    cleanup_events: list[str] = []
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **_kwargs: CommandResult("", "", 0),
    )
    monkeypatch.setattr(
        gate,
        "_observe_released_v040_reserved_leases",
        lambda: {"count": 1, "rows": [_released_v040_recovery_lease()]},
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_manager",
        lambda: cleanup_events.append("deployment"),
    )

    def delete_lease() -> None:
        assert gate._released_v040_hostname == PRIOR_RELEASED_V040_HOSTNAME
        assert gate._released_v040_worker_id == PRIOR_RELEASED_V040_WORKER_ID
        cleanup_events.append("lease")

    monkeypatch.setattr(gate, "_delete_released_v040_worker_lease", delete_lease)

    gate._recover_released_v040_startup_residue()

    assert cleanup_events == ["deployment", "lease"]
    assert gate._released_v040_hostname is None
    assert gate._released_v040_worker_id is None

    next_hostname_uuid = gate_module.UUID("21234567-89ab-4cde-8fab-0123456789ab")
    gate.temp_root = tmp_path
    gate.evidence.released_v040_image_tag = (
        f"{gate_module.RELEASED_V040_IMAGE_REPOSITORY}:released-v040-{TAG}"
    )
    gate.evidence.released_v040_image_id = IMAGE_ID
    monkeypatch.setattr(gate_module, "uuid4", lambda: next_hostname_uuid)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("later apply failed")),
    )

    with pytest.raises(RuntimeError, match="later apply failed"):
        gate._apply_released_v040_manager()

    assert gate._released_v040_hostname == f"dr-v040-{next_hostname_uuid.hex}"
    assert gate._released_v040_hostname != PRIOR_RELEASED_V040_HOSTNAME
    assert gate._released_v040_worker_id is None


@pytest.mark.parametrize(
    ("residue", "message"),
    [
        (
            {"count": 1, "rows": [_released_v040_recovery_lease(heartbeat_live=True)]},
            "live or foreign",
        ),
        (
            {
                "count": 2,
                "rows": [
                    _released_v040_recovery_lease(),
                    _released_v040_recovery_lease(
                        worker_id="31234567-89ab-4cde-8fab-0123456789ab",
                        hostname=f"dr-v040-{'3' * 32}",
                    ),
                ],
            },
            "ambiguous",
        ),
        (
            {
                "count": 1,
                "rows": [_released_v040_recovery_lease(queue_name="foreign-queue")],
            },
            "live or foreign",
        ),
        (
            {
                "count": 1,
                "rows": [_released_v040_recovery_lease(capability_schema_version=1)],
            },
            "live or foreign",
        ),
        (
            {
                "count": 1,
                "rows": [_released_v040_recovery_lease(django_ray_version="0.4.0")],
            },
            "live or foreign",
        ),
    ],
)
def test_released_v040_startup_recovery_refuses_live_ambiguous_or_foreign_leases(
    monkeypatch: pytest.MonkeyPatch,
    residue: Mapping[str, Any],
    message: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    cleanup_events: list[str] = []
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **_kwargs: CommandResult("", "", 0),
    )
    monkeypatch.setattr(gate, "_observe_released_v040_reserved_leases", lambda: residue)
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_manager",
        lambda: cleanup_events.append("deployment"),
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_worker_lease",
        lambda: cleanup_events.append("lease"),
    )

    with pytest.raises(ValueError, match=message):
        gate._recover_released_v040_startup_residue()

    assert cleanup_events == []
    assert gate._released_v040_hostname is None
    assert gate._released_v040_worker_id is None


def test_protocol_cohort_observer_reads_real_legacy_and_explicit_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    observed: dict[str, str] = {}
    payload = {
        "report": {
            "capabilities": {
                "groups": [
                    {
                        "kind": "legacy",
                        "minimum": 1,
                        "maximum": 1,
                        "heartbeat_live_leases": 1,
                    },
                    {
                        "kind": "explicit",
                        "minimum": 1,
                        "maximum": 1,
                        "heartbeat_live_leases": 1,
                    },
                ],
                "total_groups": 2,
                "total_leases": 2,
                "omitted_groups": 0,
                "omitted_leases": 0,
            },
        },
        "legacy_worker_count": 1,
        "legacy_worker_ids": ["released-v040-worker"],
    }

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-cohort-observation>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return payload

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._observe_protocol_cohorts() == payload
    assert observed["field_name"] == "protocol cohort observation"
    assert "build_protocol_status" in observed["script"]
    assert "capability_schema_version=0" in observed["script"]
    assert "last_heartbeat_at__gte=cutoff" in observed["script"]


@pytest.mark.parametrize("job_state", [None, "RUNNING", "FAILED"])
def test_protocol_handoff_observer_waits_for_reserved_job_visibility(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], job_state: str | None
) -> None:
    from django_ray.models import RayTaskExecution, TaskAttempt
    from django_ray.runner import ray_job

    row = SimpleNamespace(
        ray_job_id="reserved-job",
        ray_address="http://ray-head:8265",
        state="RUNNING",
        attempt_number=1,
        execution_generation=1,
        claimed_by_worker="released-worker",
        ray_job_request_reference=None,
        completion_data=None,
        result_data=None,
        result_reference=None,
        run_after=None,
    )
    jobs = (
        []
        if job_state is None
        else [
            SimpleNamespace(
                submission_id=row.ray_job_id,
                status=job_state,
                entrypoint="python -m django_ray.runtime.entrypoint --payload-b64 opaque",
            )
        ]
    )
    monkeypatch.setattr(RayTaskExecution.objects, "get", lambda **_kwargs: row)
    monkeypatch.setattr(
        TaskAttempt.objects, "filter", lambda **_kwargs: SimpleNamespace(values=lambda *_args: [])
    )
    monkeypatch.setattr(
        ray_job,
        "_address_pinned_job_client",
        lambda _address: SimpleNamespace(list_jobs=lambda: jobs, get_job_logs=lambda _job_id: ""),
    )
    gate = LocalKubeRayGate(_config())

    def observe(script: str, **_kwargs: object) -> Mapping[str, Any]:
        exec(compile(script, "<handoff-observer>", "exec"), {})
        return json.loads(capsys.readouterr().out)

    monkeypatch.setattr(gate, "_sensitive_django_shell", observe)
    result = gate._observe_protocol_handoff_task(TASK_ID)
    assert result["ready"] is (job_state is not None)
    if job_state is not None:
        assert result["state"] == job_state
        assert result["submission_count"] == 1
        assert result["released_carrier"] is True
    if job_state == "FAILED":
        with pytest.raises(ValueError, match="unexpected state FAILED"):
            gate._wait_for_protocol_handoff_task(TASK_ID, accepted_states=frozenset({"RUNNING"}))


def test_protocol_handoff_wait_rejects_failure_before_job_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(
        gate, "_observe_protocol_handoff_task", lambda _task_id: {"ready": False, "state": "FAILED"}
    )
    with pytest.raises(ValueError, match="handoff task reached unexpected state FAILED"):
        gate._wait_for_protocol_handoff_task(TASK_ID, accepted_states=frozenset({"RUNNING"}))


def test_protocol_handoff_wait_requires_a_replacement_worker_without_resubmission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    job_id = f"raysubmit_django_ray_v1_{'a' * 64}"
    observations = iter(
        (
            {
                "ready": True,
                "state": "RUNNING",
                "durable_state": "RUNNING",
                "attempt_number": 1,
                "execution_generation": 4,
                "worker_id": "released-v040-worker",
                "job_id": job_id,
                "released_carrier": True,
                "request_reference_absent": True,
                "submission_count": 1,
            },
            {
                "ready": True,
                "state": "RUNNING",
                "durable_state": "RUNNING",
                "attempt_number": 1,
                "execution_generation": 4,
                "worker_id": "current-explicit-worker",
                "job_id": job_id,
                "released_carrier": True,
                "request_reference_absent": True,
                "submission_count": 1,
            },
        )
    )
    monkeypatch.setattr(
        gate,
        "_observe_protocol_handoff_task",
        lambda _task_id: next(observations),
    )
    monkeypatch.setattr(gate_module.time, "sleep", lambda _seconds: None)

    result = gate._wait_for_protocol_handoff_task(
        TASK_ID,
        accepted_states=frozenset({"RUNNING"}),
        different_worker="released-v040-worker",
    )

    assert result["worker_id"] == "current-explicit-worker"
    assert result["job_id"] == job_id
    assert result["attempt_number"] == 1
    assert result["execution_generation"] == 4
    assert result["submission_count"] == 1


def _protocol_v1_survival_fixture() -> dict[str, object]:
    return {
        "pk": 51,
        "task_id": PROTOCOL_V1_SURVIVAL_TASK_ID,
        "marker": PROTOCOL_V1_SURVIVAL_MARKER,
        "row_sha256": "c" * 64,
        "state": "QUEUED",
        "execution_protocol_version": 1,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "attempt_number": 1,
        "execution_generation": 0,
        "run_after": "2026-08-14T21:00:00+00:00",
        "claimed_by_worker": None,
        "ray_job_id": None,
        "ray_job_request_reference": None,
    }


def _protocol_v1_survival_observation() -> dict[str, object]:
    fixture = _protocol_v1_survival_fixture()
    return {
        **fixture,
        "callable_path": "testproject.tasks.echo_task",
        "args_json": json.dumps([fixture["marker"]], separators=(",", ":")),
        "kwargs_json": "{}",
        "ray_address": None,
    }


def test_protocol_v1_survival_enqueue_is_deferred_on_the_live_ray_data_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = _protocol_v1_survival_fixture()
    observed: dict[str, str] = {}
    marker_uuid = gate_module.UUID("22222222-2222-4222-8222-222222222222")

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v1-survival-enqueue>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return fixture

    monkeypatch.setattr(gate_module, "uuid4", lambda: marker_uuid)
    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._enqueue_protocol_v1_survival_task() == fixture
    assert gate._protocol_v1_survival_fixture == fixture
    assert observed["field_name"] == "protocol-v1 deferred survival enqueue"
    assert f"marker = {PROTOCOL_V1_SURVIVAL_MARKER!r}" in observed["script"]
    assert f"timedelta(seconds={float(gate.config.task_timeout * 2)!r})" in observed["script"]
    assert f"backend={gate_module.RAY_JOB_GATE_QUEUE!r}" in observed["script"]
    assert f"queue_name={gate_module.RAY_JOB_GATE_QUEUE!r}" in observed["script"]
    assert "run_after=run_after" in observed["script"]
    assert ".enqueue(marker)" in observed["script"]


def test_protocol_v1_survival_requires_the_same_unclaimed_queued_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = _protocol_v1_survival_fixture()
    observation = _protocol_v1_survival_observation()
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v1_survival_task",
        lambda _fixture: observation,
    )

    assert gate._require_protocol_v1_survival_unchanged(fixture) == observation

    observation["claimed_by_worker"] = "released-manager-claimed-the-deferred-row"
    with pytest.raises(ValueError, match="changed during manager handoff"):
        gate._require_protocol_v1_survival_unchanged(fixture)


def test_protocol_v1_survival_release_is_exactly_one_unchanged_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = _protocol_v1_survival_fixture()
    released = {
        "matched": 1,
        "updated": 1,
        "pk": fixture["pk"],
        "task_id": fixture["task_id"],
        "state": "QUEUED",
        "attempt_number": 1,
        "execution_generation": 0,
        "released_at": "2026-08-14T20:30:00+00:00",
        "run_after": "2026-08-14T20:30:00+00:00",
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v1-survival-release>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return released

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._release_protocol_v1_survival_task(fixture) == released
    assert observed["field_name"] == "protocol-v1 deferred survival release"
    assert f"pk={fixture['pk']!r}" in observed["script"]
    assert f"task_id={fixture['task_id']!r}" in observed["script"]
    assert "state=TaskState.QUEUED" in observed["script"]
    assert "execution_protocol_version=1" in observed["script"]
    assert f"queue_name={gate_module.RAY_JOB_GATE_QUEUE!r}" in observed["script"]
    assert "claimed_by_worker__isnull=True" in observed["script"]
    assert "updated = queryset.update(run_after=released_at)" in observed["script"]


@pytest.mark.parametrize(
    ("cleanup", "expected_deleted"),
    [
        (
            {
                "matched": 1,
                "states": ["QUEUED"],
                "deleted": 1,
                "queued_absent": True,
            },
            1,
        ),
        (
            {
                "matched": 1,
                "states": ["RUNNING"],
                "deleted": 0,
                "queued_absent": True,
            },
            0,
        ),
    ],
)
def test_protocol_v1_survival_failure_cleanup_deletes_only_an_unclaimed_queued_row(
    monkeypatch: pytest.MonkeyPatch,
    cleanup: Mapping[str, Any],
    expected_deleted: int,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = _protocol_v1_survival_fixture()
    gate._protocol_v1_survival_fixture = fixture
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v1-survival-cleanup>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return cleanup

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._cleanup_protocol_v1_survival_task(fixture) == cleanup
    assert gate._protocol_v1_survival_fixture is None
    assert observed["field_name"] == "protocol-v1 deferred survival cleanup"
    assert f"pk={fixture['pk']!r}" in observed["script"]
    assert f"task_id={fixture['task_id']!r}" in observed["script"]
    assert "execution_protocol_version=1" in observed["script"]
    assert f"queue_name={gate_module.RAY_JOB_GATE_QUEUE!r}" in observed["script"]
    assert 'callable_path="testproject.tasks.echo_task"' in observed["script"]
    assert "claimed_by_worker__isnull=True" in observed["script"]
    assert "ray_job_request_reference__isnull=True" in observed["script"]
    assert cleanup["deleted"] == expected_deleted
    if expected_deleted == 1:
        assert cleanup["states"] == ["QUEUED"]
    else:
        assert cleanup["states"] == ["RUNNING"]


def test_protocol_v2_fixture_is_seeded_only_on_the_live_ray_data_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate._protocol_handoff_initial_revision = 7
    observed: dict[str, str] = {}
    poison_uuid = gate_module.UUID("01234567-89ab-4cde-8fab-0123456789ab")
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "poison": f"protocol_v2_application_poison_{poison_uuid.hex}",
        "row_sha256": "d" * 64,
        "created_at": "2026-08-14T20:00:00+00:00",
        "queued_at": "2026-08-14T20:00:01+00:00",
        "initial_revision": 7,
        "closed_revision": 8,
    }
    generated_ids = iter((gate_module.UUID(TASK_ID), poison_uuid))

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-fixture>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return fixture

    monkeypatch.setattr(gate_module, "uuid4", lambda: next(generated_ids))
    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._seed_protocol_v2_fixture() == {**fixture, "seed_confirmed": True}
    assert observed["field_name"] == "protocol-v2 queued fixture creation"
    assert f"queue_name={gate_module.RAY_JOB_GATE_QUEUE!r}" in observed["script"]
    assert f"task_id = {TASK_ID!r}" in observed["script"]
    assert f"poison = {fixture['poison']!r}" in observed["script"]
    assert "execution_protocol_version=2" in observed["script"]
    assert "state=TaskState.QUEUED" in observed["script"]
    assert "close_legacy_worker_admission" in observed["script"]
    created_terminal = observed["script"].index("state=TaskState.FAILED")
    admission_closed = observed["script"].index("transition = close_legacy_worker_admission")
    update_started = observed["script"].index("updated = RayTaskExecution.objects.filter(")
    queued = observed["script"].index(
        ".update(state=TaskState.QUEUED)",
        update_started,
    )
    update_verified = observed["script"].index("if updated != 1:", queued)
    queued_at_captured = observed["script"].index(
        "queued_at = timezone.now()",
        update_verified,
    )
    row_refreshed = observed["script"].index("row.refresh_from_db()", queued_at_captured)
    emitted_hash = observed["script"].index(
        '"row_sha256": row_sha256(row)',
        row_refreshed,
    )
    emitted_queued_at = observed["script"].index(
        '"queued_at": queued_at.isoformat()',
        emitted_hash,
    )
    assert (
        created_terminal
        < admission_closed
        < update_started
        < queued
        < update_verified
        < queued_at_captured
        < row_refreshed
        < emitted_hash
        < emitted_queued_at
    )


def test_protocol_v2_startup_recovery_terminalizes_before_reopen_and_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    recovered = {
        "recovered": True,
        "matched": 1,
        "terminalized": 1,
        "deleted": 1,
        "row_absent": True,
        "schema_version": 1,
        "active_write_protocol_version": 1,
        "legacy_worker_admission_enabled": True,
        "revision": 9,
        "token_count": 1,
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-startup-recovery>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return recovered

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._recover_protocol_v2_startup_fixture() == recovered
    assert observed["field_name"] == "protocol-v2 startup fixture recovery"
    assert (
        'callable_path__startswith="testproject.tasks.protocol_v2_application_poison_"'
        in observed["script"]
    )
    assert "rows = list(reserved[:2])" in observed["script"]
    terminalized = observed["script"].index(".update(state=TaskState.FAILED)")
    admission_reopened = observed["script"].index("transition = reopen_legacy_worker_admission")
    deleted = observed["script"].index("deleted, _ = exact_terminal.delete()")
    assert terminalized < admission_reopened < deleted


def test_protocol_v2_startup_recovery_accepts_a_terminal_staged_hard_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    recovered = {
        "recovered": True,
        "matched": 1,
        "terminalized": 0,
        "deleted": 1,
        "row_absent": True,
        "schema_version": 1,
        "active_write_protocol_version": 1,
        "legacy_worker_admission_enabled": True,
        "revision": 7,
        "token_count": 1,
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-terminal-stage-recovery>", "exec")
        observed["script"] = script
        return recovered

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._recover_protocol_v2_startup_fixture() == recovered
    assert "row.state in [TaskState.FAILED, TaskState.QUEUED]" in observed["script"]
    assert (
        "if policy.legacy_worker_admission_enabled and row.state != TaskState.FAILED"
        in observed["script"]
    )
    assert "terminalized = 0" in observed["script"]
    assert "deleted, _ = exact_terminal.delete()" in observed["script"]


def test_protocol_handoff_recovery_orders_both_exact_cleanups_before_manager_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    events: list[str] = []
    managers = iter(
        (
            {"replicas": 0, "ready_replicas": 0},
            {"replicas": 1, "ready_replicas": 1},
        )
    )
    monkeypatch.setattr(
        gate,
        "_recover_released_v040_startup_residue",
        lambda: events.append("recover-released-v040"),
    )
    monkeypatch.setattr(
        gate,
        "_recover_protocol_v2_startup_fixture",
        lambda: events.append("recover-protocol-v2"),
    )

    def observe_manager() -> Mapping[str, int]:
        observation = next(managers)
        events.append(f"manager:{observation['replicas']}/{observation['ready_replicas']}")
        return observation

    monkeypatch.setattr(gate, "_ray_job_manager_replica_observation", observe_manager)
    monkeypatch.setattr(
        gate,
        "_scale_ray_job_manager",
        lambda replicas: events.append(f"scale-manager:{replicas}"),
    )
    monkeypatch.setattr(
        gate,
        "_wait_for_application_topology",
        lambda: events.append("app-ready"),
    )

    gate._recover_protocol_handoff_residue()

    assert events == [
        "recover-released-v040",
        "recover-protocol-v2",
        "manager:0/0",
        "scale-manager:1",
        "app-ready",
        "manager:1/1",
    ]


def test_protocol_v2_survival_rejects_any_queue_or_claim_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {"pk": 41, "task_id": TASK_ID, "row_sha256": "d" * 64}
    observation = {
        "row_sha256": fixture["row_sha256"],
        "state": "QUEUED",
        "execution_protocol_version": 2,
        "queue_name": "protocol-v2-gate",
        "claimed_by_worker": None,
        "ray_job_id": None,
        "ray_address": None,
        "ray_data_explicit_lease_count": 1,
        "post_queue_heartbeat": True,
    }
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v2_fixture",
        lambda _fixture: observation,
    )

    with pytest.raises(ValueError, match="queued row changed"):
        gate._wait_for_protocol_v2_survival(fixture)

    observation["queue_name"] = gate_module.RAY_JOB_GATE_QUEUE
    observation["claimed_by_worker"] = "unexpected-v1-manager"
    with pytest.raises(ValueError, match="queued row changed"):
        gate._wait_for_protocol_v2_survival(fixture)


def test_protocol_v2_survival_waits_for_a_strictly_post_queue_manager_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {"pk": 41, "task_id": TASK_ID, "row_sha256": "d" * 64}
    base = {
        "row_sha256": fixture["row_sha256"],
        "state": "QUEUED",
        "execution_protocol_version": 2,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "claimed_by_worker": None,
        "ray_job_id": None,
        "ray_address": None,
        "ray_data_explicit_lease_count": 1,
    }
    observations = iter(
        (
            {**base, "post_queue_heartbeat": False},
            {**base, "post_queue_heartbeat": True},
        )
    )
    sleeps: list[float] = []
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v2_fixture",
        lambda _fixture: next(observations),
    )
    monkeypatch.setattr(gate_module.time, "sleep", sleeps.append)

    result = gate._wait_for_protocol_v2_survival(fixture)

    assert result == {**base, "post_queue_heartbeat": True}
    assert sleeps == [2]


def test_protocol_v2_visibility_requires_unchanged_status_api_report_and_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "row_sha256": "d" * 64,
    }
    row = {
        "row_sha256": fixture["row_sha256"],
        "state": "QUEUED",
        "execution_protocol_version": 2,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "claimed_by_worker": None,
        "ray_job_id": None,
        "ray_address": None,
        "ray_data_explicit_lease_count": 1,
        "post_queue_heartbeat": True,
    }
    task_status = _task_status_payload(state="QUEUED", task_id=TASK_ID)
    task_status.update(
        {
            "execution_protocol_version": 2,
            "managed_with_django_ray_version": None,
            "executor_django_ray_version": None,
            "protocol_compatible_worker_available": False,
        }
    )
    paths: list[str] = []

    def http(path: str, **_kwargs: object) -> tuple[int, bytes]:
        paths.append(path)
        if path.startswith("/api/tasks/"):
            return 200, json.dumps(task_status).encode()
        if path.startswith("/api/executions?"):
            return 200, json.dumps({"tasks": [task_status]}).encode()
        raise AssertionError(f"unexpected protocol-v2 HTTP path: {path}")

    report = {
        "policy": {
            "legacy_worker_admission_enabled": False,
            "legacy_admission_token_present": False,
            "active_write_protocol_version": 1,
        },
        "non_v1_nonterminal_count": 1,
        "unsupported_work": {
            "groups": [
                {
                    "count": 1,
                    "execution_protocol_version": 2,
                    "queue": gate_module.RAY_JOB_GATE_QUEUE,
                    "state": "QUEUED",
                }
            ],
            "total_tasks": 1,
            "omitted_tasks": 0,
        },
    }
    monkeypatch.setattr(gate, "_wait_for_protocol_v2_survival", lambda _fixture: row)
    monkeypatch.setattr(gate, "_secret_token", lambda: "local-token")
    monkeypatch.setattr(gate, "_http", http)
    monkeypatch.setattr(gate, "_observe_protocol_cohorts", lambda: {"report": report})
    monkeypatch.setattr(
        gate,
        "_protocol_metric_counts",
        lambda: {("other", "QUEUED"): 1},
    )
    monkeypatch.setattr(gate, "_observe_protocol_v2_fixture", lambda _fixture: row)

    gate._verify_protocol_v2_visibility(
        fixture,
        baseline_metrics={("other", "QUEUED"): 0},
    )

    assert paths == [
        f"/api/tasks/{TASK_ID}",
        f"/api/executions?task_id={TASK_ID}&limit=1",
    ]
    assert gate.evidence.protocol_v2_queued_unchanged is True
    assert gate.evidence.protocol_v2_unsupported_visible is True


def test_protocol_v2_direct_probe_rejects_before_input_or_application_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "poison": PROTOCOL_V2_POISON,
        "row_sha256": "d" * 64,
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-direct-rejection>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return _protocol_v2_rejection_payload()

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v2_fixture",
        lambda _fixture: {"row_sha256": fixture["row_sha256"]},
    )

    assert gate._verify_protocol_v2_rejection(fixture) == _protocol_v2_rejection_payload()
    assert observed["field_name"] == "protocol-v2 direct Ray Core rejection"
    assert "input_reference=None" in observed["script"]
    assert 'request["execution_protocol_version"] = 2' in observed["script"]
    assert "expected_execution_protocol_version=2" in observed["script"]
    assert "ExecutionProtocolRange(minimum=1, maximum=2)" in observed["script"]
    assert "application_marker_present" in observed["script"]
    assert "from testproject" not in observed["script"]
    assert gate.evidence.protocol_v2_preinvocation_rejected is True
    assert gate.evidence.protocol_v2_application_marker_absent is True


def test_protocol_v2_direct_probe_rejects_any_durable_row_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "poison": PROTOCOL_V2_POISON,
        "row_sha256": "d" * 64,
    }
    monkeypatch.setattr(
        gate,
        "_sensitive_django_shell",
        lambda _script, *, field_name, ray_authorized: _protocol_v2_rejection_payload(),
    )
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v2_fixture",
        lambda _fixture: {"row_sha256": "e" * 64},
    )

    with pytest.raises(ValueError, match="changed during direct rejection"):
        gate._verify_protocol_v2_rejection(fixture)

    assert gate.evidence.protocol_v2_preinvocation_rejected is False
    assert gate.evidence.protocol_v2_application_marker_absent is False


def test_protocol_v2_private_target_probe_proves_exact_and_mismatch_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "poison": PROTOCOL_V2_POISON,
        "row_sha256": "d" * 64,
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-private-target-execution>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return _protocol_v2_target_execution_payload()

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v2_fixture",
        lambda _fixture: {"row_sha256": fixture["row_sha256"]},
    )

    assert (
        gate._verify_protocol_v2_target_execution(fixture)
        == _protocol_v2_target_execution_payload()
    )
    assert observed["field_name"] == "protocol-v2 private target execution"
    assert "runner._submit_target_execution(" in observed["script"]
    assert "runner._poll_target_execution_results(" in observed["script"]
    assert "probe_ray_target(" in observed["script"]
    assert "ray_minor=60," in observed["script"]
    assert "build_ray_cluster_attestation(" in observed["script"]
    assert "RayTaskTargetExecutionEvidenceClaim(" in observed["script"]
    assert "ray_task_target_execution_evidence_digest(" in observed["script"]
    assert "target_execution_evidence_claim=exact_evidence_claim" in observed["script"]
    assert "claim_attestation_recorded_at=claim_attestation.observed_at" in observed["script"]
    assert "target_execution_evidence_digest=" not in observed["script"]
    assert "target_execution_claimed_at=" not in observed["script"]
    assert "manager_python_patch=expected.runtime.python_patch" in observed["script"]
    assert "route_selection_id=41" in observed["script"]
    assert 'execution.state = "RUNNING"' in observed["script"]
    assert (
        'execution.claimed_by_worker = "local-kuberay-private-p2-core-lease"' in observed["script"]
    )
    assert "execution.started_at = exact_claimed_at" in observed["script"]
    assert "execution.finished_at = None" in observed["script"]
    assert "python_patch=runtime.python_patch + 1" in observed["script"]
    assert 'execution.callable_path = "testproject.tasks.echo_task"' in observed["script"]
    assert "RayCoreTargetExecutionTransportState.COMPATIBILITY_REJECTION" in observed["script"]
    assert "mismatch_marker_present" in observed["script"]
    assert "from testproject" not in observed["script"]
    assert gate.evidence.protocol_v2_target_exact_completed is True
    assert gate.evidence.protocol_v2_target_mismatch_rejected is True
    assert gate.evidence.protocol_v2_target_mismatch_marker_absent is True


def test_protocol_v2_private_target_probe_rejects_any_durable_row_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "poison": PROTOCOL_V2_POISON,
        "row_sha256": "d" * 64,
    }
    monkeypatch.setattr(
        gate,
        "_sensitive_django_shell",
        lambda _script, *, field_name, ray_authorized: _protocol_v2_target_execution_payload(),
    )
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v2_fixture",
        lambda _fixture: {"row_sha256": "e" * 64},
    )

    with pytest.raises(ValueError, match="changed during private target execution"):
        gate._verify_protocol_v2_target_execution(fixture)

    assert gate.evidence.protocol_v2_target_exact_completed is False
    assert gate.evidence.protocol_v2_target_mismatch_rejected is False
    assert gate.evidence.protocol_v2_target_mismatch_marker_absent is False


def test_protocol_v2_cleanup_deletes_only_the_fixture_and_reopens_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "poison": PROTOCOL_V2_POISON,
        "initial_revision": 7,
        "closed_revision": 8,
        "seed_confirmed": True,
    }
    cleanup = {
        "matched": 1,
        "terminalized": 1,
        "deleted": 1,
        "protocols": [2],
        "states": ["QUEUED"],
        "row_absent": True,
        "schema_version": 1,
        "active_write_protocol_version": 1,
        "legacy_worker_admission_enabled": True,
        "revision": 9,
        "token_count": 1,
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-cleanup>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return cleanup

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._cleanup_protocol_v2_fixture(fixture) == cleanup
    assert observed["field_name"] == "protocol-v2 fixture and policy cleanup"
    assert f"expected_pk = {fixture['pk']!r}" in observed["script"]
    assert "queryset = queryset.filter(pk=expected_pk)" in observed["script"]
    assert f"task_id={fixture['task_id']!r}" in observed["script"]
    assert "execution_protocol_version" in observed["script"]
    assert "reopen_legacy_worker_admission" in observed["script"]
    terminalized = observed["script"].index(
        "terminalized = queryset.filter(state=TaskState.QUEUED).update(state=TaskState.FAILED)"
    )
    admission_reopened = observed["script"].index("transition = reopen_legacy_worker_admission")
    deleted = observed["script"].index("deleted, _ = terminal_queryset.delete()")
    assert terminalized < admission_reopened < deleted


def test_protocol_v2_cleanup_rejects_inexact_deletion_or_policy_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "poison": PROTOCOL_V2_POISON,
        "initial_revision": 7,
        "closed_revision": 8,
        "seed_confirmed": True,
    }
    monkeypatch.setattr(
        gate,
        "_sensitive_django_shell",
        lambda _script, *, field_name: {
            "matched": 0,
            "deleted": 0,
            "protocols": [],
            "row_absent": False,
            "schema_version": 1,
            "active_write_protocol_version": 1,
            "legacy_worker_admission_enabled": False,
            "revision": 8,
            "token_count": 0,
        },
    )

    with pytest.raises(ValueError, match="not restored exactly"):
        gate._cleanup_protocol_v2_fixture(fixture)


def test_protocol_v2_uncertain_cleanup_cannot_delete_a_task_id_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": None,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "poison": PROTOCOL_V2_POISON,
        "initial_revision": 7,
        "closed_revision": 8,
        "seed_confirmed": False,
    }
    cleanup = {
        "matched": 0,
        "terminalized": 0,
        "deleted": 0,
        "protocols": [],
        "states": [],
        "row_absent": True,
        "schema_version": 1,
        "active_write_protocol_version": 1,
        "legacy_worker_admission_enabled": True,
        "revision": 9,
        "token_count": 1,
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-collision-cleanup>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return cleanup

    gate._protocol_v2_fixture = fixture
    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._cleanup_protocol_v2_fixture(fixture) == cleanup
    assert gate._protocol_v2_fixture is None
    assert observed["field_name"] == "protocol-v2 fixture and policy cleanup"
    assert f"task_id={TASK_ID!r}" in observed["script"]
    assert "execution_protocol_version=2" in observed["script"]
    assert f"queue_name={gate_module.RAY_JOB_GATE_QUEUE!r}" in observed["script"]
    assert 'callable_path="testproject.tasks." + poison' in observed["script"]
    assert 'args_json=json.dumps([poison], separators=(",", ":"))' in observed["script"]
    assert 'kwargs_json="{}"' in observed["script"]
    assert '"row_absent": not queryset.exists()' in observed["script"]


def _protocol_policy_observation(revision: int) -> dict[str, object]:
    return {
        "report": {
            "policy": {
                "schema_version": 1,
                "active_write_protocol_version": 1,
                "legacy_worker_admission_enabled": True,
                "legacy_admission_token_present": True,
                "revision": revision,
            }
        },
        "legacy_worker_count": 0,
        "legacy_worker_ids": [],
    }


def _protocol_handoff_observation(
    *,
    worker_id: str,
    job_id: str,
    state: str = "FAILED",
    durable_state: str = "FAILED",
) -> dict[str, object]:
    return {
        "ready": True,
        "state": state,
        "durable_state": durable_state,
        "attempt_number": 1,
        "execution_generation": 4,
        "worker_id": worker_id,
        "job_id": job_id,
        "released_carrier": True,
        "request_reference_absent": True,
        "submission_count": 1,
        "driver_exit_code": 78,
        "fixed_refusal": True,
        "callable_marker_bound": True,
        "marker_absent": True,
        "completion_is_none": True,
        "result_is_none": True,
        "run_after_is_none": True,
        "single_failed_attempt": True,
    }


def _protocol_v1_survival_terminal(job_id: str) -> dict[str, object]:
    return {
        "ready": True,
        "state": "SUCCEEDED",
        "durable_state": "SUCCEEDED",
        "attempt_number": 1,
        "execution_generation": 1,
        "worker_id": "current-explicit-worker",
        "job_id": job_id,
        "carrier_ok": True,
        "binding_ok": True,
        "request_ok": True,
        "info_clear": True,
        "logs_clear": True,
        "submission_count": 1,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "SUCCEEDED"),
        ("durable_state", "QUEUED"),
        ("worker_id", "other-worker"),
        ("job_id", "other-job"),
        ("execution_generation", -1),
        ("attempt_number", 2),
        ("attempt_number", True),
        ("submission_count", 2),
        ("driver_exit_code", 1),
        ("released_carrier", False),
        ("request_reference_absent", False),
        ("fixed_refusal", False),
        ("callable_marker_bound", False),
        ("marker_absent", False),
        ("completion_is_none", False),
        ("result_is_none", False),
        ("run_after_is_none", False),
        ("single_failed_attempt", False),
    ],
)
def test_legacy_refusal_rejects_wrong_failure_invocation_and_retry(
    field: str, value: object
) -> None:
    gate = LocalKubeRayGate(_config())
    gate._released_v040_worker_id = PRIOR_RELEASED_V040_WORKER_ID
    observation = _protocol_handoff_observation(
        worker_id=PRIOR_RELEASED_V040_WORKER_ID,
        job_id=f"raysubmit_django_ray_v1_{'a' * 64}",
    )
    gate._require_released_v040_refusal(observation)
    observation[field] = value
    with pytest.raises(ValueError, match="exact one-attempt refusal"):
        gate._require_released_v040_refusal(observation)


@pytest.mark.parametrize("field", ["job_id", "execution_generation"])
def test_legacy_refusal_must_retain_identity_after_current_manager_restores(field: str) -> None:
    gate = LocalKubeRayGate(_config())
    gate._released_v040_worker_id = PRIOR_RELEASED_V040_WORKER_ID
    prior = _protocol_handoff_observation(
        worker_id=PRIOR_RELEASED_V040_WORKER_ID,
        job_id=f"raysubmit_django_ray_v1_{'a' * 64}",
    )
    later = dict(prior)
    later[field] = f"raysubmit_django_ray_v1_{'b' * 64}" if field == "job_id" else 5
    with pytest.raises(ValueError, match="exact one-attempt refusal"):
        gate._require_released_v040_refusal(later, prior=prior)


@pytest.mark.parametrize(
    "missing_fact", ["ray_job_manager_reconciled_same_job", "ray_job_manager_no_resubmit"]
)
def test_protocol_certification_requires_independent_current_rq2_recovery(
    monkeypatch: pytest.MonkeyPatch, missing_fact: str
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.evidence.ray_job_manager_reconciled_same_job = True
    gate.evidence.ray_job_manager_no_resubmit = True
    setattr(gate.evidence, missing_fact, False)
    monkeypatch.setattr(
        gate,
        "_recover_protocol_handoff_residue",
        lambda: pytest.fail("missing recovery proof must fail before fixture mutations"),
    )
    with pytest.raises(ValueError, match="current rq2 same-job recovery evidence is incomplete"):
        gate._verify_protocol_handoff_certification()


@pytest.mark.parametrize("mutation", [None, "payload", "row", "logs", "attempt"])
def test_legacy_refusal_inspector_checks_submitted_callable_marker_and_archive(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutation: str | None,
) -> None:
    from django_ray.models import RayTaskExecution, TaskAttempt
    from django_ray.runner import ray_job

    marker = "private-callable-marker"
    row = SimpleNamespace(
        pk=41,
        task_id=TASK_ID,
        callable_path="builtins.print",
        args_json=json.dumps([marker]),
        kwargs_json="{}",
        ray_job_id=f"raysubmit_django_ray_v1_{'a' * 64}",
        ray_address="http://ray-head:8265",
        state="FAILED",
        attempt_number=1,
        execution_generation=4,
        claimed_by_worker=PRIOR_RELEASED_V040_WORKER_ID,
        ray_job_request_reference=None,
        completion_data=None,
        result_data=None,
        result_reference=None,
        run_after=None,
    )
    payload = {
        "callable_path": row.callable_path,
        "task_execution_pk": row.pk,
        "task_id": row.task_id,
        "attempt_number": row.attempt_number,
        "execution_generation": row.execution_generation,
        "serialized_args": row.args_json,
        "serialized_kwargs": row.kwargs_json,
    }
    if mutation == "payload":
        payload["callable_path"] = "testproject.tasks.echo_task"
    if mutation == "row":
        row.args_json = '["different-marker"]'
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    info = SimpleNamespace(
        submission_id=row.ray_job_id,
        status="FAILED",
        entrypoint=f"python -m django_ray.runtime.entrypoint --payload-b64 {encoded}",
        driver_exit_code=78,
    )
    logs = "django-ray task failed: execution request rejected: legacy_request\n"
    if mutation == "logs":
        logs += marker + "\n"
    attempts = [
        {"attempt_number": 1, "state": "FAILED", "result_data": None, "result_reference": None}
    ]
    if mutation == "attempt":
        attempts.append(dict(attempts[0], attempt_number=2))
    monkeypatch.setattr(RayTaskExecution.objects, "get", lambda **_kwargs: row)
    monkeypatch.setattr(
        TaskAttempt.objects,
        "filter",
        lambda **_kwargs: SimpleNamespace(values=lambda *_args: attempts),
    )
    monkeypatch.setattr(
        ray_job,
        "_address_pinned_job_client",
        lambda _address: SimpleNamespace(
            list_jobs=lambda: [info], get_job_logs=lambda _job_id: logs
        ),
    )
    gate = LocalKubeRayGate(_config())
    gate._released_v040_worker_id = PRIOR_RELEASED_V040_WORKER_ID
    gate._released_v040_refusal_marker = marker

    def observe(script: str, **_kwargs: object) -> Mapping[str, Any]:
        exec(compile(script, "<legacy-refusal-observer>", "exec"), {})
        output = capsys.readouterr().out
        assert marker not in output
        assert encoded not in output
        return json.loads(output)

    monkeypatch.setattr(gate, "_sensitive_django_shell", observe)
    observation = gate._observe_protocol_handoff_task(TASK_ID)
    if mutation is None:
        gate._require_released_v040_refusal(observation)
    else:
        with pytest.raises(ValueError, match="exact one-attempt refusal"):
            gate._require_released_v040_refusal(observation)


def test_legacy_refusal_enqueue_binds_marker_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    scripts: list[str] = []

    def capture(script: str, **_kwargs: object) -> Mapping[str, Any]:
        compile(script, "<legacy-refusal-enqueue>", "exec")
        scripts.append(script)
        return {"task_id": TASK_ID}

    monkeypatch.setattr(gate, "_sensitive_django_shell", capture)
    assert gate._enqueue_released_v040_handoff_task() == TASK_ID
    assert len(scripts) == 1
    assert "with transaction.atomic():" in scripts[0]
    assert '    row.callable_path = "builtins.print"' in scripts[0]
    assert '    row.save(update_fields=["callable_path"])' in scripts[0]
    assert gate._released_v040_refusal_marker is not None
    assert gate._released_v040_refusal_marker in scripts[0]
    assert gate._released_v040_refusal_marker not in gate.redactor.clean(scripts[0])


def test_protocol_handoff_certification_preserves_one_job_and_restores_every_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.evidence.ray_job_manager_reconciled_same_job = True
    gate.evidence.ray_job_manager_no_resubmit = True
    released_worker = "11111111-1111-4111-8111-111111111111"
    job_id = f"raysubmit_django_ray_v1_{'a' * 64}"
    survival_job_id = f"raysubmit_django_ray_rq2_{'b' * 64}"
    survival_fixture = _protocol_v1_survival_fixture()
    survival_terminal = _protocol_v1_survival_terminal(survival_job_id)
    survival_row = {
        "pk": survival_fixture["pk"],
        "task_id": survival_fixture["task_id"],
        "state": "SUCCEEDED",
        "attempt_number": 1,
        "execution_generation": survival_terminal["execution_generation"],
        "ray_job_id": survival_job_id,
        "ray_job_request_reference": "rq2-reference",
    }
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "row_sha256": "d" * 64,
        "initial_revision": 7,
    }
    manager_observations = iter(
        (
            {"replicas": 1, "ready_replicas": 1},
            {"replicas": 1, "ready_replicas": 1},
        )
    )
    policy_observations = iter((_protocol_policy_observation(7), _protocol_policy_observation(9)))
    handoff_observations = iter(
        (
            _protocol_handoff_observation(worker_id=released_worker, job_id=job_id),
            _protocol_handoff_observation(worker_id=released_worker, job_id=job_id),
        )
    )
    events: list[str] = []

    monkeypatch.setattr(
        gate,
        "_recover_released_v040_startup_residue",
        lambda: events.append("recover-released-startup"),
    )
    monkeypatch.setattr(
        gate,
        "_recover_protocol_v2_startup_fixture",
        lambda: events.append("recover-protocol-v2-startup"),
    )

    monkeypatch.setattr(
        gate,
        "_ray_job_manager_replica_observation",
        lambda: next(manager_observations),
    )

    def observe_protocol_cohorts() -> Mapping[str, Any]:
        events.append("observe-policy")
        return next(policy_observations)

    monkeypatch.setattr(gate, "_observe_protocol_cohorts", observe_protocol_cohorts)
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_manager",
        lambda: events.append("delete-released-manager"),
    )
    monkeypatch.setattr(
        gate,
        "_apply_released_v040_manager",
        lambda: events.append("apply-released-manager"),
    )

    def wait_for_cohorts() -> Mapping[str, Any]:
        events.append("cohorts-visible")
        gate._released_v040_worker_id = released_worker
        gate.evidence.protocol_legacy_cohort_visible = True
        gate.evidence.protocol_explicit_cohort_visible = True
        return {}

    monkeypatch.setattr(gate, "_wait_for_protocol_cohorts", wait_for_cohorts)
    monkeypatch.setattr(
        gate,
        "_scale_ray_job_manager",
        lambda replicas: events.append(f"scale-current:{replicas}"),
    )
    monkeypatch.setattr(gate, "_enqueue_released_v040_handoff_task", lambda: TASK_ID)
    monkeypatch.setattr(
        gate,
        "_wait_for_protocol_handoff_task",
        lambda *_args, **_kwargs: next(handoff_observations),
    )
    monkeypatch.setattr(
        gate, "_observe_protocol_handoff_task", lambda _task_id: next(handoff_observations)
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_worker_lease",
        lambda: events.append("delete-released-lease"),
    )

    def enqueue_survival() -> Mapping[str, Any]:
        events.append("enqueue-protocol-v1-survival")
        gate._protocol_v1_survival_fixture = survival_fixture
        return survival_fixture

    monkeypatch.setattr(gate, "_enqueue_protocol_v1_survival_task", enqueue_survival)
    monkeypatch.setattr(
        gate,
        "_require_protocol_v1_survival_unchanged",
        lambda _fixture: events.append("require-protocol-v1-survival") or {},
    )
    monkeypatch.setattr(
        gate,
        "_release_protocol_v1_survival_task",
        lambda _fixture: events.append("release-protocol-v1-survival") or {},
    )
    monkeypatch.setattr(
        gate,
        "_wait_for_ray_job_gate_task",
        lambda *_args, **_kwargs: events.append("wait-protocol-v1-terminal") or survival_terminal,
    )
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v1_survival_task",
        lambda _fixture: events.append("observe-protocol-v1-terminal") or survival_row,
    )
    monkeypatch.setattr(
        gate,
        "_protocol_metric_counts",
        lambda: {("other", "QUEUED"): 0},
    )

    def seed_fixture() -> Mapping[str, Any]:
        events.append("seed-protocol-v2")
        gate._protocol_v2_fixture = fixture
        return fixture

    monkeypatch.setattr(gate, "_seed_protocol_v2_fixture", seed_fixture)

    def verify_visibility(
        _fixture: Mapping[str, Any], *, baseline_metrics: Mapping[tuple[str, str], int]
    ) -> None:
        assert baseline_metrics == {("other", "QUEUED"): 0}
        events.append("verify-protocol-v2-visibility")
        gate.evidence.protocol_v2_queued_unchanged = True
        gate.evidence.protocol_v2_unsupported_visible = True

    monkeypatch.setattr(gate, "_verify_protocol_v2_visibility", verify_visibility)

    def verify_rejection(_fixture: Mapping[str, Any]) -> Mapping[str, Any]:
        events.append("verify-protocol-v2-rejection")
        gate.evidence.protocol_v2_preinvocation_rejected = True
        gate.evidence.protocol_v2_application_marker_absent = True
        return _protocol_v2_rejection_payload()

    monkeypatch.setattr(gate, "_verify_protocol_v2_rejection", verify_rejection)

    def verify_target_execution(_fixture: Mapping[str, Any]) -> Mapping[str, Any]:
        events.append("verify-protocol-v2-target-execution")
        gate.evidence.protocol_v2_target_exact_completed = True
        gate.evidence.protocol_v2_target_mismatch_rejected = True
        gate.evidence.protocol_v2_target_mismatch_marker_absent = True
        return _protocol_v2_target_execution_payload()

    monkeypatch.setattr(
        gate,
        "_verify_protocol_v2_target_execution",
        verify_target_execution,
    )

    def cleanup_fixture(_fixture: Mapping[str, Any]) -> Mapping[str, Any]:
        events.append("cleanup-protocol-v2")
        gate._protocol_v2_fixture = None
        return {"revision": 9}

    monkeypatch.setattr(gate, "_cleanup_protocol_v2_fixture", cleanup_fixture)
    monkeypatch.setattr(
        gate,
        "_wait_for_application_topology",
        lambda: events.append("topology-restored"),
    )

    gate._verify_protocol_handoff_certification()

    assert gate.evidence.protocol_legacy_cohort_visible is True
    assert gate.evidence.protocol_explicit_cohort_visible is True
    assert gate.evidence.protocol_legacy_inline_refused is True
    assert gate.evidence.protocol_legacy_inline_no_marker is True
    assert gate.evidence.protocol_legacy_inline_single_attempt is True
    assert gate.evidence.protocol_v1_queued_survived_handoff is True
    assert gate.evidence.protocol_v2_queued_unchanged is True
    assert gate.evidence.protocol_v2_unsupported_visible is True
    assert gate.evidence.protocol_v2_preinvocation_rejected is True
    assert gate.evidence.protocol_v2_application_marker_absent is True
    assert gate.evidence.protocol_v2_target_exact_completed is True
    assert gate.evidence.protocol_v2_target_mismatch_rejected is True
    assert gate.evidence.protocol_v2_target_mismatch_marker_absent is True
    assert gate.evidence.protocol_handoff_cleanup_restored is True
    assert events == [
        "recover-released-startup",
        "recover-protocol-v2-startup",
        "observe-policy",
        "apply-released-manager",
        "cohorts-visible",
        "scale-current:0",
        "enqueue-protocol-v1-survival",
        "require-protocol-v1-survival",
        "delete-released-manager",
        "delete-released-lease",
        "scale-current:1",
        "require-protocol-v1-survival",
        "release-protocol-v1-survival",
        "wait-protocol-v1-terminal",
        "observe-protocol-v1-terminal",
        "seed-protocol-v2",
        "verify-protocol-v2-visibility",
        "verify-protocol-v2-rejection",
        "verify-protocol-v2-target-execution",
        "delete-released-manager",
        "scale-current:1",
        "cleanup-protocol-v2",
        "topology-restored",
        "observe-policy",
    ]


def test_protocol_handoff_failure_still_removes_fixture_and_restores_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.evidence.ray_job_manager_reconciled_same_job = True
    gate.evidence.ray_job_manager_no_resubmit = True
    released_worker = "11111111-1111-4111-8111-111111111111"
    job_id = f"raysubmit_django_ray_v1_{'a' * 64}"
    survival_job_id = f"raysubmit_django_ray_rq2_{'b' * 64}"
    survival_fixture = _protocol_v1_survival_fixture()
    survival_terminal = _protocol_v1_survival_terminal(survival_job_id)
    survival_row = {
        "pk": survival_fixture["pk"],
        "task_id": survival_fixture["task_id"],
        "state": "SUCCEEDED",
        "attempt_number": 1,
        "execution_generation": survival_terminal["execution_generation"],
        "ray_job_id": survival_job_id,
        "ray_job_request_reference": "rq2-reference",
    }
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "row_sha256": "d" * 64,
        "initial_revision": 7,
    }
    managers = iter(
        (
            {"replicas": 1, "ready_replicas": 1},
            {"replicas": 1, "ready_replicas": 1},
        )
    )
    policies = iter((_protocol_policy_observation(7), _protocol_policy_observation(9)))
    observations = iter(
        (
            _protocol_handoff_observation(worker_id=released_worker, job_id=job_id),
            _protocol_handoff_observation(worker_id=released_worker, job_id=job_id),
        )
    )
    events: list[str] = []
    monkeypatch.setattr(gate, "_recover_released_v040_startup_residue", lambda: None)
    monkeypatch.setattr(gate, "_recover_protocol_v2_startup_fixture", dict)
    monkeypatch.setattr(gate, "_ray_job_manager_replica_observation", lambda: next(managers))
    monkeypatch.setattr(gate, "_observe_protocol_cohorts", lambda: next(policies))
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_manager",
        lambda: events.append("delete-released-manager"),
    )
    monkeypatch.setattr(gate, "_apply_released_v040_manager", lambda: None)

    def wait_for_cohorts() -> Mapping[str, Any]:
        gate._released_v040_worker_id = released_worker
        return {}

    monkeypatch.setattr(gate, "_wait_for_protocol_cohorts", wait_for_cohorts)
    monkeypatch.setattr(
        gate,
        "_scale_ray_job_manager",
        lambda replicas: events.append(f"scale-current:{replicas}"),
    )
    monkeypatch.setattr(gate, "_enqueue_released_v040_handoff_task", lambda: TASK_ID)
    monkeypatch.setattr(
        gate,
        "_wait_for_protocol_handoff_task",
        lambda *_args, **_kwargs: next(observations),
    )
    monkeypatch.setattr(gate, "_observe_protocol_handoff_task", lambda _task_id: next(observations))
    monkeypatch.setattr(gate, "_delete_released_v040_worker_lease", lambda: None)

    def enqueue_survival() -> Mapping[str, Any]:
        gate._protocol_v1_survival_fixture = survival_fixture
        return survival_fixture

    monkeypatch.setattr(gate, "_enqueue_protocol_v1_survival_task", enqueue_survival)
    monkeypatch.setattr(
        gate,
        "_require_protocol_v1_survival_unchanged",
        lambda _fixture: {},
    )
    monkeypatch.setattr(gate, "_release_protocol_v1_survival_task", lambda _fixture: {})
    monkeypatch.setattr(
        gate,
        "_wait_for_ray_job_gate_task",
        lambda *_args, **_kwargs: survival_terminal,
    )
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v1_survival_task",
        lambda _fixture: survival_row,
    )
    monkeypatch.setattr(
        gate,
        "_protocol_metric_counts",
        lambda: {("other", "QUEUED"): 0},
    )

    def seed_fixture() -> Mapping[str, Any]:
        events.append("seed-protocol-v2")
        gate._protocol_v2_fixture = fixture
        return fixture

    monkeypatch.setattr(gate, "_seed_protocol_v2_fixture", seed_fixture)

    def fail_visibility(
        _fixture: Mapping[str, Any], *, baseline_metrics: Mapping[tuple[str, str], int]
    ) -> None:
        events.append("visibility-failed")
        raise RuntimeError("protocol-v2-visibility-failure")

    monkeypatch.setattr(gate, "_verify_protocol_v2_visibility", fail_visibility)
    monkeypatch.setattr(
        gate,
        "_verify_protocol_v2_rejection",
        lambda _fixture: pytest.fail("rejection probe must not run after visibility failure"),
    )

    def cleanup_fixture(_fixture: Mapping[str, Any]) -> Mapping[str, Any]:
        events.append("cleanup-protocol-v2")
        gate._protocol_v2_fixture = None
        return {"revision": 9}

    monkeypatch.setattr(gate, "_cleanup_protocol_v2_fixture", cleanup_fixture)
    monkeypatch.setattr(
        gate,
        "_wait_for_application_topology",
        lambda: events.append("topology-restored"),
    )

    with pytest.raises(RuntimeError, match="protocol-v2-visibility-failure"):
        gate._verify_protocol_handoff_certification()

    assert gate._protocol_v2_fixture is None
    assert gate.evidence.protocol_handoff_cleanup_restored is True
    assert events.count("delete-released-manager") == 2
    assert events.count("scale-current:1") == 2
    assert events[-2:] == ["cleanup-protocol-v2", "topology-restored"]


def test_protocol_handoff_owner_failure_cleans_the_deferred_v1_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.evidence.ray_job_manager_reconciled_same_job = True
    gate.evidence.ray_job_manager_no_resubmit = True
    released_worker = "11111111-1111-4111-8111-111111111111"
    job_id = f"raysubmit_django_ray_v1_{'a' * 64}"
    survival_fixture = _protocol_v1_survival_fixture()
    managers = iter(
        (
            {"replicas": 1, "ready_replicas": 1},
            {"replicas": 1, "ready_replicas": 1},
        )
    )
    policies = iter((_protocol_policy_observation(7), _protocol_policy_observation(7)))
    events: list[str] = []
    monkeypatch.setattr(gate, "_recover_released_v040_startup_residue", lambda: None)
    monkeypatch.setattr(gate, "_recover_protocol_v2_startup_fixture", dict)
    monkeypatch.setattr(gate, "_ray_job_manager_replica_observation", lambda: next(managers))
    monkeypatch.setattr(gate, "_observe_protocol_cohorts", lambda: next(policies))
    monkeypatch.setattr(gate, "_apply_released_v040_manager", lambda: None)

    def wait_for_cohorts() -> Mapping[str, Any]:
        gate._released_v040_worker_id = released_worker
        return {}

    monkeypatch.setattr(gate, "_wait_for_protocol_cohorts", wait_for_cohorts)
    monkeypatch.setattr(
        gate,
        "_scale_ray_job_manager",
        lambda replicas: events.append(f"scale-current:{replicas}"),
    )
    monkeypatch.setattr(gate, "_enqueue_released_v040_handoff_task", lambda: TASK_ID)
    monkeypatch.setattr(
        gate,
        "_wait_for_protocol_handoff_task",
        lambda *_args, **_kwargs: _protocol_handoff_observation(
            worker_id=released_worker,
            job_id=job_id,
        ),
    )

    def enqueue_survival() -> Mapping[str, Any]:
        gate._protocol_v1_survival_fixture = survival_fixture
        return survival_fixture

    monkeypatch.setattr(gate, "_enqueue_protocol_v1_survival_task", enqueue_survival)
    monkeypatch.setattr(
        gate,
        "_require_protocol_v1_survival_unchanged",
        lambda _fixture: (_ for _ in ()).throw(RuntimeError("v1-owner-failure")),
    )
    monkeypatch.setattr(
        gate,
        "_release_protocol_v1_survival_task",
        lambda _fixture: pytest.fail("the failed owner must not release the deferred row"),
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_manager",
        lambda: events.append("delete-released-manager"),
    )
    monkeypatch.setattr(
        gate,
        "_delete_released_v040_worker_lease",
        lambda: events.append("delete-released-lease"),
    )

    def cleanup_survival(_fixture: Mapping[str, Any]) -> Mapping[str, Any]:
        events.append("cleanup-protocol-v1-survival")
        gate._protocol_v1_survival_fixture = None
        return {
            "matched": 1,
            "states": ["QUEUED"],
            "deleted": 1,
            "queued_absent": True,
        }

    monkeypatch.setattr(gate, "_cleanup_protocol_v1_survival_task", cleanup_survival)
    monkeypatch.setattr(
        gate,
        "_wait_for_application_topology",
        lambda: events.append("topology-restored"),
    )

    with pytest.raises(RuntimeError, match="v1-owner-failure"):
        gate._verify_protocol_handoff_certification()

    assert gate._protocol_v1_survival_fixture is None
    assert gate.evidence.protocol_handoff_cleanup_restored is True
    assert events == [
        "scale-current:0",
        "delete-released-manager",
        "delete-released-lease",
        "cleanup-protocol-v1-survival",
        "scale-current:1",
        "topology-restored",
    ]


@pytest.mark.parametrize(
    ("terminal_field", "terminal_value"),
    [
        (None, None),
        ("job_id", "replacement-job"),
        ("attempt_number", 2),
        ("execution_generation", 5),
        ("submission_count", 2),
    ],
)
def test_live_rq2_gate_reconciles_one_job_and_runs_negative_fixture(
    monkeypatch: pytest.MonkeyPatch,
    terminal_field: str | None,
    terminal_value: object,
) -> None:
    gate = LocalKubeRayGate(_config())
    job_id = f"raysubmit_django_ray_rq2_{'a' * 64}"
    observations = iter(
        (
            {
                "ready": True,
                "state": "RUNNING",
                "durable_state": "RUNNING",
                "attempt_number": 1,
                "execution_generation": 4,
                "worker_id": "old-worker",
                "job_id": job_id,
                "carrier_ok": True,
                "binding_ok": True,
                "request_ok": True,
                "info_clear": True,
                "logs_clear": True,
                "submission_count": 1,
            },
            {
                "ready": True,
                "state": "RUNNING",
                "durable_state": "RUNNING",
                "attempt_number": 1,
                "execution_generation": 4,
                "worker_id": "replacement-worker",
                "job_id": job_id,
                "carrier_ok": True,
                "binding_ok": True,
                "request_ok": True,
                "info_clear": True,
                "logs_clear": True,
                "submission_count": 1,
            },
            {
                "ready": True,
                "state": "SUCCEEDED",
                "durable_state": "SUCCEEDED",
                "attempt_number": 1,
                "execution_generation": 4,
                "worker_id": "replacement-worker",
                "job_id": job_id,
                "carrier_ok": True,
                "binding_ok": True,
                "request_ok": True,
                "info_clear": True,
                "logs_clear": True,
                "submission_count": 1,
            },
        )
    )
    events: list[str] = []

    monkeypatch.setattr(gate, "_enqueue_ray_job_gate_task", lambda: TASK_ID)
    monkeypatch.setattr(gate, "_decoded_credential_values", lambda: ())
    monkeypatch.setattr(gate, "_ray_process_surfaces_clear", lambda _values: True)
    monkeypatch.setattr(
        gate,
        "_restart_ray_job_manager",
        lambda: events.append("manager-restarted"),
    )

    def observe(*_args: object, **_kwargs: object) -> Mapping[str, object]:
        observation = next(observations)
        if terminal_field is not None and observation["state"] == "SUCCEEDED":
            observation[terminal_field] = terminal_value
        return observation

    monkeypatch.setattr(gate, "_wait_for_ray_job_gate_task", observe)
    monkeypatch.setattr(
        gate,
        "_verify_missing_ray_job_request_reference",
        lambda: events.append("missing-reference-verified"),
    )

    if terminal_field is not None:
        with pytest.raises(
            ValueError, match="terminal observation lost its strict request contract"
        ):
            gate._verify_ray_job_request_reference()
        assert events == ["manager-restarted"]
        assert gate.evidence.ray_job_manager_reconciled_same_job is False
        assert gate.evidence.ray_job_manager_no_resubmit is False
        return

    gate._verify_ray_job_request_reference()

    assert events == ["manager-restarted", "missing-reference-verified"]
    assert gate.evidence.ray_job_request_reference_carrier is True
    assert gate.evidence.ray_job_raw_info_clear is True
    assert gate.evidence.ray_job_processes_clear is True
    assert gate.evidence.ray_job_logs_clear is True
    assert gate.evidence.ray_job_manager_reconciled_same_job is True
    assert gate.evidence.ray_job_manager_no_resubmit is True


def test_live_rq2_in_pod_inspectors_are_valid_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    observed_fields: list[str] = []

    def compile_private_script(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, f"<{field_name}>", "exec")
        observed_fields.append(field_name)
        return {
            "rq2 positive task enqueue": {"task_id": TASK_ID},
            "rq2 positive task observation": {},
            "rq2 missing-reference submission": {},
            "rq2 missing-reference observation": {},
            "rq2 missing-reference stale fence": {"updated": 1},
            "rq2 missing-reference disposition": {},
        }[field_name]

    monkeypatch.setattr(gate, "_sensitive_django_shell", compile_private_script)

    assert gate._enqueue_ray_job_gate_task() == TASK_ID
    gate._observe_ray_job_gate_task(TASK_ID)
    gate._submit_missing_ray_job_request_reference("private-marker")
    gate._observe_missing_ray_job_request_reference(
        task_id=TASK_ID,
        marker="private-marker",
    )
    gate._age_missing_ray_job_execution(TASK_ID)
    gate._observe_missing_ray_job_disposition(TASK_ID)

    assert observed_fields == [
        "rq2 positive task enqueue",
        "rq2 positive task observation",
        "rq2 missing-reference submission",
        "rq2 missing-reference observation",
        "rq2 missing-reference stale fence",
        "rq2 missing-reference disposition",
    ]


def test_ray_process_probe_tolerates_exits_but_still_detects_exposure(tmp_path: Path) -> None:
    unreadable = tmp_path / "99999991" / "cmdline"
    unreadable.mkdir(parents=True)
    clear = tmp_path / "99999992" / "cmdline"
    clear.parent.mkdir()
    clear.write_bytes(b"python\x00-m\x00ray.worker")

    probe = gate_module._build_ray_process_surface_probe(
        ("private-marker",),
        proc_root=tmp_path,
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"clear": True}

    exposed = tmp_path / "99999993" / "cmdline"
    exposed.parent.mkdir()
    exposed.write_bytes(b"python\x00private-marker")
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"clear": False}


def test_missing_reference_gate_restores_manager_and_rejects_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    job_id = f"raysubmit_django_ray_rq2_{'c' * 64}"
    events: list[str] = []

    monkeypatch.setattr(gate, "_register_ray_job_gate_value", lambda _value: None)
    monkeypatch.setattr(
        gate,
        "_scale_ray_job_manager",
        lambda replicas: events.append(f"scale:{replicas}"),
    )
    monkeypatch.setattr(
        gate,
        "_submit_missing_ray_job_request_reference",
        lambda _marker: {
            "task_id": TASK_ID,
            "job_id": job_id,
            "attempt_number": 1,
            "execution_generation": 7,
        },
    )
    monkeypatch.setattr(
        gate,
        "_wait_for_missing_ray_job_failure",
        lambda **_kwargs: {
            "job_id": job_id,
            "carrier_ok": True,
            "binding_ok": True,
            "info_clear": True,
            "logs_clear": True,
            "submission_count": 1,
            "attempt_number": 1,
        },
    )
    monkeypatch.setattr(gate, "_decoded_credential_values", lambda: ())
    monkeypatch.setattr(gate, "_ray_process_surfaces_clear", lambda _values: True)
    monkeypatch.setattr(
        gate,
        "_age_missing_ray_job_execution",
        lambda _task_id: events.append("aged"),
    )
    monkeypatch.setattr(
        gate,
        "_wait_for_application_topology",
        lambda: events.append("topology"),
    )
    monkeypatch.setattr(
        gate,
        "_wait_for_missing_ray_job_disposition",
        lambda _task_id: {
            "state": "FAILED",
            "attempt_number": 1,
            "execution_generation": 7,
            "run_after_is_none": True,
            "completion_is_none": True,
            "result_is_none": True,
            "fixed_error": True,
            "archived_attempts": 1,
            "submission_count": 1,
        },
    )

    gate._verify_missing_ray_job_request_reference()

    assert events == ["scale:0", "aged", "scale:1", "topology"]
    assert gate.evidence.ray_job_missing_reference_no_marker is True
    assert gate.evidence.ray_job_missing_reference_no_retry is True


def test_missing_reference_gate_restores_manager_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    events: list[str] = []

    monkeypatch.setattr(gate, "_register_ray_job_gate_value", lambda _value: None)
    monkeypatch.setattr(
        gate,
        "_scale_ray_job_manager",
        lambda replicas: events.append(f"scale:{replicas}"),
    )
    monkeypatch.setattr(
        gate,
        "_submit_missing_ray_job_request_reference",
        lambda _marker: (_ for _ in ()).throw(RuntimeError("fixture failed")),
    )
    monkeypatch.setattr(
        gate,
        "_wait_for_application_topology",
        lambda: events.append("topology"),
    )

    with pytest.raises(RuntimeError, match="fixture failed"):
        gate._verify_missing_ray_job_request_reference()

    assert events == ["scale:0", "scale:1", "topology"]


def test_setup_log_requires_migrations_static_and_runtime_env_markers() -> None:
    complete = "\n".join(
        (
            "Running migrations...",
            "Collecting static files...",
            "Building shared RuntimeEnv source bundle...",
            "RuntimeEnv bundle ready: /runtime-env/django-ray-source.zip (42 bytes)",
            "Recovery RuntimeEnv bundle ready: /runtime-env/django-ray-recovery.zip (84 bytes)",
            "Django setup complete!",
        )
    )
    inspect_setup_log(complete)

    with pytest.raises(ValueError, match="RuntimeEnv bundle ready"):
        inspect_setup_log(
            "Running migrations...\nCollecting static files...\nDjango setup complete!"
        )


def test_setup_job_fails_closed_before_reporting_completion() -> None:
    resources = list(
        yaml.safe_load_all((ROOT / "k8s/base/django-setup-job.yaml").read_text(encoding="utf-8"))
    )
    job = next(resource for resource in resources if resource.get("kind") == "Job")
    script = job["spec"]["template"]["spec"]["containers"][0]["args"][0]

    assert script.lstrip().startswith("set -euo pipefail")
    assert script.index("set -euo pipefail") < script.index("Running migrations")


def test_setup_failure_prevents_workload_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(gate, "_preflight", lambda: order.append("preflight"))
    monkeypatch.setattr(gate, "_build_images", lambda: order.append("images"))
    monkeypatch.setattr(gate, "_apply_overlay", lambda: order.append("apply"))

    def fail_setup() -> None:
        order.append("setup")
        raise ValueError("migration failed")

    monkeypatch.setattr(gate, "_run_setup", fail_setup)
    monkeypatch.setattr(gate, "_apply_workloads", lambda: order.append("workloads"))

    with pytest.raises(GateError, match="migration failed") as captured:
        gate.run()

    assert captured.value.layer == "setup"
    assert order == ["preflight", "images", "apply", "setup"]


def test_setup_delete_and_completion_wait_are_timeout_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.temp_root = tmp_path
    gate.evidence.app_tag = APP_TAG
    gate.evidence.app_image_id = IMAGE_ID
    gate.setup_pod_images = pod_image_contract(cast(dict[str, Any], _setup_pod()["spec"]))
    calls: list[tuple[str, ...]] = []
    setup_log = "\n".join(
        (
            "Running migrations...",
            "Collecting static files...",
            "Building shared RuntimeEnv source bundle...",
            "RuntimeEnv bundle ready:",
            "Recovery RuntimeEnv bundle ready:",
            "Django setup complete!",
        )
    )

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        if args[0] == "logs":
            return CommandResult(setup_log, "", 0)
        if args[:2] == ("get", "job"):
            return CommandResult(
                json.dumps(
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "metadata": {
                            "name": SETUP_JOB,
                            "namespace": EXPECTED_NAMESPACE,
                            "uid": "setup-owner",
                        },
                    }
                ),
                "",
                0,
            )
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": [_setup_pod()]}), "", 0)
        return CommandResult("", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(gate, "_verify_pod_image_ids", lambda *args, **kwargs: 1)

    gate._run_setup()

    assert calls[0][-2:] == ("--wait=true", "--timeout=300s")
    wait = next(call for call in calls if call[0] == "wait")
    assert wait[-1] == "--timeout=300s"


@pytest.mark.parametrize(
    ("pod", "message"),
    [
        (_setup_pod(owner_uid="stale-job"), "not controlled by Job/django-setup"),
        (_setup_pod(image="django-ray:stale"), "image contract does not match"),
        (_setup_pod(image_id=f"sha256:{'c' * 64}"), "does not run the locally built image ID"),
        (
            _setup_pod(
                name="source-proof-sidecar",
                extra_containers=[{"name": "django-setup", "image": "attacker:latest"}],
            ),
            "image contract does not match",
        ),
    ],
)
def test_setup_identity_rejects_stale_owner_tag_or_image_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pod: dict[str, object],
    message: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.temp_root = tmp_path
    gate.evidence.app_tag = APP_TAG
    gate.evidence.app_image_id = IMAGE_ID
    gate.setup_pod_images = pod_image_contract(cast(dict[str, Any], _setup_pod()["spec"]))
    setup_log = "\n".join(
        (
            "Running migrations...",
            "Collecting static files...",
            "Building shared RuntimeEnv source bundle...",
            "RuntimeEnv bundle ready:",
            "Recovery RuntimeEnv bundle ready:",
            "Django setup complete!",
        )
    )

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[0] == "logs":
            return CommandResult(setup_log, "", 0)
        if args[:2] == ("get", "job"):
            return CommandResult(
                json.dumps(
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "metadata": {
                            "name": SETUP_JOB,
                            "namespace": EXPECTED_NAMESPACE,
                            "uid": "setup-owner",
                        },
                    }
                ),
                "",
                0,
            )
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": [pod]}), "", 0)
        return CommandResult("", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match=message):
        gate._run_setup()


def test_docker_inspect_binds_tag_id_revision_and_source_tree() -> None:
    payload = json.dumps(
        [
            {
                "Id": IMAGE_ID,
                "RepoTags": [APP_TAG],
                "Config": {
                    "Labels": {
                        "org.opencontainers.image.revision": COMMIT,
                        "org.opencontainers.image.source-tree": SOURCE_TREE,
                    }
                },
            }
        ]
    )

    assert (
        parse_docker_image_inspect(
            payload,
            expected_tag=APP_TAG,
            commit=COMMIT,
            source_tree=SOURCE_TREE,
        )
        == IMAGE_ID
    )

    wrong_revision = payload.replace(COMMIT, "c" * 40)
    with pytest.raises(ValueError, match="revision label"):
        parse_docker_image_inspect(
            wrong_revision,
            expected_tag=APP_TAG,
            commit=COMMIT,
            source_tree=SOURCE_TREE,
        )

    wrong_tree = payload.replace(SOURCE_TREE, "d" * 40)
    with pytest.raises(ValueError, match="source-tree label"):
        parse_docker_image_inspect(
            wrong_tree,
            expected_tag=APP_TAG,
            commit=COMMIT,
            source_tree=SOURCE_TREE,
        )


@pytest.mark.parametrize(
    "runtime_id",
    [IMAGE_ID, f"docker://{IMAGE_ID}", f"containerd://{IMAGE_ID}", f"repo@{IMAGE_ID}"],
)
def test_runtime_image_id_normalization(runtime_id: str) -> None:
    assert normalize_runtime_image_id(runtime_id) == IMAGE_ID


def test_pod_image_identity_matches_source_tag_and_build_digest() -> None:
    pod = {
        "metadata": {"name": "worker", "namespace": EXPECTED_NAMESPACE, "uid": "worker-uid"},
        "spec": {"containers": [{"name": "worker", "image": APP_TAG}]},
        "status": {
            "containerStatuses": [
                {
                    "name": "worker",
                    "image": APP_TAG,
                    "imageID": f"containerd://{IMAGE_ID}",
                }
            ]
        },
    }
    gate = LocalKubeRayGate(_config())

    assert gate._verify_pod_image_ids(pod, expected_tag=APP_TAG, expected_id=IMAGE_ID) == 1

    wrong_pod = json.loads(json.dumps(pod))
    wrong_pod["status"]["containerStatuses"][0]["imageID"] = f"containerd://sha256:{'c' * 64}"
    with pytest.raises(ValueError, match="does not run the locally built image ID"):
        gate._verify_pod_image_ids(wrong_pod, expected_tag=APP_TAG, expected_id=IMAGE_ID)

    substituted = {
        "metadata": {
            "name": "substituted",
            "namespace": EXPECTED_NAMESPACE,
            "uid": "substituted-uid",
        },
        "spec": {
            "containers": [
                {"name": "attacker-main", "image": "attacker:latest"},
                {"name": "source-proof-sidecar", "image": APP_TAG},
            ]
        },
        "status": {
            "containerStatuses": [
                {
                    "name": "attacker-main",
                    "image": "attacker:latest",
                    "imageID": f"containerd://sha256:{'d' * 64}",
                },
                {
                    "name": "source-proof-sidecar",
                    "image": APP_TAG,
                    "imageID": f"containerd://{IMAGE_ID}",
                },
            ]
        },
    }
    expected_contract = PodImageContract((), (("django-ray-worker", APP_TAG),))
    with pytest.raises(ValueError, match="image contract does not match"):
        gate._verify_pod_image_ids(
            substituted,
            expected_tag=APP_TAG,
            expected_id=IMAGE_ID,
            expected_contract=expected_contract,
        )


def test_each_application_deployment_must_retain_rendered_positive_replicas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.evidence.app_tag = APP_TAG
    gate.evidence.app_image_id = IMAGE_ID
    for name in APP_DEPLOYMENTS:
        contract = PodImageContract((), ((name, APP_TAG),))
        gate.deployment_contracts[name] = DeploymentContract(1, contract, (("app", name),))

    def deployment(name: str) -> dict[str, object]:
        replicas = 0 if name == "django-ray-worker-sync" else 1
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": EXPECTED_NAMESPACE,
                "uid": f"deployment-{name}",
                "generation": 1,
            },
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {"containers": [{"name": name, "image": APP_TAG}]},
                },
            },
            "status": {
                "observedGeneration": 1,
                "replicas": replicas,
                "updatedReplicas": replicas,
                "readyReplicas": replicas,
                "availableReplicas": replicas,
            },
        }

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployment(args[2])), "", 0)
        raise AssertionError("replica drift must fail before pod inventory")

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="replicas changed from rendered 1 to live 0"):
        gate._verify_deployed_images()


def test_application_inventory_binds_pods_through_exact_replicaset_and_deployment_uids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selectors = {
        "django-web": {"app": "django-ray", "component": "web"},
        "django-ray-worker": {"app": "django-ray", "component": "worker"},
        "django-ray-worker-sync": {
            "app": "django-ray",
            "component": "worker",
            "queues": "sync",
        },
        "django-ray-worker-ml": {
            "app": "django-ray",
            "component": "worker",
            "queues": "ml",
        },
        "django-ray-worker-ray-job": {
            "app": "django-ray",
            "component": "worker",
            "queues": "ray-data",
            "runner": "ray-job",
        },
    }
    gate = LocalKubeRayGate(_config())
    gate.evidence.app_tag = APP_TAG
    gate.evidence.app_image_id = IMAGE_ID
    deployments = {
        name: _live_application_deployment(name, labels=selectors[name]) for name in APP_DEPLOYMENTS
    }
    replicasets = [
        _application_replicaset(name, labels=selectors[name]) for name in APP_DEPLOYMENTS
    ]
    pods = [_application_pod(name, labels=selectors[name]) for name in APP_DEPLOYMENTS]
    for name in APP_DEPLOYMENTS:
        gate.deployment_contracts[name] = DeploymentContract(
            replicas=1,
            pod_images=PodImageContract((), ((name, APP_TAG),)),
            selector=tuple(sorted(selectors[name].items())),
        )

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": pods}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    gate._verify_deployed_images()

    assert gate.evidence.deployments == dict.fromkeys(APP_DEPLOYMENTS, 1)

    attacker = _application_pod("django-web", labels=selectors["django-web"])
    attacker_metadata = cast(dict[str, Any], attacker["metadata"])
    attacker_metadata["name"] = "selector-attacker"
    attacker_metadata["uid"] = "selector-attacker-uid"
    attacker_metadata["ownerReferences"] = [
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "name": "attacker",
            "uid": "attacker-owner",
            "controller": True,
        }
    ]
    pods.append(attacker)

    with pytest.raises(
        ValueError, match="not controlled through an inventoried guarded ReplicaSet"
    ):
        gate._verify_deployed_images()


def test_application_convergence_waits_for_old_terminating_pod_to_disappear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, selectors = _application_inventory_fixture()
    old_replicaset, old_pod = _old_terminating_application_generation(
        "django-ray-worker",
        labels=selectors["django-ray-worker"],
    )
    replicasets.append(old_replicaset)
    pod_batches = iter([pods + [old_pod], pods])
    pod_inventory_calls = 0
    sleeps: list[float] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        nonlocal pod_inventory_calls
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            pod_inventory_calls += 1
            return CommandResult(json.dumps({"items": next(pod_batches)}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        "scripts.local_kuberay_gate.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    gate._wait_for_application_topology()

    assert pod_inventory_calls == 2
    assert sleeps == [2]
    assert gate.evidence.deployments == dict.fromkeys(APP_DEPLOYMENTS, 1)


def test_application_convergence_polls_exact_deployment_status_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, _ = _application_inventory_fixture()
    web_status = cast(dict[str, Any], deployments["django-web"]["status"])
    web_status["readyReplicas"] = 0
    pod_inventory_calls = 0
    sleeps: list[float] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        nonlocal pod_inventory_calls
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            pod_inventory_calls += 1
            web_status["readyReplicas"] = 1
            return CommandResult(json.dumps({"items": pods}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        "scripts.local_kuberay_gate.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    gate._wait_for_application_topology()

    assert pod_inventory_calls == 2
    assert sleeps == [2]
    assert gate.evidence.deployments == dict.fromkeys(APP_DEPLOYMENTS, 1)


def test_application_convergence_times_out_bounded_when_old_pod_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, selectors = _application_inventory_fixture(
        rollout_timeout=1
    )
    old_replicaset, old_pod = _old_terminating_application_generation(
        "django-ray-worker",
        labels=selectors["django-ray-worker"],
    )
    replicasets.append(old_replicaset)
    now = [0.0]
    sleeps: list[float] = []
    pod_inventory_calls = 0

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        nonlocal pod_inventory_calls
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            pod_inventory_calls += 1
            now[0] = 1.1
            return CommandResult(json.dumps({"items": pods + [old_pod]}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr("scripts.local_kuberay_gate.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "scripts.local_kuberay_gate.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    with pytest.raises(ValueError, match=r"did not converge within 1s.*old ReplicaSet") as error:
        gate._wait_for_application_topology()

    assert len(str(error.value)) <= MAX_OUTPUT_CHARACTERS
    assert pod_inventory_calls == 1
    assert sleeps == []
    assert gate.evidence.deployments == {}


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("substituted", "image contract"),
        ("replicaset-substituted", "current ReplicaSet.*image contract"),
        ("unowned", "not controlled through an inventoried guarded ReplicaSet"),
        ("hidden", "hidden from its exact owning ReplicaSet"),
    ],
)
def test_application_convergence_fails_closed_without_retrying_structural_pods(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    gate, deployments, replicasets, pods, _ = _application_inventory_fixture()
    target = pods[0]
    metadata = cast(dict[str, Any], target["metadata"])
    if mutation == "substituted":
        spec = cast(dict[str, Any], target["spec"])
        containers = cast(list[dict[str, Any]], spec["containers"])
        containers[0]["image"] = "attacker.invalid/substitute:latest"
    elif mutation == "replicaset-substituted":
        replicaset_spec = cast(dict[str, Any], replicasets[0]["spec"])
        template = cast(dict[str, Any], replicaset_spec["template"])
        template_spec = cast(dict[str, Any], template["spec"])
        containers = cast(list[dict[str, Any]], template_spec["containers"])
        containers[0]["image"] = "attacker.invalid/substitute:latest"
    elif mutation == "unowned":
        metadata["ownerReferences"] = [
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "name": "attacker",
                "uid": "attacker-owner",
                "controller": True,
            }
        ]
    else:
        metadata["labels"] = {"hidden": "true"}
    pod_inventory_calls = 0
    sleeps: list[float] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        nonlocal pod_inventory_calls
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            pod_inventory_calls += 1
            return CommandResult(json.dumps({"items": pods}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        "scripts.local_kuberay_gate.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    with pytest.raises(ValueError, match=message):
        gate._wait_for_application_topology()

    assert pod_inventory_calls == 1
    assert sleeps == []
    assert gate.evidence.deployments == {}


def test_strict_application_identity_rejects_old_terminating_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, selectors = _application_inventory_fixture()
    old_replicaset, old_pod = _old_terminating_application_generation(
        "django-ray-worker",
        labels=selectors["django-ray-worker"],
    )
    replicasets.append(old_replicaset)

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": pods + [old_pod]}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="old ReplicaSet.*still owns Pod"):
        gate._verify_deployed_images()

    assert gate.evidence.deployments == {}


def test_application_convergence_rejects_substituted_old_terminating_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, selectors = _application_inventory_fixture()
    old_replicaset, old_pod = _old_terminating_application_generation(
        "django-ray-worker",
        labels=selectors["django-ray-worker"],
    )
    old_spec = cast(dict[str, Any], old_pod["spec"])
    old_containers = cast(list[dict[str, Any]], old_spec["containers"])
    old_containers.append({"name": "substituted-sidecar", "image": "busybox:1.36"})
    replicasets.append(old_replicaset)
    sleeps: list[float] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": pods + [old_pod]}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(
        "scripts.local_kuberay_gate.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    with pytest.raises(ValueError, match="image contract.*owning ReplicaSet"):
        gate._wait_for_application_topology()

    assert sleeps == []
    assert gate.evidence.deployments == {}


def test_application_inventory_rejects_stale_replicaset_deployment_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels = {name: {"app": name} for name in APP_DEPLOYMENTS}
    gate = LocalKubeRayGate(_config())
    gate.evidence.app_tag = APP_TAG
    gate.evidence.app_image_id = IMAGE_ID
    deployments = {
        name: _live_application_deployment(name, labels=labels[name]) for name in APP_DEPLOYMENTS
    }
    replicasets = [_application_replicaset(name) for name in APP_DEPLOYMENTS]
    stale_metadata = cast(dict[str, Any], replicasets[0]["metadata"])
    stale_owner = cast(list[dict[str, Any]], stale_metadata["ownerReferences"])
    stale_owner[0]["uid"] = "stale-deployment-uid"
    pods = [_application_pod(name, labels=labels[name]) for name in APP_DEPLOYMENTS]
    for name in APP_DEPLOYMENTS:
        gate.deployment_contracts[name] = DeploymentContract(
            1,
            PodImageContract((), ((name, APP_TAG),)),
            tuple(sorted(labels[name].items())),
        )

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        return CommandResult(json.dumps({"items": pods}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="not controlled by Deployment"):
        gate._verify_deployed_images()


def test_application_inventory_rejects_hidden_replicaset_with_pinned_deployment_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, _ = _application_inventory_fixture()
    hidden_replicaset = _application_replicaset("django-web")
    metadata = cast(dict[str, Any], hidden_replicaset["metadata"])
    metadata["name"] = "hidden-replicaset"
    metadata["uid"] = "hidden-replicaset-uid"
    owner = cast(list[dict[str, Any]], metadata["ownerReferences"])[0]
    owner["name"] = "altered-name-with-live-deployment-uid"
    replicasets.append(hidden_replicaset)

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        return CommandResult(json.dumps({"items": pods}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="not controlled by Deployment/django-web"):
        gate._verify_deployed_images()


def test_application_inventory_rejects_hidden_pod_claiming_pinned_replicaset_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, deployments, replicasets, pods, _ = _application_inventory_fixture()
    hidden_replicaset = _application_replicaset(
        "django-web",
        revision="2",
        replicas=0,
    )
    replicaset_metadata = cast(dict[str, Any], hidden_replicaset["metadata"])
    replicaset_metadata["name"] = "hidden-replicaset"
    replicaset_metadata["uid"] = "hidden-replicaset-uid"
    replicasets.append(hidden_replicaset)
    hidden_pod = _application_pod("django-web", labels={"hidden": "true"})
    pod_metadata = cast(dict[str, Any], hidden_pod["metadata"])
    pod_metadata["name"] = "hidden-pod"
    pod_metadata["uid"] = "hidden-pod-uid"
    pod_metadata["ownerReferences"] = [
        {
            "apiVersion": "apps/v1",
            "kind": "ReplicaSet",
            "name": "altered-name-with-live-replicaset-uid",
            "uid": "hidden-replicaset-uid",
            "controller": False,
        },
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "name": "unrelated-controller",
            "uid": "unrelated-controller-uid",
            "controller": True,
        },
    ]
    pods.append(hidden_pod)

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:2] == ("get", "deployment"):
            return CommandResult(json.dumps(deployments[args[2]]), "", 0)
        if args[:2] == ("get", "replicasets"):
            return CommandResult(json.dumps({"items": replicasets}), "", 0)
        return CommandResult(json.dumps({"items": pods}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="not controlled by ReplicaSet/hidden-replicaset"):
        gate._verify_deployed_images()


def _live_probe_deployment(*, host: str = EXPECTED_PROBE_HOST) -> dict[str, object]:
    probe = {
        "httpGet": {
            "path": EXPECTED_PROBE_PATH,
            "port": 8000,
            "httpHeaders": [{"name": "Host", "value": host}],
        }
    }
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "django-web",
                            "readinessProbe": probe,
                            "livenessProbe": probe,
                        }
                    ]
                }
            }
        }
    }


def test_probe_contract_matches_live_allowlist() -> None:
    config_map = {"data": {"DJANGO_ALLOWED_HOSTS": f"localhost,{EXPECTED_PROBE_HOST}"}}

    assert inspect_probe_contract(_live_probe_deployment(), config_map) == EXPECTED_PROBE_HOST

    with pytest.raises(ValueError, match="absent from DJANGO_ALLOWED_HOSTS"):
        inspect_probe_contract(
            _live_probe_deployment(), {"data": {"DJANGO_ALLOWED_HOSTS": "localhost"}}
        )


def test_runtime_archive_probe_requires_generic_ray_and_fixed_bootstrap_member() -> None:
    digest = "d" * 64
    recovery_digest = "e" * 64
    payload = json.dumps(
        {
            "django_ray": "absent",
            "bytes": 293_956,
            "sha256": digest,
            "required_member": True,
            "recovery_bytes": 20_000_000,
            "recovery_sha256": recovery_digest,
            "recovery_required_members": True,
        }
    )
    assert parse_runtime_archive_probe(payload) == (
        293_956,
        digest,
        20_000_000,
        recovery_digest,
    )

    installed = payload.replace('"absent"', '"present"')
    with pytest.raises(ValueError, match="unexpectedly has django_ray"):
        parse_runtime_archive_probe(installed)

    missing = json.loads(payload)
    missing["required_member"] = False
    with pytest.raises(ValueError, match=re.escape(RUNTIME_ENV_REQUIRED_MEMBER)):
        parse_runtime_archive_probe(json.dumps(missing))

    incomplete_recovery = json.loads(payload)
    incomplete_recovery["recovery_required_members"] = False
    with pytest.raises(ValueError, match="required package closure"):
        parse_runtime_archive_probe(json.dumps(incomplete_recovery))


def test_durable_task_result_must_be_json_and_equal_value_is_preserved() -> None:
    assert parse_task_result("5") == 5
    with pytest.raises(ValueError, match="valid JSON"):
        parse_task_result("not-json")


def test_cold_ray_restart_deletes_only_verified_pod_names(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    old = [
        _ray_pod("ray-head-old", "head", "old-head"),
        _ray_pod("ray-worker-old", "worker", "old-worker"),
    ]
    new = [
        _ray_pod("ray-head-new", "head", "new-head"),
        _ray_pod("ray-worker-new", "worker", "new-worker"),
    ]
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        gate,
        "_ray_pods",
        lambda **kwargs: ("cluster-owner", old),
    )
    monkeypatch.setattr(gate, "_wait_for_ray", lambda **kwargs: new)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: calls.append(args) or CommandResult("", "", 0),
    )

    gate._prepare_ray()

    assert calls == [
        (
            "delete",
            "pod",
            "ray-head-old",
            "ray-worker-old",
            "--wait=true",
            "--timeout=300s",
        )
    ]
    assert gate.evidence.ray_restart == "performed"
    assert gate.evidence.ray_head_count == 1
    assert gate.evidence.ray_worker_count == 1
    assert gate._ray_pod_identities is not None and len(gate._ray_pod_identities) == 2
    assert re.fullmatch(r"[0-9a-f]{64}", gate.evidence.ray_pod_identity_sha256)


def test_effective_kuberay_worker_contract_accepts_the_injected_wait_gcs_init() -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    worker = _ray_pod("ray-worker", "worker", "worker-uid")

    identities = gate._ray_runtime_identities([worker])

    assert len(identities) == 1
    identity = next(iter(identities))
    assert [container.name for container in identity.init_containers] == [
        gate_module.KUBERAY_WAIT_GCS_INIT
    ]
    assert [container.name for container in identity.containers] == ["ray-worker"]


@pytest.mark.parametrize(
    "mutation", ["extra", "substituted", "script", "missing-auth", "wrong-auth"]
)
def test_effective_kuberay_worker_contract_rejects_extra_or_substituted_init(
    mutation: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    worker = _ray_pod("ray-worker", "worker", "worker-uid")
    spec = cast(dict[str, Any], worker["spec"])
    init_containers = cast(list[dict[str, Any]], spec["initContainers"])
    status = cast(dict[str, Any], worker["status"])
    init_statuses = cast(list[dict[str, Any]], status["initContainerStatuses"])
    if mutation == "extra":
        init_containers.append(
            {
                "name": "foreign-init",
                "image": "busybox:1.36",
                "command": ["true"],
                "args": [],
            }
        )
        init_statuses.append(
            {
                "name": "foreign-init",
                "image": "busybox:1.36",
                "imageID": f"containerd://sha256:{'d' * 64}",
                "restartCount": 0,
                "state": {"terminated": {"exitCode": 0, "reason": "Completed"}},
            }
        )
    elif mutation == "substituted":
        init_containers[0]["name"] = "substituted-init"
        init_statuses[0]["name"] = "substituted-init"
    elif mutation == "missing-auth":
        init_containers[0].pop("env")
    elif mutation == "wrong-auth":
        init_containers[0]["env"][1]["valueFrom"]["secretKeyRef"]["name"] = "foreign-secret"
    else:
        init_containers[0]["args"] = ["ray health-check --address attacker.invalid:6379"]

    with pytest.raises(ValueError, match="KubeRay|wait-gcs-ready|image contract"):
        gate._ray_runtime_identities([worker])


@pytest.mark.parametrize("mismatch", ["missing", "name", "image", "termination"])
def test_effective_kuberay_worker_contract_rejects_spec_status_mismatch(
    mismatch: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    worker = _ray_pod("ray-worker", "worker", "worker-uid")
    status = cast(dict[str, Any], worker["status"])
    init_statuses = cast(list[dict[str, Any]], status["initContainerStatuses"])
    if mismatch == "missing":
        init_statuses.clear()
    elif mismatch == "name":
        init_statuses[0]["name"] = "foreign-init"
    elif mismatch == "image":
        init_statuses[0]["image"] = "rayproject/ray:2.55.0-py312"
    else:
        init_statuses[0]["state"] = {"terminated": {"exitCode": 1, "reason": "Error"}}

    with pytest.raises(ValueError, match="init|image|terminate"):
        gate._ray_runtime_identities([worker])


def test_required_restart_discovers_owned_old_images_then_requires_new_effective_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    desired_cluster = _ray_cluster(uid="cluster-owner", head_sidecar=True)
    gate.rendered_ray_topology = normalize_ray_topology(cast(dict[str, Any], desired_cluster))
    gate.expected_ray_head_count = 1
    gate.expected_ray_worker_count = 1
    old_pods = [
        _ray_pod(
            "ray-head-old",
            "head",
            "old-head-uid",
            image="rayproject/ray:2.55.0-py312",
        ),
        _ray_pod(
            "ray-worker-old",
            "worker",
            "old-worker-uid",
            image="rayproject/ray:2.55.0-py312",
        ),
    ]
    new_pods = [
        _ray_pod("ray-head-new", "head", "new-head-uid", head_sidecar=True),
        _ray_pod("ray-worker-new", "worker", "new-worker-uid"),
    ]
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(desired_cluster), "", 0)
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": old_pods}), "", 0)
        if args[:2] == ("delete", "pod"):
            return CommandResult("", "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)
    monkeypatch.setattr(gate, "_wait_for_ray", lambda **kwargs: new_pods)

    cluster_uid, discovered = gate._ray_pods(
        allow_empty=True,
        contract_phase="restart-discovery",
    )
    assert cluster_uid == "cluster-owner"
    assert discovered == old_pods
    with pytest.raises(ValueError, match="effective KubeRay"):
        gate._ray_pods(allow_empty=True)

    gate._prepare_ray()

    deletion = next(call for call in calls if call[:2] == ("delete", "pod"))
    assert deletion[2:4] == ("ray-head-old", "ray-worker-old")
    assert gate.evidence.ray_restart == "performed"
    assert gate._ray_pod_identities is not None
    assert {identity.uid for identity in gate._ray_pod_identities} == {
        "new-head-uid",
        "new-worker-uid",
    }


@pytest.mark.parametrize(
    "mutation",
    ("missing-ray-head", "co-resident-plus-foreign", "full-plus-foreign", "head-init"),
)
def test_restart_discovery_rejects_unknown_co_resident_head_inventory(mutation: str) -> None:
    gate = LocalKubeRayGate(_config())
    desired_cluster = _ray_cluster(uid="cluster-owner", head_sidecar=True)
    gate.rendered_ray_topology = normalize_ray_topology(cast(dict[str, Any], desired_cluster))
    head = _ray_pod(
        "ray-head-old",
        "head",
        "old-head-uid",
        head_sidecar=mutation == "full-plus-foreign",
    )
    spec = cast(dict[str, Any], head["spec"])
    containers = cast(list[dict[str, str]], spec["containers"])
    if mutation == "missing-ray-head":
        containers.clear()
    elif mutation in {"co-resident-plus-foreign", "full-plus-foreign"}:
        containers.append({"name": "foreign-sidecar", "image": "busybox:1.36"})
    else:
        spec["initContainers"] = [{"name": "foreign-init", "image": "busybox:1.36"}]

    with pytest.raises(ValueError, match="supported KubeRay inventory"):
        gate._validate_restart_discovery_pod(head)


def test_restart_discovery_keeps_worker_inventory_exact() -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    worker = _ray_pod("ray-worker-old", "worker", "old-worker-uid")
    spec = cast(dict[str, Any], worker["spec"])
    cast(list[dict[str, str]], spec["containers"]).append(
        {"name": "foreign-sidecar", "image": "busybox:1.36"}
    )

    with pytest.raises(ValueError, match="supported KubeRay inventory"):
        gate._validate_restart_discovery_pod(worker)


def test_restart_discovery_accepts_bounded_owned_scale_down_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate, workers=1)
    old_pods = [
        _ray_pod(
            "ray-head-old",
            "head",
            "old-head-uid",
            image="rayproject/ray:2.55.0-py312",
        ),
        *[
            _ray_pod(
                f"ray-worker-old-{index}",
                "worker",
                f"old-worker-uid-{index}",
                image="rayproject/ray:2.55.0-py312",
            )
            for index in range(3)
        ],
    ]

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        if args[:2] == ("get", "pods"):
            return CommandResult(json.dumps({"items": old_pods}), "", 0)
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    cluster_uid, discovered = gate._ray_pods(
        allow_empty=True,
        contract_phase="restart-discovery",
    )

    assert cluster_uid == "cluster-owner"
    assert discovered == old_pods


def test_final_ray_identity_rejects_uid_or_runtime_image_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    gate._ray_cluster_uid = "cluster-owner"
    original = [
        _ray_pod("ray-head", "head", "head-original"),
        _ray_pod("ray-worker", "worker", "worker-original"),
    ]
    gate._ray_pod_identities = gate._ray_runtime_identities(original)
    gate.evidence.ray_pod_identity_sha256 = gate_module.pod_identity_sha256(
        tuple(gate._ray_pod_identities)
    )
    churned = [
        _ray_pod("ray-head", "head", "head-replaced"),
        _ray_pod("ray-worker", "worker", "worker-replaced"),
    ]
    monkeypatch.setattr(gate, "_ray_pods", lambda **kwargs: ("cluster-owner", churned))

    with pytest.raises(ValueError, match="UID/container/image identity changed"):
        gate._verify_ray_identity()

    image_churn = json.loads(json.dumps(original))
    image_churn[1]["status"]["containerStatuses"][0]["imageID"] = f"containerd://sha256:{'d' * 64}"
    monkeypatch.setattr(gate, "_ray_pods", lambda **kwargs: ("cluster-owner", image_churn))

    with pytest.raises(ValueError, match="UID/container/image identity changed"):
        gate._verify_ray_identity()


def test_ray_pod_inventory_matches_the_rendered_component_image_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pod = _ray_pod("ray-worker", "worker", "worker-uid")
    pod_spec = cast(dict[str, Any], pod["spec"])
    containers = cast(list[dict[str, Any]], pod_spec["containers"])
    containers[0]["image"] = "attacker:latest"
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        return CommandResult(json.dumps({"items": [pod]}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="effective KubeRay"):
        gate._ray_pods()


def test_ray_pod_inventory_rejects_noncontrolling_hidden_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pod = _ray_pod("ray-hidden", "worker", "hidden-uid")
    metadata = cast(dict[str, Any], pod["metadata"])
    labels = cast(dict[str, Any], metadata["labels"])
    labels.pop(RAY_CLUSTER_LABEL)
    owners = cast(list[dict[str, Any]], metadata["ownerReferences"])
    owners[0]["controller"] = False
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        return CommandResult(json.dumps({"items": [pod]}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="not controlled by RayCluster/ray"):
        gate._ray_pods()


def test_ray_pod_inventory_rejects_hidden_pod_with_pinned_cluster_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pod = _ray_pod("ray-hidden", "worker", "hidden-uid")
    metadata = cast(dict[str, Any], pod["metadata"])
    labels = cast(dict[str, Any], metadata["labels"])
    labels.pop(RAY_CLUSTER_LABEL)
    owner = cast(list[dict[str, Any]], metadata["ownerReferences"])[0]
    owner["name"] = "altered-name-with-live-cluster-uid"
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        return CommandResult(json.dumps({"items": [pod]}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="not controlled by RayCluster/ray"):
        gate._ray_pods()


def test_ray_pod_discovery_requires_the_live_cluster_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pod = _ray_pod("ray-head", "head", "head-uid")
    calls: list[tuple[str, ...]] = []
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        return CommandResult(json.dumps({"items": [pod]}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    assert gate._ray_pods() == ("cluster-owner", [pod])
    assert calls[0] == ("get", "raycluster", RAY_CLUSTER_NAME, "-o", "json")


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("cluster", "another-cluster", "missing the exact cluster label"),
        ("owner", "another-cluster", "not controlled by RayCluster/ray"),
        ("owner_uid", "stale-owner", "not controlled by RayCluster/ray"),
    ],
)
def test_ray_pod_discovery_rejects_non_owned_pods(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: str,
    message: str,
) -> None:
    pod = _ray_pod("ray-head", "head", "head-uid")
    metadata = cast(dict[str, Any], pod["metadata"])
    if field == "cluster":
        labels = cast(dict[str, Any], metadata["labels"])
        labels[RAY_CLUSTER_LABEL] = replacement
    elif field == "owner":
        owners = cast(list[dict[str, Any]], metadata["ownerReferences"])
        owners[0]["name"] = replacement
    else:
        owners = cast(list[dict[str, Any]], metadata["ownerReferences"])
        owners[0]["uid"] = replacement
    calls: list[tuple[str, ...]] = []
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="cluster-owner")), "", 0)
        return CommandResult(json.dumps({"items": [pod]}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match=message):
        gate._ray_pods()

    assert calls == [
        ("get", "raycluster", RAY_CLUSTER_NAME, "-o", "json"),
        (
            "get",
            "pods",
            "-o",
            "json",
        ),
    ]


def test_explicit_ray_skip_never_deletes_a_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = LocalKubeRayGate(_config(ray_restart="skip"))
    _set_ray_topology(gate)
    pods = [_ray_pod("ray-head", "head", "head"), _ray_pod("ray-worker", "worker", "worker")]
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        gate,
        "_ray_pods",
        lambda **kwargs: ("cluster-owner", []),
    )
    monkeypatch.setattr(gate, "_wait_for_ray", lambda **kwargs: pods)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: calls.append(args) or CommandResult("", "", 0),
    )

    gate._prepare_ray()

    assert calls == []
    assert gate.evidence.ray_restart == "skipped-by-explicit-trigger-choice"


def test_ray_wait_handles_the_controller_creation_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate)
    calls: list[tuple[str, ...]] = []
    head = _ray_pod("ray-head", "head", "head")
    worker = _ray_pod("ray-worker", "worker", "worker")
    batches = iter(
        [
            ("cluster-owner", []),
            ("cluster-owner", [head]),
            ("cluster-owner", [head, worker]),
            ("cluster-owner", [head, worker]),
        ]
    )
    monkeypatch.setattr("scripts.local_kuberay_gate.time.sleep", lambda seconds: None)
    monkeypatch.setattr(gate, "_ray_pods", lambda **kwargs: next(batches))

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        return CommandResult("", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    assert gate._wait_for_ray(cluster_uid="cluster-owner") == [head, worker]

    assert [call[0] for call in calls] == ["wait", "wait"]


def test_ray_wait_rejects_underprovisioned_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = LocalKubeRayGate(_config())
    _set_ray_topology(gate, workers=4)
    pods = [_ray_pod("ray-head", "head", "head"), _ray_pod("ray-worker", "worker", "worker")]
    clock = iter([0.0, 301.0])
    monkeypatch.setattr(gate, "_ray_pods", lambda **kwargs: ("cluster-owner", pods))
    monkeypatch.setattr("scripts.local_kuberay_gate.time.monotonic", lambda: next(clock))

    with pytest.raises(ValueError, match=r"did not reach exact topology.*worker:worker-group.*4"):
        gate._wait_for_ray(cluster_uid="cluster-owner")


def test_ray_pod_discovery_rejects_cluster_uid_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        if args[:3] == ("get", "raycluster", RAY_CLUSTER_NAME):
            return CommandResult(json.dumps(_ray_cluster(uid="new-owner")), "", 0)
        return CommandResult(json.dumps({"items": []}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="UID changed from old-owner to new-owner"):
        gate._ray_pods(expected_cluster_uid="old-owner", allow_empty=True)


def test_task_manager_restart_is_exact_and_web_is_only_waited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: calls.append(args) or CommandResult("", "", 0),
    )

    gate._restart_task_managers()

    assert calls[0] == (
        "rollout",
        "restart",
        *(f"deployment/{name}" for name in TASK_MANAGER_DEPLOYMENTS),
    )
    assert all("postgres" not in call and "pvc" not in call for call in calls)
    assert {call[2] for call in calls[1:]} == {f"deployment/{name}" for name in APP_DEPLOYMENTS}


def test_every_kubectl_command_carries_pinned_routing_and_sanitized_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.redactor = Redactor()
            self.args: list[tuple[str, ...]] = []
            self.timeouts: list[float | None] = []
            self.environments: list[dict[str, str] | None] = []

        def run(
            self,
            args: list[str] | tuple[str, ...],
            *,
            cwd: Path,
            input_text: str | None = None,
            check: bool = True,
            timeout: float | None = None,
            sensitive_output: bool = False,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            self.args.append(tuple(args))
            self.timeouts.append(timeout)
            self.environments.append(env)
            return CommandResult("{}", "", 0)

    runner = RecordingRunner()
    gate = LocalKubeRayGate(_config(), runner=runner)  # type: ignore[arg-type]
    payload = json.dumps(_kubeconfig_payload()).encode()
    gate.kubeconfig_path = ROOT / "tests/unit/fixtures/nonexistent-kubeconfig.json"
    gate._kubeconfig_digest = hashlib.sha256(payload).hexdigest()
    gate._kubernetes_server = "https://kubernetes.docker.internal:6443"

    # Keep this command-construction test independent of filesystem snapshot validation.
    gate._verify_kubeconfig_snapshot = lambda: None  # type: ignore[method-assign]
    for key in gate_module.KUBECTL_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, f"hostile-{key.lower()}")
    monkeypatch.setenv("DJANGO_RAY_ENV_MARKER", "preserved")

    gate._kubectl("get", "pods")
    gate._kubectl_cluster("get", "customresourcedefinitions")

    assert runner.args == [
        (
            "kubectl",
            "--kubeconfig",
            str(gate.kubeconfig_path),
            "--context",
            "docker-desktop",
            "--request-timeout=30s",
            "--namespace",
            EXPECTED_NAMESPACE,
            "get",
            "pods",
        ),
        (
            "kubectl",
            "--kubeconfig",
            str(gate.kubeconfig_path),
            "--context",
            "docker-desktop",
            "--request-timeout=30s",
            "get",
            "customresourcedefinitions",
        ),
    ]
    assert runner.timeouts == [120, 120]
    for environment in runner.environments:
        assert environment is not None
        assert not ({key.upper() for key in environment} & gate_module.KUBECTL_ENVIRONMENT_KEYS)
        assert environment["DJANGO_RAY_ENV_MARKER"] == "preserved"


def test_ambient_context_changes_cannot_redirect_pinned_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.redactor = Redactor()
            self.args: list[tuple[str, ...]] = []
            self.environments: list[dict[str, str] | None] = []

        def run(self, args: list[str], **kwargs: object) -> CommandResult:
            self.args.append(tuple(args))
            self.environments.append(cast(dict[str, str] | None, kwargs.get("env")))
            return CommandResult("{}", "", 0)

    snapshot = json.dumps(_kubeconfig_payload()).encode()
    snapshot_path = tmp_path / "kubeconfig.json"
    snapshot_path.write_bytes(snapshot)
    runner = RecordingRunner()
    gate = LocalKubeRayGate(_config(), runner=runner)  # type: ignore[arg-type]
    gate.kubeconfig_path = snapshot_path
    gate._kubeconfig_digest = hashlib.sha256(snapshot).hexdigest()
    gate._kubernetes_server = "https://kubernetes.docker.internal:6443"
    gate._docker_host = "npipe:////./pipe/dockerDesktopLinuxEngine"
    monkeypatch.setenv("DOCKER_HOST", "tcp://production.example.invalid:2376")
    monkeypatch.setenv("DOCKER_CONTEXT", "production")
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "BUILDX_BUILDER",
        "BUILDKIT_HOST",
    ):
        monkeypatch.setenv(key, f"hostile-{key.lower()}")

    gate._kubectl("get", "pods")
    gate._docker("info")

    assert runner.args[0][:5] == (
        "kubectl",
        "--kubeconfig",
        str(snapshot_path),
        "--context",
        "docker-desktop",
    )
    assert runner.args[1] == (
        "docker",
        "--host",
        "npipe:////./pipe/dockerDesktopLinuxEngine",
        "info",
    )
    assert runner.environments[0] is not None
    assert not (
        {key.upper() for key in runner.environments[0]} & gate_module.KUBECTL_ENVIRONMENT_KEYS
    )
    assert runner.environments[1] is not None
    assert not (
        {key.upper() for key in runner.environments[1]} & gate_module.DOCKER_ENVIRONMENT_KEYS
    )

    snapshot_path.write_text(json.dumps(_kubeconfig_payload(context="production")))
    with pytest.raises(ValueError, match="snapshot changed"):
        gate._kubectl("get", "pods")


def test_released_v040_image_build_preserves_source_and_aligns_ray(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gate = LocalKubeRayGate(_config())
    released_context = tmp_path / "released-v040-source"
    released_context.mkdir()
    dockerfile = released_context / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    (released_context / "Dockerfile.dockerignore").write_text("*\n!src/\n", encoding="utf-8")
    gate.released_v040_source_context = released_context
    gate.rendered_ray_topology = normalize_ray_topology(cast(dict[str, Any], _ray_cluster()))
    gate.evidence.released_v040_image_tag = "django-ray-released-v040:test-source"
    gate._ray_image_python_version = "3.12.12"
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(gate, "_verify_released_v040_source_identity", lambda: None)

    def docker(*args: str, **_kwargs: object) -> CommandResult:
        calls.append(args)
        if args[0] == "run":
            return CommandResult("3.12.12\n", "", 0)
        if args[:2] != ("image", "inspect"):
            return CommandResult("", "", 0)
        return CommandResult(
            json.dumps(
                [
                    {
                        "Id": IMAGE_ID,
                        "RepoTags": [gate.evidence.released_v040_image_tag],
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": (
                                    gate_module.RELEASED_V040_COMMIT
                                ),
                                "org.opencontainers.image.source-tree": (
                                    gate_module.RELEASED_V040_SOURCE_TREE
                                ),
                            }
                        },
                    }
                ]
            ),
            "",
            0,
        )

    monkeypatch.setattr(gate, "_docker", docker)

    gate._build_released_v040_image()

    build_call, inspect_call, verify_call = calls
    build_arg_index = build_call.index("--build-arg")
    assert build_call[build_arg_index + 1] == "PYTHON_VERSION=3.12.12"
    assert build_call[:build_arg_index] + build_call[build_arg_index + 2 :] == (
        "build",
        "--tag",
        gate.evidence.released_v040_image_tag,
        "--label",
        f"org.opencontainers.image.revision={gate_module.RELEASED_V040_COMMIT}",
        "--label",
        f"org.opencontainers.image.source-tree={gate_module.RELEASED_V040_SOURCE_TREE}",
        "--file",
        str(tmp_path / "released-v040-handoff.Dockerfile"),
        str(released_context),
    )
    assert dockerfile.read_text(encoding="utf-8") == "FROM scratch\n"
    handoff_dockerfile = (tmp_path / "released-v040-handoff.Dockerfile").read_text(encoding="utf-8")
    assert '--no-cache-dir "ray[default]==2.60.0" && python -m pip check' in handoff_dockerfile
    assert "--no-deps" not in handoff_dockerfile
    assert (tmp_path / "released-v040-handoff.Dockerfile.dockerignore").read_text() == "*\n!src/\n"
    assert inspect_call == ("image", "inspect", gate.evidence.released_v040_image_tag)
    assert verify_call == (
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--entrypoint",
        "python",
        "--",
        gate.evidence.released_v040_image_tag,
        "-I",
        "-S",
        "-B",
        "-c",
        'import sys; print(".".join(map(str, sys.version_info[:3])))',
    )
    assert gate.evidence.released_v040_image_id == IMAGE_ID


def test_image_builds_discover_one_ray_python_and_align_only_application_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gate = LocalKubeRayGate(_config())
    current_context = tmp_path / "current-source"
    released_context = tmp_path / "released-v040-source"
    current_context.mkdir()
    released_context.mkdir()
    (released_context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (released_context / "Dockerfile.dockerignore").write_text("*\n!src/\n", encoding="utf-8")
    gate.source_context = current_context
    gate.released_v040_source_context = released_context
    gate.rendered_ray_topology = normalize_ray_topology(cast(dict[str, Any], _ray_cluster()))
    gate.evidence.commit = COMMIT
    gate.evidence.source_tree = SOURCE_TREE
    gate.evidence.app_tag = APP_TAG
    gate.evidence.worker_tag = f"django-ray-worker:{TAG}"
    gate.evidence.released_v040_image_tag = f"django-ray-released-v040:{TAG}"
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(gate, "_verify_source_identity", lambda: None)
    monkeypatch.setattr(gate, "_verify_released_v040_source_identity", lambda: None)

    def docker(*args: str, **_kwargs: object) -> CommandResult:
        calls.append(args)
        if args[0] == "run":
            return CommandResult("3.12.12\n", "", 0)
        if args[:2] != ("image", "inspect"):
            return CommandResult("", "", 0)
        tag = args[2]
        if tag == gate.evidence.released_v040_image_tag:
            commit = gate_module.RELEASED_V040_COMMIT
            source_tree = gate_module.RELEASED_V040_SOURCE_TREE
        else:
            commit = COMMIT
            source_tree = SOURCE_TREE
        return CommandResult(
            json.dumps(
                [
                    {
                        "Id": IMAGE_ID,
                        "RepoTags": [tag],
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": commit,
                                "org.opencontainers.image.source-tree": source_tree,
                            }
                        },
                    }
                ]
            ),
            "",
            0,
        )

    monkeypatch.setattr(gate, "_docker", docker)

    gate._build_images()

    discovery = (
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--entrypoint",
        "python",
        "--",
        "rayproject/ray:2.60.0-py312",
        "-I",
        "-S",
        "-B",
        "-c",
        'import sys; print(".".join(map(str, sys.version_info[:3])))',
    )
    assert calls.count(discovery) == 1
    assert calls.index(discovery) < next(
        index for index, call in enumerate(calls) if call[0] == "build"
    )
    assert gate._ray_image_python_version == "3.12.12"

    python_probes = [call for call in calls if call[0] == "run"]
    assert len(python_probes) == 3
    assert {call[call.index("--") + 1] for call in python_probes} == {
        "rayproject/ray:2.60.0-py312",
        gate.evidence.app_tag,
        gate.evidence.released_v040_image_tag,
    }

    builds = [call for call in calls if call[0] == "build"]
    assert len(builds) == 3
    application_builds = [
        call for call in builds if Path(call[call.index("--file") + 1]).name != "Dockerfile.ray"
    ]
    assert len(application_builds) == 2
    assert {Path(call[call.index("--file") + 1]) for call in application_builds} == {
        current_context / "Dockerfile",
        released_context.parent / "released-v040-handoff.Dockerfile",
    }
    for call in application_builds:
        assert call.count("--build-arg") == 1
        build_arg_index = call.index("--build-arg")
        assert call[build_arg_index + 1] == "PYTHON_VERSION=3.12.12"

    worker_build = next(
        call for call in builds if Path(call[call.index("--file") + 1]).name == "Dockerfile.ray"
    )
    assert "--build-arg" not in worker_build
    assert all(not value.startswith("PYTHON_VERSION=") for value in worker_build)
