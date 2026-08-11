"""Unit tests for Ray Job runner payload handling."""

from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import django_ray.ray_job_protocol as ray_job_protocol_module
from django_ray.execution_codec import (
    ExecutionIdentity,
    ExecutionRequest,
    decode_execution_request,
    encode_execution_request,
)
from django_ray.ray_job_protocol import (
    LEGACY_RAY_JOB_SUBMISSION_ID_PREFIX,
    RAY_JOB_CONFIG_JSON_ENV_VAR,
    RAY_JOB_REQUEST_METADATA_MARKER_KEY,
    RAY_JOB_REQUEST_METADATA_MARKER_VALUE,
    RAY_JOB_REQUEST_REFERENCE_METADATA_MARKER_VALUE,
    RAY_JOB_REQUEST_REFERENCE_TRANSPORT,
    STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
    STRICT_RAY_JOB_SUBMISSION_ID_FAMILY_PREFIX,
    STRICT_RAY_JOB_SUBMISSION_ID_PREFIX,
    RayJobRequestBindingError,
    RayJobRequestBindingRejection,
    RayJobRequestExpectation,
    RayJobRequestReferenceExpectation,
    build_ray_job_request_metadata,
    build_ray_job_request_reference_metadata,
    coordination_sha256,
    fixed_safe_ray_job_metadata,
    is_rq2_ray_job_submission_id,
    is_strict_ray_job_submission_id,
    is_valid_rq2_ray_job_submission_id,
    is_valid_strict_ray_job_submission_id,
    load_ray_job_request_expectation,
    parse_ray_job_request_metadata,
    request_locator_sha256,
    request_reference_sha256,
    request_sha256,
    request_size_bytes,
    validate_ray_job_request_expectation,
    validate_ray_job_request_reference_expectation,
)
from django_ray.ray_job_request_storage import (
    RayJobRequestStorageError,
    RayJobRequestStorageRejection,
)
from django_ray.runner import (
    RayJobRequestPreparationError,
    RayJobRequestPreparationRejection,
    RayJobSubmissionUncertainError,
)
from django_ray.runner.base import JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcomeStatus
from django_ray.runner.ray_job import (
    _CONTROL_REQUEST_TIMEOUT_SECONDS,
    RayJobRunner,
    _address_pinned_job_client,
    _bounded_control_requests,
    _find_auto_ray_address,
    _resolve_submission_address,
)
from django_ray.runtime.runtime_env import (
    RuntimeEnvSnapshotError,
    normalize_runtime_env,
    runtime_env_for_storage,
)
from django_ray.workflow_plans import WorkflowPlanMismatchError

_RQ2_REQUEST_LOCATOR = (
    base64.urlsafe_b64encode(b'{"schema":"django-ray.unit-test-locator"}')
    .rstrip(b"=")
    .decode("ascii")
)


@pytest.fixture(autouse=True)
def _configured_ray_job_request_storage(settings, tmp_path: Path) -> None:
    """Give Ray Job unit tests one retrievable rq2 request store."""
    current = getattr(settings, "DJANGO_RAY", {})
    settings.DJANGO_RAY = {
        **current,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path / "request-store"),
    }


def _runner_settings(ray_address: str) -> dict[str, object]:
    """Return the minimum retrievable rq2 runner settings for address tests."""
    return {
        "RAY_ADDRESS": ray_address,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(Path.cwd()),
    }


class FakeJobClient:
    """Test double for Ray's JobSubmissionClient."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []

    def submit_job(self, **kwargs: object) -> str:
        """Record submit call and return deterministic job id."""
        self.submissions.append(kwargs)
        return str(kwargs["submission_id"])

    @staticmethod
    def _package_uri(value: object) -> object:
        if not isinstance(value, str):
            return value
        path = Path(value)
        if path.is_dir():
            digest = hashlib.sha256()
            for candidate in sorted(path.rglob("*")):
                if candidate.is_file():
                    digest.update(candidate.relative_to(path).as_posix().encode())
                    digest.update(candidate.read_bytes())
            return f"gcs://_ray_pkg_{digest.hexdigest()[:16]}.zip"
        if not path.is_file():
            return value
        digest = hashlib.sha1(path.read_bytes()).hexdigest()  # noqa: S324 - Ray contract
        return f"gcs://_ray_pkg_{digest}.zip"

    def _upload_working_dir_if_needed(self, runtime_env: dict[str, object]) -> None:
        if "working_dir" in runtime_env:
            runtime_env["working_dir"] = self._package_uri(runtime_env["working_dir"])

    def _upload_py_modules_if_needed(self, runtime_env: dict[str, object]) -> None:
        modules = runtime_env.get("py_modules")
        if isinstance(modules, list):
            runtime_env["py_modules"] = [self._package_uri(module) for module in modules]


class TestRayJobAddressResolution:
    """Selected Ray targets must be resolved without ambient routing overrides."""

    @pytest.mark.parametrize(
        ("gcs_addresses", "bootstrap_address", "expected"),
        [
            ({"cluster-a:6379", "cluster-b:6379"}, "latest:6379", "latest:6379"),
            ({"cluster-a:6379"}, "latest:6379", "cluster-a:6379"),
            (set(), "latest:6379", "latest:6379"),
        ],
    )
    def test_find_auto_ray_address_matches_env_free_discovery(
        self,
        monkeypatch,
        gcs_addresses,
        bootstrap_address,
        expected,
    ) -> None:
        from ray._private import services

        monkeypatch.setattr(services, "find_gcs_addresses", lambda: gcs_addresses)
        monkeypatch.setattr(
            services,
            "find_bootstrap_address",
            lambda _temp_dir: bootstrap_address,
        )

        assert _find_auto_ray_address() == expected

    def test_find_auto_ray_address_requires_a_running_cluster(self, monkeypatch) -> None:
        from ray._private import services

        monkeypatch.setattr(services, "find_gcs_addresses", set)
        monkeypatch.setattr(services, "find_bootstrap_address", lambda _temp_dir: None)

        with pytest.raises(ConnectionError, match="explicit 'auto' target"):
            _find_auto_ray_address()

    def test_resolve_submission_address_keeps_http_target(self, monkeypatch) -> None:
        monkeypatch.setenv("RAY_ADDRESS", "http://global-dashboard:8265")
        monkeypatch.setenv("RAY_API_SERVER_ADDRESS", "http://api-override:8265")

        assert (
            _resolve_submission_address("https://alias-dashboard:8265")
            == "https://alias-dashboard:8265"
        )

    def test_resolve_submission_address_converts_explicit_ray_client_target(
        self,
        monkeypatch,
    ) -> None:
        from ray.dashboard import utils as dashboard_utils

        selected = "ray://selected-client:10001"
        inputs: list[str] = []
        monkeypatch.setattr(
            dashboard_utils,
            "ray_client_address_to_api_server_url",
            lambda address: inputs.append(address) or "http://selected-dashboard:8265",
        )

        assert _resolve_submission_address(selected) == "http://selected-dashboard:8265"
        assert inputs == [selected]

    @pytest.mark.parametrize(
        ("selected", "resolved_input"),
        [
            ("cluster-head:6379", "cluster-head:6379"),
            ("auto", "autodetected-head:6379"),
        ],
    )
    def test_resolve_submission_address_uses_selected_bootstrap(
        self,
        monkeypatch,
        selected,
        resolved_input,
    ) -> None:
        from ray.dashboard import utils as dashboard_utils

        inputs: list[str] = []
        monkeypatch.setattr(
            "django_ray.runner.ray_job._find_auto_ray_address",
            lambda: "autodetected-head:6379",
        )
        monkeypatch.setattr(
            dashboard_utils,
            "ray_address_to_api_server_url",
            lambda address: inputs.append(address) or "http://alias-dashboard:8265",
        )
        monkeypatch.setenv("RAY_ADDRESS", "global-head:6379")

        assert _resolve_submission_address(selected) == "http://alias-dashboard:8265"
        assert inputs == [resolved_input]


class TestRayJobRequestBinding:
    """The Ray control plane must bind one exact canonical request."""

    @staticmethod
    def _request() -> tuple[ExecutionRequest, str, dict[str, str]]:
        request = ExecutionRequest(
            identity=ExecutionIdentity(7, "public-task-7", 2, 9),
            execution_protocol_version=1,
            callable_path="testproject.tasks.echo_task",
            transport_version=1,
            serialized_args="[]",
            serialized_kwargs="{}",
            input_reference=None,
            runtime_env_profile=None,
            runtime_env_hash="0" * 64,
            runtime_env_plan_identity={},
            compiled_graph_submission_transport="ray-job",
        )
        serialized = encode_execution_request(request)
        return request, serialized, build_ray_job_request_metadata(request, serialized)

    def test_loads_bounded_expectation_from_ray_job_config(self) -> None:
        request, serialized, metadata = self._request()
        config = json.dumps(
            {
                "runtime_env": {"env_vars": {"VALUE": "not retained"}},
                "metadata": {
                    "job_submission_id": "ray-internal-id",
                    **metadata,
                },
            }
        )

        expectation = load_ray_job_request_expectation(config)

        assert RAY_JOB_CONFIG_JSON_ENV_VAR == "RAY_JOB_CONFIG_JSON_ENV_VAR"
        assert isinstance(expectation, RayJobRequestExpectation)
        validate_ray_job_request_expectation(
            expectation,
            expected_identity=request.identity,
            expected_execution_protocol_version=1,
            serialized_request=serialized,
        )

    @pytest.mark.parametrize(
        "metadata",
        [
            {"django_ray_request_sha256": "0" * 64},
            {RAY_JOB_REQUEST_METADATA_MARKER_KEY: RAY_JOB_REQUEST_METADATA_MARKER_VALUE},
            {RAY_JOB_REQUEST_METADATA_MARKER_KEY: "unsupported"},
        ],
    )
    def test_partial_or_invalid_strict_metadata_never_downgrades(self, metadata) -> None:
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            parse_ray_job_request_metadata(metadata)

        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

    @pytest.mark.parametrize(
        "config",
        [
            "\ud800",
            '{"metadata":{},"metadata":{}}',
            "[]",
        ],
    )
    def test_config_errors_are_fixed_and_retain_no_raw_input(self, config) -> None:
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            load_ray_job_request_expectation(config)

        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID
        assert config not in str(exc_info.value)

    def test_valid_config_without_strict_metadata_remains_legacy(self) -> None:
        assert load_ray_job_request_expectation(None) is None
        assert parse_ray_job_request_metadata(None) is None
        assert fixed_safe_ray_job_metadata(None) is None
        assert fixed_safe_ray_job_metadata({"job_submission_id": "legacy"}) is None
        assert (
            load_ray_job_request_expectation('{"metadata":{"job_submission_id":"legacy"}}') is None
        )
        released_metadata = {
            "django_ray_task_id": "7",
            "django_ray_attempt_number": "2",
            "django_ray_execution_generation": "0",
            "callable_path": "testproject.tasks.echo_task",
            "runtime_env_profile": "",
            "runtime_env_hash": "0" * 64,
        }
        assert parse_ray_job_request_metadata(released_metadata) is None
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            parse_ray_job_request_metadata({}, required=True)
        assert exc_info.value.classification is RayJobRequestBindingRejection.MISSING

    @pytest.mark.parametrize(
        ("value", "classification"),
        [
            (object(), RayJobRequestBindingRejection.INVALID),
            ("x" * (4 * 1024 * 1024 + 1), RayJobRequestBindingRejection.RESOURCE_LIMIT),
        ],
        ids=("wrong-type", "oversize"),
    )
    def test_config_type_and_size_are_bounded(self, value, classification) -> None:
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            load_ray_job_request_expectation(value)
        assert exc_info.value.classification is classification

    def test_metadata_size_and_unicode_are_bounded_before_field_use(self) -> None:
        _request, _serialized, metadata = self._request()
        oversized = metadata | {"django_ray_public_task_id": "x" * (16 * 1024)}
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            parse_ray_job_request_metadata(oversized)
        assert exc_info.value.classification is RayJobRequestBindingRejection.RESOURCE_LIMIT

        malformed = metadata | {"django_ray_public_task_id": "\ud800"}
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            parse_ray_job_request_metadata(malformed)
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

    def test_metadata_size_is_checked_before_canonical_json(
        self,
        monkeypatch,
    ) -> None:
        _request, _serialized, metadata = self._request()
        oversized = metadata | {"django_ray_public_task_id": "x" * (16 * 1024 + 1)}
        monkeypatch.setattr(
            ray_job_protocol_module.json,
            "dumps",
            lambda *_args, **_kwargs: pytest.fail(
                "metadata was serialized before its values were bounded"
            ),
        )

        with pytest.raises(RayJobRequestBindingError) as exc_info:
            parse_ray_job_request_metadata(oversized)

        assert exc_info.value.classification is RayJobRequestBindingRejection.RESOURCE_LIMIT

    def test_utf8_bounds_and_request_hashing_do_not_encode_the_whole_input(
        self,
        monkeypatch,
    ) -> None:
        chunk_sizes: list[int] = []

        class RecordingHash:
            def update(self, value: bytes) -> None:
                chunk_sizes.append(len(value))

            @staticmethod
            def hexdigest() -> str:
                return "f" * 64

        monkeypatch.setattr(
            ray_job_protocol_module.hashlib,
            "sha256",
            lambda: RecordingHash(),
        )
        value = "é" * (64 * 1024 + 1)

        assert request_sha256(value) == "f" * 64
        assert chunk_sizes == [2 * 64 * 1024, 2]

        monkeypatch.setattr(ray_job_protocol_module, "EXECUTION_REQUEST_MAX_BYTES", 8)
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            request_sha256("123456789")
        assert exc_info.value.classification is RayJobRequestBindingRejection.RESOURCE_LIMIT

        monkeypatch.setattr(ray_job_protocol_module, "RAY_JOB_CONFIG_JSON_MAX_BYTES", 8)
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            load_ray_job_request_expectation("é" * 5)
        assert exc_info.value.classification is RayJobRequestBindingRejection.RESOURCE_LIMIT

    def test_digest_and_builder_reject_invalid_unicode_or_identity(self) -> None:
        request, serialized, _metadata = self._request()
        with pytest.raises(RayJobRequestBindingError):
            request_sha256("\ud800")
        invalid_request = replace(
            request,
            identity=ExecutionIdentity(7, "public-task-7", 2, -1),
        )
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            coordination_sha256(invalid_request.identity)
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

        with pytest.raises(RayJobRequestBindingError) as exc_info:
            build_ray_job_request_metadata(invalid_request, serialized)
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

        with pytest.raises(RayJobRequestBindingError) as exc_info:
            build_ray_job_request_reference_metadata(
                invalid_request,
                serialized,
                "s3://django-ray/requests/request.json",
                _RQ2_REQUEST_LOCATOR,
            )
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("django_ray_task_execution_pk", "not-a-number"),
            ("django_ray_task_execution_pk", "9" * 20),
            ("django_ray_attempt_number", "0"),
        ],
    )
    def test_metadata_counters_reject_noncanonical_values(self, key, value) -> None:
        _request, _serialized, metadata = self._request()
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            parse_ray_job_request_metadata(metadata | {key: value})
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

    def test_counter_conversion_error_stays_fixed(self, monkeypatch) -> None:
        _request, _serialized, metadata = self._request()

        def fail_conversion(_value: str) -> int:
            raise ValueError("attacker-controlled conversion detail")

        monkeypatch.setattr(
            ray_job_protocol_module,
            "int",
            fail_conversion,
            raising=False,
        )

        with pytest.raises(RayJobRequestBindingError) as exc_info:
            parse_ray_job_request_metadata(metadata)

        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID
        assert "attacker-controlled" not in str(exc_info.value)

    def test_json_integer_parser_bounds_digits_before_conversion(self) -> None:
        assert (
            ray_job_protocol_module._bounded_json_int("9223372036854775807") == 9223372036854775807
        )
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            ray_job_protocol_module._bounded_json_int("9" * 5000)
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

    def test_oversized_config_integer_is_fixed_at_the_entrypoint_boundary(
        self,
        monkeypatch,
    ) -> None:
        from django_ray.runtime.entrypoint import execute_task_from_payload

        oversized_digits = "9" * 5000
        config = f'{{"runtime_env":{{"value":{oversized_digits}}},"metadata":{{}}}}'
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            load_ray_job_request_expectation(config)
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID
        assert oversized_digits not in str(exc_info.value)

        monkeypatch.setenv(RAY_JOB_CONFIG_JSON_ENV_VAR, config)
        result = json.loads(execute_task_from_payload("%%%invalid%%%"))

        assert result["success"] is False
        assert result["exception_type"] == "RayExecutionRequestIncompatible"
        assert result["retryable"] is False
        assert oversized_digits not in json.dumps(result)

    @pytest.mark.parametrize(
        ("change", "classification"),
        [
            (
                {"expected_identity": ExecutionIdentity(8, "public-task-7", 2, 9)},
                RayJobRequestBindingRejection.IDENTITY_MISMATCH,
            ),
            (
                {"expected_execution_protocol_version": 2},
                RayJobRequestBindingRejection.PROTOCOL_MISMATCH,
            ),
            (
                {"serialized_request": "{}"},
                RayJobRequestBindingRejection.DIGEST_MISMATCH,
            ),
            (
                {"expected_submission_transport": "ray-client"},
                RayJobRequestBindingRejection.TRANSPORT_MISMATCH,
            ),
        ],
    )
    def test_validation_classifies_each_independent_mismatch(
        self,
        change,
        classification,
    ) -> None:
        request, serialized, metadata = self._request()
        expectation = parse_ray_job_request_metadata(metadata, required=True)
        assert expectation is not None
        arguments = {
            "expected_identity": request.identity,
            "expected_execution_protocol_version": 1,
            "serialized_request": serialized,
        } | change

        with pytest.raises(RayJobRequestBindingError) as exc_info:
            validate_ray_job_request_expectation(expectation, **arguments)

        assert exc_info.value.classification is classification

    def test_builder_rejects_non_ray_job_transport(self) -> None:
        request, serialized, _metadata = self._request()
        invalid_request = replace(request, compiled_graph_submission_transport="ray-client")
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            build_ray_job_request_metadata(invalid_request, serialized)
        assert exc_info.value.classification is (RayJobRequestBindingRejection.TRANSPORT_MISMATCH)

        with pytest.raises(RayJobRequestBindingError) as exc_info:
            build_ray_job_request_reference_metadata(
                invalid_request,
                serialized,
                "s3://django-ray/requests/request.json",
                _RQ2_REQUEST_LOCATOR,
            )
        assert exc_info.value.classification is (RayJobRequestBindingRejection.TRANSPORT_MISMATCH)

    def test_rq2_metadata_binds_only_opaque_request_reference_fields(self) -> None:
        request, serialized, _metadata = self._request()
        reference = "s3://django-ray/requests/" + "a" * 64 + "?bytes=512"

        metadata = build_ray_job_request_reference_metadata(
            request,
            serialized,
            reference,
            _RQ2_REQUEST_LOCATOR,
        )

        assert metadata == {
            "django_ray_request_binding": (RAY_JOB_REQUEST_REFERENCE_METADATA_MARKER_VALUE),
            "django_ray_coordination_sha256": coordination_sha256(request.identity),
            "django_ray_execution_protocol_version": "1",
            "django_ray_request_sha256": request_sha256(serialized),
            "django_ray_request_size_bytes": str(request_size_bytes(serialized)),
            "django_ray_request_reference_sha256": request_reference_sha256(reference),
            "django_ray_request_locator_sha256": request_locator_sha256(_RQ2_REQUEST_LOCATOR),
            "django_ray_submission_transport": RAY_JOB_REQUEST_REFERENCE_TRANSPORT,
        }
        serialized_metadata = json.dumps(metadata)
        for forbidden in (
            request.callable_path,
            request.identity.task_id,
            "django_ray_task_execution_pk",
            "django_ray_public_task_id",
            "django_ray_attempt_number",
            "django_ray_execution_generation",
            "runtime_env_profile",
            "runtime_env_hash",
            _RQ2_REQUEST_LOCATOR,
        ):
            assert forbidden not in serialized_metadata

        expectation = parse_ray_job_request_metadata(metadata, required=True)
        assert isinstance(expectation, RayJobRequestReferenceExpectation)
        validate_ray_job_request_reference_expectation(
            expectation,
            expected_identity=request.identity,
            expected_execution_protocol_version=request.execution_protocol_version,
            serialized_request=serialized,
            request_reference=reference,
            request_locator=_RQ2_REQUEST_LOCATOR,
        )

        incomplete_metadata = dict(metadata)
        incomplete_metadata.pop("django_ray_request_locator_sha256")
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            parse_ray_job_request_metadata(incomplete_metadata, required=True)
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

        malformed_metadata = dict(metadata)
        malformed_metadata["django_ray_request_locator_sha256"] = "not-a-digest"
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            parse_ray_job_request_metadata(malformed_metadata, required=True)
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

    @pytest.mark.parametrize(
        ("change", "classification"),
        [
            (
                {"expected_identity": ExecutionIdentity(8, "public-task-7", 2, 9)},
                RayJobRequestBindingRejection.IDENTITY_MISMATCH,
            ),
            (
                {"expected_execution_protocol_version": 2},
                RayJobRequestBindingRejection.PROTOCOL_MISMATCH,
            ),
            (
                {"serialized_request": "{}"},
                RayJobRequestBindingRejection.DIGEST_MISMATCH,
            ),
            (
                {"request_reference": "s3://different-reference"},
                RayJobRequestBindingRejection.DIGEST_MISMATCH,
            ),
            (
                {"request_locator": "different_locator_token"},
                RayJobRequestBindingRejection.DIGEST_MISMATCH,
            ),
            (
                {"expected_submission_transport": "ray-job"},
                RayJobRequestBindingRejection.TRANSPORT_MISMATCH,
            ),
            (
                {
                    "expected_submission_id": (
                        f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{'0' * 64}"
                    )
                },
                RayJobRequestBindingRejection.IDENTITY_MISMATCH,
            ),
            (
                {"expected_submission_id": "raysubmit_django_ray_rq2_invalid"},
                RayJobRequestBindingRejection.INVALID,
            ),
            (
                {
                    "expected_request_sha256": object(),
                    "expected_request_size_bytes": 1,
                },
                RayJobRequestBindingRejection.INVALID,
            ),
            (
                {"expected_request_sha256": "0" * 64},
                RayJobRequestBindingRejection.INVALID,
            ),
        ],
    )
    def test_rq2_validation_classifies_each_independent_mismatch(
        self,
        change,
        classification,
    ) -> None:
        request, serialized, _metadata = self._request()
        reference = "s3://django-ray/requests/" + "b" * 64 + "?bytes=512"
        metadata = build_ray_job_request_reference_metadata(
            request,
            serialized,
            reference,
            _RQ2_REQUEST_LOCATOR,
        )
        expectation = parse_ray_job_request_metadata(metadata, required=True)
        assert isinstance(expectation, RayJobRequestReferenceExpectation)
        arguments = {
            "expected_identity": request.identity,
            "expected_execution_protocol_version": request.execution_protocol_version,
            "expected_submission_id": (
                f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}"
                f"{coordination_sha256(request.identity)}"
            ),
            "serialized_request": serialized,
            "request_reference": reference,
            "request_locator": _RQ2_REQUEST_LOCATOR,
        } | change

        with pytest.raises(RayJobRequestBindingError) as exc_info:
            validate_ray_job_request_reference_expectation(expectation, **arguments)

        assert exc_info.value.classification is classification

    def test_rq2_reference_binding_is_bounded_and_fixed(self, monkeypatch) -> None:
        request, serialized, _metadata = self._request()
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            request_reference_sha256("")
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

        with pytest.raises(RayJobRequestBindingError) as exc_info:
            request_locator_sha256(object())  # type: ignore[arg-type]
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

        monkeypatch.setattr(
            ray_job_protocol_module,
            "RAY_JOB_REQUEST_REFERENCE_MAX_BYTES",
            8,
        )

        with pytest.raises(RayJobRequestBindingError) as exc_info:
            build_ray_job_request_reference_metadata(
                request,
                serialized,
                "x" * 9,
                _RQ2_REQUEST_LOCATOR,
            )
        assert exc_info.value.classification is RayJobRequestBindingRejection.RESOURCE_LIMIT

        with pytest.raises(RayJobRequestBindingError) as exc_info:
            request_reference_sha256("\ud800")
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

        monkeypatch.setattr(
            ray_job_protocol_module,
            "RAY_JOB_REQUEST_LOCATOR_MAX_CHARS",
            8,
        )
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            request_locator_sha256("a" * 9)
        assert exc_info.value.classification is RayJobRequestBindingRejection.RESOURCE_LIMIT

        with pytest.raises(RayJobRequestBindingError) as exc_info:
            request_locator_sha256("a+")
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

    def test_rq2_request_digest_mismatch_is_checked_after_equal_size(self) -> None:
        request, serialized, _metadata = self._request()
        reference = "s3://django-ray/requests/" + "b" * 64 + "?bytes=512"
        metadata = build_ray_job_request_reference_metadata(
            request,
            serialized,
            reference,
            _RQ2_REQUEST_LOCATOR,
        )
        expectation = parse_ray_job_request_metadata(metadata, required=True)
        assert isinstance(expectation, RayJobRequestReferenceExpectation)
        replacement = f"{serialized[:-1]} "
        assert len(replacement.encode("utf-8")) == len(serialized.encode("utf-8"))

        with pytest.raises(RayJobRequestBindingError) as exc_info:
            validate_ray_job_request_reference_expectation(
                expectation,
                serialized_request=replacement,
            )

        assert exc_info.value.classification is RayJobRequestBindingRejection.DIGEST_MISMATCH

    @pytest.mark.parametrize(
        "leaky_key",
        [
            "django_ray_task_execution_pk",
            "django_ray_public_task_id",
            "django_ray_attempt_number",
            "django_ray_execution_generation",
            "django_ray_task_id",
            "callable_path",
            "runtime_env_profile",
            "runtime_env_hash",
        ],
    )
    def test_rq2_rejects_raw_identity_and_visibility_fields(self, leaky_key) -> None:
        request, serialized, _metadata = self._request()
        reference = "s3://django-ray/requests/" + "d" * 64 + "?bytes=512"
        metadata = build_ray_job_request_reference_metadata(
            request,
            serialized,
            reference,
            _RQ2_REQUEST_LOCATOR,
        )

        raw_metadata = metadata | {leaky_key: "do-not-retain"}
        projected = fixed_safe_ray_job_metadata(raw_metadata)

        assert projected is not None
        assert projected[leaky_key] == ""
        assert "do-not-retain" not in json.dumps(projected)
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            parse_ray_job_request_metadata(projected)

        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

    def test_rq1_rejects_rq2_only_fields(self) -> None:
        _request, _serialized, metadata = self._request()
        for rq2_key in (
            "django_ray_coordination_sha256",
            "django_ray_request_size_bytes",
            "django_ray_request_reference_sha256",
            "django_ray_request_locator_sha256",
        ):
            projected = fixed_safe_ray_job_metadata(metadata | {rq2_key: "0"})
            assert projected is not None
            assert projected[rq2_key] == ""
            with pytest.raises(RayJobRequestBindingError) as exc_info:
                parse_ray_job_request_metadata(projected)
            assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

    @pytest.mark.parametrize("family", ["rq1", "rq2"])
    @pytest.mark.parametrize(
        "reserved_key",
        ["django_ray_future_identity", "django_ray_request_locator"],
    )
    def test_strict_families_reject_unknown_reserved_namespace_fields(
        self,
        family,
        reserved_key,
    ) -> None:
        request, serialized, rq1_metadata = self._request()
        if family == "rq1":
            metadata = rq1_metadata
        else:
            metadata = build_ray_job_request_reference_metadata(
                request,
                serialized,
                "s3://django-ray/requests/" + "e" * 64 + "?bytes=512",
                _RQ2_REQUEST_LOCATOR,
            )

        projected = fixed_safe_ray_job_metadata(
            metadata
            | {
                reserved_key: "do-not-retain",
                "job_name": "ray-added-extra",
            }
        )

        assert projected is not None
        assert reserved_key not in projected
        assert "do-not-retain" not in json.dumps(projected)
        assert "job_name" not in projected
        with pytest.raises(RayJobRequestBindingError) as exc_info:
            parse_ray_job_request_metadata(projected)
        assert exc_info.value.classification is RayJobRequestBindingRejection.INVALID

    def test_rq2_ignores_generic_ray_added_metadata_fields(self) -> None:
        request, serialized, _metadata = self._request()
        metadata = build_ray_job_request_reference_metadata(
            request,
            serialized,
            "s3://django-ray/requests/" + "f" * 64 + "?bytes=512",
            _RQ2_REQUEST_LOCATOR,
        )

        expectation = parse_ray_job_request_metadata(
            metadata | {"job_name": "ray-added-extra"},
            required=True,
        )

        assert isinstance(expectation, RayJobRequestReferenceExpectation)

    def test_rq1_and_rq2_validators_reject_cross_family_expectations(self) -> None:
        request, serialized, rq1_metadata = self._request()
        reference = "s3://django-ray/requests/" + "c" * 64 + "?bytes=512"
        rq2_metadata = build_ray_job_request_reference_metadata(
            request,
            serialized,
            reference,
            _RQ2_REQUEST_LOCATOR,
        )
        rq1_expectation = parse_ray_job_request_metadata(rq1_metadata, required=True)
        rq2_expectation = parse_ray_job_request_metadata(rq2_metadata, required=True)
        assert isinstance(rq1_expectation, RayJobRequestExpectation)
        assert isinstance(rq2_expectation, RayJobRequestReferenceExpectation)

        with pytest.raises(RayJobRequestBindingError) as rq1_error:
            validate_ray_job_request_expectation(
                rq2_expectation,  # type: ignore[arg-type]
                expected_identity=request.identity,
                expected_execution_protocol_version=1,
            )
        assert rq1_error.value.classification is RayJobRequestBindingRejection.INVALID

        with pytest.raises(RayJobRequestBindingError) as rq2_error:
            validate_ray_job_request_reference_expectation(
                rq1_expectation,  # type: ignore[arg-type]
                expected_identity=request.identity,
                expected_execution_protocol_version=1,
            )
        assert rq2_error.value.classification is RayJobRequestBindingRejection.INVALID

    def test_safe_projection_discards_extras_and_invalid_values(self) -> None:
        _request, _serialized, metadata = self._request()
        metadata["secret"] = "do-not-retain"
        metadata["django_ray_public_task_id"] = "\ud800"

        projected = fixed_safe_ray_job_metadata(metadata)

        assert projected is not None
        assert "secret" not in projected
        assert projected["django_ray_public_task_id"] == ""
        with pytest.raises(RayJobRequestBindingError):
            parse_ray_job_request_metadata(projected, required=True)

    def test_strict_submission_id_marker_cannot_downgrade(self) -> None:
        malformed = f"{STRICT_RAY_JOB_SUBMISSION_ID_PREFIX}not-a-digest"
        assert is_strict_ray_job_submission_id(malformed)
        assert not is_valid_strict_ray_job_submission_id(malformed)
        rq2 = f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{'a' * 64}"
        assert is_rq2_ray_job_submission_id(rq2)
        assert is_valid_rq2_ray_job_submission_id(rq2)
        assert is_valid_strict_ray_job_submission_id(rq2)
        malformed_rq2 = f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}corrupt"
        assert is_rq2_ray_job_submission_id(malformed_rq2)
        assert not is_valid_rq2_ray_job_submission_id(malformed_rq2)
        for future_or_corrupt in (
            f"{STRICT_RAY_JOB_SUBMISSION_ID_FAMILY_PREFIX}3_{'a' * 64}",
            f"{STRICT_RAY_JOB_SUBMISSION_ID_FAMILY_PREFIX}-corrupt",
        ):
            assert is_strict_ray_job_submission_id(future_or_corrupt)
            assert not is_valid_strict_ray_job_submission_id(future_or_corrupt)


class TestRayJobRequestPreparationErrors:
    """Manager-side rq2 preparation failures remain fixed and classifiable."""

    def test_retry_disposition_is_fixed_by_classification(self) -> None:
        for classification in RayJobRequestPreparationRejection:
            error = RayJobRequestPreparationError(classification)
            assert str(error) == (f"Ray Job request preparation rejected: {classification.value}")
            assert error.requires_nonretryable_disposition is (
                classification is not RayJobRequestPreparationRejection.STORAGE_UNAVAILABLE
            )

    @pytest.mark.parametrize(
        "storage_config",
        [
            {"INPUT_STORAGE_BACKEND": None},
            {"INPUT_STORAGE_BACKEND": "s3", "INPUT_STORAGE_S3_BUCKET": None},
        ],
        ids=("disabled", "malformed-partial"),
    )
    def test_runner_control_construction_does_not_require_submission_storage(
        self,
        monkeypatch,
        storage_config: dict[str, object],
    ) -> None:
        config = _runner_settings("ray://password-do-not-retain:10001")
        config.update(storage_config)
        monkeypatch.setattr(
            "django_ray.runner.ray_job.get_settings",
            lambda: config,
        )

        runner = RayJobRunner()

        assert runner.ray_address == "ray://password-do-not-retain:10001"

    @pytest.mark.parametrize("ray_address", [None, "", 1])
    def test_runner_control_rejects_invalid_ray_address(
        self,
        monkeypatch,
        ray_address: object,
    ) -> None:
        config = _runner_settings("ray://unused:10001")
        config["RAY_ADDRESS"] = ray_address
        monkeypatch.setattr("django_ray.runner.ray_job.get_settings", lambda: config)

        with pytest.raises(RayJobRequestPreparationError) as exc_info:
            RayJobRunner()

        assert exc_info.value.classification is RayJobRequestPreparationRejection.CONFIGURATION
        assert exc_info.value.requires_nonretryable_disposition is True

    def test_runner_settings_load_failure_is_fixed(self, monkeypatch) -> None:
        from django.core.exceptions import ImproperlyConfigured

        settings_error = ImproperlyConfigured("credential-do-not-retain")
        monkeypatch.setattr(
            "django_ray.runner.ray_job.get_settings",
            lambda: (_ for _ in ()).throw(settings_error),
        )

        with pytest.raises(RayJobRequestPreparationError) as exc_info:
            RayJobRunner()

        assert exc_info.value.classification is (RayJobRequestPreparationRejection.CONFIGURATION)
        assert "credential-do-not-retain" not in str(exc_info.value)
        assert exc_info.value.__cause__ is settings_error


@pytest.mark.django_db
class TestRayJobRunnerPublicReservation:
    """The public runner reserves only an exact persisted claimed row."""

    @staticmethod
    def _task(*, claimed_by_worker: str | None = "direct-owner"):
        from django_ray.models import RayTaskExecution, TaskState

        runtime_env = normalize_runtime_env({})
        return RayTaskExecution.objects.create(
            task_id=f"direct-submit-{claimed_by_worker or 'unclaimed'}",
            callable_path="testproject.tasks.echo_task",
            args_json="[]",
            kwargs_json="{}",
            state=TaskState.RUNNING,
            claimed_by_worker=claimed_by_worker,
            execution_protocol_version=1,
            runtime_env_profile=runtime_env.profile,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
        )

    def test_reserve_is_exact_and_idempotent(self) -> None:
        task = self._task()
        runner = RayJobRunner()
        handle = runner.submission_handle(task)

        first = runner._reserve_public_submission(
            task,
            handle,
            callable_path=task.callable_path,
            args_json=task.args_json,
            kwargs_json=task.kwargs_json,
            input_reference=task.input_reference,
        )
        second = runner._reserve_public_submission(
            task,
            handle,
            callable_path=task.callable_path,
            args_json=task.args_json,
            kwargs_json=task.kwargs_json,
            input_reference=task.input_reference,
        )

        assert first is True
        assert second is False
        task.refresh_from_db()
        assert task.ray_job_id == handle.ray_job_id
        assert task.ray_address == handle.ray_address
        assert task.ray_job_request_reference is None

    @pytest.mark.parametrize(
        "mutation",
        [
            {"claimed_by_worker": None},
            {"args_json": '["different"]'},
            {"execution_generation": 1},
        ],
    )
    def test_reserve_rejects_unclaimed_or_divergent_rows(self, mutation) -> None:
        task = self._task()
        runner = RayJobRunner()
        handle = runner.submission_handle(task)
        for field, value in mutation.items():
            setattr(task, field, value)
            task.save(update_fields=[field])

        with pytest.raises(RayJobRequestPreparationError) as exc_info:
            runner._reserve_public_submission(
                task,
                handle,
                callable_path="testproject.tasks.echo_task",
                args_json="[]",
                kwargs_json="{}",
                input_reference=None,
            )

        assert exc_info.value.classification is (RayJobRequestPreparationRejection.BINDING_MISMATCH)
        task.refresh_from_db()
        assert task.ray_job_id is None
        assert task.ray_address is None

    def test_public_submit_rejects_non_model_execution_before_remote_work(self) -> None:
        runner = RayJobRunner()
        task = SimpleNamespace(
            pk=991,
            task_id="not-a-persisted-execution",
            attempt_number=1,
            execution_generation=1,
        )

        with pytest.raises(RayJobRequestPreparationError) as exc_info:
            runner.submit(
                task_execution=task,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert exc_info.value.classification is RayJobRequestPreparationRejection.BINDING_MISMATCH

    @pytest.mark.parametrize("persisted_job_id", [None, "replacement"])
    def test_public_submit_rejects_partial_or_replaced_reservation(
        self,
        persisted_job_id: str | None,
    ) -> None:
        task = self._task()
        runner = RayJobRunner()
        handle = runner.submission_handle(task)
        task.ray_job_id = (
            None
            if persisted_job_id is None
            else f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{'0' * 64}"
        )
        task.ray_address = handle.ray_address
        task.save(update_fields=["ray_job_id", "ray_address"])

        with pytest.raises(RayJobRequestPreparationError) as exc_info:
            runner.submit(
                task_execution=task,
                callable_path=task.callable_path,
                args=(),
                kwargs={},
            )

        assert exc_info.value.classification is RayJobRequestPreparationRejection.BINDING_MISMATCH
        task.refresh_from_db()
        assert task.ray_job_id == (
            None
            if persisted_job_id is None
            else f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{'0' * 64}"
        )
        assert task.ray_address == handle.ray_address

    @pytest.mark.parametrize(
        ("release_outcome", "classification"),
        [
            ("error", RayJobRequestPreparationRejection.CONFIGURATION),
            ("refused", RayJobRequestPreparationRejection.BINDING_MISMATCH),
        ],
    )
    def test_public_submit_maps_release_failure_to_fixed_classification(
        self,
        monkeypatch: pytest.MonkeyPatch,
        release_outcome: str,
        classification: RayJobRequestPreparationRejection,
    ) -> None:
        task = self._task()
        runner = RayJobRunner()

        def definite_failure(**_kwargs: object) -> SubmissionHandle:
            raise ValueError("untrusted submission detail")

        def release(*_args: object, **_kwargs: object) -> bool:
            if release_outcome == "error":
                raise RayJobRequestStorageError(RayJobRequestStorageRejection.CONFIGURATION)
            return False

        monkeypatch.setattr(runner, "_submit_serialized_request", definite_failure)
        monkeypatch.setattr(
            "django_ray.runner.ray_job.release_ray_job_request_reservation",
            release,
        )

        with pytest.raises(RayJobRequestPreparationError) as exc_info:
            runner.submit(
                task_execution=task,
                callable_path=task.callable_path,
                args=(),
                kwargs={},
            )

        assert exc_info.value.classification is classification

    def test_public_submit_persists_exact_rq2_tuple(self, monkeypatch) -> None:
        from django_ray.models import InputPayloadKind, TaskInputPayload

        task = self._task()
        client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _address=None: client)

        handle = runner.submit(
            task_execution=task,
            callable_path=task.callable_path,
            args=(),
            kwargs={},
        )

        task.refresh_from_db()
        assert task.ray_job_id == handle.ray_job_id
        assert task.ray_address == handle.ray_address
        assert task.ray_job_request_reference is not None
        payload = TaskInputPayload.objects.get(reference=task.ray_job_request_reference)
        assert payload.payload_kind == InputPayloadKind.RAY_JOB_REQUEST
        assert len(client.submissions) == 1
        assert client.submissions[0]["submission_id"] == handle.ray_job_id

    def test_public_submit_does_not_repeat_an_existing_reservation(
        self,
        monkeypatch,
    ) -> None:
        task = self._task()
        runner = RayJobRunner()
        handle = runner.submission_handle(task)
        assert runner._reserve_public_submission(
            task,
            handle,
            callable_path=task.callable_path,
            args_json=task.args_json,
            kwargs_json=task.kwargs_json,
            input_reference=task.input_reference,
        )
        submit_calls: list[object] = []

        def unexpected_submit(**kwargs: object) -> SubmissionHandle:
            submit_calls.append(kwargs)
            raise AssertionError("an existing reservation must not be submitted again")

        monkeypatch.setattr(runner, "_submit_serialized_request", unexpected_submit)

        with pytest.raises(RayJobSubmissionUncertainError) as exc_info:
            runner.submit(
                task_execution=task,
                callable_path=task.callable_path,
                args=(),
                kwargs={},
            )

        assert exc_info.value.submission_id == handle.ray_job_id
        assert exc_info.value.observed_submission_id is None
        assert str(exc_info.value).endswith("durable submission reservation already exists")
        assert submit_calls == []
        task.refresh_from_db()
        assert task.ray_job_id == handle.ray_job_id
        assert task.ray_address == handle.ray_address
        assert task.ray_job_request_reference is None

    def test_public_submit_releases_exact_tuple_on_definite_failure(
        self,
        monkeypatch,
    ) -> None:
        from django_ray.models import InputPayloadKind, TaskInputPayload

        task = self._task()
        runner = RayJobRunner()
        client_error = ConnectionError("Ray dashboard unavailable before submit")
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda _address=None: (_ for _ in ()).throw(client_error),
        )

        with pytest.raises(ConnectionError) as exc_info:
            runner.submit(
                task_execution=task,
                callable_path=task.callable_path,
                args=(),
                kwargs={},
            )

        assert exc_info.value is client_error
        task.refresh_from_db()
        assert task.ray_job_id is None
        assert task.ray_address is None
        assert task.ray_job_request_reference is None
        payload = TaskInputPayload.objects.get()
        assert payload.payload_kind == InputPayloadKind.RAY_JOB_REQUEST

    def test_public_submit_retains_tuple_when_acceptance_is_uncertain(
        self,
        monkeypatch,
    ) -> None:
        class UncertainClient(FakeJobClient):
            def submit_job(self, **kwargs: object) -> str:
                self.submissions.append(kwargs)
                raise TimeoutError("response unavailable after request")

        task = self._task()
        client = UncertainClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _address=None: client)

        with pytest.raises(RayJobSubmissionUncertainError):
            runner.submit(
                task_execution=task,
                callable_path=task.callable_path,
                args=(),
                kwargs={},
            )

        task.refresh_from_db()
        assert task.ray_job_id is not None
        assert task.ray_address is not None
        assert task.ray_job_request_reference is not None
        assert len(client.submissions) == 1


class TestRayJobRunnerSubmit:
    """Tests for RayJobRunner.submit."""

    @pytest.fixture(autouse=True)
    def _fake_request_storage(self, monkeypatch) -> None:
        prepared_requests: list[SimpleNamespace] = []
        storage_events: list[str] = []

        def prepare(serialized_request: str, _config) -> SimpleNamespace:
            storage_events.append("prepare")
            digest = hashlib.sha256(serialized_request.encode("utf-8")).hexdigest()
            size_bytes = len(serialized_request.encode("utf-8"))
            reference = f"resultfs://sha256/{digest}?bytes={size_bytes}"
            locator_json = json.dumps(
                {
                    "digest": digest,
                    "reference": reference,
                    "size_bytes": size_bytes,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            encoded_locator = (
                base64.urlsafe_b64encode(locator_json.encode("utf-8")).rstrip(b"=").decode("ascii")
            )
            prepared = SimpleNamespace(
                serialized_request=serialized_request,
                request=decode_execution_request(serialized_request),
                reference=reference,
                locator_json=locator_json,
                encoded_locator=encoded_locator,
                backend="filesystem",
                digest=digest,
                size_bytes=size_bytes,
                envelope_version=1,
            )
            prepared_requests.append(prepared)
            return prepared

        def attach(
            prepared,
            *,
            task_execution,
            submission_handle,
            using=None,
        ) -> str:
            del using
            storage_events.append("attach")
            assert submission_handle.ray_job_id == RayJobRunner.submission_id(task_execution)
            task_execution.ray_job_request_reference = prepared.reference
            return prepared.reference

        def reserve(
            _runner,
            task_execution,
            submission_handle,
            **_request_fields,
        ) -> bool:
            storage_events.append("reserve")
            task_execution.claimed_by_worker = "unit-test-worker"
            task_execution.ray_job_id = submission_handle.ray_job_id
            task_execution.ray_address = submission_handle.ray_address
            return True

        def release(_runner, task_execution, _submission_handle) -> None:
            storage_events.append("release")
            task_execution.ray_job_id = None
            task_execution.ray_address = None
            task_execution.ray_job_request_reference = None

        monkeypatch.setattr("django_ray.runner.ray_job.prepare_ray_job_request", prepare)
        monkeypatch.setattr(
            "django_ray.runner.ray_job.register_and_attach_ray_job_request",
            attach,
        )
        monkeypatch.setattr(RayJobRunner, "_reserve_public_submission", reserve)
        monkeypatch.setattr(RayJobRunner, "_release_public_submission", release)
        self.prepared_requests = prepared_requests
        self.storage_events = storage_events

    @pytest.mark.parametrize(
        ("field", "replacement"),
        [
            ("task_id", "django-task-replacement"),
            ("pk", 124),
            ("attempt_number", 3),
            ("execution_generation", 12),
        ],
    )
    def test_submission_id_fences_each_execution_identity_field(
        self,
        field,
        replacement,
    ) -> None:
        values = {
            "task_id": "django-task-123",
            "pk": 123,
            "attempt_number": 2,
            "execution_generation": 11,
        }
        baseline = RayJobRunner.submission_id(SimpleNamespace(**values))
        changed_values = values | {field: replacement}
        changed = RayJobRunner.submission_id(SimpleNamespace(**changed_values))

        assert RayJobRunner.submission_id(SimpleNamespace(**values)) == baseline
        assert changed != baseline
        digest = baseline.removeprefix(STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX)
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")
        assert is_strict_ray_job_submission_id(baseline)
        assert is_valid_strict_ray_job_submission_id(baseline)
        assert not is_strict_ray_job_submission_id(f"{LEGACY_RAY_JOB_SUBMISSION_ID_PREFIX}{digest}")

    @pytest.mark.parametrize(
        "task_execution",
        [
            SimpleNamespace(
                pk=None,
                task_id="django-task-123",
                attempt_number=2,
                execution_generation=11,
            ),
            SimpleNamespace(
                pk=123,
                task_id="django-task-123",
                attempt_number=-1,
                execution_generation=11,
            ),
        ],
        ids=("missing-primary-key", "invalid-counter"),
    )
    def test_submission_id_rejects_invalid_execution_identity(self, task_execution) -> None:
        with pytest.raises(RayJobRequestPreparationError) as exc_info:
            RayJobRunner.submission_id(task_execution)

        assert exc_info.value.classification is RayJobRequestPreparationRejection.INVALID_REQUEST

    def test_submit_maps_invalid_request_encoding_to_fixed_rejection(self) -> None:
        runner = RayJobRunner()
        task_execution = SimpleNamespace(
            pk=992,
            task_id="django-task-992",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=0,
        )

        with pytest.raises(RayJobRequestPreparationError) as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert exc_info.value.classification is RayJobRequestPreparationRejection.INVALID_REQUEST
        assert self.storage_events == ["reserve", "release"]

    @pytest.mark.parametrize(
        ("binding_rejection", "preparation_rejection"),
        [
            (
                RayJobRequestBindingRejection.RESOURCE_LIMIT,
                RayJobRequestPreparationRejection.RESOURCE_LIMIT,
            ),
            (
                RayJobRequestBindingRejection.INVALID,
                RayJobRequestPreparationRejection.INVALID_REQUEST,
            ),
        ],
    )
    def test_submit_maps_typed_request_binding_failure_to_fixed_rejection(
        self,
        monkeypatch: pytest.MonkeyPatch,
        binding_rejection: RayJobRequestBindingRejection,
        preparation_rejection: RayJobRequestPreparationRejection,
    ) -> None:
        binding_error = RayJobRequestBindingError(binding_rejection)

        def reject_binding(*_args: object, **_kwargs: object) -> dict[str, str]:
            raise binding_error

        monkeypatch.setattr(
            "django_ray.runner.ray_job.build_ray_job_request_reference_metadata",
            reject_binding,
        )
        runner = RayJobRunner()
        task_execution = SimpleNamespace(
            pk=993,
            task_id="django-task-993",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=1,
        )

        with pytest.raises(RayJobRequestPreparationError) as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert exc_info.value.classification is preparation_rejection
        assert exc_info.value.__cause__ is binding_error
        assert self.storage_events == ["reserve", "prepare", "release"]

    def test_submit_rejects_divergent_immutable_snapshot_before_storage(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        divergent_snapshot = normalize_runtime_env({"env_vars": {"VALUE": "different"}})

        @contextmanager
        def snapshot_with_different_identity(_runtime_env):
            yield divergent_snapshot

        monkeypatch.setattr(
            "django_ray.runner.ray_job.snapshot_local_runtime_env",
            snapshot_with_different_identity,
        )
        runner = RayJobRunner()
        task_execution = SimpleNamespace(
            pk=994,
            task_id="django-task-994",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=1,
        )

        with pytest.raises(WorkflowPlanMismatchError, match="immutable snapshot"):
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert self.storage_events == ["reserve", "release"]

    def test_submit_rejects_storage_attachment_reference_mismatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "django_ray.runner.ray_job.register_and_attach_ray_job_request",
            lambda *_args, **_kwargs: "resultfs://sha256/different?bytes=1",
        )
        runner = RayJobRunner()
        task_execution = SimpleNamespace(
            pk=995,
            task_id="django-task-995",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=1,
        )

        with pytest.raises(RayJobRequestPreparationError) as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert exc_info.value.classification is RayJobRequestPreparationRejection.INTEGRITY_MISMATCH
        assert self.storage_events == ["reserve", "prepare", "release"]

    def test_submission_handle_exposes_the_identity_before_submit(self) -> None:
        runner = RayJobRunner()
        task_execution = SimpleNamespace(
            pk=123,
            task_id="django-task-123",
            attempt_number=2,
            execution_generation=11,
            ray_target_address="ray://selected-cluster:10001",
            ray_address="ray://stale-cluster:10001",
        )

        handle = runner.submission_handle(task_execution)

        assert handle.ray_job_id == RayJobRunner.submission_id(task_execution)
        assert handle.ray_address == "ray://selected-cluster:10001"
        assert handle.submitted_at.tzinfo is UTC

    def test_submit_uses_bounded_request_reference_entrypoint(self, monkeypatch) -> None:
        """The command line carries only an opaque request locator."""
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)

        task_execution = SimpleNamespace(
            pk=123,
            task_id="django-task-123",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=2,
            execution_generation=11,
            execution_protocol_version=1,
        )

        handle = runner.submit(
            task_execution=task_execution,
            callable_path="testproject.tasks.echo_task",
            args=("it's broken",),
            kwargs={"publisher": "O'Reilly"},
        )

        assert handle.ray_job_id == RayJobRunner.submission_id(task_execution)
        assert handle.ray_job_id.startswith(STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX)
        assert len(handle.ray_job_id) == (
            len(STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX) + 64
        )
        assert len(fake_client.submissions) == 1
        assert len(self.prepared_requests) == 1

        submission = fake_client.submissions[0]
        assert submission["submission_id"] == handle.ray_job_id
        entrypoint = str(submission["entrypoint"])
        prefix = "python -m django_ray.runtime.entrypoint --request-ref-b64 "

        assert entrypoint.startswith(prefix)
        assert "it's broken" not in entrypoint
        assert "O'Reilly" not in entrypoint

        prepared = self.prepared_requests[0]
        assert entrypoint.removeprefix(prefix) == prepared.encoded_locator
        payload_json = prepared.serialized_request
        payload = json.loads(payload_json)

        assert payload["callable_path"] == "testproject.tasks.echo_task"
        assert json.loads(payload["serialized_args"]) == ["it's broken"]
        assert json.loads(payload["serialized_kwargs"]) == {"publisher": "O'Reilly"}
        assert payload["task_execution_pk"] == 123
        assert payload["task_id"] == "django-task-123"
        assert payload["attempt_number"] == 2
        assert payload["execution_generation"] == 11
        assert payload["request_schema"] == "django-ray.execution-request"
        assert payload["request_schema_version"] == 1
        assert payload["execution_protocol_version"] == 1
        assert payload["compiled_graph_submission_transport"] == "ray-job"
        assert payload["runtime_env_profile"] is None
        assert len(payload["runtime_env_hash"]) == 64
        assert payload["runtime_env_plan_identity"]["plan_format"] == (
            "django-ray.runtime-env-plan"
        )
        assert payload["runtime_env_plan_identity"]["plan_format_version"] == 1
        assert payload["runtime_env_plan_identity"]["reusable"] is True
        assert payload["runtime_env_plan_identity"]["unresolved_paths"] == []
        metadata = submission["metadata"]
        assert isinstance(metadata, dict)
        assert set(metadata) == {
            "django_ray_request_binding",
            "django_ray_coordination_sha256",
            "django_ray_execution_protocol_version",
            "django_ray_request_sha256",
            "django_ray_request_size_bytes",
            "django_ray_request_reference_sha256",
            "django_ray_request_locator_sha256",
            "django_ray_submission_transport",
        }
        assert task_execution.ray_job_request_reference == prepared.reference
        expectation = parse_ray_job_request_metadata(metadata, required=True)
        assert isinstance(expectation, RayJobRequestReferenceExpectation)
        assert expectation.execution_protocol_version == 1
        assert expectation.request_sha256 == request_sha256(payload_json)
        assert expectation.request_reference_sha256 == request_reference_sha256(prepared.reference)
        assert expectation.request_locator_sha256 == request_locator_sha256(
            prepared.encoded_locator
        )
        assert expectation.submission_transport == RAY_JOB_REQUEST_REFERENCE_TRANSPORT
        validate_ray_job_request_reference_expectation(
            expectation,
            expected_identity=ExecutionIdentity(123, "django-task-123", 2, 11),
            expected_execution_protocol_version=1,
            serialized_request=payload_json,
            request_reference=prepared.reference,
            request_locator=prepared.encoded_locator,
        )

    def test_submit_prepares_and_binds_before_client_or_runtime_upload(
        self,
        monkeypatch,
    ) -> None:
        events = self.storage_events

        class OrderedClient(FakeJobClient):
            def _upload_working_dir_if_needed(self, runtime_env) -> None:
                events.append("upload_working_dir")
                super()._upload_working_dir_if_needed(runtime_env)

            def _upload_py_modules_if_needed(self, runtime_env) -> None:
                events.append("upload_py_modules")
                super()._upload_py_modules_if_needed(runtime_env)

            def submit_job(self, **kwargs: object) -> str:
                events.append("submit")
                return super().submit_job(**kwargs)

        client = OrderedClient()
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda _address=None: events.append("client") or client,
        )
        task = SimpleNamespace(
            pk=201,
            task_id="django-task-201",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=5,
            execution_protocol_version=1,
        )

        runner.submit(
            task_execution=task,
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        assert events == [
            "reserve",
            "prepare",
            "attach",
            "client",
            "upload_working_dir",
            "upload_py_modules",
            "submit",
        ]
        assert task.ray_job_request_reference == self.prepared_requests[0].reference

    def test_oversized_request_is_fixed_before_storage_client_or_upload(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "django_ray.execution_codec.EXECUTION_REQUEST_MAX_BYTES",
            128,
        )
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda _address=None: pytest.fail("Ray Job client was opened"),
        )
        task = SimpleNamespace(
            pk=202,
            task_id="django-task-202",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=6,
            execution_protocol_version=1,
        )

        with pytest.raises(RayJobRequestPreparationError) as exc_info:
            runner.submit(
                task_execution=task,
                callable_path="testproject.tasks.echo_task",
                args=("oversized" * 100,),
                kwargs={},
            )

        assert exc_info.value.classification is (RayJobRequestPreparationRejection.RESOURCE_LIMIT)
        assert exc_info.value.requires_nonretryable_disposition is True
        assert self.prepared_requests == []
        assert self.storage_events == ["reserve", "release"]
        assert task.ray_job_id is None
        assert task.ray_address is None
        assert task.ray_job_request_reference is None

    @pytest.mark.parametrize(
        ("storage_rejection", "runner_rejection", "nonretryable"),
        [
            (
                RayJobRequestStorageRejection.CONFIGURATION,
                RayJobRequestPreparationRejection.CONFIGURATION,
                True,
            ),
            (
                RayJobRequestStorageRejection.STORAGE_UNAVAILABLE,
                RayJobRequestPreparationRejection.STORAGE_UNAVAILABLE,
                False,
            ),
        ],
    )
    def test_storage_preparation_failure_is_fixed_and_definite(
        self,
        monkeypatch,
        storage_rejection,
        runner_rejection,
        nonretryable,
    ) -> None:
        storage_error = RayJobRequestStorageError(storage_rejection)
        monkeypatch.setattr(
            "django_ray.runner.ray_job.prepare_ray_job_request",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(storage_error),
        )
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda _address=None: pytest.fail("Ray Job client was opened"),
        )
        task = SimpleNamespace(
            pk=203,
            task_id="django-task-203",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=7,
            execution_protocol_version=1,
        )

        with pytest.raises(RayJobRequestPreparationError) as exc_info:
            runner.submit(
                task_execution=task,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert exc_info.value.classification is runner_rejection
        assert exc_info.value.requires_nonretryable_disposition is nonretryable
        assert exc_info.value.__cause__ is storage_error
        assert self.storage_events == ["reserve", "release"]
        assert task.ray_job_request_reference is None

    def test_submit_rejects_a_returned_identity_mismatch(self, monkeypatch) -> None:
        alternate_submission_id = (
            f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{'f' * 64}"
        )

        class MismatchedJobClient(FakeJobClient):
            def submit_job(self, **kwargs: object) -> str:
                super().submit_job(**kwargs)
                return alternate_submission_id

        fake_client = MismatchedJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        task_execution = SimpleNamespace(
            pk=129,
            task_id="django-task-129",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=4,
            execution_generation=15,
            execution_protocol_version=1,
        )

        with pytest.raises(
            RayJobSubmissionUncertainError,
            match="unexpected submission ID",
        ) as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        expected_id = RayJobRunner.submission_id(task_execution)
        assert exc_info.value.submission_id == expected_id
        assert exc_info.value.observed_submission_id is None
        assert exc_info.value.__cause__ is None
        assert fake_client.submissions[0]["submission_id"] == expected_id

    def test_submit_does_not_retain_or_render_an_untrusted_returned_identity(
        self,
        monkeypatch,
    ) -> None:
        class HostileIdentity:
            def __repr__(self) -> str:
                raise AssertionError("untrusted identity was rendered")

            def __eq__(self, _other: object) -> bool:
                raise AssertionError("untrusted identity was compared")

            def __ne__(self, _other: object) -> bool:
                raise AssertionError("untrusted identity was compared")

        class MismatchedJobClient(FakeJobClient):
            def __init__(self, returned_value: object) -> None:
                super().__init__()
                self.returned_value = returned_value

            def submit_job(self, **kwargs: object) -> Any:
                super().submit_job(**kwargs)
                return self.returned_value

        returned_values: tuple[object, ...] = (
            "private-returned-id-" + "x" * 20_000,
            ["unhashable-returned-id"],
            HostileIdentity(),
        )
        for offset, returned_value in enumerate(returned_values):
            fake_client = MismatchedJobClient(returned_value)
            runner = RayJobRunner()
            monkeypatch.setattr(
                runner,
                "_get_client",
                lambda _ray_address=None, client=fake_client: client,
            )
            task_execution = SimpleNamespace(
                pk=230 + offset,
                task_id=f"django-task-private-return-{offset}",
                runtime_env_profile=None,
                runtime_env_json="{}",
                runtime_env_hash="",
                attempt_number=1,
                execution_generation=offset,
                execution_protocol_version=1,
            )

            with pytest.raises(RayJobSubmissionUncertainError) as exc_info:
                runner.submit(
                    task_execution=task_execution,
                    callable_path="testproject.tasks.echo_task",
                    args=(),
                    kwargs={},
                )

            assert str(exc_info.value).endswith("submit_job returned an unexpected submission ID")
            assert "private-returned-id" not in str(exc_info.value)
            assert exc_info.value.observed_submission_id is None

    def test_submit_durable_uses_persisted_opaque_inputs_without_hydration(
        self,
        monkeypatch,
    ) -> None:
        import django_ray.runner.ray_job as ray_job_module

        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        monkeypatch.setattr(
            ray_job_module,
            "serialize_args",
            lambda _value: pytest.fail("durable input was hydrated and reserialized"),
        )
        task_execution = SimpleNamespace(
            pk=141,
            task_id="django-task-141",
            callable_path="testproject.tasks.persisted_callable",
            args_json='["persisted"]',
            kwargs_json='{"source":"row"}',
            input_reference=None,
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=3,
            execution_generation=19,
            execution_protocol_version=1,
        )

        runner.submit_durable(task_execution)

        request = self.prepared_requests[0].request
        assert request.callable_path == task_execution.callable_path
        assert request.serialized_args == task_execution.args_json
        assert request.serialized_kwargs == task_execution.kwargs_json
        assert request.compiled_graph_submission_transport == "ray-job"

    def test_submit_wraps_only_the_submission_rpc_as_uncertain(self, monkeypatch) -> None:
        submission_error = TimeoutError("response timed out after acceptance")

        class TimingOutJobClient(FakeJobClient):
            def submit_job(self, **kwargs: object) -> str:
                self.submissions.append(kwargs)
                raise submission_error

        fake_client = TimingOutJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        task_execution = SimpleNamespace(
            pk=130,
            task_id="django-task-130",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=16,
            execution_protocol_version=1,
        )

        with pytest.raises(RayJobSubmissionUncertainError) as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        expected_id = RayJobRunner.submission_id(task_execution)
        assert exc_info.value.submission_id == expected_id
        assert exc_info.value.__cause__ is submission_error
        assert fake_client.submissions[0]["submission_id"] == expected_id

    def test_submit_treats_post_request_snapshot_cleanup_failure_as_uncertain(
        self,
        monkeypatch,
    ) -> None:
        import django_ray.runner.ray_job as ray_job_module

        cleanup_error = PermissionError("temporary snapshot cleanup failed")
        original_snapshot = ray_job_module.snapshot_local_runtime_env

        @contextmanager
        def cleanup_fails(runtime_env):
            with original_snapshot(runtime_env) as immutable_snapshot:
                yield immutable_snapshot
            raise cleanup_error

        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        monkeypatch.setattr(ray_job_module, "snapshot_local_runtime_env", cleanup_fails)
        task_execution = SimpleNamespace(
            pk=132,
            task_id="django-task-132",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=18,
            execution_protocol_version=1,
        )

        with pytest.raises(RayJobSubmissionUncertainError) as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        expected_id = RayJobRunner.submission_id(task_execution)
        assert exc_info.value.submission_id == expected_id
        assert exc_info.value.__cause__ is cleanup_error
        assert fake_client.submissions[0]["submission_id"] == expected_id

    def test_submit_keeps_pre_request_errors_definite(self, monkeypatch) -> None:
        address_error = ConnectionError("selected Ray dashboard is unavailable")
        runner = RayJobRunner()

        def fail_before_request(_ray_address=None):
            raise address_error

        monkeypatch.setattr(runner, "_get_client", fail_before_request)
        task_execution = SimpleNamespace(
            pk=131,
            task_id="django-task-131",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=17,
            execution_protocol_version=1,
        )

        with pytest.raises(ConnectionError) as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert exc_info.value is address_error

    def test_submit_keeps_runtime_env_secrets_out_of_plan_identity_payload(
        self,
        monkeypatch,
    ) -> None:
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        runtime_env = normalize_runtime_env(
            {"env_vars": {"API_TOKEN": "do-not-persist"}},
            profile="secret-profile",
        )
        task_execution = SimpleNamespace(
            pk=125,
            task_id="django-task-125",
            runtime_env_profile=runtime_env.profile,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
            attempt_number=1,
            execution_generation=2,
            execution_protocol_version=1,
        )

        runner.submit(
            task_execution=task_execution,
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        payload = json.loads(self.prepared_requests[0].serialized_request)
        assert "do-not-persist" not in json.dumps(payload)
        assert payload["runtime_env_plan_identity"]["reusable"] is False
        assert payload["runtime_env_plan_identity"]["unresolved_paths"] == [
            "spec.env_vars.API_TOKEN.value"
        ]

    def test_submit_rejects_corrupt_runtime_env_before_upload_or_request(
        self,
        monkeypatch,
    ) -> None:
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda _ray_address=None: pytest.fail("Ray Job client was opened"),
        )
        runtime_env = normalize_runtime_env(
            {"env_vars": {"VALUE": "arbitrary-customer-marker-7cf3"}},
            profile="thin",
        )
        task_execution = SimpleNamespace(
            pk=126,
            task_id="django-task-126-corrupt",
            runtime_env_profile=runtime_env.profile,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash="0" * 64,
            attempt_number=1,
            execution_generation=2,
            execution_protocol_version=1,
        )

        with pytest.raises(RuntimeEnvSnapshotError, match="hash does not match") as exc_info:
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert "arbitrary-customer-marker-7cf3" not in str(exc_info.value)

    def test_submit_keeps_large_runtime_env_identity_out_of_entrypoint(self, monkeypatch) -> None:
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        runtime_env = normalize_runtime_env(
            {"excludes": [f"{'x' * 2040}{index:04d}" for index in range(1024)]}
        )
        task_execution = SimpleNamespace(
            pk=126,
            task_id="django-task-126",
            runtime_env_profile=None,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
            attempt_number=1,
            execution_generation=2,
            execution_protocol_version=1,
        )

        runner.submit(
            task_execution=task_execution,
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        entrypoint = str(fake_client.submissions[0]["entrypoint"])
        assert len(entrypoint.encode("utf-8")) < 32 * 1024

    def test_submit_uploads_immutable_local_runtime_env_snapshots(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        working_dir = tmp_path / "working-dir"
        working_dir.mkdir()
        (working_dir / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        py_module = tmp_path / "shared_module"
        py_module.mkdir()
        (py_module / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
        runtime_env = normalize_runtime_env(
            {
                "working_dir": str(working_dir),
                "py_modules": [str(py_module)],
            }
        )
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        task_execution = SimpleNamespace(
            pk=127,
            task_id="django-task-127",
            runtime_env_profile=None,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
            attempt_number=1,
            execution_generation=2,
            execution_protocol_version=1,
        )

        runner.submit(
            task_execution=task_execution,
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        submitted_runtime_env = fake_client.submissions[0]["runtime_env"]
        assert isinstance(submitted_runtime_env, dict)
        assert str(submitted_runtime_env["working_dir"]).startswith("gcs://_ray_pkg_")
        assert len(str(submitted_runtime_env["working_dir"]).split("_")[-1]) == 20
        submitted_py_modules = submitted_runtime_env["py_modules"]
        assert isinstance(submitted_py_modules, list)
        assert str(submitted_py_modules[0]).startswith("gcs://_ray_pkg_")
        assert len(str(submitted_py_modules[0]).split("_")[-1]) == 20

    def test_submit_rejects_local_source_mutation_before_job_creation(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        working_dir = tmp_path / "working-dir"
        working_dir.mkdir()
        source = working_dir / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        runtime_env = normalize_runtime_env({"working_dir": str(working_dir)})

        class MutatingJobClient(FakeJobClient):
            def _upload_working_dir_if_needed(self, runtime_env: dict[str, object]) -> None:
                source.write_text("VALUE = 2\n", encoding="utf-8")
                super()._upload_working_dir_if_needed(runtime_env)

        fake_client = MutatingJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        task_execution = SimpleNamespace(
            pk=128,
            task_id="django-task-128",
            runtime_env_profile=None,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
            attempt_number=1,
            execution_generation=2,
            execution_protocol_version=1,
        )

        with pytest.raises(WorkflowPlanMismatchError, match="changed"):
            runner.submit(
                task_execution=task_execution,
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert fake_client.submissions == []

    def test_submit_transports_external_input_by_reference_only(self, monkeypatch) -> None:
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)
        reference = "s3://inputs/django-ray/inputs/aa/bb/" + "a" * 64 + ".json?bytes=4"
        task_execution = SimpleNamespace(
            pk=124,
            task_id="django-task-124",
            input_reference=reference,
            args_json="null",
            kwargs_json="null",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
            attempt_number=1,
            execution_generation=2,
            execution_protocol_version=1,
        )

        runner.submit(
            task_execution=task_execution,
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        payload = json.loads(self.prepared_requests[0].serialized_request)
        assert payload["transport_version"] == 2
        assert payload["input_reference"] == reference
        assert payload["serialized_args"] == "null"
        assert payload["serialized_kwargs"] == "null"
        assert payload["compiled_graph_submission_transport"] == "ray-job"

    def test_submit_uses_runtime_env_and_configured_ray_address(self, monkeypatch) -> None:
        """Submit should pass configured runtime_env and keep configured ray_address."""
        fake_client = FakeJobClient()
        addresses: list[str | None] = []
        monkeypatch.setattr(
            "django_ray.runner.ray_job.get_settings",
            lambda: _runner_settings("ray://unit-test:10001"),
        )
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda ray_address=None: addresses.append(ray_address) or fake_client,
        )

        runtime_env = normalize_runtime_env(
            {"env_vars": {"MY_ENV": "1"}},
            profile="custom",
        )
        handle = runner.submit(
            task_execution=SimpleNamespace(
                pk=55,
                task_id="django-task-55",
                runtime_env_profile=runtime_env.profile,
                runtime_env_json=runtime_env.serialized,
                runtime_env_hash=runtime_env.digest,
                attempt_number=1,
                execution_generation=4,
                execution_protocol_version=1,
            ),
            callable_path="testproject.tasks.add_numbers",
            args=(1, 2),
            kwargs={},
        )

        assert handle.ray_address == "ray://unit-test:10001"
        assert addresses == ["ray://unit-test:10001"]
        submission = fake_client.submissions[0]
        assert submission["runtime_env"] == {"env_vars": {"MY_ENV": "1"}}

    def test_submit_decrypts_stored_runtime_env_before_job_submission(
        self,
        monkeypatch,
        settings,
    ) -> None:
        key = base64.urlsafe_b64encode(bytes(reversed(range(32)))).rstrip(b"=").decode("ascii")
        encryption_config = {
            "RUNTIME_ENV_STORAGE_MODE": "encrypted",
            "RUNTIME_ENV_ENCRYPTION_KEYS": {"runner-key": key},
            "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "runner-key",
        }
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "ray://encrypted-job:10001",
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(Path.cwd()),
            **encryption_config,
        }
        runtime_env = normalize_runtime_env(
            {"env_vars": {"EXECUTION_MODE": "encrypted-ray-job"}},
            profile="encrypted-job",
        )
        task_id = "encrypted-ray-job-task"
        stored = runtime_env_for_storage(
            runtime_env,
            task_id=task_id,
            config=encryption_config,
        )
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: fake_client)

        runner.submit(
            task_execution=SimpleNamespace(
                pk=56,
                task_id=task_id,
                runtime_env_profile=stored.profile,
                runtime_env_json=stored.serialized,
                runtime_env_hash=stored.digest,
                attempt_number=1,
                execution_generation=4,
                execution_protocol_version=1,
            ),
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        assert stored.serialized != runtime_env.serialized
        submission = fake_client.submissions[0]
        assert submission["runtime_env"] == runtime_env.spec
        payload = json.loads(self.prepared_requests[0].serialized_request)
        assert payload["runtime_env_profile"] == runtime_env.profile
        assert payload["runtime_env_hash"] == runtime_env.digest
        assert payload["runtime_env_plan_identity"]["profile"] == runtime_env.profile
        metadata = submission["metadata"]
        assert isinstance(metadata, dict)
        assert "runtime_env_profile" not in metadata
        assert "runtime_env_hash" not in metadata

    def test_get_client_pins_configured_address_against_ray_environment(
        self,
        monkeypatch,
    ) -> None:
        """Ambient Ray variables must not replace durable django-ray routing."""
        from ray import __version__ as ray_version
        from ray.dashboard.modules.job import sdk as job_sdk

        resolved: list[str] = []

        def resolve_submission_address(address: str) -> str:
            resolved.append(address)
            return "http://alias-dashboard:8265"

        monkeypatch.setattr(
            "django_ray.runner.ray_job._resolve_submission_address",
            resolve_submission_address,
        )
        monkeypatch.setattr(
            job_sdk.JobSubmissionClient,
            "_check_connection_and_version",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setenv("RAY_ADDRESS", "http://global-dashboard:8265")
        monkeypatch.setenv("RAY_API_SERVER_ADDRESS", "http://api-override:8265")
        monkeypatch.setattr(
            "django_ray.runner.ray_job.get_settings",
            lambda: _runner_settings("ray://alias-head:10001"),
        )

        client = RayJobRunner()._get_client()

        assert isinstance(client, job_sdk.JobSubmissionClient)
        assert resolved == ["ray://alias-head:10001"]
        assert client._address == "http://alias-dashboard:8265"
        assert client._cookies is None
        assert client._default_metadata == {}
        assert client._headers == {}
        assert client._verify is True
        assert client._ssl_context is None
        assert client._client_ray_version == ray_version

    def test_address_pinned_client_bounds_version_and_control_requests(
        self,
        monkeypatch,
    ) -> None:
        from ray import __version__ as ray_version
        from ray.dashboard.modules.dashboard_sdk import SubmissionClient

        requests: list[tuple[str, float | None]] = []

        class VersionResponse:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, str]:
                return {"ray_version": ray_version}

        def record_request(
            _client,
            _method,
            endpoint,
            *,
            data=None,
            json_data=None,
            **kwargs,
        ):
            del data, json_data
            requests.append((endpoint, kwargs.get("timeout")))
            return VersionResponse()

        monkeypatch.setattr(
            "django_ray.runner.ray_job._resolve_submission_address",
            lambda _address: "http://alias-dashboard:8265",
        )
        monkeypatch.setattr(SubmissionClient, "_do_request", record_request)

        client = _address_pinned_job_client("ray://alias-head:10001")
        client._do_request("GET", "/unbounded")
        with _bounded_control_requests(client):
            client._do_request("GET", "/bounded")

        assert requests == [
            ("/api/version", _CONTROL_REQUEST_TIMEOUT_SECONDS),
            ("/unbounded", None),
            ("/bounded", _CONTROL_REQUEST_TIMEOUT_SECONDS),
        ]

    def test_submit_uses_persisted_backend_target(self, monkeypatch) -> None:
        """Each backend alias must submit against its persisted Ray cluster."""
        addresses: list[str | None] = []
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda ray_address=None: addresses.append(ray_address) or fake_client,
        )

        for index, address in enumerate(
            ("ray://alias-a:10001", "ray://alias-b:10001"),
            start=1,
        ):
            handle = runner.submit(
                task_execution=SimpleNamespace(
                    pk=index,
                    task_id=f"django-task-{index}",
                    ray_target_address=address,
                    ray_address="ray://stale-handle:10001",
                    runtime_env_profile=None,
                    runtime_env_json="{}",
                    runtime_env_hash="",
                    attempt_number=1,
                    execution_generation=0,
                    execution_protocol_version=1,
                ),
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )
            assert handle.ray_address == address

        assert addresses == ["ray://alias-a:10001", "ray://alias-b:10001"]

    def test_submit_accepts_legacy_address_without_dedicated_target(self, monkeypatch) -> None:
        addresses: list[str | None] = []
        fake_client = FakeJobClient()
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda ray_address=None: addresses.append(ray_address) or fake_client,
        )

        handle = runner.submit(
            task_execution=SimpleNamespace(
                pk=3,
                task_id="django-task-3",
                ray_address="ray://legacy:10001",
                runtime_env_profile=None,
                runtime_env_json="{}",
                runtime_env_hash="",
                attempt_number=1,
                execution_generation=0,
                execution_protocol_version=1,
            ),
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        assert handle.ray_address == "ray://legacy:10001"
        assert addresses == ["ray://legacy:10001"]


class TestRayJobRunnerStatusAndControl:
    """Tests for get_status/cancel/get_logs behavior."""

    @staticmethod
    def _make_handle(job_id: str = "raysubmit_test_001") -> SubmissionHandle:
        return SubmissionHandle(
            ray_job_id=job_id,
            ray_address="ray://test:10001",
            submitted_at=datetime.now(UTC),
        )

    def test_get_status_maps_known_states(self, monkeypatch) -> None:
        """Known Ray status values should map to JobStatus correctly."""
        runner = RayJobRunner()
        expected = {
            "PENDING": JobStatus.PENDING,
            "RUNNING": JobStatus.RUNNING,
            "SUCCEEDED": JobStatus.SUCCEEDED,
            "FAILED": JobStatus.FAILED,
            "STOPPED": JobStatus.STOPPED,
        }

        for raw_status, mapped in expected.items():
            message = f"msg-{raw_status}"
            client = SimpleNamespace(
                get_job_status=lambda _job_id, value=raw_status: value,
                get_job_info=lambda _job_id, msg=message: SimpleNamespace(
                    message=msg,
                    start_time=123,
                    end_time=456,
                ),
            )
            monkeypatch.setattr(
                runner, "_get_client", lambda _ray_address=None, client=client: client
            )

            info = runner.get_status(self._make_handle())

            assert info.status == mapped
            assert info.message == f"msg-{raw_status}"
            assert info.start_time == 123
            assert info.end_time == 456

    def test_get_status_uses_unknown_for_unmapped_state(self, monkeypatch) -> None:
        runner = RayJobRunner()
        client = SimpleNamespace(
            get_job_status=lambda _job_id: "MYSTERY",
            get_job_info=lambda _job_id: SimpleNamespace(message="mystery"),
        )
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: client)

        info = runner.get_status(self._make_handle())

        assert info.status == JobStatus.UNKNOWN
        assert info.message == "mystery"

    def test_get_status_exposes_only_fixed_protocol_metadata_and_exit_code(
        self,
        monkeypatch,
    ) -> None:
        _request, _serialized, metadata = TestRayJobRequestBinding._request()
        client = SimpleNamespace(
            get_job_status=lambda _job_id: "FAILED",
            get_job_info=lambda _job_id: SimpleNamespace(
                message="driver failed",
                metadata={"customer_secret": "do-not-retain", **metadata},
                driver_exit_code=78,
            ),
        )
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: client)

        info = runner.get_status(self._make_handle())

        assert info.status is JobStatus.FAILED
        assert info.driver_exit_code == 78
        assert info.metadata is not None
        assert "customer_secret" not in info.metadata
        assert parse_ray_job_request_metadata(info.metadata, required=True) is not None

    def test_get_status_discards_non_integer_driver_exit_code(self, monkeypatch) -> None:
        client = SimpleNamespace(
            get_job_status=lambda _job_id: "FAILED",
            get_job_info=lambda _job_id: SimpleNamespace(
                message="driver failed",
                metadata=None,
                driver_exit_code=True,
            ),
        )
        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: client)

        info = runner.get_status(self._make_handle())

        assert info.driver_exit_code is None
        assert info.metadata is None

    def test_get_status_returns_unknown_on_client_exception(self, monkeypatch) -> None:
        runner = RayJobRunner()

        class FailingClient:
            def get_job_status(self, _job_id):
                raise RuntimeError("ray api unavailable")

        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: FailingClient())

        info = runner.get_status(self._make_handle("raysubmit_fail_001"))

        assert info.status == JobStatus.UNKNOWN
        assert info.job_id == "raysubmit_fail_001"
        assert "ray api unavailable" in (info.message or "")

    def test_status_and_cancellation_survive_broken_exception_messages(self, monkeypatch) -> None:
        calls = 0

        class BrokenControlError(RuntimeError):
            def __str__(self) -> str:
                nonlocal calls
                calls += 1
                raise RuntimeError("secondary password=do-not-expose")

        class FailingClient:
            def get_job_status(self, _job_id):
                raise BrokenControlError()

            def stop_job(self, _job_id):
                raise BrokenControlError()

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: FailingClient())
        handle = self._make_handle("raysubmit_broken_control_001")

        info = runner.get_status(handle)
        cancellation = runner.cancel_with_status(handle)

        assert info.status == JobStatus.UNKNOWN
        assert info.message == "exception message unavailable"
        assert cancellation.status == CancellationOutcomeStatus.INDETERMINATE
        assert cancellation.message == (
            "Ray Job stop request raised BrokenControlError: exception message unavailable"
        )
        assert "secondary password" not in (cancellation.message or "")
        assert calls == 2

    def test_control_methods_normalize_client_construction_failure(self, monkeypatch) -> None:
        runner = RayJobRunner()

        def unavailable(_ray_address=None):
            raise TimeoutError("ray dashboard request timed out")

        monkeypatch.setattr(runner, "_get_client", unavailable)
        handle = self._make_handle("raysubmit_client_unavailable_001")

        info = runner.get_status(handle)
        cancellation = runner.cancel_with_status(handle)

        assert info.status == JobStatus.UNKNOWN
        assert info.job_id == handle.ray_job_id
        assert "ray dashboard request timed out" in (info.message or "")
        assert cancellation.status == CancellationOutcomeStatus.INDETERMINATE
        assert "ray dashboard request timed out" in (cancellation.message or "")
        assert runner.get_logs(handle) is None

    def test_status_logs_and_cancellation_use_handle_address(self, monkeypatch) -> None:
        """Control-plane calls must stay on the cluster recorded in the handle."""
        addresses: list[str | None] = []

        class Client:
            def get_job_status(self, _job_id: str) -> str:
                return "RUNNING"

            def get_job_info(self, _job_id: str) -> SimpleNamespace:
                return SimpleNamespace(message=None)

            def get_job_logs(self, _job_id: str) -> str:
                return "logs"

            def stop_job(self, _job_id: str) -> bool:
                return True

        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda ray_address=None: addresses.append(ray_address) or Client(),
        )
        handle = self._make_handle()

        assert runner.get_status(handle).status == JobStatus.RUNNING
        assert runner.get_logs(handle) == "logs"
        assert runner.cancel(handle) is True
        assert addresses == ["ray://test:10001"] * 3

    def test_cancel_returns_true_on_success(self, monkeypatch) -> None:
        stopped: list[str] = []

        class Client:
            def stop_job(self, job_id: str) -> bool:
                stopped.append(job_id)
                return True

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: Client())

        ok = runner.cancel(self._make_handle("raysubmit_cancel_001"))

        assert ok is True
        assert stopped == ["raysubmit_cancel_001"]

    def test_cancel_returns_false_when_job_was_not_running(self, monkeypatch) -> None:
        class Client:
            def stop_job(self, _job_id: str) -> bool:
                return False

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: Client())
        handle = self._make_handle("raysubmit_cancel_not_running_001")

        assert runner.cancel(handle) is False
        outcome = runner.cancel_with_status(handle)
        assert outcome.status == CancellationOutcomeStatus.NOT_APPLICABLE
        assert "not running" in (outcome.message or "")

    def test_cancel_returns_false_on_exception(self, monkeypatch) -> None:
        class Client:
            def stop_job(self, _job_id: str) -> None:
                raise RuntimeError("cannot stop")

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: Client())

        ok = runner.cancel(self._make_handle("raysubmit_cancel_002"))

        assert ok is False

    def test_cancel_with_status_preserves_indeterminate_api_failure(self, monkeypatch) -> None:
        class Client:
            def stop_job(self, _job_id: str) -> None:
                raise RuntimeError("cannot stop")

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: Client())

        outcome = runner.cancel_with_status(self._make_handle("raysubmit_cancel_003"))

        assert outcome.status == CancellationOutcomeStatus.INDETERMINATE
        assert "cannot stop" in (outcome.message or "")

    def test_get_logs_returns_none_on_exception(self, monkeypatch) -> None:
        class Client:
            def get_job_logs(self, _job_id: str) -> str:
                raise RuntimeError("logs unavailable")

        runner = RayJobRunner()
        monkeypatch.setattr(runner, "_get_client", lambda _ray_address=None: Client())

        logs = runner.get_logs(self._make_handle("raysubmit_logs_001"))

        assert logs is None

    def test_get_logs_returns_log_content(self, monkeypatch) -> None:
        runner = RayJobRunner()
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda _ray_address=None: SimpleNamespace(
                get_job_logs=lambda _job_id: "line-1\nline-2"
            ),
        )

        logs = runner.get_logs(self._make_handle("raysubmit_logs_002"))

        assert logs == "line-1\nline-2"
