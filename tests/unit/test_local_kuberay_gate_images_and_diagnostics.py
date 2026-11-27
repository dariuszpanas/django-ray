"""Unit tests for local KubeRay gate image builds, redaction, runtime_env, and diagnostics."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import pytest

from scripts import local_kuberay_gate as gate_module
from scripts.local_kuberay_gate import (
    APP_DEPLOYMENTS,
    EXPECTED_NAMESPACE,
    EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES,
    MAX_OUTPUT_CHARACTERS,
    RUNTIME_ENV_ENCRYPTION_PROBE_PATH,
    RUNTIME_ENV_FAILURE_FIXTURE_SCRIPT,
    RUNTIME_ENV_STORAGE_PROBE_MARKER,
    CommandResult,
    GateConfig,
    GateError,
    LocalKubeRayGate,
    Redactor,
    normalize_ray_topology,
    secret_data_sha256,
)
from tests.unit._local_kuberay_gate_helpers import (
    APP_TAG,
    COMMIT,
    IMAGE_ID,
    ROOT,
    RUNTIME_ENV_CANARY_TASK_ID,
    RUNTIME_ENV_TAMPER_TASK_ID,
    RUNTIME_ENV_UNKNOWN_KEY_TASK_ID,
    SOURCE_TREE,
    TAG,
    TASK_ID,
    TOKEN68,
    _bounded_poll_metadata,
    _config,
    _execution_protocol_metrics,
    _kubeconfig_payload,
    _percent_hex_case_variants,
    _ray_cluster,
    _runtime_env_encryption_resources,
    _runtime_env_envelope,
    _task_status_payload,
    _token_representations,
)


def test_kind_load_uses_only_the_pinned_docker_endpoint_and_sanitized_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.redactor = Redactor()
            self.calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

        def run(self, args: list[str], **kwargs: object) -> CommandResult:
            self.calls.append((tuple(args), cast(dict[str, str] | None, kwargs.get("env"))))
            return CommandResult("", "", 0)

    runner = RecordingRunner()
    gate = LocalKubeRayGate(
        _config(context="kind-local", kind_cluster_name="local"),
        runner=runner,  # type: ignore[arg-type]
    )
    gate.source_context = tmp_path
    gate.released_v040_source_context = tmp_path
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (tmp_path / "Dockerfile.dockerignore").write_text("*\n!src/\n", encoding="utf-8")
    gate._docker_host = "unix:///var/run/docker.sock"
    gate.evidence.commit = COMMIT
    gate.evidence.source_tree = SOURCE_TREE
    gate.evidence.app_tag = APP_TAG
    gate.evidence.worker_tag = f"django-ray-worker:{TAG}"
    gate.evidence.released_v040_image_tag = f"django-ray-released-v040:{TAG}"
    gate.rendered_ray_topology = normalize_ray_topology(cast(dict[str, Any], _ray_cluster()))
    gate.resources = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "django-ray-config"},
            "data": {},
        }
    ]
    monkeypatch.setattr(gate, "_verify_source_identity", lambda: None)
    monkeypatch.setattr(gate, "_verify_released_v040_source_identity", lambda: None)
    for key in gate_module.KIND_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, f"hostile-{key.lower()}")

    def docker(*args: str, **kwargs: object) -> CommandResult:
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

    kind_args, environment = runner.calls[-1]
    assert kind_args[:3] == ("kind", "load", "docker-image")
    assert kind_args[3:6] == (
        gate.evidence.app_tag,
        gate.evidence.worker_tag,
        gate.evidence.released_v040_image_tag,
    )
    assert environment is not None
    assert environment["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert environment["KIND_EXPERIMENTAL_PROVIDER"] == "docker"
    assert not (
        ({key.upper() for key in environment} - {"DOCKER_HOST", "KIND_EXPERIMENTAL_PROVIDER"})
        & gate_module.KIND_ENVIRONMENT_KEYS
    )


def test_token_redactor_covers_diagnostics_and_evidence_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = TOKEN68
    encoded = base64.b64encode(token.encode()).decode()
    representations = _token_representations(token)
    output: list[str] = []
    gate = LocalKubeRayGate(_config(), output=output.append)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(
            json.dumps({"data": {"DJANGO_API_TOKEN": encoded}}), "", 0
        ),
    )
    assert gate._secret_token() == token
    gate.mutated = True
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(
            "\n".join(f"line {index} {' | '.join(representations)}" for index in range(200)),
            "",
            0,
        ),
    )

    gate.diagnostics("api-smoke")

    serialized = "\n".join(output)
    assert all(representation not in serialized for representation in representations)
    assert token[:24] not in serialized
    assert "[REDACTED]" in serialized
    assert len(serialized.splitlines()) <= 1 + 3 * 80


def test_diagnostics_redact_before_bounding_output(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "0123456789abcdefghijKLMNOPQRSTuvwxyzABCD"
    output: list[str] = []
    gate = LocalKubeRayGate(_config(), output=output.append)
    gate.redactor.register(token)
    gate.mutated = True
    diagnostic = ("p" * 100) + token + ("z" * (MAX_OUTPUT_CHARACTERS - 20))
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(diagnostic, "", 0),
    )

    gate.diagnostics("setup")

    serialized = "\n".join(output)
    assert token[20:] not in serialized
    assert "[REDACTED]" in serialized


def test_every_evidence_field_passes_through_the_token_redactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "evidence-token-that-must-never-be-printed-123456"
    output: list[str] = []
    gate = LocalKubeRayGate(_config(), output=output.append)
    gate.redactor.register(token)
    monkeypatch.setattr(gate, "_verify_final_identity", lambda: None)
    evidence = gate.evidence
    for field_name in (
        "commit",
        "source_tree",
        "kubeconfig_sha256",
        "kubernetes_server",
        "docker_host",
        "app_tag",
        "worker_tag",
        "released_v040_image_tag",
        "app_image_id",
        "worker_image_id",
        "released_v040_image_id",
        "setup_bundle_sha256",
        "recovery_bundle_sha256",
        "ray_restart",
        "ray_cluster_uid",
        "ray_pod_identity_sha256",
        "task_id",
        "task_state",
        "workflow_task_id",
        "workflow_task_state",
        "workflow_availability",
        "workflow_failure_task_id",
        "workflow_failure_task_state",
        "workflow_failure_availability",
        "workflow_terminal_only_task_id",
        "workflow_terminal_only_task_state",
        "workflow_terminal_only_failure_task_id",
        "workflow_terminal_only_failure_task_state",
        "workflow_showcase_task_id",
        "workflow_showcase_task_state",
        "workflow_showcase_failure_task_id",
        "workflow_showcase_failure_task_state",
        "workflow_recovery_task_id",
        "workflow_recovery_task_state",
    ):
        setattr(evidence, field_name, token)
    evidence.setup_bundle_bytes = cast(Any, token)
    evidence.recovery_bundle_bytes = cast(Any, token)
    evidence.ray_head_count = cast(Any, token)
    evidence.ray_worker_count = cast(Any, token)
    evidence.web_restart_count = cast(Any, token)
    evidence.task_result = token
    evidence.api_task_status_bounded = cast(Any, token)
    evidence.api_workflow_runtime_polls_bounded = cast(Any, token)
    evidence.api_bulk_reset_absent = cast(Any, token)
    evidence.api_legacy_workflow_node_absent = cast(Any, token)
    evidence.api_execution_delete_rejected = cast(Any, token)
    evidence.api_legacy_workflow_graph_absent = cast(Any, token)
    evidence.runtime_env_encryption_overlay = cast(Any, token)
    evidence.runtime_env_encryption_canary = cast(Any, token)
    evidence.runtime_env_encryption_envelope = cast(Any, token)
    evidence.runtime_env_encryption_marker_absent = cast(Any, token)
    evidence.runtime_env_encryption_tamper_rejected = cast(Any, token)
    evidence.runtime_env_encryption_unknown_key_rejected = cast(Any, token)
    evidence.runtime_env_encryption_retry_preserved = cast(Any, token)
    evidence.runtime_env_encryption_logs_clear = cast(Any, token)
    evidence.django_ray_secret_preserved = cast(Any, token)
    evidence.ray_job_request_reference_carrier = cast(Any, token)
    evidence.ray_job_raw_info_clear = cast(Any, token)
    evidence.ray_job_processes_clear = cast(Any, token)
    evidence.ray_job_logs_clear = cast(Any, token)
    evidence.ray_job_manager_reconciled_same_job = cast(Any, token)
    evidence.ray_job_manager_no_resubmit = cast(Any, token)
    evidence.ray_job_missing_reference_no_marker = cast(Any, token)
    evidence.ray_job_missing_reference_no_retry = cast(Any, token)
    evidence.protocol_legacy_cohort_visible = cast(Any, token)
    evidence.protocol_explicit_cohort_visible = cast(Any, token)
    evidence.protocol_legacy_inline_refused = cast(Any, token)
    evidence.protocol_legacy_inline_no_marker = cast(Any, token)
    evidence.protocol_legacy_inline_single_attempt = cast(Any, token)
    evidence.protocol_v1_queued_survived_handoff = cast(Any, token)
    evidence.protocol_v2_queued_unchanged = cast(Any, token)
    evidence.protocol_v2_unsupported_visible = cast(Any, token)
    evidence.protocol_v2_preinvocation_rejected = cast(Any, token)
    evidence.protocol_v2_application_marker_absent = cast(Any, token)
    evidence.protocol_v2_target_exact_completed = cast(Any, token)
    evidence.protocol_v2_target_mismatch_rejected = cast(Any, token)
    evidence.protocol_v2_target_mismatch_marker_absent = cast(Any, token)
    evidence.protocol_handoff_cleanup_restored = cast(Any, token)
    evidence.workflow_schema_version = cast(Any, token)
    evidence.workflow_attempt_number = cast(Any, token)
    evidence.workflow_topology_nodes = cast(Any, token)
    evidence.workflow_topology_edges = cast(Any, token)
    evidence.workflow_node_details = cast(Any, token)
    evidence.workflow_leaf_tasks = cast(Any, token)
    evidence.workflow_admin_routes = cast(Any, token)
    evidence.workflow_admin_actions = cast(Any, token)
    evidence.workflow_current_manifests = cast(Any, token)
    evidence.workflow_pending_manifests = cast(Any, token)
    evidence.workflow_unlinked_pages = cast(Any, token)
    evidence.workflow_failure_attempt_number = cast(Any, token)
    evidence.workflow_failure_schema_version = cast(Any, token)
    evidence.workflow_failure_topology_nodes = cast(Any, token)
    evidence.workflow_failure_topology_edges = cast(Any, token)
    evidence.workflow_failure_node_details = cast(Any, token)
    evidence.workflow_failure_leaf_tasks = cast(Any, token)
    evidence.workflow_failure_pending_nodes = cast(Any, token)
    evidence.workflow_failure_running_nodes = cast(Any, token)
    evidence.workflow_failure_succeeded_nodes = cast(Any, token)
    evidence.workflow_failure_failed_nodes = cast(Any, token)
    evidence.workflow_failure_path_nodes = cast(Any, token)
    evidence.workflow_failure_origins = cast(Any, token)
    evidence.workflow_failure_incoming_edges = cast(Any, token)
    evidence.workflow_failure_admin_routes = cast(Any, token)
    evidence.workflow_failure_admin_actions = cast(Any, token)
    evidence.workflow_failure_current_manifests = cast(Any, token)
    evidence.workflow_failure_pending_manifests = cast(Any, token)
    evidence.workflow_failure_unlinked_pages = cast(Any, token)
    evidence.workflow_terminal_only_attempt_number = cast(Any, token)
    evidence.workflow_terminal_only_schema_version = cast(Any, token)
    evidence.workflow_terminal_only_summary_revision = cast(Any, token)
    evidence.workflow_terminal_only_declared_nodes = cast(Any, token)
    evidence.workflow_terminal_only_declared_edges = cast(Any, token)
    evidence.workflow_terminal_only_admin_actions = cast(Any, token)
    evidence.workflow_terminal_only_graph_advertised = cast(Any, token)
    evidence.workflow_terminal_only_storage_rows = cast(Any, token)
    evidence.workflow_terminal_only_failure_attempt_number = cast(Any, token)
    evidence.workflow_terminal_only_failure_schema_version = cast(Any, token)
    evidence.workflow_terminal_only_failure_summary_revision = cast(Any, token)
    evidence.workflow_terminal_only_failure_declared_nodes = cast(Any, token)
    evidence.workflow_terminal_only_failure_declared_edges = cast(Any, token)
    evidence.workflow_terminal_only_failure_admin_actions = cast(Any, token)
    evidence.workflow_terminal_only_failure_graph_advertised = cast(Any, token)
    evidence.workflow_terminal_only_failure_storage_rows = cast(Any, token)
    evidence.workflow_showcase_attempt_number = cast(Any, token)
    evidence.workflow_showcase_topology_nodes = cast(Any, token)
    evidence.workflow_showcase_topology_edges = cast(Any, token)
    evidence.workflow_showcase_longest_path_layers = cast(Any, token)
    evidence.workflow_showcase_detail_links = cast(Any, token)
    evidence.workflow_showcase_failure_attempt_number = cast(Any, token)
    evidence.workflow_showcase_failure_failed_nodes = cast(Any, token)
    evidence.workflow_showcase_failure_pending_descendants = cast(Any, token)
    evidence.workflow_showcase_failure_running_nodes = cast(Any, token)
    evidence.workflow_showcase_failure_succeeded_nodes = cast(Any, token)
    evidence.workflow_showcase_failure_path_nodes = cast(Any, token)
    evidence.workflow_showcase_failure_detail_links = cast(Any, token)
    evidence.workflow_recovery_attempt_number = cast(Any, token)
    evidence.workflow_recovery_attempt_count = cast(Any, token)
    evidence.workflow_recovery_distinct_runs = cast(Any, token)
    evidence.workflow_recovery_early_topology_nodes = cast(Any, token)
    evidence.workflow_recovery_early_topology_edges = cast(Any, token)
    evidence.workflow_recovery_early_pending_nodes = cast(Any, token)
    evidence.workflow_recovery_early_succeeded_nodes = cast(Any, token)
    evidence.workflow_recovery_early_failed_nodes = cast(Any, token)
    evidence.workflow_recovery_early_detail_links = cast(Any, token)
    evidence.workflow_recovery_mid_topology_nodes = cast(Any, token)
    evidence.workflow_recovery_mid_topology_edges = cast(Any, token)
    evidence.workflow_recovery_mid_pending_nodes = cast(Any, token)
    evidence.workflow_recovery_mid_succeeded_nodes = cast(Any, token)
    evidence.workflow_recovery_mid_failed_nodes = cast(Any, token)
    evidence.workflow_recovery_mid_detail_links = cast(Any, token)
    evidence.workflow_recovery_success_topology_nodes = cast(Any, token)
    evidence.workflow_recovery_success_topology_edges = cast(Any, token)
    evidence.workflow_recovery_success_succeeded_nodes = cast(Any, token)
    evidence.workflow_recovery_success_detail_links = cast(Any, token)
    evidence.workflow_recovery_admin_attempts = cast(Any, token)
    evidence.deployments = cast(dict[str, int], dict.fromkeys(APP_DEPLOYMENTS, token))
    evidence.prometheus_counts = cast(
        dict[str, int], dict.fromkeys(("django-ray", "ray-head", "ray-workers"), token)
    )

    gate._emit_evidence()

    serialized = "\n".join(output)
    assert token not in serialized
    assert serialized.count("[REDACTED]") >= 95


def test_secret_token_is_decoded_in_memory_and_registered_for_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = TOKEN68
    payload = json.dumps({"data": {"DJANGO_API_TOKEN": base64.b64encode(token.encode()).decode()}})
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(payload, "", 0),
    )

    assert gate._secret_token() == token
    assert gate.redactor.clean(f"Bearer {token}") == "Bearer [REDACTED]"
    assert gate.redactor.clean(payload) == '{"data": {"DJANGO_API_TOKEN": "[REDACTED]"}}'
    assert all(
        gate.redactor.clean(variant) == "[REDACTED]"
        for variant in _percent_hex_case_variants(quote(token, safe=""))
    )


def test_secret_token_decode_failure_has_no_private_exception_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_bytes = b"\xffprivate-token-bytes-that-must-not-enter-the-exception-graph"
    encoded = base64.b64encode(private_bytes).decode()
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(
        gate,
        "_secret_data",
        lambda: {"DJANGO_API_TOKEN": encoded},
    )

    with pytest.raises(ValueError, match="base64-encoded UTF-8") as captured:
        gate._secret_token()

    assert encoded not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_secret_preservation_compares_the_complete_data_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = base64.b64encode(TOKEN68.encode()).decode()
    baseline = {
        "DJANGO_API_TOKEN": encoded,
        "DJANGO_SECRET_KEY": base64.b64encode(b"django-secret").decode(),
        "DATABASE_PASSWORD": base64.b64encode(b"database-secret").decode(),
    }
    gate = LocalKubeRayGate(_config())
    responses = iter((baseline, dict(reversed(tuple(baseline.items())))))
    monkeypatch.setattr(gate, "_secret_data", lambda: next(responses))

    assert gate._secret_token() == TOKEN68
    gate._verify_preserved_secret()

    assert gate.evidence.django_ray_secret_preserved is True
    assert gate._secret_data_sha256 == secret_data_sha256(baseline)


def test_secret_preservation_rejects_any_data_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = base64.b64encode(TOKEN68.encode()).decode()
    baseline = {
        "DJANGO_API_TOKEN": encoded,
        "DJANGO_SECRET_KEY": base64.b64encode(b"django-secret").decode(),
    }
    changed = {
        **baseline,
        "DJANGO_SECRET_KEY": base64.b64encode(b"changed-secret").decode(),
    }
    gate = LocalKubeRayGate(_config())
    responses = iter((baseline, changed))
    monkeypatch.setattr(gate, "_secret_data", lambda: next(responses))

    gate._secret_token()
    with pytest.raises(ValueError, match="data changed"):
        gate._verify_preserved_secret()

    assert gate.evidence.django_ray_secret_preserved is False


def test_component_secrets_are_redacted_and_preserved_as_complete_mappings(monkeypatch) -> None:
    gate = LocalKubeRayGate(_config())
    operator_data = {"DJANGO_API_TOKEN": base64.b64encode(TOKEN68.encode()).decode()}
    component_data = {
        name: {
            "COMPONENT_TOKEN": base64.b64encode(
                f"random-{name}-credential-value-1234567890".encode()
            ).decode()
        }
        for name in gate_module.COMPONENT_SECRET_NAMES
    }
    monkeypatch.setattr(gate, "_secret_data", lambda: operator_data)
    monkeypatch.setattr(gate, "_component_secret_data", lambda name: component_data[name])
    gate._secret_token()
    gate._capture_component_secrets()
    gate._verify_preserved_secret()
    assert set(gate._component_secret_digests) == set(gate_module.COMPONENT_SECRET_NAMES)
    for data in component_data.values():
        encoded = data["COMPONENT_TOKEN"]
        value = base64.b64decode(encoded).decode()
        assert value not in gate.redactor.clean(value)
        assert encoded not in gate.redactor.clean(encoded)
    component_data["django-ray-auth"]["UNEXPECTED_NEW_KEY"] = base64.b64encode(b"changed").decode()
    with pytest.raises(ValueError, match="component Secret data changed"):
        gate._verify_preserved_secret()


@pytest.mark.parametrize("encoded", [None, "invalid base64", base64.b64encode(b"short").decode()])
def test_demo_enqueue_rejects_missing_or_invalid_separate_credentials(monkeypatch, encoded) -> None:
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(gate, "_secret_token", lambda: TOKEN68)
    monkeypatch.setattr(gate, "_component_secret_data", lambda name: {"DJANGO_DEMO_TOKEN": encoded})
    with pytest.raises(ValueError, match="demo Secret"):
        gate._workflow_enqueue_headers(gate_module.COMPLEX_WORKFLOW_ENQUEUE_PATH)


def test_demo_enqueue_refuses_the_operator_credential(monkeypatch) -> None:
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(gate, "_secret_token", lambda: TOKEN68)
    monkeypatch.setattr(
        gate,
        "_component_secret_data",
        lambda name: {"DJANGO_DEMO_TOKEN": base64.b64encode(TOKEN68.encode()).decode()},
    )
    with pytest.raises(ValueError, match="must be distinct"):
        gate._workflow_enqueue_headers(gate_module.COMPLEX_WORKFLOW_ENQUEUE_PATH)


@pytest.mark.parametrize(
    "token",
    [
        "local-token-that-must-never-be-printed-123456",
        "A" * 32,
        "A" * 31 + "=",
        "A" * 30 + "==",
        "A" * 510 + "==",
        "A" * 511 + "=",
        "A" * 512,
        TOKEN68,
    ],
)
def test_secret_token_accepts_strict_token68_values(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    encoded = base64.b64encode(token.encode()).decode()
    gate = LocalKubeRayGate(_config())
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(
            json.dumps({"data": {"DJANGO_API_TOKEN": encoded}}), "", 0
        ),
    )

    assert gate._secret_token() == token


@pytest.mark.parametrize(
    "token",
    [
        "A" * 31,
        "A" * 32 + "\n",
        "A" * 32 + "\t",
        "A" * 32 + " ",
        "A" * 31 + '"',
        "A" * 31 + "\\",
        "A" * 16 + "=" + "A" * 16,
        "A" * 32 + "===",
        "A" * 32 + "â‚¬",
        "A" * 512 + "=",
        "A" * 511 + "==",
        "A" * 513,
    ],
)
def test_secret_token_rejects_unbounded_or_non_header_safe_values(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    encoded = base64.b64encode(token.encode()).decode()
    gate = LocalKubeRayGate(_config())

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        assert kwargs["sensitive_output"] is True
        return CommandResult(json.dumps({"data": {"DJANGO_API_TOKEN": encoded}}), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="Bearer token68 alphabet"):
        gate._secret_token()


def test_prometheus_layer_bounds_and_redacts_a_large_last_error() -> None:
    token = TOKEN68
    encoded = base64.b64encode(token.encode()).decode()
    representations = _token_representations(token)
    gate = LocalKubeRayGate(_config(), output=lambda value: None)
    gate.redactor.register(token)
    gate.redactor.register(encoded)
    last_error = f"{'x' * 20_000}|{'|'.join(representations)}"
    payload = {
        "status": "success",
        "data": {
            "activeTargets": [
                {
                    "labels": {"instance": "django-ray:80", "job": "django-ray"},
                    "health": "down",
                    "lastError": last_error,
                },
                {
                    "labels": {"instance": "ray-head:8080", "job": "ray-head"},
                    "health": "up",
                    "lastError": "",
                },
                {
                    "labels": {"instance": "ray-worker:8080", "job": "ray-workers"},
                    "health": "up",
                    "lastError": "",
                },
            ]
        },
    }

    def fail_with_prometheus_error() -> None:
        gate_module.wait_for_healthy_targets(
            lambda: payload,
            timeout=0,
            interval=1,
        )

    with pytest.raises(GateError) as captured:
        gate._layer("prometheus", fail_with_prometheus_error)

    detail = str(captured.value)
    assert captured.value.layer == "prometheus"
    assert len(detail) <= MAX_OUTPUT_CHARACTERS
    assert detail.startswith("[truncated redacted error; original_characters=")
    assert all(representation not in detail for representation in representations)
    assert token[:24] not in detail
    assert "[REDACTED]" in detail


def test_preflight_registers_secret_before_any_mutation_or_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class PreflightRunner:
        def __init__(self) -> None:
            self.redactor = Redactor()
            self.sensitive_commands: list[tuple[str, ...]] = []
            self.routing_environments: list[tuple[str, dict[str, str] | None]] = []

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
            command = tuple(args)
            if sensitive_output:
                self.sensitive_commands.append(command)
            if command[0] in {"docker", "kubectl"}:
                self.routing_environments.append((command[0], env))
            if command[:3] == ("git", "rev-parse", "--show-toplevel"):
                return CommandResult(str(ROOT), "", 0)
            if command[:2] == ("git", "status"):
                return CommandResult("", "", 0)
            if command == (
                "git",
                "rev-parse",
                "--verify",
                f"{gate_module.RELEASED_V040_COMMIT}^{{commit}}",
            ):
                return CommandResult(gate_module.RELEASED_V040_COMMIT, "", 0)
            if command == (
                "git",
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/tags/{gate_module.RELEASED_V040_TAG}",
            ):
                assert check is False
                return CommandResult("", "", 1)
            if command == (
                "git",
                "rev-parse",
                "--verify",
                f"{gate_module.RELEASED_V040_COMMIT}^{{tree}}",
            ):
                return CommandResult(gate_module.RELEASED_V040_SOURCE_TREE, "", 0)
            if command[:3] == ("git", "rev-parse", "--verify"):
                return CommandResult(SOURCE_TREE if "tree" in command[3] else COMMIT, "", 0)
            if command == ("kubectl", "config", "current-context"):
                return CommandResult("docker-desktop", "", 0)
            if "config" in command and "view" in command:
                return CommandResult(json.dumps(_kubeconfig_payload()), "", 0)
            if command[:3] == ("docker", "context", "show"):
                return CommandResult("desktop-linux", "", 0)
            if command[:3] == ("docker", "context", "inspect"):
                return CommandResult(
                    json.dumps(
                        [
                            {
                                "Endpoints": {
                                    "docker": {"Host": "npipe:////./pipe/dockerDesktopLinuxEngine"}
                                }
                            }
                        ]
                    ),
                    "",
                    0,
                )
            if "info" in command and command[0] == "docker":
                return CommandResult('"28.0.0"', "", 0)
            if "customresourcedefinition" in command:
                return CommandResult("customresourcedefinition/rayclusters.ray.io", "", 0)
            if "clusterrole" in command:
                return CommandResult("clusterrole/prometheus-django-ray", "", 0)
            raise AssertionError(f"unexpected preflight command: {command}")

    runner = PreflightRunner()
    gate = LocalKubeRayGate(_config(), runner=runner)  # type: ignore[arg-type]
    gate.temp_root = tmp_path
    events: list[str] = []
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    for key in gate_module.PROXY_ENVIRONMENT_KEYS | {
        "DOCKER_CONTEXT",
        "DOCKER_TLS_VERIFY",
        "BUILDX_BUILDER",
        "KUBECONFIG",
    }:
        monkeypatch.setenv(key, f"hostile-{key.lower()}")
    monkeypatch.setattr(shutil, "which", lambda executable: f"/bin/{executable}")
    monkeypatch.setattr(
        gate,
        "_secret_token",
        lambda: events.append("secret-registered") or "local-token",
    )
    monkeypatch.setattr(
        gate, "_capture_component_secrets", lambda: events.append("components-registered")
    )

    with pytest.raises(ValueError, match="legacy cluster-scoped"):
        gate._preflight()

    assert events == ["secret-registered", "components-registered"]
    assert gate.mutated is False
    assert len(runner.sensitive_commands) == 1
    assert "config" in runner.sensitive_commands[0]
    assert gate.kubeconfig_path is not None and gate.kubeconfig_path.is_file()
    assert (
        gate.evidence.kubeconfig_sha256
        == hashlib.sha256(gate.kubeconfig_path.read_bytes()).hexdigest()
    )
    assert runner.redactor.clean("kube-token") == "[REDACTED]"
    for executable, environment in runner.routing_environments:
        assert environment is not None
        removed = (
            gate_module.DOCKER_ENVIRONMENT_KEYS
            if executable == "docker"
            else gate_module.KUBECTL_ENVIRONMENT_KEYS
        )
        assert not ({key.upper() for key in environment} & removed)


@pytest.mark.parametrize("standalone", [False, True])
def test_api_smoke_requires_401_200_and_durable_five(
    monkeypatch: pytest.MonkeyPatch, standalone: bool
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    calls: list[tuple[str, str, bool]] = []
    detail_response: dict[str, object] = {
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
    }
    task_status_response = _task_status_payload()
    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        response_limit: int = MAX_OUTPUT_CHARACTERS,
        required_response_headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        authenticated = headers == {"Authorization": f"Bearer {token}"}
        calls.append((path, method, authenticated))
        if path == "/api/openapi.json":
            assert response_limit > MAX_OUTPUT_CHARACTERS
            return 200, json.dumps(
                {
                    "paths": {
                        "/api/executions/{execution_id}": {"get": {"operationId": "get_execution"}},
                        "/api/executions/{execution_id}/retry": {"post": {}},
                        "/api/cluster/workflows/{task_id}/node-detail": {"get": {}},
                    }
                }
            ).encode()
        if not authenticated:
            return 401, b"{}"
        if path == "/api/enqueue/add/2/3":
            return 200, json.dumps({"task_id": TASK_ID}).encode()
        if path == "/api/metrics":
            return 200, _execution_protocol_metrics()
        if path == f"/api/tasks/{TASK_ID}":
            assert response_limit == EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES
            assert required_response_headers == {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            }
            return 200, json.dumps(task_status_response).encode()
        if path == f"/api/executions?task_id={TASK_ID}&limit=1":
            return 200, json.dumps(
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
                }
            ).encode()
        if path == "/api/executions/17" and method == "DELETE":
            return 405, b"{}"
        if path == "/api/executions/17":
            return 200, json.dumps(detail_response).encode()
        return 200, b"{}"

    monkeypatch.setattr(gate, "_http", request)

    def verify():
        if standalone:
            return gate_module.api_assertions.verify_application_api(
                request, get_token=lambda: token, task_timeout=gate.config.task_timeout
            )
        gate._verify_api()
        return gate.evidence

    observations = verify()

    assert observations.task_id == TASK_ID
    assert observations.task_state == "SUCCEEDED"
    assert observations.task_result == 5
    assert observations.api_task_status_bounded is True
    assert observations.api_bulk_reset_absent is True
    assert observations.api_legacy_workflow_node_absent is True
    assert observations.api_execution_delete_rejected is True
    assert observations.api_legacy_workflow_graph_absent is True
    assert calls[:5] == [
        ("/api/enqueue/add/2/3", "POST", False),
        ("/api/executions/stats", "GET", False),
        ("/api/metrics", "GET", False),
        ("/api/executions?limit=1", "GET", False),
        ("/api/tasks/00000000-0000-4000-8000-000000000000", "GET", False),
    ]
    assert calls[-2:] == [
        ("/api/executions/17", "DELETE", True),
        ("/api/executions/17", "GET", True),
    ]

    detail_response["response_max_bytes"] = None
    with pytest.raises(ValueError, match="lost its bounded projection contract"):
        verify()

    detail_response["response_max_bytes"] = 262_144
    task_status_response["input_omission_reason"] = "invented"
    with pytest.raises(ValueError, match="unknown input omission reason"):
        verify()


def test_api_smoke_rejects_an_openapi_execution_delete() -> None:
    gate = LocalKubeRayGate(_config())

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        response_limit: int = MAX_OUTPUT_CHARACTERS,
    ) -> tuple[int, bytes]:
        if path == "/api/openapi.json":
            assert response_limit > MAX_OUTPUT_CHARACTERS
            return 200, json.dumps(
                {
                    "paths": {
                        "/api/executions/{execution_id}": {
                            "get": {},
                            "delete": {},
                        }
                    }
                }
            ).encode()
        return 401, b"{}"

    gate._http = request  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="advertises unsafe DELETE"):
        gate._verify_api()


def test_api_smoke_rejects_the_legacy_workflow_graph_in_openapi() -> None:
    gate = LocalKubeRayGate(_config())

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        response_limit: int = MAX_OUTPUT_CHARACTERS,
    ) -> tuple[int, bytes]:
        if path == "/api/openapi.json":
            assert response_limit > MAX_OUTPUT_CHARACTERS
            return 200, json.dumps(
                {
                    "paths": {
                        "/api/executions/{execution_id}": {"get": {}},
                        "/api/cluster/workflows/{task_id}/graph": {"get": {}},
                    }
                }
            ).encode()
        return 401, b"{}"

    gate._http = request  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="removed legacy workflow graph"):
        gate._verify_api()


@pytest.mark.parametrize(
    ("retired_path", "message"),
    [
        ("/api/executions/reset", "removed bulk execution reset"),
        (
            "/api/cluster/workflows/{task_id}/nodes/{node_id}",
            "removed legacy workflow node route",
        ),
    ],
)
def test_api_smoke_rejects_retired_routes_in_openapi(
    retired_path: str,
    message: str,
) -> None:
    gate = LocalKubeRayGate(_config())

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        response_limit: int = MAX_OUTPUT_CHARACTERS,
    ) -> tuple[int, bytes]:
        if path == "/api/openapi.json":
            return 200, json.dumps(
                {
                    "paths": {
                        "/api/executions/{execution_id}": {"get": {}},
                        retired_path: {"post": {}},
                    }
                }
            ).encode()
        return 401, b"{}"

    gate._http = request  # type: ignore[method-assign]

    with pytest.raises(ValueError, match=message):
        gate._verify_api()


@pytest.mark.parametrize(
    ("missing_path", "message"),
    [
        (
            "/api/executions/{execution_id}/retry",
            "OpenAPI execution retry path must be an object",
        ),
        (
            "/api/cluster/workflows/{task_id}/node-detail",
            "OpenAPI workflow node-detail path must be an object",
        ),
    ],
)
def test_api_smoke_requires_supported_exact_routes_in_openapi(
    missing_path: str,
    message: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    paths = {
        "/api/executions/{execution_id}": {"get": {}},
        "/api/executions/{execution_id}/retry": {"post": {}},
        "/api/cluster/workflows/{task_id}/node-detail": {"get": {}},
    }
    paths.pop(missing_path)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        response_limit: int = MAX_OUTPUT_CHARACTERS,
    ) -> tuple[int, bytes]:
        if path == "/api/openapi.json":
            return 200, json.dumps({"paths": paths}).encode()
        return 401, b"{}"

    gate._http = request  # type: ignore[method-assign]

    with pytest.raises(ValueError, match=message):
        gate._verify_api()


def test_api_smoke_rejects_task_id_evidence_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        response_limit: int = MAX_OUTPUT_CHARACTERS,
    ) -> tuple[int, bytes]:
        if path == "/api/openapi.json":
            assert response_limit > MAX_OUTPUT_CHARACTERS
            return 200, json.dumps(
                {
                    "paths": {
                        "/api/executions/{execution_id}": {"get": {}},
                        "/api/executions/{execution_id}/retry": {"post": {}},
                        "/api/cluster/workflows/{task_id}/node-detail": {"get": {}},
                    }
                }
            ).encode()
        if headers is None:
            return 401, b"{}"
        if path == "/api/metrics":
            return 200, _execution_protocol_metrics()
        if path == "/api/enqueue/add/2/3":
            return 200, b'{"task_id":"forged\\npreserved=everything"}'
        return 200, b"{}"

    monkeypatch.setattr(gate, "_http", request)

    with pytest.raises(ValueError, match="canonical UUID"):
        gate._verify_api()


def _runtime_env_failure_observations() -> dict[str, dict[str, object]]:
    common: dict[str, object] = {
        "state": "FAILED",
        "attempt_number": 1,
        "execution_generation": 1,
        "claimed": True,
        "lifecycle_timestamps": True,
        "no_ray_submission": True,
        "no_result": True,
        "attempts": [{"attempt_number": 1, "state": "FAILED"}],
    }
    return {
        "ciphertext": {
            **common,
            "archive_fingerprint": "a" * 64,
            "authentication_failed": True,
            "key_unavailable": False,
        },
        "key_id": {
            **common,
            "archive_fingerprint": "b" * 64,
            "authentication_failed": False,
            "key_unavailable": True,
        },
    }


def test_private_runtime_env_envelope_inspection_registers_every_raw_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized, nonce, ciphertext = _runtime_env_envelope()
    digest = "d" * 64
    gate = LocalKubeRayGate(_config())
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append((args, kwargs))
        marker_match = re.search(
            r"print\('(django_ray_private_json_complete_v1_[0-9a-f]{32})'\)\n\Z",
            args[-1],
        )
        assert marker_match is not None
        return CommandResult(
            json.dumps(
                {
                    "envelope": serialized,
                    "profile": "thin",
                    "runtime_env_hash": digest,
                }
            )
            + f"\n{marker_match.group(1)}\n",
            "",
            0,
        )

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    gate._inspect_runtime_env_canary_envelope(
        task_id=RUNTIME_ENV_CANARY_TASK_ID,
        profile="thin",
        digest=digest,
    )

    assert calls[0][1]["sensitive_output"] is True
    assert "testproject/manage.py" in calls[0][0]
    no_imports = calls[0][0].index("--no-imports")
    assert calls[0][0][no_imports + 1] == "-c"
    assert gate.evidence.runtime_env_encryption_envelope is True
    assert gate.evidence.runtime_env_encryption_marker_absent is True
    assert gate.redactor.clean(f"{serialized}|{nonce}|{ciphertext}") == (
        "[REDACTED]|[REDACTED]|[REDACTED]"
    )
    assert gate.runner.redactor.clean(serialized) == "[REDACTED]"


def test_failure_fixture_creation_is_one_sensitive_atomic_storage_seam_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    serialized, nonce, ciphertext = _runtime_env_envelope()
    tampered_payload = json.loads(serialized)
    tampered_payload["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    tampered = json.dumps(tampered_payload, sort_keys=True, separators=(",", ":"))
    unknown_payload = json.loads(serialized)
    unknown_payload["key_id"] = "django-ray-gate-unknown"
    unknown = json.dumps(unknown_payload, sort_keys=True, separators=(",", ":"))

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append((args, kwargs))
        marker_match = re.search(
            r"print\('(django_ray_private_json_complete_v1_[0-9a-f]{32})'\)\n\Z",
            args[-1],
        )
        assert marker_match is not None
        return CommandResult(
            json.dumps(
                {
                    "ciphertext": {
                        "id": 11,
                        "task_id": RUNTIME_ENV_TAMPER_TASK_ID,
                        "envelope": tampered,
                        "nonce": nonce,
                        "ciphertext": tampered_payload["ciphertext"],
                    },
                    "key_id": {
                        "id": 12,
                        "task_id": RUNTIME_ENV_UNKNOWN_KEY_TASK_ID,
                        "envelope": unknown,
                        "nonce": nonce,
                        "ciphertext": ciphertext,
                    },
                }
            )
            + f"\n{marker_match.group(1)}\n",
            "",
            0,
        )

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    assert gate._create_runtime_env_failure_fixtures() == {
        "ciphertext": (11, RUNTIME_ENV_TAMPER_TASK_ID),
        "key_id": (12, RUNTIME_ENV_UNKNOWN_KEY_TASK_ID),
    }

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs["sensitive_output"] is True
    no_imports = args.index("--no-imports")
    assert args[no_imports + 1] == "-c"
    marker_match = re.search(
        r"\nprint\('django_ray_private_json_complete_v1_[0-9a-f]{32}'\)\n\Z",
        args[-1],
    )
    assert marker_match is not None
    script = args[-1][: marker_match.start()]
    assert script == RUNTIME_ENV_FAILURE_FIXTURE_SCRIPT
    assert script.count("with transaction.atomic():") == 1
    assert "runtime_env_for_storage(resolved, task_id=task_id)" in script
    assert script.index('envelope["ciphertext"] =') < script.index(
        "RayTaskExecution.objects.create"
    )
    assert script.index('envelope["key_id"] =') < script.index("RayTaskExecution.objects.create")
    assert gate._runtime_env_fixture_values_registered is True
    for value in (
        tampered,
        tampered_payload["ciphertext"],
        unknown,
        nonce,
        ciphertext,
    ):
        assert gate.redactor.clean(value) == "[REDACTED]"


def test_malformed_private_fixture_payload_suppresses_runtime_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_value = "unregistered-mutated-envelope-that-must-remain-private"
    malformed = f'{{"ciphertext":{{"envelope":"{private_value}"'
    output: list[str] = []
    gate = LocalKubeRayGate(_config(), output=output.append)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(malformed, "", 0),
    )

    with pytest.raises(ValueError, match="valid private JSON") as captured:
        gate._create_runtime_env_failure_fixtures()

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert private_value not in str(captured.value)
    assert gate._runtime_env_fixture_values_registered is False

    commands: list[tuple[str, ...]] = []

    def diagnostics_kubectl(*args: str, **kwargs: object) -> CommandResult:
        commands.append(args)
        return CommandResult("ordinary resource status", "", 0)

    monkeypatch.setattr(gate, "_kubectl", diagnostics_kubectl)
    gate.mutated = True
    gate.diagnostics("runtime-env-encryption")

    assert commands == [("get", "pods,deployments,jobs,pvc", "-o", "wide")]
    assert private_value not in "\n".join(output)


def test_runtime_env_failure_invariants_require_pre_ray_permanent_failures() -> None:
    observations = _runtime_env_failure_observations()

    LocalKubeRayGate._validate_runtime_env_failure_invariants(observations)

    observations["ciphertext"]["no_ray_submission"] = False
    with pytest.raises(ValueError, match="lifecycle boundary"):
        LocalKubeRayGate._validate_runtime_env_failure_invariants(observations)


def test_runtime_env_encryption_layer_proves_canary_corruption_and_retry_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.resources = _runtime_env_encryption_resources()
    token = "local-token-that-must-never-be-printed-123456"
    digest = "d" * 64
    fixtures = {
        "ciphertext": (11, RUNTIME_ENV_TAMPER_TASK_ID),
        "key_id": (12, RUNTIME_ENV_UNKNOWN_KEY_TASK_ID),
    }
    calls: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes]:
        authenticated = headers == {"Authorization": f"Bearer {token}"}
        calls.append((path, method, authenticated))
        if path == RUNTIME_ENV_ENCRYPTION_PROBE_PATH:
            return 200, json.dumps({"task_id": RUNTIME_ENV_CANARY_TASK_ID}).encode()
        if path == f"/api/cluster/runtime-env/{RUNTIME_ENV_CANARY_TASK_ID}":
            return 200, json.dumps(
                {
                    "task_id": RUNTIME_ENV_CANARY_TASK_ID,
                    "state": "SUCCEEDED",
                    "runtime_env_profile": "thin",
                    "runtime_env_hash": digest,
                    "result": {"storage_encryption_verified": True},
                    "error": None,
                    **_bounded_poll_metadata(),
                }
            ).encode()
        for execution_id, task_id in fixtures.values():
            if path == f"/api/executions?task_id={task_id}&limit=1":
                return 200, json.dumps(
                    {
                        "tasks": [
                            {
                                "id": execution_id,
                                "task_id": task_id,
                                "state": "FAILED",
                                "attempt_number": 1,
                                "execution_generation": 1,
                                "result_data": None,
                                "runtime_env_profile": "thin",
                            }
                        ]
                    }
                ).encode()
        if path == "/api/executions/11/retry":
            return 409, b'{"detail":"snapshot integrity failure"}'
        raise AssertionError(path)

    monkeypatch.setattr(gate, "_http", request)

    def inspect_envelope(**kwargs: object) -> None:
        assert kwargs == {
            "task_id": RUNTIME_ENV_CANARY_TASK_ID,
            "profile": "thin",
            "digest": digest,
        }
        gate.evidence.runtime_env_encryption_envelope = True
        gate.evidence.runtime_env_encryption_marker_absent = True

    monkeypatch.setattr(gate, "_inspect_runtime_env_canary_envelope", inspect_envelope)
    monkeypatch.setattr(gate, "_create_runtime_env_failure_fixtures", lambda: fixtures)
    observations = _runtime_env_failure_observations()
    monkeypatch.setattr(gate, "_runtime_env_failure_invariants", lambda _fixtures: observations)
    monkeypatch.setattr(
        gate,
        "_verify_runtime_env_logs_clear",
        lambda: setattr(gate.evidence, "runtime_env_encryption_logs_clear", True),
    )

    gate._verify_runtime_env_encryption()

    assert all(authenticated for _, _, authenticated in calls)
    assert calls[0][:2] == (RUNTIME_ENV_ENCRYPTION_PROBE_PATH, "POST")
    assert calls[-1][:2] == ("/api/executions/11/retry", "POST")
    assert gate.evidence.runtime_env_encryption_overlay is True
    assert gate.evidence.runtime_env_encryption_canary is True
    assert gate.evidence.runtime_env_encryption_envelope is True
    assert gate.evidence.runtime_env_encryption_marker_absent is True
    assert gate.evidence.runtime_env_encryption_tamper_rejected is True
    assert gate.evidence.runtime_env_encryption_unknown_key_rejected is True
    assert gate.evidence.runtime_env_encryption_retry_preserved is True
    assert gate.evidence.runtime_env_encryption_logs_clear is True


def test_runtime_env_log_scan_rejects_protected_values_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    serialized, nonce, ciphertext = _runtime_env_envelope()
    for value in (serialized, nonce, ciphertext):
        gate._register_runtime_env_protected_value(value)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(
            f"ordinary log\n{RUNTIME_ENV_STORAGE_PROBE_MARKER}\n",
            "",
            0,
        ),
    )

    with pytest.raises(ValueError, match="protected RuntimeEnv storage value") as captured:
        gate._verify_runtime_env_logs_clear()

    assert RUNTIME_ENV_STORAGE_PROBE_MARKER not in str(captured.value)
    assert serialized not in str(captured.value)
    assert gate.evidence.runtime_env_encryption_logs_clear is False


def test_runtime_env_log_scan_reads_the_complete_bounded_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append(args)
        return CommandResult("ordinary logs", "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    gate._verify_runtime_env_logs_clear()

    assert len(calls) == 2
    assert all("--tail=-1" in call for call in calls)
    assert all("--since=15m" in call for call in calls)
    assert all("--limit-bytes=1048576" in call for call in calls)


def _stub_successful_gate_layers(
    gate: LocalKubeRayGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for method_name in (
        "_preflight",
        "_build_images",
        "_apply_overlay",
        "_run_setup",
        "_apply_workloads",
        "_prepare_ray",
        "_restart_task_managers",
        "_wait_for_application_topology",
        "_verify_deployed_images",
        "_verify_authentication",
        "_recover_protocol_handoff_residue",
        "_verify_generic_ray_nodes",
        "_verify_probes",
        "_verify_api",
        "_verify_ray_job_request_reference",
        "_verify_protocol_handoff_certification",
        "_verify_runtime_env_encryption",
        "_verify_workflow_progress",
        "_verify_workflow_admin",
        "_verify_prometheus",
    ):
        monkeypatch.setattr(gate, method_name, lambda: None)


def test_protocol_recovery_runs_after_app_readiness_and_before_any_task_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    gate = LocalKubeRayGate(_config(), output=lambda _value: None)
    _stub_successful_gate_layers(gate, monkeypatch)
    monkeypatch.setattr(gate, "_run_setup", lambda: events.append("database-ready"))
    monkeypatch.setattr(
        gate,
        "_wait_for_application_topology",
        lambda: events.append("app-ready"),
    )
    monkeypatch.setattr(
        gate,
        "_verify_deployed_images",
        lambda: events.append("image-identity"),
    )

    def recover_protocol_handoff_residue() -> None:
        events.append("recover-released-v040")
        events.append("recover-protocol-v2")

    monkeypatch.setattr(
        gate,
        "_recover_protocol_handoff_residue",
        recover_protocol_handoff_residue,
    )
    monkeypatch.setattr(
        gate,
        "_verify_generic_ray_nodes",
        lambda: events.append("runtime-env"),
    )
    monkeypatch.setattr(gate, "_verify_probes", lambda: events.append("probes"))
    monkeypatch.setattr(gate, "_verify_api", lambda: events.append("api-smoke"))
    monkeypatch.setattr(
        gate,
        "_verify_ray_job_request_reference",
        lambda: events.append("ray-job-request-reference"),
    )
    monkeypatch.setattr(
        gate,
        "_verify_protocol_handoff_certification",
        lambda: events.append("protocol-handoff"),
    )
    monkeypatch.setattr(
        gate,
        "_verify_runtime_env_encryption",
        lambda: events.append("runtime-env-encryption"),
    )
    monkeypatch.setattr(
        gate,
        "_verify_workflow_progress",
        lambda: events.append("workflow-progress"),
    )
    monkeypatch.setattr(
        gate,
        "_verify_workflow_admin",
        lambda: events.append("workflow-admin"),
    )
    monkeypatch.setattr(gate, "_verify_prometheus", lambda: events.append("prometheus"))
    monkeypatch.setattr(
        gate,
        "_evidence_lines",
        lambda: ("=== Local KubeRay final gate evidence ===",),
    )

    gate.run()

    assert events == [
        "database-ready",
        "app-ready",
        "image-identity",
        "recover-released-v040",
        "recover-protocol-v2",
        "runtime-env",
        "probes",
        "api-smoke",
        "ray-job-request-reference",
        "protocol-handoff",
        "runtime-env-encryption",
        "workflow-progress",
        "workflow-admin",
        "prometheus",
    ]


def test_temporary_workspace_creation_failure_is_bounded_redacted_and_has_no_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = TOKEN68
    encoded = base64.b64encode(token.encode()).decode()
    mixed_case_encoded = _percent_hex_case_variants(quote(encoded, safe=""))[-1]
    gate = LocalKubeRayGate(_config())
    gate.redactor.register(token)
    gate.redactor.register(encoded)

    def fail_creation(*args: object, **kwargs: object) -> None:
        raise OSError(f"{'x' * 20_000}{mixed_case_encoded}")

    monkeypatch.setattr(gate_module.tempfile, "TemporaryDirectory", fail_creation)
    monkeypatch.setattr(gate_module, "LocalKubeRayGate", lambda config: gate)

    result = gate_module.main(
        [
            "--context",
            "docker-desktop",
            "--namespace",
            EXPECTED_NAMESPACE,
            "--ray-restart",
            "skip",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert len(captured.err) <= MAX_OUTPUT_CHARACTERS
    assert "FAILED [preflight]: temporary workspace creation failed" in captured.err
    assert "workspace creation error:" in captured.err
    assert token not in captured.err
    assert encoded not in captured.err
    assert mixed_case_encoded not in captured.err
    assert "Traceback" not in captured.err
    assert "=== Local KubeRay final gate evidence ===" not in captured.out


def test_successful_gate_with_cleanup_failure_withholds_all_final_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    gate = LocalKubeRayGate(_config(), output=lambda value: events.append(f"output:{value}"))
    _stub_successful_gate_layers(gate, monkeypatch)
    monkeypatch.setattr(
        gate,
        "_evidence_lines",
        lambda: ("=== Local KubeRay final gate evidence ===", f"source_tree={SOURCE_TREE}"),
    )

    class CleanupFailure:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.name = str(tmp_path)
            events.append("workspace-created")

        def cleanup(self) -> None:
            events.append("workspace-cleanup")
            raise OSError("cleanup-failure-marker")

    monkeypatch.setattr(gate_module.tempfile, "TemporaryDirectory", CleanupFailure)
    monkeypatch.setattr(gate_module, "LocalKubeRayGate", lambda config: gate)

    result = gate_module.main(
        [
            "--context",
            "docker-desktop",
            "--namespace",
            EXPECTED_NAMESPACE,
            "--ray-restart",
            "skip",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "FAILED [final-identity]: temporary workspace cleanup failed" in captured.err
    assert "cleanup-failure-marker" in captured.err
    assert len(captured.err) <= MAX_OUTPUT_CHARACTERS
    assert "Traceback" not in captured.err
    assert "workspace-cleanup" in events
    assert not any("=== Local KubeRay final gate evidence ===" in event for event in events)
    assert "output:[final-identity] passed" not in events


def test_primary_and_cleanup_failures_are_both_preserved_without_false_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = TOKEN68
    encoded = base64.b64encode(token.encode()).decode()
    mixed_case_encoded = _percent_hex_case_variants(quote(encoded, safe=""))[-1]
    events: list[str] = []
    gate = LocalKubeRayGate(_config(), output=lambda value: events.append(f"output:{value}"))
    gate.redactor.register(token)
    gate.redactor.register(encoded)
    _stub_successful_gate_layers(gate, monkeypatch)

    def fail_api() -> None:
        raise RuntimeError(f"{'p' * 20_000}primary-failure-marker")

    monkeypatch.setattr(gate, "_verify_api", fail_api)
    monkeypatch.setattr(
        gate,
        "diagnostics",
        lambda layer: events.append(f"diagnostics:{layer}"),
    )

    class PrimaryAndCleanupFailure:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.name = str(tmp_path)
            events.append("workspace-created")

        def cleanup(self) -> None:
            events.append("workspace-cleanup")
            raise OSError(f"{'c' * 20_000}{mixed_case_encoded}")

    monkeypatch.setattr(
        gate_module.tempfile,
        "TemporaryDirectory",
        PrimaryAndCleanupFailure,
    )
    monkeypatch.setattr(gate_module, "LocalKubeRayGate", lambda config: gate)

    result = gate_module.main(
        [
            "--context",
            "docker-desktop",
            "--namespace",
            EXPECTED_NAMESPACE,
            "--ray-restart",
            "skip",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert len(captured.err) <= MAX_OUTPUT_CHARACTERS
    assert "FAILED [api-smoke]:" in captured.err
    assert "primary-failure-marker" in captured.err
    assert "temporary workspace cleanup also failed:" in captured.err
    assert token not in captured.err
    assert encoded not in captured.err
    assert mixed_case_encoded not in captured.err
    assert "Traceback" not in captured.err
    assert events.index("diagnostics:api-smoke") < events.index("workspace-cleanup")
    assert not any("=== Local KubeRay final gate evidence ===" in event for event in events)
    assert "output:[final-identity] passed" not in events


def test_success_evidence_is_emitted_only_after_workspace_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    gate = LocalKubeRayGate(_config(), output=lambda value: events.append(f"output:{value}"))
    _stub_successful_gate_layers(gate, monkeypatch)
    monkeypatch.setattr(
        gate,
        "_evidence_lines",
        lambda: ("=== Local KubeRay final gate evidence ===", f"source_tree={SOURCE_TREE}"),
    )

    class SuccessfulTemporaryDirectory:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.name = str(tmp_path)
            events.append("workspace-created")

        def cleanup(self) -> None:
            events.append("workspace-cleanup")

    monkeypatch.setattr(
        gate_module.tempfile,
        "TemporaryDirectory",
        SuccessfulTemporaryDirectory,
    )

    gate.run()

    cleanup_index = events.index("workspace-cleanup")
    final_pass_index = events.index("output:[final-identity] passed")
    evidence_index = events.index("output:=== Local KubeRay final gate evidence ===")
    assert cleanup_index < final_pass_index < evidence_index
    assert gate.temp_root is None
    assert gate.diagnostics_attempted is False


def test_final_evidence_identity_failure_is_labeled_once_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gate = LocalKubeRayGate(_config())
    for method_name in (
        "_preflight",
        "_build_images",
        "_apply_overlay",
        "_run_setup",
        "_apply_workloads",
        "_prepare_ray",
        "_restart_task_managers",
        "_wait_for_application_topology",
        "_verify_deployed_images",
        "_verify_authentication",
        "_recover_protocol_handoff_residue",
        "_verify_generic_ray_nodes",
        "_verify_probes",
        "_verify_api",
        "_verify_ray_job_request_reference",
        "_verify_protocol_handoff_certification",
        "_verify_runtime_env_encryption",
        "_verify_workflow_progress",
        "_verify_workflow_admin",
        "_verify_prometheus",
    ):
        monkeypatch.setattr(gate, method_name, lambda: None)
    identity_checks = 0

    def fail_final_identity() -> None:
        nonlocal identity_checks
        identity_checks += 1
        raise RuntimeError("final identity changed before evidence")

    monkeypatch.setattr(gate, "_verify_final_identity", fail_final_identity)
    monkeypatch.setattr(gate_module, "LocalKubeRayGate", lambda config: gate)

    result = gate_module.main(
        [
            "--context",
            "docker-desktop",
            "--namespace",
            EXPECTED_NAMESPACE,
            "--ray-restart",
            "skip",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert identity_checks == 1
    assert "FAILED [final-identity]: final identity changed before evidence" in captured.err
    assert "Traceback" not in captured.err
    assert "=== Local KubeRay final gate evidence ===" not in captured.out


def test_main_preserves_primary_failure_when_diagnostics_fail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "diagnostic-token-that-must-never-be-printed"

    class FailingGate:
        def __init__(self, config: GateConfig) -> None:
            self.redactor = Redactor()
            self.redactor.register(token)

        def run(self) -> None:
            raise GateError("api-smoke", token)

        def diagnostics(self, layer: str) -> None:
            raise RuntimeError(token)

    monkeypatch.setattr(gate_module, "LocalKubeRayGate", FailingGate)

    assert (
        gate_module.main(
            [
                "--context",
                "docker-desktop",
                "--namespace",
                EXPECTED_NAMESPACE,
                "--ray-restart",
                "skip",
            ]
        )
        == 1
    )

    stderr = capsys.readouterr().err
    assert token not in stderr
    assert "FAILED [api-smoke]: [REDACTED]" in stderr
    assert "bounded diagnostics unavailable: [REDACTED]" in stderr


def test_main_bounds_and_redacts_mixed_case_encoded_diagnostics_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    encoded_variants = _percent_hex_case_variants(quote(TOKEN68, safe=""))
    mixed_case_encoded = encoded_variants[len(encoded_variants) // 2 + 1]

    class FailingGate:
        def __init__(self, config: GateConfig) -> None:
            self.redactor = Redactor()
            self.redactor.register(TOKEN68)

        def run(self) -> None:
            raise GateError("api-smoke", "primary failure")

        def diagnostics(self, layer: str) -> None:
            raise RuntimeError(f"{'x' * 20_000}{mixed_case_encoded}")

    monkeypatch.setattr(gate_module, "LocalKubeRayGate", FailingGate)

    assert (
        gate_module.main(
            [
                "--context",
                "docker-desktop",
                "--namespace",
                EXPECTED_NAMESPACE,
                "--ray-restart",
                "skip",
            ]
        )
        == 1
    )

    stderr = capsys.readouterr().err
    assert len(stderr) <= MAX_OUTPUT_CHARACTERS
    assert stderr.startswith("[truncated redacted error; original_characters=")
    assert "FAILED [api-smoke]: primary failure" in stderr
    assert TOKEN68 not in stderr
    assert mixed_case_encoded not in stderr
