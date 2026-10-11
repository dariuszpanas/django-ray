"""Request binding, redaction and owned process boundaries without live Ray."""

import hashlib
import json
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qualification.application import run_rq2 as probe


def receipt(**updates):
    return {
        "schema_version": 1,
        "layer": probe.LAYER,
        "status": "passed",
        "complete_application_gate": False,
        "generation": "before",
        "budget_seconds": probe.BUDGET_SECONDS,
        "assertions": dict.fromkeys(probe.FACTS, True),
        **updates,
    }


@pytest.mark.parametrize("fact", probe.FACTS)
@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_receipt_requires_every_actual_boolean(fact, value):
    evidence = receipt()
    evidence["assertions"][fact] = value
    with pytest.raises(ValueError):
        probe.validate_receipt(evidence)


@pytest.mark.parametrize(
    "updates",
    [{"generation": "other"}, {"budget_seconds": 181}, {"assertions": {}}, {"assertions": None}],
)
def test_receipt_rejects_changed_contract(updates):
    with pytest.raises(ValueError):
        probe.validate_receipt(receipt(**updates))


def test_collector_requires_exact_generation_and_complete_rq2_layer():
    from qualification.application.run_chainsaw import RECEIPTS, parse_receipts

    raw = json.dumps(receipt()).encode()
    assert parse_receipts(raw, ("before-rq2",)) == {"before-rq2": raw}
    with pytest.raises(ValueError, match="generation"):
        parse_receipts(raw, ("after-rq2",))
    assert RECEIPTS["assert-before"][1][-1] == "before-rq2"
    assert RECEIPTS["assert-after"][1][-1] == "after-rq2"


def test_cli_help_needs_no_django_or_ray(tmp_path):
    script = """
import runpy, sys
sys.path.insert(0, sys.argv[1])
sys.argv = ['run_rq2', '--help']
try:
    runpy.run_module('qualification.application.run_rq2', run_name='__main__')
except SystemExit as error:
    assert error.code == 0
assert 'django' not in sys.modules and 'ray' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(Path(__file__).resolve().parents[2])],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("crash", [False, True])
def test_manager_stop_observes_exit_and_only_signals_owned_child(crash, monkeypatch):
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    process = Mock(returncode=None)
    process.wait.return_value = -signal.SIGKILL if crash else 0
    probe.stop_manager(process, crash=crash)
    (process.kill if crash else process.terminate).assert_called_once_with()
    process.wait.assert_called_once_with(timeout=2)


def test_manager_stop_escalates_without_exceeding_cleanup_budget():
    process = Mock(returncode=None)
    process.wait.side_effect = [subprocess.TimeoutExpired("owned", 3), -9]
    probe.stop_manager(process)
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert [call.kwargs["timeout"] for call in process.wait.call_args_list] == [2, 2]


def test_reaped_child_is_never_signalled_and_cannot_count_as_crash():
    process = Mock(returncode=0)
    probe.stop_manager(process)
    process.kill.assert_not_called()
    process.terminate.assert_not_called()
    with pytest.raises(ValueError):
        probe.stop_manager(process, crash=True)


def test_manager_does_not_escape_generation_process_group(monkeypatch):
    popen = Mock()
    monkeypatch.setattr(probe.subprocess, "Popen", popen)
    probe._start_manager()
    args, kwargs = popen.call_args
    assert args[0][4:] == [
        "--queue",
        probe.QUEUE,
        "--concurrency",
        "1",
    ]
    assert "start_new_session" not in kwargs
    assert kwargs == {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}


@pytest.mark.parametrize("original_cluster_flag", [False, True])
def test_manager_argv_uses_production_jobs_queue_and_mode_without_native_init(
    original_cluster_flag, tmp_path, settings, monkeypatch
):
    from django.core.management.base import CommandError

    from django_ray.conf.settings import get_settings
    from django_ray.management.commands.django_ray_worker import Command

    settings.DJANGO_RAY = {
        "RUNNER": "ray_job",
        "RAY_ADDRESS": probe.ADDRESS,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    settings.TASKS = {
        probe.QUEUE: {
            "BACKEND": "django_ray.backends.RayTaskBackend",
            "QUEUES": [probe.QUEUE],
            "OPTIONS": {"RAY_JOB_ONLY": True, "RAY_ADDRESS": probe.ADDRESS},
        }
    }
    popen = Mock()
    monkeypatch.setattr(probe.subprocess, "Popen", popen)
    probe._start_manager()
    argv = popen.call_args.args[0][4:]
    if original_cluster_flag:
        argv += ["--cluster", probe.ADDRESS]
    command = Command()
    options = vars(command.create_parser("django", "django_ray_worker").parse_args(argv))
    native_init = Mock(side_effect=AssertionError("Must not initialize native Ray"))
    monkeypatch.setattr(command, "_init_local_ray", native_init)
    monkeypatch.setattr(command, "_init_cluster_ray", native_init)
    monkeypatch.setattr(command, "setup_signal_handlers", Mock())

    class LeaseBoundaryReachedError(Exception):
        pass

    lease = Mock(side_effect=LeaseBoundaryReachedError)
    monkeypatch.setattr(command, "_create_lease", lease)
    if original_cluster_flag:
        assert command._requested_worker_family(options) == "ray_core"
        with pytest.raises(CommandError, match="Ray Job"):
            command.handle(**options)
        lease.assert_not_called()
    else:
        assert command._requested_worker_family(options) == "ray_job"
        assert command._parse_queues(options) == [probe.QUEUE]
        assert command._get_default_execution_mode(get_settings()) == ("ray", None)
        with pytest.raises(LeaseBoundaryReachedError):
            command.handle(**options)
        assert command.execution_mode == "ray"
        assert command.cluster_address is None
        lease.assert_called_once_with(probe.QUEUE)
        command._initialize_ray_execution()
    native_init.assert_not_called()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (None, "running"),
        (0, "exited_zero"),
        (1, "exited_nonzero"),
        (-9, "signalled"),
        (True, "unavailable"),
    ],
)
def test_manager_diagnostic_uses_only_fixed_exit_categories(code, expected):
    assert probe._manager_state(Mock(poll=Mock(return_value=code))) == expected


def test_manager_diagnostic_handles_missing_child_and_unreadable_exit():
    assert probe._manager_state(None) == "not_started"
    process = Mock(poll=Mock(side_effect=RuntimeError("sensitive process details")))
    assert probe._manager_state(process) == "unavailable"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("sensitive"), "timeout"),
        (subprocess.TimeoutExpired("sensitive", 3), "timeout"),
        (ValueError("sensitive"), "validation"),
        (OSError("sensitive"), "os_error"),
        (subprocess.CalledProcessError(1, "sensitive", output="sensitive"), "process_error"),
        (RuntimeError("sensitive"), "unexpected"),
    ],
)
def test_failure_diagnostic_categories_never_render_exception_details(error, expected):
    diagnostic = probe.Diagnostics()
    diagnostic.enter("initial_inspect")
    diagnostic.failed(error, Mock(poll=Mock(return_value=1)))
    assert diagnostic.snapshot()["failure_type"] == expected
    assert "sensitive" not in json.dumps(diagnostic.snapshot())


def test_cleanup_failure_preserves_initial_phase_and_exit_category():
    diagnostic = probe.Diagnostics()
    diagnostic.enter("initial_effect")
    diagnostic.failed(ValueError("sensitive"), Mock(poll=Mock(return_value=1)))
    diagnostic.enter("cleanup")
    diagnostic.cleanup_failed(TimeoutError("sensitive"), Mock(poll=Mock(return_value=None)))
    assert diagnostic.snapshot() == {
        "phase": "cleanup",
        "failure_phase": "initial_effect",
        "failure_type": "validation",
        "manager_at_failure": "exited_nonzero",
        "cleanup_failure_type": "timeout",
        "manager_after_cleanup": "running",
        "start_observation": dict.fromkeys(probe.START_FIELDS, "not_observed"),
    }


def test_diagnostics_reject_unlisted_phases_and_whitelist_all_serialized_values():
    diagnostic = probe.Diagnostics()
    with pytest.raises(ValueError, match="Unknown"):
        diagnostic.enter("sensitive phase")
    for name in diagnostic.snapshot():
        setattr(diagnostic, name, ["sensitive"])
    observed = diagnostic.snapshot()
    startup = observed.pop("start_observation")
    assert set(observed.values()) <= probe.PHASES | probe.ERROR_TYPES | probe.MANAGER_STATES
    assert set(startup.values()) == {"unavailable"}
    assert "sensitive" not in json.dumps(observed)


def test_startup_diagnostics_reject_unknown_fields_and_filter_every_value():
    diagnostic = probe.Diagnostics()
    with pytest.raises(ValueError, match="Unknown"):
        diagnostic.starting(sensitive="secret")
    diagnostic.starting(**dict.fromkeys(probe.START_FIELDS, "sensitive"))
    assert set(diagnostic.snapshot()["start_observation"].values()) == {"unavailable"}
    assert "sensitive" not in json.dumps(diagnostic.snapshot())


@pytest.mark.django_db
@pytest.mark.parametrize("terminal", [False, True])
def test_startup_observes_owned_row_lease_and_job_without_exposing_data(terminal):
    from django_ray.models import RayTaskExecution, TaskWorkerLease

    row = RayTaskExecution.objects.create(
        task_id="sensitive-task-id",
        callable_path="sensitive.callable",
        state="FAILED" if terminal else "RUNNING",
        claimed_by_worker="sensitive-worker",
        ray_job_id="sensitive-job",
        ray_job_request_reference="sensitive-reference",
        error_message="sensitive-error" if terminal else None,
    )
    TaskWorkerLease.objects.create(
        worker_id="sensitive-worker", hostname="owned", pid=12345, queue_name=probe.QUEUE
    )
    info = SimpleNamespace(
        status="FAILED" if terminal else "RUNNING",
        error_type=SimpleNamespace(value="JOB_ENTRYPOINT_COMMAND_ERROR") if terminal else None,
        driver_exit_code=78 if terminal else None,
        entrypoint="sensitive-command",
        message="sensitive-message",
    )
    client = Mock(get_job_info=Mock(return_value=info))
    diagnostics = probe.Diagnostics()
    if terminal:
        with pytest.raises(ValueError, match="terminated before"):
            probe.observe_start(row.pk, SimpleNamespace(pid=12345), client, diagnostics)
    else:
        probe.observe_start(row.pk, SimpleNamespace(pid=12345), client, diagnostics)
    observed = diagnostics.snapshot()["start_observation"]
    assert observed == {
        "task_state": "FAILED" if terminal else "RUNNING",
        "manager_lease": "active",
        "claim_owner": "owned_manager",
        "request_binding": "present",
        "submission_binding": "present",
        "task_error": "present" if terminal else "absent",
        "task_error_origin": "other" if terminal else "absent",
        "job_status": "FAILED" if terminal else "RUNNING",
        "job_error": "JOB_ENTRYPOINT_COMMAND_ERROR" if terminal else "none",
        "driver_exit": "preflight_refusal" if terminal else "not_exited",
    }
    assert "sensitive" not in json.dumps(diagnostics.snapshot())
    client.get_job_info.assert_called_once_with(row.ray_job_id)
    client.get_job_logs.assert_not_called()
    client.list_jobs.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("message", "origin"),
    [
        ("Failed to reserve Ray Job submission identity: sensitive", "reservation_failed"),
        ("Failed to submit to Ray: sensitive", "submission_failed"),
        ("sensitive", "other"),
    ],
)
def test_startup_error_prefix_never_exposes_its_free_form_suffix(message, origin):
    from django_ray.models import RayTaskExecution

    row = RayTaskExecution.objects.create(
        task_id="owned", callable_path="builtins.print", error_message=message
    )
    diagnostics = probe.Diagnostics()
    probe.observe_start(row.pk, SimpleNamespace(pid=12345), Mock(), diagnostics)
    assert diagnostics.snapshot()["start_observation"]["task_error_origin"] == origin
    assert "sensitive" not in json.dumps(diagnostics.snapshot())


@pytest.mark.django_db
def test_startup_classifies_every_fixed_production_preparation_rejection():
    from django_ray.models import RayTaskExecution
    from django_ray.runner.errors import (
        RayJobRequestPreparationError,
        RayJobRequestPreparationRejection,
    )

    row = RayTaskExecution.objects.create(task_id="owned", callable_path="builtins.print")
    diagnostics = probe.Diagnostics()
    for kind in RayJobRequestPreparationRejection:
        row.error_message = str(RayJobRequestPreparationError(kind))
        row.save(update_fields=["error_message"])
        probe.observe_start(row.pk, SimpleNamespace(pid=12345), Mock(), diagnostics)
        assert (
            diagnostics.snapshot()["start_observation"]["task_error_origin"] == "prep_" + kind.value
        )


@pytest.mark.django_db
def test_unclaimed_startup_and_unreadable_job_are_distinct_and_never_retried():
    from django_ray.models import RayTaskExecution

    row = RayTaskExecution.objects.create(task_id="owned", callable_path="builtins.print")
    diagnostics = probe.Diagnostics()
    client = Mock(get_job_info=Mock(side_effect=ValueError("sensitive")))
    probe.observe_start(row.pk, SimpleNamespace(pid=12345), client, diagnostics)
    observed = diagnostics.snapshot()["start_observation"]
    assert observed["task_state"] == "QUEUED"
    assert observed["manager_lease"] == "absent"
    assert observed["claim_owner"] == "unclaimed"
    assert observed["job_status"] == "not_submitted"
    client.get_job_info.assert_not_called()
    row.ray_job_id = "owned-reservation"
    row.save(update_fields=["ray_job_id"])
    probe.observe_start(row.pk, SimpleNamespace(pid=12345), client, diagnostics)
    assert diagnostics.snapshot()["start_observation"]["job_status"] == "unavailable"
    client.get_job_info.assert_called_once()
    assert "sensitive" not in json.dumps(diagnostics.snapshot())
    client.get_job_info.side_effect = TimeoutError("sensitive process deadline")
    with pytest.raises(TimeoutError):
        probe.observe_start(row.pk, SimpleNamespace(pid=12345), client, diagnostics)


def test_work_budget_starts_before_django_bootstrap(monkeypatch):
    monkeypatch.setattr(probe.time, "monotonic", lambda: 100.0)
    diagnostic = probe.Diagnostics()
    assert diagnostic.work_deadline == 270.0
    monkeypatch.setattr(probe.time, "monotonic", lambda: 120.0)
    assert diagnostic.work_deadline - probe.time.monotonic() == 150.0


@pytest.mark.django_db
@pytest.mark.parametrize("cleanup_error", [False, True])
@pytest.mark.parametrize("bootstrap_delay", [0, 171])
def test_real_observation_preserves_premature_manager_exit_before_cleanup(
    tmp_path, settings, monkeypatch, cleanup_error, bootstrap_delay
):
    import django
    from django.db import connection

    from django_ray.models import RayTaskExecution
    from django_ray.runner import ray_job
    from testproject import qualification_rq2_tasks

    row = RayTaskExecution.objects.create(
        task_id="owned-rq2-fixture", callable_path="builtins.print"
    )
    settings.RQ2_GENERATION = "before"
    settings.RQ2_ROOT = tmp_path / "owned"
    clock = [100.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(django, "setup", lambda: clock.__setitem__(0, clock[0] + bootstrap_delay))
    monkeypatch.setattr(connection, "vendor", "postgresql")
    monkeypatch.setattr(ray_job, "_address_pinned_job_client", Mock())
    controlled = Mock()
    controlled.using.return_value.enqueue.return_value = SimpleNamespace(id=row.task_id)
    monkeypatch.setattr(qualification_rq2_tasks, "controlled", controlled)
    process = Mock(returncode=1, poll=Mock(return_value=1))
    monkeypatch.setattr(probe, "_start_manager", Mock(return_value=process))
    alarm = Mock()
    monkeypatch.setattr(probe.signal, "alarm", alarm, raising=False)
    if cleanup_error:
        monkeypatch.setattr(probe, "stop_manager", Mock(side_effect=TimeoutError("sensitive")))
    inspect = Mock(side_effect=AssertionError("Manager exit must precede Jobs observation"))
    monkeypatch.setattr(probe, "inspect_job", inspect)
    diagnostic = probe.Diagnostics()
    facts = dict.fromkeys(probe.FACTS, False)
    with pytest.raises(TimeoutError if cleanup_error or bootstrap_delay else ValueError):
        probe.verify("before", facts, diagnostic)
    observed = diagnostic.snapshot()
    assert observed["failure_phase"] == "initial_effect"
    assert observed["failure_type"] == ("timeout" if bootstrap_delay else "validation")
    assert observed["manager_at_failure"] == "exited_nonzero"
    assert observed["manager_after_cleanup"] == "exited_nonzero"
    assert observed["cleanup_failure_type"] == ("timeout" if cleanup_error else "none")
    assert not any(facts.values())
    inspect.assert_not_called()
    process.kill.assert_not_called()
    process.terminate.assert_not_called()
    alarm.assert_called_once_with(5)


@pytest.fixture
def bound_job(db, tmp_path, monkeypatch):
    from django_ray.execution_codec import (
        ExecutionIdentity,
        ExecutionRequest,
        encode_execution_request,
    )
    from django_ray.models import RayTaskExecution, TaskInputPayload
    from django_ray.ray_job_protocol import build_ray_job_request_reference_metadata
    from django_ray.ray_job_request_storage import prepare_ray_job_request
    from django_ray.runner.ray_job import RayJobRunner

    marker = "a" * 32
    row = RayTaskExecution.objects.create(
        task_id="public-task-canary",
        callable_path="testproject.qualification_rq2_tasks.controlled",
        args_json=json.dumps(["success", marker]),
        kwargs_json="{}",
        state="RUNNING",
        execution_generation=1,
        ray_address=probe.ADDRESS,
    )
    request = ExecutionRequest(
        identity=ExecutionIdentity(
            task_execution_pk=row.pk, task_id=row.task_id, attempt_number=1, execution_generation=1
        ),
        execution_protocol_version=1,
        callable_path=row.callable_path,
        transport_version=1,
        serialized_args=row.args_json,
        serialized_kwargs=row.kwargs_json,
        input_reference=None,
        runtime_env_profile=None,
        runtime_env_hash="a" * 64,
        runtime_env_plan_identity={},
        compiled_graph_submission_transport="ray-job",
    )
    serialized = encode_execution_request(request)
    prepared = prepare_ray_job_request(
        serialized,
        {
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        },
    )
    monkeypatch.setattr(probe, "REQUEST_ROOT", str(tmp_path.resolve()))
    row.ray_job_id = RayJobRunner.submission_id(row)
    row.ray_job_request_reference = prepared.reference
    row.save()
    TaskInputPayload.objects.create(
        reference=prepared.reference,
        payload_kind="ray_job_request",
        backend="filesystem",
        digest=prepared.digest,
        size_bytes=prepared.size_bytes,
        envelope_version=prepared.envelope_version,
    )
    info = SimpleNamespace(
        entrypoint=f"python -m django_ray.runtime.entrypoint --request-ref-b64 {prepared.encoded_locator}",
        metadata=build_ray_job_request_reference_metadata(
            request, serialized, prepared.reference, prepared.encoded_locator
        ),
        status="RUNNING",
        driver_exit_code=None,
    )
    client = SimpleNamespace(
        get_job_info=Mock(return_value=info),
        get_job_logs=Mock(return_value=""),
        list_jobs=Mock(
            return_value=[SimpleNamespace(submission_id=row.ray_job_id, metadata=info.metadata)]
        ),
    )
    return row, client, marker, info, prepared


def test_real_stored_request_metadata_and_registry_are_bound(bound_job):
    row, client, marker, _, _ = bound_job
    assert probe.inspect_job(row, client, marker) == "RUNNING"


@pytest.mark.parametrize(
    "key",
    [
        "django_ray_coordination_sha256",
        "django_ray_request_sha256",
        "django_ray_request_reference_sha256",
    ],
)
def test_another_submission_id_with_same_request_binding_is_not_accepted(bound_job, key):
    row, client, marker, info, _ = bound_job
    client.list_jobs.return_value.append(
        SimpleNamespace(submission_id="different-job", metadata={key: info.metadata[key]})
    )
    with pytest.raises(ValueError, match="another Ray Job"):
        probe.inspect_job(row, client, marker)


def test_public_observer_rejects_encryption_key_disclosure(bound_job, monkeypatch):
    row, client, marker, info, _ = bound_job
    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_ENCRYPTION_KEY", "secret-encryption-canary")
    info.message = "secret-encryption-canary"
    with pytest.raises(ValueError, match="public surface"):
        probe.inspect_job(row, client, marker)


def test_effect_observation_is_bounded_and_never_accepts_replay(tmp_path):
    path = tmp_path / "effects"
    assert probe.one_effect(path) is False
    path.write_bytes(b"")
    assert probe.one_effect(path) is False
    path.write_bytes(b"x")
    assert probe.one_effect(path) is True
    path.write_bytes(b"x" * 1000)
    with pytest.raises(ValueError, match="more than one"):
        probe.one_effect(path)


@pytest.mark.parametrize("case", ["success", "missing"])
def test_actual_fixture_callable_writes_only_one_owned_effect(tmp_path, settings, case):
    from testproject.qualification_rq2_tasks import controlled

    settings.RQ2_ROOT = tmp_path
    (tmp_path / "success-release").touch()
    assert controlled.func(case, "a" * 32) == {"value": 42, "effect": "once"}
    assert (tmp_path / f"{case}-effects").read_bytes() == b"x"
    assert not (tmp_path / "missing-setup").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "locator",
        "binding",
        "registry",
        "kind",
        "public-id",
        "callable",
        "argument",
        "logs",
        "duplicate",
        "oversize",
    ],
)
def test_changed_or_leaking_job_observations_fail_closed(bound_job, failure):
    from django_ray.models import TaskInputPayload

    row, client, marker, info, _ = bound_job
    if failure == "locator":
        info.entrypoint += " --payload-b64 unexpected"
    elif failure == "binding":
        info.metadata["django_ray_request_sha256"] = "0" * 64
    elif failure == "registry":
        TaskInputPayload.objects.filter(reference=row.ray_job_request_reference).update(
            state="PURGED"
        )
    elif failure == "kind":
        TaskInputPayload.objects.filter(reference=row.ray_job_request_reference).update(
            payload_kind="task_input"
        )
    elif failure in {"public-id", "callable", "argument"}:
        info.message = {
            "public-id": row.task_id,
            "callable": row.callable_path,
            "argument": marker,
        }[failure]
    elif failure == "logs":
        client.get_job_logs.return_value = marker
    elif failure == "duplicate":
        client.list_jobs.return_value *= 2
    else:
        info.message = "x" * probe.MAX_OBSERVATION_BYTES
    with pytest.raises((ValueError, RuntimeError)):
        probe.inspect_job(row, client, marker)


@pytest.mark.parametrize("failure", [None, "exit", "rejection", "payload"])
def test_missing_request_requires_fixed_exit_and_no_public_payload(bound_job, failure):
    row, client, marker, info, _ = bound_job
    info.status, info.driver_exit_code = "FAILED", 78
    rejected = probe.REFUSAL_LINE
    if failure == "exit":
        info.driver_exit_code = 1
    elif failure == "rejection":
        rejected = "django-ray task completed successfully"
    elif failure == "payload":
        rejected = marker
    client.get_job_logs.return_value = "\n".join(["Ray startup", rejected])
    if failure:
        with pytest.raises(ValueError):
            probe.inspect_job(row, client, marker, missing=True)
    else:
        assert probe.inspect_job(row, client, marker, missing=True) == "FAILED"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "missing_marker",
    [
        None,
        "DJANGO_SETTINGS_MODULE",
        "DJANGO_RAY_QUALIFICATION_RQ2",
        "DJANGO_RAY_QUALIFICATION_RQ2_GENERATION",
        "DJANGO_RAY_QUALIFICATION_RQ2_SETUP_MARKER",
    ],
)
def test_negative_submission_uses_real_preparation_and_typed_delete(
    tmp_path, settings, monkeypatch, capsys, missing_marker
):
    from django_ray.models import RayTaskExecution, TaskInputPayload
    from django_ray.ray_job_request_storage import (
        RayJobRequestLoadError,
        decode_ray_job_request_locator,
        load_ray_job_request,
    )

    settings.DJANGO_RAY = {
        "RAY_ADDRESS": probe.ADDRESS,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    environment = {
        "DJANGO_SETTINGS_MODULE": "testproject.settings_qualification_jobs",
        "DJANGO_RAY_QUALIFICATION_RQ2": "owned-application-job",
        "DJANGO_RAY_QUALIFICATION_RQ2_GENERATION": "before",
        "DJANGO_RAY_QUALIFICATION_RQ2_SETUP_MARKER": "missing",
    }
    if missing_marker:
        del environment[missing_marker]
    runtime_env = json.dumps({"env_vars": environment}, sort_keys=True, separators=(",", ":"))
    row = RayTaskExecution.objects.create(
        task_id="missing-fixture",
        callable_path="builtins.print",
        args_json='["canary"]',
        runtime_env_json=runtime_env,
        runtime_env_hash=hashlib.sha256(runtime_env.encode()).hexdigest(),
        ray_target_address=probe.ADDRESS,
    )
    submissions = []

    def submit(**kwargs):
        submissions.append(kwargs)
        return kwargs["submission_id"]

    client = SimpleNamespace(
        _upload_working_dir_if_needed=lambda _: None,
        _upload_py_modules_if_needed=lambda _: None,
        submit_job=submit,
    )
    if missing_marker:
        from django_ray import ray_job_request_storage

        delete = Mock(side_effect=AssertionError("Unarmed sentinel must preserve its request"))
        monkeypatch.setattr(ray_job_request_storage, "delete_ray_job_request_reference", delete)
        with pytest.raises(ValueError, match="sentinel is not armed"):
            probe.missing_submission(row, client, generation="before")
        delete.assert_not_called()
        assert submissions == []
        return
    probe.missing_submission(row, client, generation="before")
    assert len(submissions) == 1
    row.refresh_from_db()
    assert row.state == "RUNNING" and row.execution_generation == 1
    assert row.ray_job_request_reference.startswith("rq2ref:v1:")
    locator = decode_ray_job_request_locator(submissions[0]["entrypoint"].split()[-1])
    assert locator.reference == row.ray_job_request_reference
    assert TaskInputPayload.objects.get(reference=locator.reference).state == "ACTIVE"
    with pytest.raises(RayJobRequestLoadError):
        load_ray_job_request(locator)
    from django_ray.runtime import entrypoint

    monkeypatch.setenv(
        "RAY_JOB_CONFIG_JSON_ENV_VAR", json.dumps({"metadata": submissions[0]["metadata"]})
    )
    bootstrap = Mock(side_effect=AssertionError("Request refusal must precede Django setup"))
    invoke = Mock(side_effect=AssertionError("Request refusal must precede application effects"))
    monkeypatch.setattr(entrypoint, "bootstrap_django", bootstrap)
    monkeypatch.setattr(entrypoint, "_invoke_task_callable", invoke)
    assert entrypoint.main(["--request-ref-b64", submissions[0]["entrypoint"].split()[-1]]) == 78
    captured = capsys.readouterr()
    assert captured.err.strip() == probe.REFUSAL_LINE
    assert captured.out == ""
    bootstrap.assert_not_called()
    invoke.assert_not_called()


@pytest.mark.parametrize("phase", sorted(probe.PHASES))
def test_failure_receipt_retains_only_fixed_partial_evidence(tmp_path, monkeypatch, capsys, phase):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr(probe.sys, "platform", "linux")
    monkeypatch.setattr(probe.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(probe.signal, "signal", Mock(return_value=None))
    alarm = Mock()
    monkeypatch.setattr(probe.signal, "alarm", alarm, raising=False)

    def fail(generation, facts, diagnostics):
        diagnostics.enter(phase)
        facts[probe.FACTS[0]] = True
        raise RuntimeError("unbounded sensitive error")

    monkeypatch.setattr(probe, "verify", fail)
    target = tmp_path / "receipt.json"
    assert probe.main(["--generation", "before", "--receipt", str(target)]) == 1
    output = capsys.readouterr().out
    assert "sensitive" not in output and "unbounded" not in output
    retained = json.loads(output)
    assert retained["status"] == "failed" and retained["assertions"][probe.FACTS[0]] is True
    assert retained["diagnostics"]["failure_phase"] == phase
    assert retained["diagnostics"]["failure_type"] == "unexpected"
    assert not target.exists()
    assert [call.args[0] for call in alarm.call_args_list] == [170, 0]
