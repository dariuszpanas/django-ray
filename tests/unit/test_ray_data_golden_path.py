"""Hermetic contract tests for the bundled Ray Data golden path."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from django_ray.runtime.context import DurableTaskContext
from testproject.apps.cluster_tasks import ray_data_job, tasks

PROJECT_ROOT = Path(__file__).parents[2]
DEPLOYMENT_KEY = "test-deployment"
TASK_ID = "00000000-0000-4000-8000-000000000041"


class _Vector:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def astype(self, data_type: str, *, copy: bool) -> _Vector:
        assert data_type == "float64"
        assert copy is False
        return _Vector([float(value) for value in self.values])

    def __mul__(self, value: float) -> _Vector:
        return _Vector([item * value for item in self.values])

    def __add__(self, value: float) -> _Vector:
        return _Vector([item + value for item in self.values])

    def tolist(self) -> list[Any]:
        return list(self.values)


def _request(tmp_path: Path) -> tuple[dict[str, Any], Path, Path]:
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    input_path = input_root / "input.jsonl"
    input_path.write_text(
        '{"record_id":"a","value":1}\n{"record_id":"b","value":2}\n{"record_id":"c","value":3}\n',
        encoding="utf-8",
    )
    output_root = tmp_path / "artifacts"
    output_root.mkdir()
    request = {
        "input_uri": input_path.as_uri(),
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "input_root_uri": input_root.as_uri(),
        "output_root_uri": output_root.as_uri(),
        "deployment_key": DEPLOYMENT_KEY,
        "run_key": "orders-2026-08-01",
        "task_id": TASK_ID,
        "application_revision": "app-7f30a1",
        "model_revision": "score-v3",
        "task_execution_pk": 41,
        "execution_generation": 2,
        "attempt_number": 1,
        "scale": 2.0,
        "bias": 1.0,
    }
    return request, input_path, output_root


def _attempt_dir(
    output_root: Path,
    *,
    deployment_key: str = DEPLOYMENT_KEY,
    task_id: str = TASK_ID,
    task_execution_pk: int = 41,
    execution_generation: int = 2,
    attempt_number: int = 1,
) -> Path:
    return (
        output_root
        / "deployments"
        / deployment_key
        / "tasks"
        / task_id
        / "executions"
        / str(task_execution_pk)
        / "runs"
        / "orders-2026-08-01"
        / f"g-{execution_generation}"
        / f"a-{attempt_number:04d}"
    )


class _FakeActorPoolStrategy:
    def __init__(self, *, size: int) -> None:
        assert size == 1
        self.size = size


class _FakeDataset:
    def __init__(self, observation: dict[str, Any], *, input_path: Path | None = None) -> None:
        self.observation = observation
        self.input_path = input_path

    def map_batches(self, callable_type: type[Any], **options: Any) -> _FakeDataset:
        assert callable_type is ray_data_job.DeterministicBatchScorer
        assert isinstance(options.pop("compute"), _FakeActorPoolStrategy)
        constructor = options.pop("fn_constructor_kwargs")
        assert options == {
            "batch_size": 256,
            "batch_format": "numpy",
            "zero_copy_batch": True,
            "udf_modifying_row_count": False,
            "num_cpus": 1,
        }
        scorer = callable_type(**constructor)
        scored = scorer({"value": _Vector([1, 2, 3])})
        self.observation["scores"] = scored["score"].tolist()
        return self

    def write_parquet(self, output_path: str, *, mode: str) -> None:
        assert mode == "error"
        path = Path(output_path)
        path.mkdir(parents=True)
        (path / "worker-output.parquet").write_bytes(b"fake-parquet")
        self.observation["output_path"] = path


class _FakeRayData:
    def __init__(self, observation: dict[str, Any], *, input_path: Path | None = None) -> None:
        self.observation = observation
        self.input_path = input_path

    def read_json(self, input_path: str) -> _FakeDataset:
        self.observation["input_path"] = Path(input_path)
        return _FakeDataset(self.observation, input_path=self.input_path)


def _install_fake_execution(
    monkeypatch: pytest.MonkeyPatch,
    observation: dict[str, Any],
    *,
    input_path: Path | None = None,
) -> None:
    def inspect_output(
        output_dir: Path, parquet: object
    ) -> tuple[int, int, list[dict[str, str]], int, str]:
        del parquet
        _, total_bytes, content_sha256 = ray_data_job._inspect_output_content(output_dir)
        return (
            3,
            1,
            [
                {"name": "record_id", "type": "string"},
                {"name": "value", "type": "int64"},
                {"name": "score", "type": "double"},
            ],
            total_bytes,
            content_sha256,
        )

    monkeypatch.setattr(
        ray_data_job,
        "_load_ray_data",
        lambda: (
            _FakeRayData(observation, input_path=input_path),
            _FakeActorPoolStrategy,
            object(),
        ),
    )
    monkeypatch.setattr(
        ray_data_job,
        "_inspect_parquet_output",
        inspect_output,
    )


def _assert_json_metadata_only(value: Any) -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_metadata_only(item)
        return
    assert isinstance(value, dict)
    for key, item in value.items():
        assert isinstance(key, str)
        _assert_json_metadata_only(item)


def test_deterministic_scorer_preserves_columns_and_adds_scores() -> None:
    values = _Vector([1, 2, 3])
    batch = {"record_id": ("a", "b", "c"), "value": values}

    result = ray_data_job.DeterministicBatchScorer(scale=2.5, bias=-0.5)(batch)

    assert result["record_id"] is batch["record_id"]
    assert result["value"] is values
    assert result["score"].tolist() == [2.0, 4.5, 7.0]
    assert "score" not in batch


def test_deterministic_scorer_rejects_missing_or_non_numeric_values() -> None:
    scorer = ray_data_job.DeterministicBatchScorer(scale=1.0, bias=0.0)

    with pytest.raises(ValueError, match="must contain"):
        scorer({"other": _Vector([1])})
    with pytest.raises(ValueError, match="must be numeric"):
        scorer({"value": _Vector([object()])})


def test_distributed_recipe_module_has_no_django_imports() -> None:
    source = Path(ray_data_job.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert "django" not in roots
    assert "django_ray" not in roots


def test_real_probe_forces_one_disposable_local_ray_runtime() -> None:
    source = (PROJECT_ROOT / "scripts/ray_data_golden_path_probe.py").read_text(encoding="utf-8")

    assert 'address="local"' in source
    uv_hook_guard = 'os.environ["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] = "0"'
    assert uv_hook_guard in source
    assert source.index(uv_hook_guard) < source.index("from testproject")
    assert 'os.environ.pop("RAY_ADDRESS", None)' in source
    assert 'os.environ["RAY_ADDRESS"] = probe_ray_address' in source
    assert "ray.shutdown()" in source
    assert "_build_probe_working_dir_archive(working_dir_archive)" in source
    assert '"ray-data-probe-working-dir.zip"' in source
    assert "_use_preinstalled_probe_dependencies(django_settings)" in source
    assert '"django_ray_worker"' in source
    assert '"ray-data"' in source
    assert '"--cluster"' not in source
    assert '"management_worker_routed": True' in source
    assert '"ray_job_submission": True' in source
    assert '"durable_task_succeeded": True' in source
    assert '"completion_envelope_persisted": True' in source
    assert '"tampered_output_rejected": True' in source
    assert "run_ray_data_batch_job" not in source
    assert '"automatic_retry_recovered": True' in source
    assert '"ray_jobs_submitted": len(submissions)' in source
    assert "_address_pinned_job_client" in source
    assert "_bounded_control_requests" in source
    assert 'statuses != {1: "SUCCEEDED", 2: "SUCCEEDED"}' in source
    assert '"ray_job_transports_succeeded": True' in source
    assert '"strict_ray_job_request_binding": True' in source
    assert '"versioned_completion_envelope": True' in source
    assert '"executor_provenance_archived": True' in source
    assert '"preinstalled_ray_data_environment": True' in source
    assert '"disposable_cluster_target_pinned": True' in source
    assert '"failed_artifact_rejected": failed_artifact_rejected' in source
    assert '"idempotent_artifact_adoption": adopted_again == adopted' in source


def _strict_job_metadata(*, execution_pk: int, attempt: int, generation: int) -> dict[str, str]:
    from django_ray.execution_codec import ExecutionIdentity, ExecutionRequest
    from django_ray.ray_job_protocol import build_ray_job_request_metadata

    return build_ray_job_request_metadata(
        ExecutionRequest(
            identity=ExecutionIdentity(
                task_execution_pk=execution_pk,
                task_id=f"probe-task-{execution_pk}",
                attempt_number=attempt,
                execution_generation=generation,
            ),
            execution_protocol_version=1,
            callable_path="testproject.tasks.add_numbers",
            transport_version=1,
            serialized_args="[]",
            serialized_kwargs="{}",
            input_reference=None,
            runtime_env_profile=None,
            runtime_env_hash="0" * 64,
            runtime_env_plan_identity={},
            compiled_graph_submission_transport="ray-job",
        ),
        "{}",
    )


def test_real_probe_reads_two_retained_job_api_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Let pytest restore the process environment after importing the standalone
    # probe, whose module-level guard intentionally changes this variable.
    monkeypatch.setenv("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "test-sentinel")
    from scripts import ray_data_golden_path_probe as probe

    jobs = [
        SimpleNamespace(metadata={}, submission_id=None, status="RUNNING"),
        SimpleNamespace(
            metadata=_strict_job_metadata(execution_pk=41, attempt=1, generation=7),
            submission_id="raysubmit_django_ray_rq1_" + "1" * 64,
            status=SimpleNamespace(value="SUCCEEDED"),
        ),
        SimpleNamespace(
            metadata=_strict_job_metadata(execution_pk=41, attempt=2, generation=8),
            submission_id="raysubmit_django_ray_rq1_" + "2" * 64,
            status=SimpleNamespace(value="SUCCEEDED"),
        ),
    ]
    addresses: list[str] = []

    class _Client:
        def list_jobs(self) -> list[SimpleNamespace]:
            return jobs

    monkeypatch.setattr(
        "django_ray.runner.ray_job._address_pinned_job_client",
        lambda address: addresses.append(address) or _Client(),
    )

    assert probe._load_ray_job_submission_evidence(
        ray_address="127.0.0.1:6379", execution_pk=41
    ) == {
        1: (7, "raysubmit_django_ray_rq1_" + "1" * 64),
        2: (8, "raysubmit_django_ray_rq1_" + "2" * 64),
    }
    assert addresses == ["127.0.0.1:6379"]


def test_real_probe_waits_for_both_ray_job_transports_to_be_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import ray_data_golden_path_probe as probe

    def jobs(second_status: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                metadata=_strict_job_metadata(execution_pk=41, attempt=1, generation=7),
                submission_id="raysubmit_django_ray_rq1_" + "1" * 64,
                status=SimpleNamespace(value="SUCCEEDED"),
            ),
            SimpleNamespace(
                metadata=_strict_job_metadata(execution_pk=41, attempt=2, generation=8),
                submission_id="raysubmit_django_ray_rq1_" + "2" * 64,
                status=SimpleNamespace(value=second_status),
            ),
        ]

    responses = [jobs("RUNNING"), jobs("SUCCEEDED")]

    class _Client:
        def list_jobs(self) -> list[SimpleNamespace]:
            return responses.pop(0)

    monkeypatch.setattr(
        "django_ray.runner.ray_job._address_pinned_job_client", lambda _address: _Client()
    )
    monkeypatch.setattr(probe.time, "sleep", lambda _seconds: None)

    assert probe._load_ray_job_submission_evidence(
        ray_address="127.0.0.1:6379", execution_pk=41
    ) == {
        1: (7, "raysubmit_django_ray_rq1_" + "1" * 64),
        2: (8, "raysubmit_django_ray_rq1_" + "2" * 64),
    }
    assert responses == []


def test_success_publishes_one_canonical_manifest_and_bounded_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, input_path, output_root = _request(tmp_path)
    observation: dict[str, Any] = {}
    _install_fake_execution(monkeypatch, observation)

    result = ray_data_job.run_ray_data_batch_job(**request)

    attempt_dir = _attempt_dir(output_root)
    completion_path = attempt_dir / "completion.json"
    encoded = completion_path.read_bytes()
    manifest = json.loads(encoded)
    _, expected_output_bytes, expected_output_sha256 = ray_data_job._inspect_output_content(
        attempt_dir / "data"
    )
    assert encoded == ray_data_job._canonical_json(manifest)
    assert manifest == {
        "schema_version": 1,
        "status": "artifact_complete",
        "run": {
            "deployment_key": DEPLOYMENT_KEY,
            "key": "orders-2026-08-01",
            "task_id": TASK_ID,
            "task_execution_pk": 41,
            "execution_generation": 2,
            "attempt_number": 1,
        },
        "input": {"uri": input_path.as_uri(), "sha256": request["input_sha256"]},
        "operation": {"name": "deterministic-batch-score-v1", "scale": 2.0, "bias": 1.0},
        "application": {"revision": "app-7f30a1", "model_revision": "score-v3"},
        "output": {
            "uri": (attempt_dir / "data").as_uri(),
            "format": "parquet",
            "row_count": 3,
            "file_count": 1,
            "total_bytes": expected_output_bytes,
            "content_sha256": expected_output_sha256,
            "schema": [
                {"name": "record_id", "type": "string"},
                {"name": "value", "type": "int64"},
                {"name": "score", "type": "double"},
            ],
        },
        "summary": {"outcome": "artifact_complete"},
    }
    assert observation == {
        "input_path": input_path,
        "scores": [3.0, 5.0, 7.0],
        "output_path": attempt_dir / "data",
    }
    assert result["manifest_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert result["status"] == "artifact_complete"
    assert result["deployment_key"] == DEPLOYMENT_KEY
    assert result["task_id"] == TASK_ID
    assert result["task_execution_pk"] == 41
    assert result["execution_generation"] == 2
    assert result["output_bytes"] == expected_output_bytes
    assert result["output_sha256"] == expected_output_sha256
    assert len(ray_data_job._canonical_json(result)) <= ray_data_job.MAX_RESULT_BYTES
    assert not any(key in result for key in ("dataset", "object_ref", "rows", "batches"))
    _assert_json_metadata_only(result)


def test_post_manifest_fault_is_not_adoptable_without_durable_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, output_root = _request(tmp_path)
    _install_fake_execution(monkeypatch, {})
    original_completion_result = ray_data_job._completion_result

    def fail_after_manifest(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise RuntimeError("simulated post-manifest task fault")

    monkeypatch.setattr(ray_data_job, "_completion_result", fail_after_manifest)
    with pytest.raises(RuntimeError, match="post-manifest task fault"):
        ray_data_job.run_ray_data_batch_job(**request)

    completion_path = _attempt_dir(output_root) / "completion.json"
    manifest, encoded = ray_data_job._read_completion_manifest(completion_path)
    artifact_result = original_completion_result(
        manifest,
        manifest_uri=completion_path.as_uri(),
        manifest_bytes=encoded,
    )
    validation = {
        "result": artifact_result,
        "output_root_uri": request["output_root_uri"],
        "deployment_key": request["deployment_key"],
        "task_id": request["task_id"],
        "task_execution_pk": request["task_execution_pk"],
        "execution_generation": request["execution_generation"],
        "attempt_number": request["attempt_number"],
    }

    with pytest.raises(ray_data_job.ArtifactNotAdoptableError, match="SUCCEEDED"):
        ray_data_job.validate_adoptable_artifact(
            **validation,
            durable_state="FAILED",
        )
    assert (
        ray_data_job.validate_adoptable_artifact(
            **validation,
            durable_state="SUCCEEDED",
        )
        == manifest
    )


def test_completed_attempt_replays_without_ray_or_output_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, output_root = _request(tmp_path)
    observation: dict[str, Any] = {}
    _install_fake_execution(monkeypatch, observation)
    first = ray_data_job.run_ray_data_batch_job(**request)
    completion_path = _attempt_dir(output_root) / "completion.json"
    first_bytes = completion_path.read_bytes()
    first_mtime = completion_path.stat().st_mtime_ns

    monkeypatch.setattr(
        ray_data_job,
        "_load_ray_data",
        lambda: pytest.fail("idempotent replay must not import or execute Ray Data"),
    )
    second = ray_data_job.run_ray_data_batch_job(**request)

    assert second == first
    assert completion_path.read_bytes() == first_bytes
    assert completion_path.stat().st_mtime_ns == first_mtime


def test_completed_attempt_rejects_deleted_output_without_ray(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, output_root = _request(tmp_path)
    _install_fake_execution(monkeypatch, {})
    ray_data_job.run_ray_data_batch_job(**request)
    output_file = next((_attempt_dir(output_root) / "data").glob("*.parquet"))
    output_file.unlink()
    output_file.parent.rmdir()
    monkeypatch.setattr(
        ray_data_job,
        "_load_ray_data",
        lambda: pytest.fail("output verification must fail before importing Ray Data"),
    )

    with pytest.raises(ray_data_job.OutputChangedError, match="no longer matches"):
        ray_data_job.run_ray_data_batch_job(**request)


def test_completed_attempt_rejects_same_size_output_mutation_without_ray(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, output_root = _request(tmp_path)
    _install_fake_execution(monkeypatch, {})
    ray_data_job.run_ray_data_batch_job(**request)
    output_file = next((_attempt_dir(output_root) / "data").glob("*.parquet"))
    original = output_file.read_bytes()
    output_file.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    monkeypatch.setattr(
        ray_data_job,
        "_load_ray_data",
        lambda: pytest.fail("output verification must fail before importing Ray Data"),
    )

    with pytest.raises(ray_data_job.OutputChangedError, match="no longer matches"):
        ray_data_job.run_ray_data_batch_job(**request)


def test_output_content_scan_is_bounded(tmp_path: Path) -> None:
    output_dir = tmp_path / "data"
    output_dir.mkdir()
    for index in range(ray_data_job.MAX_OUTPUT_ENTRIES + 1):
        (output_dir / f"entry-{index:03d}").mkdir()

    with pytest.raises(ray_data_job.RayDataRecipeError, match="entry recipe limit"):
        ray_data_job._inspect_output_content(output_dir)


def test_non_parquet_output_is_never_silently_adopted(tmp_path: Path) -> None:
    output_dir = tmp_path / "data"
    output_dir.mkdir()
    (output_dir / "worker-output.parquet").write_bytes(b"parquet")
    (output_dir / "unbounded-sidecar.bin").write_bytes(b"sidecar")

    with pytest.raises(ray_data_job.RayDataRecipeError, match="unexpected non-Parquet"):
        ray_data_job._inspect_output_content(output_dir)


def test_output_content_rejects_symbolic_links(tmp_path: Path) -> None:
    output_dir = tmp_path / "data"
    output_dir.mkdir()
    target = tmp_path / "target.parquet"
    target.write_bytes(b"parquet")
    link = output_dir / "worker-output.parquet"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this host does not permit unprivileged file symlinks")

    with pytest.raises(ray_data_job.RayDataRecipeError, match="symbolic link"):
        ray_data_job._inspect_output_content(output_dir)


def test_empty_output_has_one_explicit_bounded_identity(tmp_path: Path) -> None:
    output_dir = tmp_path / "data"
    output_dir.mkdir()

    assert ray_data_job._inspect_parquet_output(output_dir, object()) == (
        0,
        0,
        [],
        0,
        hashlib.sha256().hexdigest(),
    )
    ray_data_job._verify_output_content(
        output_dir,
        expected_file_count=0,
        expected_total_bytes=0,
        expected_content_sha256=hashlib.sha256().hexdigest(),
    )
    validation = {
        "deployment_key": DEPLOYMENT_KEY,
        "run_key": "empty-run",
        "task_id": TASK_ID,
        "task_execution_pk": 41,
        "execution_generation": 2,
        "attempt_number": 1,
        "input_uri": (tmp_path / "input.jsonl").as_uri(),
        "input_sha256": "a" * 64,
        "output_uri": output_dir.as_uri(),
        "application_revision": "app-v1",
        "model_revision": "model-v1",
        "scale": 2.0,
        "bias": 1.0,
    }
    manifest = ray_data_job._build_manifest(
        **validation,
        row_count=0,
        file_count=0,
        total_bytes=0,
        content_sha256=hashlib.sha256().hexdigest(),
        output_schema=[],
    )
    ray_data_job._validate_existing_manifest(manifest, **validation)
    manifest["output"]["row_count"] = 1
    with pytest.raises(ray_data_job.CompletionConflictError, match="inconsistent empty"):
        ray_data_job._validate_existing_manifest(manifest, **validation)


def test_successful_empty_writer_publishes_an_explicit_replayable_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, output_root = _request(tmp_path)
    observation: dict[str, Any] = {}
    inspect_parquet_output = ray_data_job._inspect_parquet_output
    _install_fake_execution(monkeypatch, observation)
    monkeypatch.setattr(ray_data_job, "_inspect_parquet_output", inspect_parquet_output)

    def write_no_blocks(dataset: _FakeDataset, output_path: str, *, mode: str) -> None:
        del dataset
        assert mode == "error"
        observation["output_path"] = Path(output_path)

    monkeypatch.setattr(_FakeDataset, "write_parquet", write_no_blocks)

    first = ray_data_job.run_ray_data_batch_job(**request)
    data_dir = _attempt_dir(output_root) / "data"
    assert data_dir.is_dir()
    assert list(data_dir.iterdir()) == []
    assert first["row_count"] == 0
    assert first["file_count"] == 0
    assert first["output_bytes"] == 0
    assert first["output_sha256"] == hashlib.sha256().hexdigest()

    monkeypatch.setattr(
        ray_data_job,
        "_load_ray_data",
        lambda: pytest.fail("an empty completed attempt must replay without Ray Data"),
    )
    assert ray_data_job.run_ray_data_batch_job(**request) == first


def test_completed_output_scan_error_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "data"
    output_dir.mkdir()

    def inaccessible_scan(path: Path) -> object:
        del path
        raise PermissionError("fixture output is inaccessible")

    monkeypatch.setattr(ray_data_job.os, "scandir", inaccessible_scan)

    with pytest.raises(ray_data_job.OutputChangedError, match="no longer matches"):
        ray_data_job._verify_output_content(
            output_dir,
            expected_file_count=0,
            expected_total_bytes=0,
            expected_content_sha256=hashlib.sha256().hexdigest(),
        )


def test_file_hash_and_manifest_reads_stop_at_their_byte_budgets(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.bin"
    oversized.write_bytes(b"12345")

    with pytest.raises(ray_data_job.RayDataRecipeError, match="hashing budget"):
        ray_data_job._sha256_file(oversized, maximum_bytes=4)

    completion = tmp_path / "completion.json"
    completion.write_bytes(b"{" + (b"x" * ray_data_job.MAX_MANIFEST_BYTES))
    with pytest.raises(ray_data_job.CompletionConflictError, match="invalid size"):
        ray_data_job._read_completion_manifest(completion)

    completion.write_bytes(b'{"value":NaN}')
    with pytest.raises(ray_data_job.CompletionConflictError, match="canonical JSON"):
        ray_data_job._read_completion_manifest(completion)


def test_hash_rejects_a_same_size_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.bin"
    replacement = tmp_path / "replacement.bin"
    displaced = tmp_path / "displaced.bin"
    target.write_bytes(b"first")
    replacement.write_bytes(b"other")
    original_open = Path.open
    swapped = False

    def swap_before_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal swapped
        if path == target and not swapped:
            swapped = True
            target.replace(displaced)
            replacement.replace(target)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swap_before_open)

    with pytest.raises(ray_data_job.RayDataRecipeError, match="changed while"):
        ray_data_job._sha256_file(target, maximum_bytes=5, expected_bytes=5)


def test_parquet_metadata_errors_use_the_recipe_error_boundary(tmp_path: Path) -> None:
    output_dir = tmp_path / "data"
    output_dir.mkdir()
    (output_dir / "worker-output.parquet").write_bytes(b"not parquet")

    def broken_parquet_file(path: Path) -> object:
        del path
        raise OSError("corrupt footer")

    with pytest.raises(ray_data_job.RayDataRecipeError, match="metadata could not be inspected"):
        ray_data_job._inspect_parquet_output(
            output_dir,
            SimpleNamespace(ParquetFile=broken_parquet_file),
        )


def test_output_byte_limit_is_enforced_before_parquet_metadata_is_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "data"
    output_dir.mkdir()
    (output_dir / "worker-output.parquet").write_bytes(b"oversized")
    parsed = False

    def parse_unbounded_file(source: object) -> object:
        del source
        nonlocal parsed
        parsed = True
        pytest.fail("Parquet metadata must not be parsed before byte limits pass")

    monkeypatch.setattr(ray_data_job, "MAX_OUTPUT_BYTES", 4)
    with pytest.raises(ray_data_job.RayDataRecipeError, match="byte recipe limit"):
        ray_data_job._inspect_parquet_output(
            output_dir,
            SimpleNamespace(ParquetFile=parse_unbounded_file),
        )
    assert parsed is False


def test_parquet_output_identity_is_revalidated_after_metadata_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "data"
    output_dir.mkdir()
    output_file = output_dir / "worker-output.parquet"
    output_file.write_bytes(b"bounded")
    identities = iter(
        [
            ([output_file], 7, "a" * 64),
            ([output_file], 7, "b" * 64),
        ]
    )

    class EmptySchema:
        def __len__(self) -> int:
            return 0

        def __getitem__(self, index: int) -> object:
            raise IndexError(index)

    parquet_file = SimpleNamespace(
        metadata=SimpleNamespace(num_rows=0),
        schema_arrow=EmptySchema(),
    )
    monkeypatch.setattr(ray_data_job, "_inspect_output_content", lambda _path: next(identities))

    with pytest.raises(ray_data_job.RayDataRecipeError, match="changed while"):
        ray_data_job._inspect_parquet_output(
            output_dir,
            SimpleNamespace(ParquetFile=lambda _source: parquet_file),
        )


def test_schema_field_limit_is_checked_before_iteration() -> None:
    class OversizedSchema:
        def __len__(self) -> int:
            return ray_data_job.MAX_SCHEMA_FIELDS + 1

        def __getitem__(self, index: int) -> object:
            pytest.fail(f"oversized schema field {index} must not be materialized")

    with pytest.raises(ray_data_job.RayDataRecipeError, match="schema exceeds"):
        ray_data_job._bounded_schema(OversizedSchema())


def test_file_uri_rejects_encoded_controls_and_preserves_symlink_checks(tmp_path: Path) -> None:
    request, input_path, _ = _request(tmp_path)
    for encoded_control in ("%0d", "%C2%85"):
        with pytest.raises(ValueError, match="encoded control"):
            ray_data_job._path_from_file_uri(
                input_path.as_uri() + encoded_control,
                label="input_uri",
            )
    with pytest.raises(ValueError, match="invalid percent escaping"):
        ray_data_job._path_from_file_uri(input_path.as_uri() + "%ZZ", label="input_uri")
    with pytest.raises(ValueError, match="remote or UNC"):
        ray_data_job._path_from_file_uri("file:////server/share/input.jsonl", label="input_uri")

    link = input_path.parent / "input-link.jsonl"
    try:
        link.symlink_to(input_path)
    except OSError:
        pytest.skip("this host does not permit unprivileged file symlinks")
    request["input_uri"] = link.as_uri()
    request["input_sha256"] = hashlib.sha256(input_path.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="non-symlink"):
        ray_data_job.run_ray_data_batch_job(**request)


def test_output_root_must_not_be_a_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _, _ = _request(tmp_path)
    actual_output = tmp_path / "actual-output"
    actual_output.mkdir()
    output_link = tmp_path / "output-link"
    try:
        output_link.symlink_to(actual_output, target_is_directory=True)
    except OSError:
        pytest.skip("this host does not permit unprivileged directory symlinks")
    request["output_root_uri"] = output_link.as_uri()
    monkeypatch.setattr(
        ray_data_job,
        "_load_ray_data",
        lambda: pytest.fail("a symlink output root must fail before Ray Data is imported"),
    )

    with pytest.raises(ValueError, match="non-symlink directory"):
        ray_data_job.run_ray_data_batch_job(**request)


def test_configured_output_root_must_exist_before_ray_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, output_root = _request(tmp_path)
    output_root.rmdir()
    monkeypatch.setattr(
        ray_data_job,
        "_load_ray_data",
        lambda: pytest.fail("a missing configured root must fail before Ray Data is imported"),
    )

    with pytest.raises(ValueError, match="configured output root"):
        ray_data_job.run_ray_data_batch_job(**request)


def test_input_must_stay_inside_the_server_controlled_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, _ = _request(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"record_id":"outside","value":9}\n', encoding="utf-8")
    request["input_uri"] = outside.as_uri()
    request["input_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    monkeypatch.setattr(
        ray_data_job,
        "_load_ray_data",
        lambda: pytest.fail("an unconfigured input must fail before Ray Data is imported"),
    )

    with pytest.raises(ValueError, match="configured input root"):
        ray_data_job.run_ray_data_batch_job(**request)


def test_input_and_output_roots_must_not_overlap(tmp_path: Path) -> None:
    request, _, _ = _request(tmp_path)
    request["output_root_uri"] = (tmp_path / "inputs" / "artifacts").as_uri()

    with pytest.raises(ValueError, match="must not overlap"):
        ray_data_job.run_ray_data_batch_job(**request)


def test_generated_attempt_namespace_rejects_a_symlink_before_ray_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, output_root = _request(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    deployments = output_root / "deployments"
    try:
        deployments.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this host does not permit unprivileged directory symlinks")
    monkeypatch.setattr(
        ray_data_job,
        "_load_ray_data",
        lambda: pytest.fail("an owned-namespace symlink must fail before Ray Data is imported"),
    )

    with pytest.raises(ray_data_job.IncompleteAttemptError, match="linked or non-directory"):
        ray_data_job.run_ray_data_batch_job(**request)


def test_completion_publication_never_overwrites_a_racing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completion = tmp_path / "completion.json"
    competing = b'{"competing":true}'

    def competing_link(source: Path, destination: Path) -> None:
        assert source.name.startswith(".completion.json.")
        assert source.name.endswith(".tmp")
        Path(destination).write_bytes(competing)
        raise FileExistsError("simulated competing publisher")

    monkeypatch.setattr(ray_data_job.os, "link", competing_link)

    with pytest.raises(ray_data_job.CompletionConflictError, match="already exists"):
        ray_data_job._publish_completion_manifest(completion, {"schema_version": 1})
    assert completion.read_bytes() == competing


def test_completion_publication_owns_only_its_unique_temporary_file(tmp_path: Path) -> None:
    completion = tmp_path / "completion.json"
    unrelated_temporary = tmp_path / ".completion.json.tmp"
    unrelated_temporary.write_bytes(b"another publisher")

    encoded = ray_data_job._publish_completion_manifest(completion, {"schema_version": 1})

    assert completion.read_bytes() == encoded
    if os.name != "nt":
        assert stat.S_IMODE(completion.stat().st_mode) == 0o640
    assert unrelated_temporary.read_bytes() == b"another publisher"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        ".completion.json.tmp",
        "completion.json",
    ]


def test_new_attempt_uses_a_new_immutable_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, output_root = _request(tmp_path)
    _install_fake_execution(monkeypatch, {})
    first = ray_data_job.run_ray_data_batch_job(**request)
    second = ray_data_job.run_ray_data_batch_job(
        **{**request, "execution_generation": 3, "attempt_number": 2}
    )
    duplicate_enqueue = ray_data_job.run_ray_data_batch_job(
        **{**request, "task_execution_pk": 42, "execution_generation": 1}
    )
    other_task_id = "00000000-0000-4000-8000-000000000042"
    other_task = ray_data_job.run_ray_data_batch_job(**{**request, "task_id": other_task_id})
    other_deployment = ray_data_job.run_ray_data_batch_job(
        **{**request, "deployment_key": "another-deployment"}
    )

    assert first["output_uri"] != second["output_uri"]
    assert first["manifest_uri"] != second["manifest_uri"]
    assert first["output_uri"] != duplicate_enqueue["output_uri"]
    assert first["output_uri"] != other_task["output_uri"]
    assert first["output_uri"] != other_deployment["output_uri"]
    assert _attempt_dir(output_root).is_dir()
    assert _attempt_dir(output_root, execution_generation=3, attempt_number=2).is_dir()
    assert _attempt_dir(output_root, task_execution_pk=42, execution_generation=1).is_dir()
    assert _attempt_dir(output_root, task_id=other_task_id).is_dir()
    assert _attempt_dir(output_root, deployment_key="another-deployment").is_dir()


def test_partial_attempt_fails_closed_and_is_never_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, output_root = _request(tmp_path)
    observation: dict[str, Any] = {}
    _install_fake_execution(monkeypatch, observation)

    def fail_after_partial_write(output_path: str) -> None:
        path = Path(output_path)
        path.mkdir(parents=True)
        (path / "partial.parquet").write_bytes(b"partial")
        raise RuntimeError("simulated writer failure")

    def fail_write(dataset: _FakeDataset, path: str, *, mode: str) -> None:
        del dataset
        assert mode == "error"
        fail_after_partial_write(path)

    monkeypatch.setattr(_FakeDataset, "write_parquet", fail_write)
    with pytest.raises(RuntimeError, match="simulated writer failure"):
        ray_data_job.run_ray_data_batch_job(**request)

    attempt_dir = _attempt_dir(output_root)
    assert attempt_dir.is_dir()
    assert not (attempt_dir / "completion.json").exists()
    monkeypatch.setattr(
        ray_data_job,
        "_load_ray_data",
        lambda: pytest.fail("an incomplete attempt must fail before Ray execution"),
    )
    with pytest.raises(ray_data_job.IncompleteAttemptError, match="without completion.json"):
        ray_data_job.run_ray_data_batch_job(**request)


def test_changed_input_never_publishes_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, input_path, output_root = _request(tmp_path)
    observation: dict[str, Any] = {}
    _install_fake_execution(monkeypatch, observation)
    original_write = _FakeDataset.write_parquet

    def mutate_input_after_write(dataset: _FakeDataset, output_path: str, *, mode: str) -> None:
        original_write(dataset, output_path, mode=mode)
        input_path.write_text('{"record_id":"changed","value":99}\n', encoding="utf-8")

    monkeypatch.setattr(_FakeDataset, "write_parquet", mutate_input_after_write)

    with pytest.raises(ray_data_job.InputChangedError):
        ray_data_job.run_ray_data_batch_job(**request)
    completion = _attempt_dir(output_root) / "completion.json"
    assert not completion.exists()


def test_missing_ray_data_extra_fails_before_attempt_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, output_root = _request(tmp_path)

    def missing_extra() -> tuple[Any, Any, Any]:
        raise ray_data_job.RayDataDependencyError("install matching ray[data]")

    monkeypatch.setattr(ray_data_job, "_load_ray_data", missing_extra)
    with pytest.raises(ray_data_job.RayDataDependencyError, match=r"ray\[data\]"):
        ray_data_job.run_ray_data_batch_job(**request)
    assert not _attempt_dir(output_root).exists()


def test_manifest_uri_is_bounded_before_input_read_or_attempt_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, output_root = _request(tmp_path)
    original_file_uri = ray_data_job._file_uri

    def reject_completion_uri(path: Path) -> str:
        if path.name == "completion.json":
            raise ValueError("normalized file URI exceeds the limit")
        return original_file_uri(path)

    monkeypatch.setattr(ray_data_job, "_file_uri", reject_completion_uri)
    monkeypatch.setattr(
        ray_data_job,
        "_validate_input",
        lambda *args: pytest.fail("manifest URI must be checked before reading input"),
    )

    with pytest.raises(ValueError, match="normalized file URI exceeds"):
        ray_data_job.run_ray_data_batch_job(**request)
    assert not _attempt_dir(output_root).exists()


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"input_uri": "s3://bucket/input.jsonl"}, "file URI"),
        ({"output_root_uri": "file://user@example/artifacts"}, "remote authority"),
        ({"run_key": "../escape"}, "unsupported characters"),
        ({"deployment_key": "../escape"}, "unsupported characters"),
        ({"task_id": "not-a-uuid"}, "canonical UUID"),
        ({"input_sha256": "A" * 64}, "lowercase SHA-256"),
        ({"task_execution_pk": True}, "task_execution_pk"),
        ({"execution_generation": 0}, "execution_generation"),
        ({"attempt_number": 0}, "attempt_number"),
        ({"scale": float("inf")}, "scale"),
        ({"application_revision": "contains spaces"}, "unsupported characters"),
    ],
)
def test_control_metadata_is_strictly_bounded(
    tmp_path: Path, updates: dict[str, Any], message: str
) -> None:
    request, _, _ = _request(tmp_path)

    with pytest.raises(ValueError, match=message):
        ray_data_job.run_ray_data_batch_job(**{**request, **updates})


def test_conflicting_completed_attempt_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, output_root = _request(tmp_path)
    _install_fake_execution(monkeypatch, {})
    ray_data_job.run_ray_data_batch_job(**request)
    completion = _attempt_dir(output_root) / "completion.json"
    manifest = json.loads(completion.read_bytes())
    manifest["input"]["sha256"] = "0" * 64
    completion.write_bytes(ray_data_job._canonical_json(manifest))

    with pytest.raises(ray_data_job.CompletionConflictError, match="input"):
        ray_data_job.run_ray_data_batch_job(**request)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total_bytes", -1),
        ("content_sha256", "not-a-sha256-digest"),
    ],
)
def test_invalid_completed_output_identity_is_not_adopted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    request, _, output_root = _request(tmp_path)
    _install_fake_execution(monkeypatch, {})
    ray_data_job.run_ray_data_batch_job(**request)
    completion = _attempt_dir(output_root) / "completion.json"
    manifest = json.loads(completion.read_bytes())
    manifest["output"][field] = value
    completion.write_bytes(ray_data_job._canonical_json(manifest))

    with pytest.raises(ray_data_job.CompletionConflictError, match="output counts"):
        ray_data_job.run_ray_data_batch_job(**request)


def _task_arguments(tmp_path: Path) -> dict[str, Any]:
    request, _, _ = _request(tmp_path)
    return {
        key: request[key]
        for key in (
            "input_uri",
            "input_sha256",
            "run_key",
            "application_revision",
            "model_revision",
            "scale",
            "bias",
        )
    }


def test_django_task_is_pinned_to_the_ray_job_only_queue_and_backend() -> None:
    assert tasks.ray_data_batch_score.backend == "ray-data"
    assert tasks.ray_data_batch_score.queue_name == "ray-data"
    settings_source = (PROJECT_ROOT / "testproject/settings.py").read_text(encoding="utf-8")
    ray_data_backend = settings_source.split('"ray-data": {', maxsplit=1)[1].split(
        '"recovery-showcase": {', maxsplit=1
    )[0]
    assert '"RAY_JOB_ONLY": True' in ray_data_backend


@pytest.mark.django_db
def test_public_enqueue_persists_the_dedicated_queue_and_runtime_profile(tmp_path: Path) -> None:
    from django_ray.models import RayTaskExecution

    result = tasks.ray_data_batch_score.enqueue(**_task_arguments(tmp_path))
    execution = RayTaskExecution.objects.get(task_id=result.id)

    assert execution.queue_name == "ray-data"
    assert execution.runtime_env_profile == "ray-data"


def test_dev_ray_core_worker_cannot_claim_the_ray_data_queue() -> None:
    source = (PROJECT_ROOT / "k8s/overlays/dev/worker-all-queues.yaml").read_text(encoding="utf-8")
    arguments = source.split("args:", maxsplit=1)[1].split("envFrom:", maxsplit=1)[0]

    assert "--all-queues" not in arguments
    assert "ray-data" not in arguments
    assert "--cluster" in arguments


def test_django_task_requires_a_fenced_outer_ray_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _task_arguments(tmp_path)
    monkeypatch.setattr(tasks, "get_current_task_context", lambda: None)
    with pytest.raises(RuntimeError, match="outer Ray Job driver"):
        tasks.ray_data_batch_score.func(**arguments)

    monkeypatch.setattr(
        tasks,
        "get_current_task_context",
        lambda: DurableTaskContext(
            task_pk=41,
            task_id=TASK_ID,
            attempt_number=1,
            execution_generation=2,
        ),
    )
    with pytest.raises(RuntimeError, match="outer Ray Job driver"):
        tasks.ray_data_batch_score.func(**arguments)

    monkeypatch.setattr(
        tasks,
        "get_current_task_context",
        lambda: DurableTaskContext(task_pk=41, ray_job_driver=True),
    )
    with pytest.raises(RuntimeError, match="durable fenced attempt"):
        tasks.ray_data_batch_score.func(**arguments)


def test_django_task_passes_only_durable_identity_and_bounded_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _task_arguments(tmp_path)
    monkeypatch.setattr(
        tasks,
        "get_current_task_context",
        lambda: DurableTaskContext(
            task_pk=41,
            task_id=TASK_ID,
            attempt_number=3,
            execution_generation=9,
            ray_job_driver=True,
        ),
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(tasks.settings, "RAY_DATA_INPUT_ROOT", str(tmp_path / "inputs"))
    monkeypatch.setattr(tasks.settings, "RAY_DATA_OUTPUT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setattr(tasks.settings, "RAY_DATA_DEPLOYMENT_KEY", DEPLOYMENT_KEY)

    def execute(**options: Any) -> dict[str, Any]:
        captured.update(options)
        return {"status": "artifact_complete"}

    monkeypatch.setattr(tasks, "run_ray_data_batch_job", execute)

    assert tasks.ray_data_batch_score.func(**arguments) == {"status": "artifact_complete"}
    assert captured == {
        **arguments,
        "input_root_uri": (tmp_path / "inputs").as_uri(),
        "output_root_uri": (tmp_path / "artifacts").as_uri(),
        "deployment_key": DEPLOYMENT_KEY,
        "task_id": TASK_ID,
        "task_execution_pk": 41,
        "execution_generation": 9,
        "attempt_number": 3,
    }


def test_django_task_injects_first_attempt_failure_only_after_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = {
        **_task_arguments(tmp_path),
        "failure_fixture": tasks.RAY_DATA_AFTER_MANIFEST_FAILURE_FIXTURE,
    }
    monkeypatch.setattr(
        tasks,
        "get_current_task_context",
        lambda: DurableTaskContext(
            task_pk=41,
            task_id=TASK_ID,
            attempt_number=1,
            execution_generation=2,
            ray_job_driver=True,
        ),
    )
    monkeypatch.setattr(tasks.settings, "RAY_DATA_INPUT_ROOT", str(tmp_path / "inputs"))
    monkeypatch.setattr(tasks.settings, "RAY_DATA_OUTPUT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setattr(tasks.settings, "RAY_DATA_DEPLOYMENT_KEY", DEPLOYMENT_KEY)
    calls: list[str] = []
    monkeypatch.setattr(
        tasks,
        "run_ray_data_batch_job",
        lambda **_options: calls.append("manifest-published") or {"status": "complete"},
    )
    with pytest.raises(
        tasks.RayDataGoldenPathFixtureError,
        match="failed after publishing the first-attempt manifest",
    ):
        tasks.ray_data_batch_score.func(**arguments)

    assert calls == ["manifest-published"]


def test_django_task_retry_completes_without_reinjecting_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = {
        **_task_arguments(tmp_path),
        "failure_fixture": tasks.RAY_DATA_AFTER_MANIFEST_FAILURE_FIXTURE,
    }
    monkeypatch.setattr(
        tasks,
        "get_current_task_context",
        lambda: DurableTaskContext(
            task_pk=41,
            task_id=TASK_ID,
            attempt_number=2,
            execution_generation=3,
            ray_job_driver=True,
        ),
    )
    monkeypatch.setattr(tasks.settings, "RAY_DATA_INPUT_ROOT", str(tmp_path / "inputs"))
    monkeypatch.setattr(tasks.settings, "RAY_DATA_OUTPUT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setattr(tasks.settings, "RAY_DATA_DEPLOYMENT_KEY", DEPLOYMENT_KEY)
    expected = {"status": "artifact_complete"}
    monkeypatch.setattr(tasks, "run_ray_data_batch_job", lambda **_options: expected)

    assert tasks.ray_data_batch_score.func(**arguments) is expected


def test_django_task_rejects_unknown_failure_fixture_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executed = False

    def execute(**_options: Any) -> dict[str, Any]:
        nonlocal executed
        executed = True
        return {}

    monkeypatch.setattr(tasks, "run_ray_data_batch_job", execute)

    with pytest.raises(ValueError, match="not a supported Ray Data probe fixture"):
        tasks.ray_data_batch_score.func(
            **_task_arguments(tmp_path),
            failure_fixture="unknown",  # type: ignore[arg-type]
        )

    assert executed is False
