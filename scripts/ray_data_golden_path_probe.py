"""Run bounded, real-Ray evidence for the application-owned Ray Data recipe."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

# Ray 2.56 otherwise detects this script's ``uv run`` ancestor and propagates
# the outer editable-project command into each minimal immutable working-dir
# archive. The disposable environment already contains the exact dependency
# set, so workers must use that preinstalled interpreter directly.
os.environ["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] = "0"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from testproject.apps.cluster_tasks.ray_data_job import (  # noqa: E402
    MAX_RESULT_BYTES,
    ArtifactNotAdoptableError,
    OutputChangedError,
    _attempt_paths,
    _completion_result,
    _inspect_output_content,
    _read_completion_manifest,
    validate_adoptable_artifact,
)

MAX_PROBE_SOURCE_FILES = 5_000
MAX_PROBE_SOURCE_BYTES = 32 * 1024 * 1024


def _build_probe_working_dir_archive(destination: Path) -> None:
    """Freeze only the import roots needed by both disposable Ray Jobs."""
    file_count = 0
    byte_count = 0
    with ZipFile(destination, "w") as archive:
        for root_name in ("src", "testproject"):
            source_root = ROOT / root_name
            for source in sorted(source_root.rglob("*"), key=lambda path: path.as_posix()):
                if source.is_symlink():
                    raise AssertionError("probe source archive cannot contain symlinks")
                if not source.is_file():
                    continue
                relative = source.relative_to(ROOT)
                if "__pycache__" in relative.parts or source.suffix in {".pyc", ".pyo"}:
                    continue
                contents = source.read_bytes()
                file_count += 1
                byte_count += len(contents)
                if file_count > MAX_PROBE_SOURCE_FILES or byte_count > MAX_PROBE_SOURCE_BYTES:
                    raise AssertionError("probe source archive exceeded its bounded contract")
                member = ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                member.compress_type = ZIP_DEFLATED
                member.create_system = 3
                member.external_attr = 0o100644 << 16
                archive.writestr(member, contents)
    if file_count == 0:
        raise AssertionError("probe source archive was empty")


def _use_preinstalled_probe_dependencies(django_settings: Any) -> None:
    """Treat the isolated command environment as the disposable Ray node image."""
    config = getattr(django_settings, "DJANGO_RAY", None)
    if not isinstance(config, dict):
        raise AssertionError("probe expected mutable DJANGO_RAY settings")
    profiles = config.get("RUNTIME_ENV_PROFILES")
    if not isinstance(profiles, dict):
        raise AssertionError("probe expected RuntimeEnv profiles")
    project = profiles.get("project")
    ray_data = profiles.get("ray-data")
    if not isinstance(project, dict) or not isinstance(ray_data, dict):
        raise AssertionError("probe expected project and ray-data RuntimeEnv profiles")
    child = ray_data.get("runtime_env")
    if not isinstance(child, dict):
        raise AssertionError("probe expected a composed ray-data RuntimeEnv profile")

    # ``uv run --with ray[data]==...`` supplies the exact dependency set to the
    # local node. Avoid a second Ray-managed virtualenv, which is both redundant
    # and unsupported by pip-less uv Python installations on Windows.
    profiles["project"] = {**project, "pip": []}
    profiles["ray-data"] = {
        **ray_data,
        "runtime_env": {**child, "pip": []},
    }


def _path_from_uri(uri: str) -> Path:
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise AssertionError("probe result did not contain a local file URI")
    decoded_path = unquote(parsed.path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", decoded_path):
        decoded_path = decoded_path[1:]
    return Path(decoded_path)


def _read_bounded_rows(
    output_uri: str,
    *,
    expected_rows: int,
    expected_bytes: int,
    expected_sha256: str,
) -> list[dict[str, object]]:
    parquet = importlib.import_module("pyarrow.parquet")

    files, total_bytes, content_sha256 = _inspect_output_content(_path_from_uri(output_uri))
    if not files or len(files) > 8:
        raise AssertionError(f"probe expected between 1 and 8 Parquet files, found {len(files)}")
    if total_bytes != expected_bytes or content_sha256 != expected_sha256:
        raise AssertionError("probe result did not identify its exact bounded Parquet output")
    observed_rows = sum(parquet.ParquetFile(path).metadata.num_rows for path in files)
    if observed_rows != expected_rows:
        raise AssertionError(f"probe expected {expected_rows} rows, found {observed_rows}")

    rows: list[dict[str, object]] = []
    for path in files:
        rows.extend(parquet.read_table(path).to_pylist())
    return sorted(rows, key=lambda row: str(row["record_id"]))


def _assert_metadata_only(value: object) -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            _assert_metadata_only(item)
        return
    if not isinstance(value, dict):
        raise AssertionError(f"non-JSON result value: {type(value).__name__}")
    for key, item in value.items():
        if not isinstance(key, str):
            raise AssertionError("result dictionary contains a non-string key")
        _assert_metadata_only(item)


def _worker_log_tail(path: Path, *, maximum_chars: int = 8_000) -> str:
    try:
        contents = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "<worker log unavailable>"
    return contents[-maximum_chars:]


def _wait_for_recovered_execution(
    execution_pk: int, worker: subprocess.Popen[str], log: Path
) -> Any:
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskState

    deadline = time.monotonic() + 300
    terminal = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.LOST}
    while time.monotonic() < deadline:
        execution = RayTaskExecution.objects.get(pk=execution_pk)
        if execution.state in terminal:
            attempts = list(
                TaskAttempt.objects.filter(execution_id=execution_pk).order_by("attempt_number")
            )
            if len(attempts) >= 2:
                return execution
            raise AssertionError(
                f"Ray Data task became terminal before framework retry: {execution.state}: "
                f"{execution.error_message}\n{_worker_log_tail(log)}"
            )
        return_code = worker.poll()
        if return_code is not None:
            raise AssertionError(
                f"Ray Job management worker exited with {return_code} before completion:\n"
                f"{_worker_log_tail(log)}"
            )
        time.sleep(0.25)
    raise AssertionError(f"Ray Data recovery task timed out:\n{_worker_log_tail(log)}")


def _load_ray_job_submission_evidence(
    *, ray_address: str, execution_pk: int
) -> dict[int, tuple[int, str]]:
    """Read retained terminal Job API metadata instead of a database polling race."""
    from django_ray.ray_job_protocol import (
        RayJobRequestBindingError,
        is_valid_strict_ray_job_submission_id,
        parse_ray_job_request_metadata,
    )
    from django_ray.runner.ray_job import (
        _address_pinned_job_client,
        _bounded_control_requests,
    )

    client = _address_pinned_job_client(ray_address)
    deadline = time.monotonic() + 30
    observed_statuses: dict[int, str] = {}
    while time.monotonic() < deadline:
        submissions: dict[int, tuple[int, str]] = {}
        statuses: dict[int, str] = {}
        with _bounded_control_requests(client):
            jobs = client.list_jobs()
        for job in jobs:
            metadata = job.metadata
            try:
                expectation = parse_ray_job_request_metadata(metadata)
            except RayJobRequestBindingError as error:
                raise AssertionError(
                    "Ray Job metadata contained an invalid strict binding"
                ) from error
            if expectation is None or expectation.identity.task_execution_pk != execution_pk:
                continue
            attempt_number = expectation.identity.attempt_number
            execution_generation = expectation.identity.execution_generation
            if expectation.execution_protocol_version != 1:
                raise AssertionError("Ray Job metadata advertised an unexpected protocol")
            submission_id = job.submission_id
            if not is_valid_strict_ray_job_submission_id(submission_id):
                raise AssertionError("Ray Job metadata matched a non-strict submission job")
            identity = (execution_generation, submission_id)
            previous = submissions.setdefault(attempt_number, identity)
            if previous != identity:
                raise AssertionError(
                    f"attempt {attempt_number} had multiple Ray Job identities: "
                    f"{previous}, {identity}"
                )
            status = getattr(job.status, "value", job.status)
            statuses[attempt_number] = str(status)

        observed_statuses = statuses
        if set(submissions) == {1, 2} and all(
            status in {"STOPPED", "SUCCEEDED", "FAILED"} for status in statuses.values()
        ):
            # The outer process exits 0 after it durably delivers either envelope.
            # TaskAttempt, not Ray's transport status, records the first task failure.
            if statuses != {1: "SUCCEEDED", 2: "SUCCEEDED"}:
                raise AssertionError(
                    f"expected two successful Ray Job transports, found {statuses}"
                )
            return submissions
        time.sleep(0.25)

    raise AssertionError(
        f"Ray Job submissions did not reach retained terminal states: {observed_statuses}"
    )


def _stop_worker(worker: subprocess.Popen[str]) -> None:
    if worker.poll() is not None:
        return
    worker.terminate()
    try:
        worker.wait(timeout=15)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=15)


def main() -> int:
    import ray

    fixture = (
        '{"record_id":"a","value":1}\n'
        '{"record_id":"b","value":2}\n'
        '{"record_id":"c","value":3}\n'
        '{"record_id":"d","value":4}\n'
    )
    with tempfile.TemporaryDirectory(prefix="django-ray-data-probe-") as temporary:
        root = Path(temporary)
        input_root = root / "inputs"
        input_root.mkdir()
        output_root = root / "artifacts"
        output_root.mkdir()
        working_dir_archive = root / "ray-data-probe-working-dir.zip"
        _build_probe_working_dir_archive(working_dir_archive)
        os.environ["DJANGO_SETTINGS_MODULE"] = "testproject.settings"
        os.environ["DJANGO_DEPLOYMENT_MODE"] = "demo"
        os.environ["DATABASE_ENGINE"] = "django.db.backends.sqlite3"
        os.environ["DATABASE_NAME"] = str(root / "probe.sqlite3")
        os.environ["DJANGO_RAY_WORKING_DIR_URI"] = str(working_dir_archive)
        os.environ["DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE"] = "plaintext"
        os.environ["DJANGO_RAY_DATA_INPUT_ROOT"] = str(input_root)
        os.environ["DJANGO_RAY_DATA_OUTPUT_ROOT"] = str(output_root)
        os.environ["DJANGO_RAY_DATA_DEPLOYMENT_KEY"] = "real-ray-data-probe"
        os.environ["RAY_MAX_RETRIES"] = "2"
        os.environ["RAY_RETRY_DELAY_SECONDS"] = "0"

        import django
        from django.conf import settings as django_settings
        from django.core.management import call_command
        from django.db import connections

        # Do not let Ray's ``auto`` discovery attach this probe to another local
        # development cluster. Start one disposable cluster, then pin every
        # durable submission and worker control request to its exact address.
        os.environ.pop("RAY_ADDRESS", None)
        ray_context = ray.init(
            address="local",
            num_cpus=2,
            include_dashboard=True,
            log_to_driver=False,
        )
        probe_ray_address = str(ray_context.address_info.get("gcs_address", ""))
        if not probe_ray_address:
            ray.shutdown()
            raise AssertionError("local Ray probe did not expose its GCS address")
        os.environ["RAY_ADDRESS"] = probe_ray_address

        try:
            django.setup()
            _use_preinstalled_probe_dependencies(django_settings)
            call_command("migrate", "django_ray", interactive=False, verbosity=0)

            from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
            from testproject.apps.cluster_tasks.tasks import (
                RAY_DATA_AFTER_MANIFEST_FAILURE_FIXTURE,
                RAY_DATA_AFTER_MANIFEST_FAILURE_MESSAGE,
                ray_data_batch_score,
            )

            input_path = input_root / "input.jsonl"
            input_path.write_text(fixture, encoding="utf-8")
            enqueue_request = {
                "input_uri": input_path.as_uri(),
                "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                "run_key": "real-ray-data-probe",
                "application_revision": "probe-app-v1",
                "model_revision": "probe-model-v1",
                "scale": 2.0,
                "bias": 1.0,
                "failure_fixture": RAY_DATA_AFTER_MANIFEST_FAILURE_FIXTURE,
            }
        except BaseException:
            ray.shutdown()
            connections.close_all()
            raise
        worker: subprocess.Popen[str] | None = None
        worker_log_path = root / "ray-data-worker.log"
        try:
            task_result = ray_data_batch_score.enqueue(**enqueue_request)
            execution = RayTaskExecution.objects.get(task_id=task_result.id)
            if execution.queue_name != "ray-data" or execution.runtime_env_profile != "ray-data":
                raise AssertionError(
                    "Ray Data task was not durably routed to its dedicated profile"
                )
            if execution.ray_target_address != probe_ray_address:
                raise AssertionError("Ray Data task did not snapshot the disposable cluster target")

            connections.close_all()
            worker_environment = dict(os.environ)
            worker_environment["PYTHONUNBUFFERED"] = "1"
            with worker_log_path.open("w", encoding="utf-8") as worker_log:
                worker = subprocess.Popen(
                    [
                        sys.executable,
                        "testproject/manage.py",
                        "django_ray_worker",
                        "--queue",
                        "ray-data",
                        "--concurrency",
                        "1",
                    ],
                    cwd=ROOT,
                    env=worker_environment,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                execution = _wait_for_recovered_execution(execution.pk, worker, worker_log_path)
                submissions = _load_ray_job_submission_evidence(
                    ray_address=probe_ray_address,
                    execution_pk=execution.pk,
                )

            if execution.state != TaskState.SUCCEEDED:
                raise AssertionError(
                    f"routed Ray Data task ended as {execution.state}: "
                    f"{execution.error_message}\n{_worker_log_tail(worker_log_path)}"
                )
            attempts = list(
                TaskAttempt.objects.filter(execution=execution).order_by("attempt_number")
            )
            attempt_states = [
                (int(attempt.attempt_number), str(attempt.state)) for attempt in attempts
            ]
            if attempt_states != [(1, TaskState.FAILED), (2, TaskState.SUCCEEDED)]:
                raise AssertionError(
                    f"expected one archived failure then success, found {attempt_states}"
                )
            if RAY_DATA_AFTER_MANIFEST_FAILURE_MESSAGE not in str(attempts[0].error_message or ""):
                raise AssertionError("first archived attempt did not retain fixture failure")
            if set(submissions) != {1, 2}:
                raise AssertionError(
                    f"expected two observed Ray Job attempts, found {sorted(submissions)}"
                )
            ray_job_ids = [submissions[number][1] for number in (1, 2)]
            if len(set(ray_job_ids)) != 2 or not all(
                ray_job_id.startswith("raysubmit_") for ray_job_id in ray_job_ids
            ):
                raise AssertionError(f"expected two distinct Ray Job submissions: {ray_job_ids}")
            if execution.ray_job_id != ray_job_ids[1]:
                raise AssertionError("durable success did not retain the second Ray Job identity")
            if execution.result_data is None:
                raise AssertionError("routed Ray Data task did not persist its bounded result")
            second = json.loads(execution.result_data)
            if not isinstance(second, dict):
                raise AssertionError("routed Ray Data task did not return bounded metadata")
            completion_envelope = json.loads(execution.completion_data or "null")
            if (
                not isinstance(completion_envelope, dict)
                or completion_envelope.get("success") is not True
                or completion_envelope.get("completion_schema") != "django-ray.execution-completion"
                or completion_envelope.get("execution_protocol_version") != 1
                or not completion_envelope.get("executor_django_ray_version")
            ):
                raise AssertionError(
                    "Ray Job driver did not persist a strict successful completion envelope"
                )
            if not all(attempt.executor_django_ray_version for attempt in attempts):
                raise AssertionError("archived Ray Job attempts lost executor provenance")

            first_generation = submissions[1][0]
            _, _, first_completion_path = _attempt_paths(
                output_root,
                "real-ray-data-probe",
                "real-ray-data-probe",
                str(execution.task_id),
                execution.pk,
                first_generation,
                1,
            )
            first_manifest, first_completion_bytes = _read_completion_manifest(
                first_completion_path
            )
            first = _completion_result(
                first_manifest,
                manifest_uri=first_completion_path.as_uri(),
                manifest_bytes=first_completion_bytes,
            )
            if first["attempt_number"] != 1 or first["execution_generation"] != first_generation:
                raise AssertionError("failed artifact did not retain its first-attempt fence")
            try:
                validate_adoptable_artifact(
                    first,
                    durable_state=attempts[0].state,
                    output_root_uri=output_root.as_uri(),
                    deployment_key="real-ray-data-probe",
                    task_id=str(execution.task_id),
                    task_execution_pk=execution.pk,
                    execution_generation=first_generation,
                    attempt_number=1,
                )
            except ArtifactNotAdoptableError:
                failed_artifact_rejected = True
            else:
                raise AssertionError("failed durable attempt exposed an adoptable artifact")

            completion_path = _path_from_uri(second["manifest_uri"])
            _, completion_bytes = _read_completion_manifest(completion_path)
            if hashlib.sha256(completion_bytes).hexdigest() != second.get("manifest_sha256"):
                raise AssertionError(
                    "success result did not identify its exact completion manifest"
                )
            completion_mtime = completion_path.stat().st_mtime_ns
            adopted = validate_adoptable_artifact(
                second,
                durable_state=execution.state,
                output_root_uri=output_root.as_uri(),
                deployment_key="real-ray-data-probe",
                task_id=str(execution.task_id),
                task_execution_pk=execution.pk,
                execution_generation=execution.execution_generation,
                attempt_number=execution.attempt_number,
            )
            adopted_again = validate_adoptable_artifact(
                second,
                durable_state=execution.state,
                output_root_uri=output_root.as_uri(),
                deployment_key="real-ray-data-probe",
                task_id=str(execution.task_id),
                task_execution_pk=execution.pk,
                execution_generation=execution.execution_generation,
                attempt_number=execution.attempt_number,
            )
            if _read_completion_manifest(completion_path)[1] != completion_bytes:
                raise AssertionError("repeated artifact adoption changed the completion manifest")
            if completion_path.stat().st_mtime_ns != completion_mtime:
                raise AssertionError("repeated artifact adoption rewrote the completion manifest")
            if first["output_uri"] == second["output_uri"]:
                raise AssertionError("framework retry did not receive a new output namespace")
            try:
                validate_adoptable_artifact(
                    second,
                    durable_state=execution.state,
                    output_root_uri=output_root.as_uri(),
                    deployment_key="real-ray-data-probe",
                    task_id=str(execution.task_id),
                    task_execution_pk=execution.pk,
                    execution_generation=first_generation,
                    attempt_number=1,
                )
            except ArtifactNotAdoptableError:
                stale_fence_rejected = True
            else:
                raise AssertionError("successful artifact crossed an earlier attempt fence")

            for result in (first, second):
                _assert_metadata_only(result)
                encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
                if len(encoded) > MAX_RESULT_BYTES:
                    raise AssertionError("probe result exceeded the bounded result contract")
                if any(key in result for key in ("dataset", "object_ref", "rows", "batches")):
                    raise AssertionError(
                        "probe result leaked distributed data or a framework handle"
                    )

            expected = [
                {"record_id": "a", "value": 1, "score": 3.0},
                {"record_id": "b", "value": 2, "score": 5.0},
                {"record_id": "c", "value": 3, "score": 7.0},
                {"record_id": "d", "value": 4, "score": 9.0},
            ]
            if (
                _read_bounded_rows(
                    first["output_uri"],
                    expected_rows=4,
                    expected_bytes=first["output_bytes"],
                    expected_sha256=first["output_sha256"],
                )
                != expected
            ):
                raise AssertionError("first attempt produced unexpected rows")
            if (
                _read_bounded_rows(
                    second["output_uri"],
                    expected_rows=4,
                    expected_bytes=second["output_bytes"],
                    expected_sha256=second["output_sha256"],
                )
                != expected
            ):
                raise AssertionError("second attempt produced unexpected rows")

            second_output = _path_from_uri(second["output_uri"])
            tampered_file = _inspect_output_content(second_output)[0][0]
            with tampered_file.open("ab") as target:
                target.write(b"tampered")
            try:
                validate_adoptable_artifact(
                    second,
                    durable_state=execution.state,
                    output_root_uri=output_root.as_uri(),
                    deployment_key="real-ray-data-probe",
                    task_id=str(execution.task_id),
                    task_execution_pk=execution.pk,
                    execution_generation=execution.execution_generation,
                    attempt_number=execution.attempt_number,
                )
            except OutputChangedError:
                pass
            else:
                raise AssertionError("artifact adoption accepted tampered Parquet output")

            evidence = {
                "schema_version": 1,
                "outcome": "passed",
                "python_version": platform.python_version(),
                "ray_version": ray.__version__,
                "input_rows": 4,
                "attempts_completed": len(attempts),
                "ray_jobs_submitted": len(submissions),
                "ray_job_transports_succeeded": True,
                "strict_ray_job_request_binding": True,
                "versioned_completion_envelope": True,
                "executor_provenance_archived": True,
                "preinstalled_ray_data_environment": True,
                "disposable_cluster_target_pinned": True,
                "automatic_retry_recovered": True,
                "failed_artifact_rejected": failed_artifact_rejected,
                "idempotent_artifact_adoption": adopted_again == adopted,
                "stale_attempt_fence_rejected": stale_fence_rejected,
                "new_attempt_namespace_isolated": True,
                "bounded_json_result": True,
                "tampered_output_rejected": True,
                "management_worker_routed": True,
                "ray_job_submission": True,
                "durable_task_succeeded": True,
                "outer_ray_job_context": True,
                "completion_envelope_persisted": True,
                "artifact_adopted_after_success": adopted == json.loads(completion_bytes),
            }
            print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
        finally:
            if worker is not None:
                _stop_worker(worker)
            ray.shutdown()
            connections.close_all()

    if ray.is_initialized():
        raise AssertionError("Ray remained initialized after probe cleanup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
