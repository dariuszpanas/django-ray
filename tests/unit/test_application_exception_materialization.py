"""Fixed exception fixture contracts with clocks and native seams replaced."""

import ast
import json
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from qualification.application import exception_materialization as probe


def record(transport="ray_core", *, number=1):
    value = {
        "schema_version": 1,
        "trigger": "broken_exception_message_attempt1",
        "transport": transport,
        "execution_id": number,
        "task_id": f"00000000-0000-4000-8000-{number:012d}",
        "attempt_numbers": [1, 2],
        "attempt_states": ["FAILED", "SUCCEEDED"],
        "current_attempt": 2,
        "current_generation": 2,
        "execution_protocol_version": 1,
        "result": {"recovered": True, "attempt": 2},
        "expected_exception_type": "testproject.qualification_exception_tasks.BrokenMessageFixtureError",
        "failure_message": "exception message unavailable",
        "manager_versions": ["0.8.0", "0.8.0"],
        "executor_versions": ["0.8.0", "0.8.0"],
        "original_failure_traceback_verified": True,
        "secondary_rendering_text_absent": True,
        "current_owner_verified": True,
        "automatic_recovery_observed": True,
        "current_api_verified": True,
        "archived_admin_verified": True,
        "raw_fields_preserved_after_reads": True,
        "owned_execution_and_attempt_rows_removed": True,
        "complete_application_gate": False,
        "manual_retry_used": False,
        "third_attempt_observed": False,
        "wire_retryable_field_directly_observed": False,
        "wire_exception_type_field_directly_observed": False,
        "extra_manager_started_by_observer": False,
    }
    if transport == "rq2_job":
        value.update(
            job_attempt_numbers=[1, 2],
            job_generation_numbers=[1, 2],
            transport_statuses=["SUCCEEDED", "SUCCEEDED"],
            driver_exit_codes=[0, 0],
            typed_request_bindings_verified=True,
            per_identity_submission_unique=True,
            settled_without_attempt3=True,
            individual_request_GC_claimed=False,
        )
    return value


@pytest.mark.parametrize("transport", ["ray_core", "rq2_job"])
def test_fixed_record_round_trips_without_becoming_full_gate(transport):
    value = json.loads(json.dumps(record(transport)))
    probe.validate_record(value, transport=transport, expected_version="0.8.0")
    assert value["complete_application_gate"] is False
    assert value["wire_exception_type_field_directly_observed"] is False


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("execution_id", True),
        ("execution_id", 0),
        ("attempt_numbers", [True, 2]),
        ("current_generation", 3),
        ("attempt_states", ["SUCCEEDED", "SUCCEEDED"]),
        ("result", {"recovered": 1, "attempt": 2}),
        ("failure_message", "transport failed"),
        ("expected_exception_type", "builtins.RuntimeError"),
        ("current_api_verified", 1),
        ("owned_execution_and_attempt_rows_removed", False),
        ("manual_retry_used", True),
        ("wire_retryable_field_directly_observed", True),
        ("complete_application_gate", True),
        ("executor_versions", ["0.8.0", "0.9.0"]),
        ("manager_versions", ["0.8.0\n", "0.8.0"]),
        ("task_id", "00000000-0000-1000-8000-000000000001"),
    ],
)
def test_record_rejects_coercions_and_unsupported_outcomes(field, bad):
    value = record()
    value[field] = bad
    with pytest.raises(ValueError):
        probe.validate_record(value, transport="ray_core")


@pytest.mark.parametrize("field", sorted(probe.FIELDS))
def test_every_common_field_is_mandatory(field):
    value = record()
    del value[field]
    with pytest.raises(ValueError):
        probe.validate_record(value, transport="ray_core")


@pytest.mark.parametrize("codes", [[False, 0], [None, 0], [0.0, 0], [1, 0]])
def test_job_record_requires_model_integer_zero_without_wire_claim(codes):
    value = record("rq2_job")
    value["driver_exit_codes"] = codes
    with pytest.raises(ValueError):
        probe.validate_record(value, transport="rq2_job")


def test_unknown_fields_wrong_transport_and_foreign_package_are_refused():
    value = record()
    value["raw_request"] = "must not be retained"
    with pytest.raises(ValueError):
        probe.validate_record(value, transport="ray_core")
    with pytest.raises(ValueError):
        probe.validate_record(record("rq2_job"), transport="ray_core")
    with pytest.raises(ValueError):
        probe.validate_record(record(), transport="ray_core", expected_version="0.9.0")


@pytest.mark.parametrize("duplicate", ["task_id", "execution_id", "version", "count"])
def test_two_transports_in_both_generations_have_distinct_owned_identities(duplicate):
    values = [
        record(transport, number=index)
        for index, transport in enumerate(("ray_core", "rq2_job", "ray_core", "rq2_job"), 1)
    ]
    probe.validate_distinct_records(values)
    if duplicate == "count":
        values.pop()
    elif duplicate == "version":
        values[-1]["manager_versions"] = ["0.9.0"] * 2
        values[-1]["executor_versions"] = ["0.9.0"] * 2
    else:
        values[-1][duplicate] = values[0][duplicate]
    with pytest.raises(ValueError):
        probe.validate_distinct_records(values)


@pytest.mark.parametrize("late", [False, True])
def test_wait_retains_one_original_deadline_and_refuses_late_truth(monkeypatch, late):
    clock = [10.0]
    sleeps = []
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(probe.time, "sleep", sleep)
    calls = []

    def predicate():
        calls.append(clock[0])
        if late:
            clock[0] = 10.3
            return True
        return False

    with pytest.raises(TimeoutError):
        probe._wait(predicate, deadline=10.3, alive=lambda: None)
    assert calls
    assert all(0 < item <= 0.2 for item in sleeps)
    if not late:
        assert sleeps == pytest.approx([0.2, 0.1])


def test_wait_never_swallows_manager_predicate_or_transport_failure(monkeypatch):
    monkeypatch.setattr(probe.time, "monotonic", lambda: 1.0)
    for alive, predicate in (
        (Mock(side_effect=ValueError("manager")), Mock()),
        (Mock(), Mock(side_effect=RuntimeError("predicate"))),
    ):
        with pytest.raises((ValueError, RuntimeError)):
            probe._wait(predicate, deadline=2, alive=alive)


def test_pure_validator_import_has_no_django_sdk_task_or_process_seam(tmp_path):
    script = """
import importlib.abc, sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'django', 'django_ray', 'ray', 'testproject'}:
            raise ModuleNotFoundError('Native import denied', name=fullname)
sys.meta_path.insert(0, Deny())
sys.path.insert(0, sys.argv[1])
from qualification.application.exception_materialization import validate_record
try:
    validate_record(None, transport='ray_core')
except ValueError:
    pass
else:
    raise AssertionError('Missing mandatory evidence accepted')
assert not any(name.split('.')[0] in {'django','django_ray','ray','testproject'} for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(Path(__file__).resolve().parents[2])],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_task_body_uses_strict_attempt_identity_instead_of_a_counter():
    path = Path(__file__).resolve().parents[2] / "testproject/qualification_exception_tasks.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
    for node in selected:
        if isinstance(node, ast.FunctionDef):
            node.decorator_list = []
    body = ast.Module(body=selected, type_ignores=[])
    context = SimpleNamespace(
        execution_protocol_version=1, attempt_number=1, execution_generation=1
    )
    namespace: dict[str, Any] = {
        "os": SimpleNamespace(
            environ={"DJANGO_SETTINGS_MODULE": "testproject.settings_qualification"}
        ),
        "settings": SimpleNamespace(SETTINGS_MODULE="testproject.settings_qualification"),
        "SETTINGS": {
            "testproject.settings_qualification",
            "testproject.settings_qualification_jobs",
        },
        "RENDERING_SENTINEL": "qualification exception rendering sentinel",
        "require_strict_task_execution_context": lambda: context,
    }
    exec(compile(body, str(path), "exec"), namespace)
    task = namespace["broken_message_then_recover"]
    with pytest.raises(namespace["BrokenMessageFixtureError"]) as first:
        task()
    with pytest.raises(RuntimeError, match="rendering sentinel"):
        str(first.value)
    context.attempt_number = context.execution_generation = 2
    assert task() == {"recovered": True, "attempt": 2}
    context.attempt_number = context.execution_generation = 3
    with pytest.raises(ValueError):
        task()
    namespace["settings"].SETTINGS_MODULE = "testproject.settings"
    with pytest.raises(ValueError):
        task()


@pytest.mark.parametrize("transport", ["ray_core", "rq2_job"])
def test_preflight_uses_existing_normalized_short_and_full_retry_denylist(
    settings,
    monkeypatch,
    transport,
):
    from django.db import connection

    from django_ray.conf import settings as config_module

    module = "testproject.settings_qualification" + ("_jobs" if transport == "rq2_job" else "")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", module)
    settings.RQ2_GENERATION = "before"
    # Each later override creates a holder with its own SETTINGS_MODULE=None.
    settings.SETTINGS_MODULE = module
    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_RQ2", "owned-application-job")
    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_RQ2_GENERATION", "before")
    monkeypatch.setattr(connection, "vendor", "postgresql")
    forbidden = Mock(side_effect=AssertionError("No database connection admitted"))
    monkeypatch.setattr(connection, "ensure_connection", forbidden)
    config = {"MAX_TASK_ATTEMPTS": 3, "RETRY_BACKOFF_SECONDS": 5, "RETRY_EXCEPTION_DENYLIST": []}
    monkeypatch.setattr(config_module, "get_settings", lambda: config)
    probe._configuration(transport)
    for entry in (
        probe.EXPECTED_TYPE,
        "BrokenMessageFixtureError",
        " other.BrokenMessageFixtureError ",
    ):
        config["RETRY_EXCEPTION_DENYLIST"] = [entry]
        with pytest.raises(ValueError):
            probe._configuration(transport)
    forbidden.assert_not_called()


def test_presentation_reads_only_existing_routes_and_removes_its_session(monkeypatch):
    from qualification.application import run_api

    row = SimpleNamespace(
        pk=21,
        task_id=record()["task_id"],
        managed_with_django_ray_version="0.8.0",
        executor_django_ray_version="0.8.0",
    )
    failed = SimpleNamespace(pk=22)
    calls = []
    sessions = []

    @contextmanager
    def session(**_kwargs):
        sessions.append("created")
        try:
            yield "qualification=fixed-test-cookie"
        finally:
            sessions.append("removed")

    def request(path, **kwargs):
        calls.append((path, kwargs))
        if path.startswith("/admin/"):
            if not kwargs["headers"]:
                return 302, b""
            return 200, (
                "<p>exception message unavailable</p><pre>"
                + probe.EXPECTED_TYPE
                + " broken_message_then_recover</pre>"
            ).encode()
        if not kwargs["headers"]:
            return 401, b"{}"
        value = {
            "task_id": row.task_id,
            "state": "SUCCEEDED",
            "attempt_number": 2,
            "execution_generation": 2,
            "execution_protocol_version": 1,
            "managed_with_django_ray_version": "0.8.0",
            "executor_django_ray_version": "0.8.0",
        }
        if path.startswith("/api/executions/"):
            value.update(
                id=row.pk,
                result_data=json.dumps(probe.RESULT),
                error_message=None,
                result_data_omission_reason=None,
            )
        return 200, json.dumps(value).encode()

    monkeypatch.setattr(run_api, "ApplicationHttp", lambda *args, **kwargs: request)
    monkeypatch.setattr(probe, "_owned_admin_session", session)
    monkeypatch.setattr(probe.time, "monotonic", lambda: 1)
    probe._presentation(row, failed, token="fixed-test-token", deadline=2)
    assert sessions == ["created", "removed"]
    assert len(calls) == 6
    assert {path for path, _ in calls} == {
        f"/api/tasks/{row.task_id}",
        "/api/executions/21",
        "/admin/django_ray/taskattempt/22/change/",
    }
    assert all(kwargs["response_limit"] <= 1024 * 1024 for _, kwargs in calls)


@pytest.mark.parametrize("failure", ["missing", "extra", "false", "wrong-transport"])
def test_both_collector_branches_require_the_new_pure_record(failure):
    from qualification.application.run_chainsaw import parse_receipts
    from tests.unit.test_application_chainsaw import receipt as core_receipt
    from tests.unit.test_application_rq2 import receipt as job_receipt

    for layer, maker, name in (
        ("ray_core", lambda: json.loads(core_receipt("application_core")), "before-core"),
        ("rq2_job", job_receipt, "before-rq2"),
    ):
        value = maker()
        if failure == "missing":
            del value["exception_materialization"]
        elif failure == "extra":
            value["exception_materialization"]["raw_job"] = "not permitted"
        elif failure == "false":
            value["exception_materialization"]["owned_execution_and_attempt_rows_removed"] = False
        else:
            value["exception_materialization"] = record(
                "rq2_job" if layer == "ray_core" else "ray_core"
            )
        with pytest.raises(ValueError):
            parse_receipts(json.dumps(value).encode(), (name,))


@pytest.mark.parametrize("case", ["absent", "valid", "none", "malformed", "old-assertion"])
def test_historical_receipt_policy_only_waives_absent_record(case):
    from qualification.application.run_chainsaw import parse_receipts
    from qualification.application.run_rq2 import validate_receipt
    from tests.unit.test_application_chainsaw import receipt as core_receipt
    from tests.unit.test_application_rq2 import receipt as job_receipt

    for transport, maker, name in (
        ("ray_core", lambda: json.loads(core_receipt("application_core")), "before-core"),
        ("rq2_job", job_receipt, "before-rq2"),
    ):
        value = maker()
        if case in {"absent", "old-assertion"}:
            del value["exception_materialization"]
        elif case == "none":
            value["exception_materialization"] = None
        elif case == "malformed":
            value["exception_materialization"] = {}
        if case == "old-assertion":
            facts = value["lease_policy"] if transport == "ray_core" else value["assertions"]
            facts[next(iter(facts))] = False
        raw = json.dumps(value).encode()
        if case in {"absent", "valid"}:
            assert parse_receipts(raw, (name,), require_exception_materialization=False) == {
                name: raw
            }
            if transport == "rq2_job":
                validate_receipt(value, require_exception_materialization=False)
        else:
            with pytest.raises(ValueError):
                parse_receipts(raw, (name,), require_exception_materialization=False)
            if transport == "rq2_job":
                with pytest.raises(ValueError):
                    validate_receipt(value, require_exception_materialization=False)


@pytest.mark.parametrize("policy", [None, 0, 1, "false"])
def test_receipt_exception_policy_requires_an_actual_boolean(policy):
    from qualification.application.run_chainsaw import parse_receipts
    from qualification.application.run_rq2 import validate_receipt
    from tests.unit.test_application_chainsaw import receipt as core_receipt
    from tests.unit.test_application_rq2 import receipt as job_receipt

    with pytest.raises(ValueError, match="receipt policy"):
        parse_receipts(
            core_receipt("application_setup"),
            ("setup",),
            require_exception_materialization=policy,
        )
    with pytest.raises(ValueError, match="receipt policy"):
        validate_receipt(job_receipt(), require_exception_materialization=policy)


def test_fixed_diagnostic_allowlists_admit_only_the_named_new_top_level_field():
    from qualification.application import safe_diagnostics

    assert "exception_materialization" in safe_diagnostics.STAGES
    for layer, transport in (("application_core", "ray_core"), ("application_rq2", "rq2_job")):
        value = {"layer": layer, "exception_materialization": record(transport)}
        assert safe_diagnostics.passed_receipt(value) is value
        value["raw_request"] = "not permitted"
        with pytest.raises(ValueError):
            safe_diagnostics.passed_receipt(value)


@pytest.fixture
def bound_retry_jobs(monkeypatch):
    """Real protocol hashes/validation; all storage, DB and SDK I/O replaced."""
    from django_ray import ray_job_protocol as protocol
    from django_ray import ray_job_request_storage as storage
    from django_ray.execution_codec import (
        ExecutionIdentity,
        ExecutionRequest,
        encode_execution_request,
    )
    from django_ray.models import TaskInputPayload
    from django_ray.runner import ray_job

    row = SimpleNamespace(
        pk=21,
        task_id=record()["task_id"],
        args_json="[]",
        kwargs_json="{}",
        callable_path=probe.CALLABLE,
        ray_address="http://ray-head:8265",
    )
    infos, requests, locators, registry, job_logs = {}, {}, {}, {}, {}
    jobs = []
    for number in (1, 2):
        identity = ExecutionIdentity(row.pk, row.task_id, number, number)
        request = ExecutionRequest(
            identity=identity,
            execution_protocol_version=1,
            callable_path=probe.CALLABLE,
            transport_version=1,
            serialized_args="[]",
            serialized_kwargs="{}",
            input_reference=None,
            runtime_env_profile=None,
            runtime_env_hash="a" * 64,
            runtime_env_plan_identity={},
            compiled_graph_submission_transport="ray-job",
        )
        serialized = encode_execution_request(request)
        reference, carrier = f"rq2ref:v1:owned-{number}", f"owned_carrier_{number}"
        locator = SimpleNamespace(reference=reference, filesystem_path="/rq2/requests")
        submission = (
            protocol.STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX
            + protocol.coordination_sha256(identity)
        )
        metadata = protocol.build_ray_job_request_reference_metadata(
            request, serialized, reference, carrier
        )
        info = SimpleNamespace(
            entrypoint=f"python -m django_ray.runtime.entrypoint --request-ref-b64 {carrier}",
            metadata=metadata,
            status="SUCCEEDED",
            driver_exit_code=0,
        )
        info.model_dump = lambda info=info: {
            "entrypoint": info.entrypoint,
            "metadata": info.metadata,
            "status": info.status,
            "driver_exit_code": info.driver_exit_code,
        }
        infos[submission] = info
        job_logs[submission] = (
            "django-ray task failed: exception message unavailable\n"
            if number == 1
            else "django-ray task completed successfully\n"
        )
        locators[carrier] = locator
        requests[reference] = SimpleNamespace(request=request, serialized_request=serialized)
        registry[reference] = SimpleNamespace(
            payload_kind="ray_job_request",
            state="ACTIVE",
            backend="filesystem",
            digest=protocol.request_sha256(serialized),
            size_bytes=protocol.request_size_bytes(serialized),
        )
        jobs.append(SimpleNamespace(submission_id=submission, metadata=dict(metadata)))
        if number == 2:
            row.ray_job_id, row.ray_job_request_reference = submission, reference
    client = SimpleNamespace(
        list_jobs=Mock(return_value=jobs),
        get_job_info=Mock(side_effect=infos.__getitem__),
        get_job_logs=Mock(side_effect=job_logs.__getitem__),
        fixture_logs=job_logs,
    )
    monkeypatch.setattr(storage, "decode_ray_job_request_locator", locators.__getitem__)
    load = Mock(side_effect=lambda locator, **kwargs: requests[locator.reference])
    monkeypatch.setattr(storage, "load_ray_job_request", load)
    monkeypatch.setattr(
        storage,
        "ray_job_request_reference_content_identity",
        lambda reference: (registry[reference].digest, registry[reference].size_bytes),
    )
    monkeypatch.setattr(
        TaskInputPayload.objects, "get", lambda **kwargs: registry[kwargs["reference"]]
    )
    client.fixture_control_requests = ray_job._bounded_control_requests
    monkeypatch.setattr(ray_job, "_bounded_control_requests", lambda _client: nullcontext())
    monkeypatch.setattr(probe.time, "monotonic", lambda: 1)
    return row, client, infos, jobs, requests, locators, load


def test_retry_jobs_bind_two_exact_identities_without_counting_foreign_jobs(bound_retry_jobs):
    row, client, infos, jobs, _requests, _locators, load = bound_retry_jobs
    jobs.append(SimpleNamespace(submission_id="unrelated-existing-job", metadata={}))
    complete, submissions = probe._job_observation(row, client, deadline=2)
    assert complete is True and len(set(submissions)) == 2
    assert set(submissions) == set(infos)
    assert submissions[1] == row.ray_job_id
    assert [call.kwargs["expected_identity"].attempt_number for call in load.call_args_list] == [
        1,
        2,
    ]
    assert [
        call.kwargs["expected_identity"].execution_generation for call in load.call_args_list
    ] == [1, 2]
    assert all(
        call.kwargs["expected_execution_protocol_version"] == 1 for call in load.call_args_list
    )
    assert client.get_job_info.call_count == 2


@pytest.mark.parametrize(
    "failure", ["duplicate", "rebound", "carrier", "current", "inventory", "arguments"]
)
def test_retry_jobs_reject_aliases_and_unbound_carriers(bound_retry_jobs, failure):
    row, client, infos, jobs, _requests, locators, _load = bound_retry_jobs
    first = next(iter(infos.values()))
    if failure == "duplicate":
        jobs.append(SimpleNamespace(submission_id="duplicate", metadata=dict(first.metadata)))
    elif failure == "rebound":
        first.metadata = dict(list(infos.values())[1].metadata)
    elif failure == "carrier":
        locators["owned_carrier_1"].filesystem_path = "/unknown"
    elif failure == "current":
        row.ray_job_request_reference = "rq2ref:v1:not-the-owned-second-reference"
    elif failure == "inventory":
        jobs.extend(
            SimpleNamespace(submission_id=f"other-{number}", metadata={}) for number in range(64)
        )
    else:
        row.args_json = '["different"]'
    with pytest.raises(ValueError):
        probe._job_observation(row, client, deadline=2)


@pytest.mark.parametrize("code", [False, 0.0, 78])
def test_retry_jobs_do_not_coerce_nonzero_or_noninteger_sdk_driver_exit(bound_retry_jobs, code):
    row, client, infos, *_ = bound_retry_jobs
    next(iter(infos.values())).driver_exit_code = code
    with pytest.raises(ValueError, match="SDK integer zero"):
        probe._job_observation(row, client, deadline=2)


def test_retry_job_discovery_keeps_waiting_for_missing_model_exit_code(bound_retry_jobs):
    row, client, infos, *_ = bound_retry_jobs
    next(iter(infos.values())).driver_exit_code = None
    assert probe._job_observation(row, client, deadline=2)[0] is False


@pytest.mark.parametrize("failed_body", [False, True])
def test_owned_session_is_removed_even_when_an_http_observation_fails(
    settings, monkeypatch, failed_body
):
    from django.contrib import auth
    from django.contrib.sessions.backends import db

    module = "testproject.settings_qualification_jobs"
    settings.SESSION_ENGINE = "django.contrib.sessions.backends.db"
    settings.AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
    settings.SESSION_COOKIE_NAME = "qualification"
    settings.SETTINGS_MODULE = module
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", module)
    user = SimpleNamespace(
        pk=5,
        is_active=True,
        is_staff=True,
        is_superuser=True,
        get_session_auth_hash=lambda: "fixed-test-hash",
    )
    monkeypatch.setattr(
        auth,
        "get_user_model",
        lambda: SimpleNamespace(objects=SimpleNamespace(get=Mock(return_value=user))),
    )

    class Session(dict):
        session_key = "fixed-owned-key"
        set_expiry = Mock()
        save = Mock()
        delete = Mock()
        exists = Mock(return_value=False)

    session = Session()
    monkeypatch.setattr(db, "SessionStore", lambda: session)
    monkeypatch.setattr(probe.time, "monotonic", lambda: 1)
    context = pytest.raises(RuntimeError, match="HTTP failed") if failed_body else nullcontext()
    with context, probe._owned_admin_session(deadline=2) as cookie:
        assert cookie == "qualification=fixed-owned-key"
        if failed_body:
            raise RuntimeError("HTTP failed")
    session.delete.assert_called_once_with("fixed-owned-key")
    session.exists.assert_called_once_with("fixed-owned-key")


def test_settled_cleanup_refuses_mutated_history_before_deleting(monkeypatch):
    from django.db import transaction

    from django_ray.models import RayTaskExecution

    row = SimpleNamespace(
        state="SUCCEEDED", attempt_number=2, execution_generation=2, delete=Mock()
    )
    selected = SimpleNamespace(get=Mock(return_value=row))
    monkeypatch.setattr(RayTaskExecution.objects, "select_for_update", lambda: selected)
    monkeypatch.setattr(transaction, "atomic", nullcontext)
    monkeypatch.setattr(probe.time, "monotonic", lambda: 1)
    monkeypatch.setattr(probe, "_raw_fields", lambda *_, **_kwargs: ("changed", []))
    with pytest.raises(ValueError, match="changed before exact owned cleanup"):
        probe._remove_settled(21, record()["task_id"], ("original", []), deadline=2)
    selected.get.assert_called_once_with(pk=21, task_id=record()["task_id"])
    row.delete.assert_not_called()


@pytest.fixture
def durable_history(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from django.utils import timezone

    from django_ray.conf import settings as configuration
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease

    now = datetime(2026, 11, 17, tzinfo=UTC)
    monkeypatch.setattr(probe.time, "monotonic", lambda: 1)
    common = {
        "execution_protocol_version": 1,
        "managed_with_django_ray_version": "0.8.0",
        "executor_django_ray_version": "0.8.0",
        "result_reference": None,
    }
    failed = SimpleNamespace(
        **common,
        pk=31,
        attempt_number=1,
        state="FAILED",
        result_data=None,
        error_message=probe.FALLBACK,
        error_traceback=f"Traceback: broken_message_then_recover\n{probe.EXPECTED_TYPE}: <exception str() failed>",
        started_at=now - timedelta(seconds=20),
        finished_at=now - timedelta(seconds=18),
        created_at=now - timedelta(seconds=17),
    )
    succeeded = SimpleNamespace(
        **common,
        pk=32,
        attempt_number=2,
        state="SUCCEEDED",
        result_data=json.dumps(probe.RESULT),
        error_message=None,
        error_traceback=None,
        started_at=now - timedelta(seconds=10),
        finished_at=now - timedelta(seconds=5),
        created_at=now - timedelta(seconds=4),
    )
    row = SimpleNamespace(
        **vars(succeeded),
        execution_generation=2,
        callable_path=probe.CALLABLE,
        created_with_django_ray_version="0.8.0",
        task_id=record()["task_id"],
        run_after=now - timedelta(seconds=12),
        runtime_env_profile="project",
        ray_job_id="ray-core-owned-result",
        ray_job_request_reference=None,
        ray_address="ray://ray-head:10001",
        ray_target_address="ray://ray-head:10001",
        claimed_by_worker="qualification-worker",
    )
    row.pk, row.created_at = 21, now - timedelta(seconds=30)
    attempts = [failed, succeeded]
    lease = SimpleNamespace(
        is_active=True,
        django_ray_version="0.8.0",
        started_at=now - timedelta(seconds=25),
        last_heartbeat_at=now - timedelta(seconds=1),
        hostname="django-manager-owned",
        pid=123,
    )
    monkeypatch.setattr(RayTaskExecution.objects, "get", Mock(return_value=row))
    monkeypatch.setattr(
        TaskAttempt.objects,
        "filter",
        lambda **kwargs: SimpleNamespace(order_by=lambda *_: attempts),
    )
    monkeypatch.setattr(TaskWorkerLease.objects, "get", Mock(return_value=lease))
    monkeypatch.setattr(timezone, "now", lambda: now)
    monkeypatch.setattr(
        configuration,
        "get_settings",
        lambda: {"WORKER_LEASE_SECONDS": 60, "RAY_ADDRESS": "ray://ray-head:10001"},
    )
    return row, attempts, lease


def test_terminal_history_uses_real_archive_fields_and_accepts_elapsed_run_after(durable_history):
    row, attempts, _lease = durable_history
    assert row.run_after is not None
    assert not hasattr(attempts[0], "generation") and not hasattr(attempts[0], "ray_job_id")
    actual, archived = probe._terminal_snapshot(
        21, row.task_id, transport="ray_core", version="0.8.0", owner="django-manager-", deadline=2
    )
    assert actual is row and archived == attempts


@pytest.mark.parametrize(
    "failure",
    [
        "secondary",
        "transport",
        "class",
        "version",
        "attempt3",
        "generation",
        "archive-time",
        "profile",
    ],
)
def test_terminal_history_rejects_substitute_failure_or_unowned_recovery(durable_history, failure):
    row, attempts, _lease = durable_history
    if failure == "secondary":
        attempts[0].error_traceback += probe.RENDERING_SENTINEL
    elif failure == "transport":
        attempts[0].error_message = "Strict Ray Job terminated without an exact completion envelope"
    elif failure == "class":
        attempts[0].error_traceback = "builtins.RuntimeError: other error"
    elif failure == "version":
        attempts[0].executor_django_ray_version = "other"
    elif failure == "attempt3":
        attempts.append(SimpleNamespace(attempt_number=3))
    elif failure == "generation":
        row.execution_generation = 3
    elif failure == "profile":
        row.runtime_env_profile = "thin"
    else:
        attempts[0].created_at = attempts[0].started_at
    with pytest.raises(ValueError):
        probe._terminal_snapshot(
            21,
            row.task_id,
            transport="ray_core",
            version="0.8.0",
            owner="django-manager-",
            deadline=2,
        )


@pytest.fixture
def observer_seams(monkeypatch):
    from django_ray.models import RayTaskExecution
    from django_ray.runner import ray_job

    clock = [1.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        probe.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(probe, "_configuration", lambda _transport: ({}, "0.8.0"))
    row = SimpleNamespace(pk=21, task_id=record()["task_id"], state="SUCCEEDED", attempt_number=2)
    task = Mock()
    task.using.return_value.enqueue.return_value = SimpleNamespace(id=row.task_id)
    monkeypatch.setitem(
        sys.modules,
        "testproject.qualification_exception_tasks",
        SimpleNamespace(broken_message_then_recover=task),
    )
    monkeypatch.setattr(RayTaskExecution.objects, "get", Mock(return_value=row))
    monkeypatch.setattr(ray_job, "_address_pinned_job_client", lambda _address: SimpleNamespace())
    monkeypatch.setattr(
        probe,
        "_terminal_snapshot",
        Mock(return_value=(row, [SimpleNamespace(pk=31), SimpleNamespace(pk=32)])),
    )
    raw = Mock(return_value=("unchanged", [{"id": 31}, {"id": 32}]))
    monkeypatch.setattr(probe, "_raw_fields", raw)
    presentation, cleanup = Mock(), Mock()
    monkeypatch.setattr(probe, "_presentation", presentation)
    monkeypatch.setattr(probe, "_remove_settled", cleanup)
    manager = SimpleNamespace(pid=123, poll=Mock(return_value=None))
    jobs = Mock(return_value=(True, ("owned-first", "owned-second")))
    monkeypatch.setattr(probe, "_job_observation", jobs)
    return clock, row, task, raw, presentation, cleanup, manager, jobs


@pytest.mark.parametrize("transport", ["ray_core", "rq2_job"])
def test_observer_keeps_original_budget_manager_and_cleanup_contract(observer_seams, transport):
    clock, _row, task, raw, presentation, cleanup, manager, jobs = observer_seams
    result = probe.observe(
        transport=transport,
        token="fixed-test-token",
        deadline=10,
        manager=manager if transport == "rq2_job" else None,
        manager_prefix="django-manager-" if transport == "ray_core" else None,
    )
    probe.validate_record(result, transport=transport)
    assert result["owned_execution_and_attempt_rows_removed"] is True
    assert result["extra_manager_started_by_observer"] is False
    task.using.return_value.enqueue.assert_called_once_with()
    assert presentation.call_count == 1 and cleanup.call_count == 1 and raw.call_count == 2
    assert cleanup.call_args.kwargs["deadline"] == 10
    if transport == "rq2_job":
        assert clock[0] >= 5 and jobs.call_count >= 2
        assert manager.poll.call_count >= 2
    else:
        jobs.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["read", "mutation", "cleanup", "late-read", "settlement-budget"]
)
def test_uncertain_or_failed_observation_never_claims_cleanup_success(observer_seams, failure):
    clock, _row, _task, raw, presentation, cleanup, manager, _jobs = observer_seams
    transport, deadline = "ray_core", 10
    if failure == "read":
        presentation.side_effect = ValueError("fixed read refusal")
    elif failure == "mutation":
        raw.side_effect = [("original", []), ("changed", [])]
    elif failure == "cleanup":
        cleanup.side_effect = ValueError("fixed cleanup refusal")
    elif failure == "late-read":
        presentation.side_effect = lambda *args, **kwargs: clock.__setitem__(0, 11)
    else:
        transport, deadline = "rq2_job", 3
    with pytest.raises((ValueError, TimeoutError)):
        probe.observe(
            transport=transport,
            token="fixed-test-token",
            deadline=deadline,
            manager=manager if transport == "rq2_job" else None,
            manager_prefix="django-manager-" if transport == "ray_core" else None,
        )
    if failure != "cleanup":
        cleanup.assert_not_called()


@pytest.mark.parametrize("stage", ["configuration", "client"])
def test_expired_configuration_or_client_does_not_enqueue_more_work(
    observer_seams, monkeypatch, stage
):
    from django_ray.runner import ray_job

    clock, _row, task, _raw, _presentation, _cleanup, manager, _jobs = observer_seams

    def configure(_transport):
        clock[0] = 3
        return {}, "0.8.0"

    if stage == "configuration":
        monkeypatch.setattr(probe, "_configuration", configure)
    else:

        def client(_address):
            clock[0] = 3
            return SimpleNamespace()

        monkeypatch.setattr(ray_job, "_address_pinned_job_client", client)
    with pytest.raises(TimeoutError):
        probe.observe(
            transport="rq2_job" if stage == "client" else "ray_core",
            token="fixed-test-token",
            deadline=2,
            manager=manager if stage == "client" else None,
            manager_prefix="django-manager-" if stage == "configuration" else None,
        )
    task.using.return_value.enqueue.assert_not_called()


def test_retry_jobs_match_actual_entrypoint_failure_and_success_diagnostics(bound_retry_jobs):
    row, client, _infos, *_ = bound_retry_jobs
    complete, submissions = probe._job_observation(row, client, deadline=2)
    assert complete is True
    assert client.fixture_logs[submissions[0]].splitlines() == [
        "django-ray task failed: exception message unavailable"
    ]
    assert client.fixture_logs[submissions[1]].splitlines() == [
        "django-ray task completed successfully"
    ]


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "duplicate",
        "wrong-attempt",
        "invalid-envelope",
        "rendering",
        "raw-task",
        "raw-callable",
        "raw-request",
        "fallback-on-other-line",
    ],
)
def test_retry_job_terminal_logs_refuse_substitutes_and_raw_values(
    bound_retry_jobs, attempt, failure
):
    row, client, infos, _jobs, requests, *_ = bound_retry_jobs
    submission = list(infos)[attempt - 1]
    original = client.fixture_logs[submission]
    if failure == "missing":
        value = ""
    elif failure == "duplicate":
        value = original + original
    elif failure == "wrong-attempt":
        value = (
            "django-ray task completed successfully\n"
            if attempt == 1
            else "django-ray task failed: exception message unavailable\n"
        )
    elif failure == "invalid-envelope":
        value = "django-ray task produced an invalid completion envelope\n"
    else:
        forbidden = {
            "rendering": probe.RENDERING_SENTINEL,
            "raw-task": row.task_id,
            "raw-callable": probe.CALLABLE,
            "raw-request": list(requests.values())[attempt - 1].serialized_request,
            "fallback-on-other-line": "unexpected prefix: exception message unavailable",
        }
        value = original + forbidden[failure] + "\n"
    client.fixture_logs[submission] = value
    with pytest.raises(ValueError):
        probe._job_observation(row, client, deadline=2)


@pytest.mark.parametrize("attempt", [1, 2])
def test_expected_fallback_is_not_permitted_in_either_sdk_model(bound_retry_jobs, attempt):
    row, client, infos, *_ = bound_retry_jobs
    info = list(infos.values())[attempt - 1]
    original = info.model_dump
    info.model_dump = lambda: {**original(), "message": "exception message unavailable"}
    with pytest.raises(ValueError, match="public surface"):
        probe._job_observation(row, client, deadline=2)


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("surface", ["sdk", "log"])
@pytest.mark.parametrize(
    "key",
    [
        "fixture_password",
        "fixture_secret",
        "fixture_token",
        "fixture_api_key",
        "fixture_encryption_key",
    ],
)
def test_both_retry_job_surfaces_refuse_nonempty_sensitive_environment_values(
    bound_retry_jobs,
    monkeypatch,
    attempt,
    surface,
    key,
):
    row, client, infos, *_ = bound_retry_jobs
    canary = "synthetic-fixture-secret-canary-765-never-a-real-credential"
    monkeypatch.setattr(probe.os, "environ", {key: canary, "EMPTY_FIXTURE_SECRET": ""})
    submission = list(infos)[attempt - 1]
    if surface == "log":
        client.fixture_logs[submission] += canary + "\n"
    else:
        info = infos[submission]
        original = info.model_dump
        info.model_dump = lambda: {**original(), "message": canary}
    with pytest.raises(ValueError, match="public surface"):
        probe._job_observation(row, client, deadline=2)


def test_clean_secret_environment_and_empty_values_keep_fixed_diagnostics_admissible(
    bound_retry_jobs,
    monkeypatch,
):
    row, client, infos, *_ = bound_retry_jobs
    secret = "synthetic-fixture-secret-canary-not-in-any-observed-surface"
    public = "synthetic-public-fixture-startup-marker"
    monkeypatch.setattr(
        probe.os,
        "environ",
        {
            "FIXTURE_PASSWORD": secret,
            "FIXTURE_SECRET": secret,
            "FIXTURE_TOKEN": secret,
            "FIXTURE_API_KEY": secret,
            "FIXTURE_ENCRYPTION_KEY": secret,
            "EMPTY_FIXTURE_PASSWORD": "",
            "EMPTY_FIXTURE_SECRET": "",
            "EMPTY_FIXTURE_TOKEN": "",
            "EMPTY_FIXTURE_API_KEY": "",
            "EMPTY_FIXTURE_ENCRYPTION_KEY": "",
            "PUBLIC_LABEL": public,
        },
    )
    for submission, info in infos.items():
        original = info.model_dump
        info.model_dump = lambda original=original: {**original(), "message": public}
        client.fixture_logs[submission] += public + "\n"
    assert probe._job_observation(row, client, deadline=2)[0] is True


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("registry", "job_registry"),
        ("carrier", "job_carrier"),
        ("exit", "job_exit"),
        ("diagnostic", "job_diagnostic_count"),
    ],
)
def test_native_guard_failures_retain_fixed_observer_codes(
    bound_retry_jobs, monkeypatch, failure, expected
):
    from django_ray.models import TaskInputPayload

    row, client, infos, _jobs, _requests, _locators, _load = bound_retry_jobs
    first = next(iter(infos))
    if failure == "registry":
        original = TaskInputPayload.objects.get

        def purged(**kwargs):
            value = original(**kwargs)
            value.state = "PURGED"
            return value

        monkeypatch.setattr(TaskInputPayload.objects, "get", purged)
    elif failure == "carrier":
        infos[first].entrypoint = "python arbitrary"
    elif failure == "exit":
        infos[first].driver_exit_code = True
    else:
        client.fixture_logs[first] = ""
    with pytest.raises(ValueError) as caught:
        probe._job_observation(row, client, deadline=2)
    assert getattr(caught.value, "code", None) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("MISSING", "binding_missing"),
        ("INVALID", "binding_invalid"),
        ("RESOURCE_LIMIT", "binding_resource_limit"),
        ("IDENTITY_MISMATCH", "binding_identity_mismatch"),
        ("PROTOCOL_MISMATCH", "binding_protocol_mismatch"),
        ("DIGEST_MISMATCH", "binding_digest_mismatch"),
        ("TRANSPORT_MISMATCH", "binding_transport_mismatch"),
    ],
)
def test_trusted_request_binding_errors_use_only_fixed_enum_codes(name, expected):
    from django_ray.ray_job_protocol import RayJobRequestBindingError, RayJobRequestBindingRejection

    error = RayJobRequestBindingError(RayJobRequestBindingRejection[name])
    assert probe.observation_failure_code(error) == expected


def test_unknown_or_poisoned_observer_errors_are_never_rendered():
    from django_ray.ray_job_protocol import RayJobRequestBindingError, RayJobRequestBindingRejection

    class Poison:
        def __str__(self):
            raise AssertionError("error or observed values were rendered")

    class PoisonError(ValueError):
        def __str__(self):
            raise AssertionError("exception was rendered")

        def __getattribute__(self, name):
            if name in {"args", "code", "classification", "__cause__", "__context__"}:
                raise AssertionError("untrusted exception data was inspected")
            return super().__getattribute__(name)

    class ForeignObservationError(probe.ObservationError):
        def __getattribute__(self, name):
            if name == "code":
                raise AssertionError("foreign guard code was inspected")
            return super().__getattribute__(name)

    poisoned = probe.ObservationError("job_registry")
    cast(Any, poisoned).code = Poison()
    binding = RayJobRequestBindingError(RayJobRequestBindingRejection.INVALID)
    cast(Any, binding).classification = Poison()
    for error in (
        PoisonError(),
        ValueError(Poison()),
        ForeignObservationError("job_registry"),
        poisoned,
        binding,
    ):
        assert probe.observation_failure_code(error) == "unclassified"


def test_first_observer_failure_code_survives_cleanup_and_outer_reentry():
    from qualification.application.run_rq2 import Diagnostics

    diagnostic = Diagnostics()
    diagnostic.enter("exception_materialization")
    diagnostic.failed(probe.ObservationError("job_registry"), SimpleNamespace(poll=lambda: None))
    diagnostic.enter("cleanup")
    diagnostic.cleanup_failed(ValueError(), SimpleNamespace(poll=lambda: 1))
    diagnostic.failed(probe.ObservationError("job_exit"))
    value = diagnostic.snapshot()
    assert value["exception_observation_code"] == "job_registry"
    assert value["failure_phase"] == "exception_materialization"
    assert value["failure_type"] == "validation"
    assert value["manager_at_failure"] == "running"
    assert value["cleanup_failure_type"] == "validation"
    assert value["manager_after_cleanup"] == "exited_nonzero"


@pytest.mark.parametrize("code", ["job_registry", "private-secret-canary", None, 1])
def test_safe_jobs_projection_whitelists_observer_codes_without_raw_fields(code):
    from qualification.application.run_rq2 import Diagnostics
    from qualification.application.safe_diagnostics import lines

    diagnostic = Diagnostics()
    diagnostic.enter("exception_materialization")
    diagnostic.failed(probe.ObservationError("job_registry"))
    original = diagnostic.snapshot()
    original["exception_observation_code"] = code
    original["raw_error"] = "private-secret-canary"
    projected = lines(
        json.dumps(
            {"layer": "application_rq2", "status": "failed", "diagnostics": original}
        ).encode()
    )
    value = projected[0]["diagnostics"]
    assert value["exception_observation_code"] == (
        "job_registry" if code == "job_registry" else "unclassified"
    )
    assert "raw_error" not in value
    assert "private-secret-canary" not in json.dumps(projected)


def test_observer_code_is_absent_for_passing_other_phase_and_historical_snapshots():
    from qualification.application.run_rq2 import Diagnostics
    from qualification.application.safe_diagnostics import lines

    diagnostic = Diagnostics()
    diagnostic.enter("exception_materialization")
    assert "exception_observation_code" not in diagnostic.snapshot()
    diagnostic.enter("adoption")
    diagnostic.failed(probe.ObservationError("job_registry"))
    assert "exception_observation_code" not in diagnostic.snapshot()
    historical = {
        "phase": "cleanup",
        "failure_phase": "exception_materialization",
        "failure_type": "validation",
        "manager_at_failure": "running",
        "cleanup_failure_type": "none",
        "manager_after_cleanup": "exited_nonzero",
    }
    projected = lines(
        json.dumps(
            {"layer": "application_rq2", "status": "failed", "diagnostics": historical}
        ).encode()
    )
    assert "exception_observation_code" not in projected[0]["diagnostics"]


@pytest.mark.parametrize("substage", sorted(probe.OBSERVATION_SUBSTAGES))
def test_actual_observer_timeouts_retain_fixed_substage(observer_seams, monkeypatch, substage):
    from django_ray.models import RayTaskExecution
    from django_ray.runner import ray_job
    from qualification.application.run_rq2 import Diagnostics

    clock, row, task, _raw, presentation, cleanup, manager, jobs = observer_seams
    diagnostic = Diagnostics()
    diagnostic.enter("exception_materialization")

    class PrivateTimeoutError(TimeoutError):
        def __str__(self):
            raise AssertionError("private timeout was rendered")

    error = PrivateTimeoutError("private-timeout-canary")
    if substage == "configuration":
        monkeypatch.setattr(probe, "_configuration", Mock(side_effect=error))
    elif substage == "client":
        monkeypatch.setattr(ray_job, "_address_pinned_job_client", Mock(side_effect=error))
    elif substage == "enqueue":
        task.using.return_value.enqueue.side_effect = error
    elif substage == "recovery":
        monkeypatch.setattr(RayTaskExecution.objects, "get", Mock(side_effect=[row, error]))
    elif substage == "job_settlement":
        jobs.side_effect = error
    elif substage == "presentation":
        presentation.side_effect = error
    else:
        cleanup.side_effect = error
    with pytest.raises(PrivateTimeoutError) as caught:
        probe.observe(
            transport="rq2_job",
            token="fixed-test-token",
            deadline=diagnostic.work_deadline,
            manager=manager,
            progress=diagnostic.exception_progress,
        )
    diagnostic.failed(caught.value, manager)
    value = diagnostic.snapshot()
    assert value["failure_type"] == "timeout"
    assert value["exception_observation_code"] == "unclassified"
    assert value["exception_observation_substage"] == substage
    assert value["exception_observation_entry_remaining_seconds"] == 170
    assert diagnostic.work_deadline == 171 and clock[0] < 171
    assert "private-timeout-canary" not in json.dumps(value, allow_nan=False)
    if substage != "cleanup":
        cleanup.assert_not_called()


@pytest.mark.parametrize("late_entry", [False, True])
def test_exception_entry_budget_distinguishes_late_entry_from_consumed_work(
    observer_seams, monkeypatch, late_entry
):
    from django_ray.models import RayTaskExecution
    from qualification.application.run_rq2 import Diagnostics

    clock, row, _task, _raw, _presentation, cleanup, manager, _jobs = observer_seams
    diagnostic = Diagnostics()
    if late_entry:
        clock[0] = 170
    diagnostic.enter("exception_materialization")
    calls = 0

    def get(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            clock[0] = 172
        return row

    monkeypatch.setattr(RayTaskExecution.objects, "get", get)
    with pytest.raises(TimeoutError) as caught:
        probe.observe(
            transport="rq2_job",
            token="fixed-test-token",
            deadline=diagnostic.work_deadline,
            manager=manager,
            progress=diagnostic.exception_progress,
        )
    diagnostic.failed(caught.value, manager)
    value = diagnostic.snapshot()
    assert type(caught.value) is TimeoutError
    assert value["exception_observation_substage"] == "recovery"
    assert value["exception_observation_entry_remaining_seconds"] == (1 if late_entry else 170)
    assert diagnostic.work_deadline == 171 and clock[0] == 172 and calls == 2
    cleanup.assert_not_called()


def test_exception_entry_context_survives_cleanup_and_outer_reentry(monkeypatch):
    from qualification.application import run_rq2

    clock = [100.0]
    monkeypatch.setattr(run_rq2.time, "monotonic", lambda: clock[0])
    diagnostic = run_rq2.Diagnostics()
    clock[0] = 250
    diagnostic.enter("exception_materialization")
    diagnostic.exception_progress("job_settlement")
    diagnostic.failed(TimeoutError("private-timeout-canary"), SimpleNamespace(poll=lambda: None))
    diagnostic.enter("cleanup")
    diagnostic.exception_progress("cleanup")
    diagnostic.cleanup_failed(ValueError("private-cleanup-canary"), SimpleNamespace(poll=lambda: 1))
    clock[0] = 269
    diagnostic.enter("exception_materialization")
    diagnostic.exception_progress("client")
    diagnostic.failed(ValueError("private-outer-canary"))
    value = diagnostic.snapshot()
    assert value["failure_type"] == "timeout"
    assert value["cleanup_failure_type"] == "validation"
    assert value["exception_observation_substage"] == "job_settlement"
    assert value["exception_observation_entry_remaining_seconds"] == 20
    assert diagnostic.work_deadline == 270
    assert "private-" not in json.dumps(value, allow_nan=False)


@pytest.mark.parametrize(
    ("substage", "remaining", "expected_substage", "expected_remaining"),
    [
        ("recovery", 0, "recovery", 0),
        ("recovery", 0.125, "recovery", 0.125),
        ("cleanup", 170, "cleanup", 170),
        ("private-context-canary", 1, "unavailable", 1),
        (1, 1, "unavailable", 1),
        (None, 1, "unavailable", 1),
        ("recovery", True, "recovery", None),
        ("recovery", -1, "recovery", None),
        ("recovery", 171, "recovery", None),
        ("recovery", float("nan"), "recovery", None),
        ("recovery", float("inf"), "recovery", None),
        ("recovery", 10**1000, "recovery", None),
        ("recovery", "private-context-canary", "recovery", None),
    ],
)
def test_exception_context_is_rewhitelisted_by_snapshot_and_safe_projection(
    substage, remaining, expected_substage, expected_remaining
):
    from qualification.application.run_rq2 import Diagnostics
    from qualification.application.safe_diagnostics import lines

    diagnostic = Diagnostics()
    diagnostic.enter("exception_materialization")
    diagnostic.failed(TimeoutError())
    cast(Any, diagnostic).exception_observation_substage = substage
    cast(Any, diagnostic).exception_observation_entry_remaining_seconds = remaining
    direct = diagnostic.snapshot()
    assert direct["exception_observation_substage"] == expected_substage
    assert direct["exception_observation_entry_remaining_seconds"] == expected_remaining
    incoming = dict(direct)
    incoming["exception_observation_substage"] = substage
    incoming["exception_observation_entry_remaining_seconds"] = remaining
    incoming["raw_error"] = "private-context-canary"
    projected = lines(
        json.dumps(
            {
                "layer": "application_rq2",
                "status": "failed",
                "diagnostics": incoming,
            }
        ).encode()
    )
    value = projected[0]["diagnostics"]
    assert value["exception_observation_substage"] == expected_substage
    assert value["exception_observation_entry_remaining_seconds"] == expected_remaining
    assert "raw_error" not in value
    assert "private-context-canary" not in json.dumps(projected, allow_nan=False)


@pytest.mark.parametrize("case", ["passing", "other_phase", "historical", "healthy"])
def test_exception_context_is_absent_outside_current_failed_observation(case):
    from qualification.application.run_rq2 import Diagnostics
    from qualification.application.safe_diagnostics import lines

    diagnostic = Diagnostics()
    diagnostic.enter("exception_materialization")
    diagnostic.exception_progress("recovery")
    if case == "other_phase":
        diagnostic.enter("adoption")
    if case != "healthy":
        diagnostic.failed(TimeoutError())
    original = diagnostic.snapshot()
    keys = {"exception_observation_substage", "exception_observation_entry_remaining_seconds"}
    if case in {"other_phase", "healthy"}:
        assert not keys.intersection(original)
    if case == "historical":
        for key in keys:
            original.pop(key)
    if case == "passing":
        original.update(
            exception_observation_substage="recovery",
            exception_observation_entry_remaining_seconds=10,
        )
    projected = lines(
        json.dumps(
            {
                "layer": "application_rq2",
                "status": "passed" if case == "passing" else "failed",
                "diagnostics": original,
            }
        ).encode()
    )
    assert not keys.intersection(projected[0]["diagnostics"])


def test_progress_rejects_unknown_labels_and_does_not_change_the_clock(monkeypatch):
    from qualification.application import run_rq2

    monkeypatch.setattr(run_rq2.time, "monotonic", lambda: 100.0)
    diagnostic = run_rq2.Diagnostics()
    diagnostic.enter("exception_materialization")
    for value in ("private-context-canary", 1, None):
        with pytest.raises(ValueError, match="Unknown exception observation substage"):
            diagnostic.exception_progress(cast(Any, value))
    assert diagnostic.work_deadline == 270
    diagnostic.failed(TimeoutError())
    assert "private-context-canary" not in json.dumps(diagnostic.snapshot())


@pytest.mark.parametrize("transport", ["ray_core", "rq2_job"])
def test_observer_uses_supplied_jobs_client_without_reconstruction(
    observer_seams, monkeypatch, transport
):
    from django_ray.runner import ray_job

    clock, _row, task, raw, presentation, cleanup, manager, jobs = observer_seams
    owned_client = object()
    factory = Mock(side_effect=AssertionError("Supplied client must not be reconstructed"))
    monkeypatch.setattr(ray_job, "_address_pinned_job_client", factory)
    result = probe.observe(
        transport=transport,
        token="fixed-test-token",
        deadline=10,
        manager=manager if transport == "rq2_job" else None,
        manager_prefix="django-manager-" if transport == "ray_core" else None,
        job_client=owned_client,
    )
    probe.validate_record(result, transport=transport)
    factory.assert_not_called()
    task.using.return_value.enqueue.assert_called_once_with()
    assert presentation.call_count == 1 and cleanup.call_count == 1 and raw.call_count == 2
    assert cleanup.call_args.kwargs["deadline"] == 10
    if transport == "rq2_job":
        assert clock[0] >= 5 and jobs.call_count >= 2
        assert manager.poll.call_count >= 2
        assert all(call.args[1] is owned_client for call in jobs.call_args_list)
        assert all(call.kwargs["deadline"] == 10 for call in jobs.call_args_list)
    else:
        jobs.assert_not_called()


def test_observer_constructs_one_pinned_jobs_client_when_not_supplied(observer_seams, monkeypatch):
    from django_ray.runner import ray_job

    clock, _row, _task, _raw, _presentation, cleanup, manager, jobs = observer_seams
    owned_client = object()
    factory = Mock(return_value=owned_client)
    monkeypatch.setattr(ray_job, "_address_pinned_job_client", factory)
    result = probe.observe(
        transport="rq2_job", token="fixed-test-token", deadline=10, manager=manager
    )
    probe.validate_record(result, transport="rq2_job")
    factory.assert_called_once_with("http://ray-head:8265")
    assert clock[0] >= 5 and jobs.call_count >= 2
    assert all(call.args[1] is owned_client for call in jobs.call_args_list)
    assert all(call.kwargs["deadline"] == 10 for call in jobs.call_args_list)
    assert cleanup.call_args.kwargs["deadline"] == 10


def test_actual_rq2_verify_hands_off_its_original_pinned_client_and_deadline(
    tmp_path, settings, monkeypatch
):
    import django
    from django.db import connection

    from django_ray import __version__
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease
    from django_ray.runner import ray_job
    from qualification.application import run_rq2

    clock = [100.0]
    monkeypatch.setattr(run_rq2.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        run_rq2.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(django, "setup", Mock())
    monkeypatch.setattr(connection, "vendor", "postgresql")
    settings.RQ2_GENERATION = "before"
    settings.RQ2_ROOT = tmp_path / "owned"
    success = SimpleNamespace(
        pk=21,
        task_id="owned-success-task",
        ray_job_id="owned-success-job",
        attempt_number=1,
        execution_generation=1,
        execution_protocol_version=1,
        ray_job_request_reference="owned-success-reference",
        claimed_by_worker="original-worker",
        state="RUNNING",
        result_data=json.dumps({"value": 42, "effect": "once"}),
        executor_django_ray_version=__version__,
    )
    adopted = SimpleNamespace(**{**vars(success), "claimed_by_worker": "replacement-worker"})
    missing = SimpleNamespace(
        pk=22,
        task_id="owned-missing-task",
        ray_job_id="owned-missing-job",
        attempt_number=1,
        execution_generation=1,
        execution_protocol_version=1,
        ray_job_request_reference="owned-missing-reference",
        state="FAILED",
        run_after=None,
        completion_data=None,
        result_data=None,
        result_reference=None,
        error_message="Strict Ray Job terminated without an exact completion envelope",
        refresh_from_db=Mock(),
    )

    def get_row(**lookup):
        row = (
            missing
            if lookup.get("pk") == missing.pk or lookup.get("task_id") == missing.task_id
            else success
        )
        if row is success and (settings.RQ2_ROOT / "success-release").exists():
            row.state = "SUCCEEDED"
        return row

    monkeypatch.setattr(RayTaskExecution.objects, "get", Mock(side_effect=get_row))
    adoption = Mock()
    adoption.exclude.return_value = adoption
    adoption.first.return_value = adopted
    monkeypatch.setattr(RayTaskExecution.objects, "filter", Mock(return_value=adoption))
    attempts = Mock()
    attempts.count.return_value = 1
    monkeypatch.setattr(TaskAttempt.objects, "filter", Mock(return_value=attempts))
    leases = {
        "original-worker": SimpleNamespace(
            worker_id="original-worker", is_active=True, pid=1, django_ray_version=__version__
        ),
        "replacement-worker": SimpleNamespace(
            worker_id="replacement-worker", is_active=True, pid=2, django_ray_version=__version__
        ),
    }
    monkeypatch.setattr(
        TaskWorkerLease.objects, "get", Mock(side_effect=lambda worker_id: leases[worker_id])
    )
    task = Mock()
    task.using.return_value.enqueue.side_effect = [
        SimpleNamespace(id=success.task_id),
        SimpleNamespace(id=missing.task_id),
    ]
    monkeypatch.setitem(
        sys.modules, "testproject.qualification_rq2_tasks", SimpleNamespace(controlled=task)
    )
    monkeypatch.setitem(
        sys.modules, "testproject.qualification_process", SimpleNamespace(read_receipt=Mock())
    )
    managers = [
        SimpleNamespace(pid=pid, returncode=None, poll=Mock(return_value=None)) for pid in (1, 2, 3)
    ]
    monkeypatch.setattr(run_rq2, "_start_manager", Mock(side_effect=managers))

    def stop_manager(manager, *, crash=False):
        manager.returncode = -9 if crash else 0
        manager.poll.return_value = manager.returncode

    stop = Mock(side_effect=stop_manager)
    monkeypatch.setattr(run_rq2, "stop_manager", stop)
    monkeypatch.setattr(run_rq2, "one_effect", lambda path: path.name == "success-effects")
    monkeypatch.setattr(run_rq2, "inspect_job", Mock(side_effect=lambda row, *_a, **_k: row.state))
    monkeypatch.setattr(run_rq2, "missing_submission", Mock())
    alarm = Mock()
    monkeypatch.setattr(run_rq2.signal, "alarm", alarm, raising=False)
    owned_client = object()
    factory = Mock(return_value=owned_client)
    monkeypatch.setattr(ray_job, "_address_pinned_job_client", factory)
    evidence = record("rq2_job")
    observe = Mock(return_value=evidence)
    monkeypatch.setattr(probe, "observe", observe)
    diagnostics = run_rq2.Diagnostics()
    facts = dict.fromkeys(run_rq2.FACTS, False)

    result = run_rq2.verify("before", facts, diagnostics, token="fixed-test-token")

    assert result is evidence and all(facts.values())
    factory.assert_called_once_with(run_rq2.ADDRESS)
    observe.assert_called_once_with(
        transport="rq2_job",
        token="fixed-test-token",
        deadline=270,
        manager=managers[2],
        job_client=owned_client,
        progress=diagnostics.exception_progress,
        operation=diagnostics.exception_operation,
    )
    assert observe.call_args.kwargs["job_client"] is owned_client
    assert diagnostics.work_deadline == 270 and clock[0] >= 104
    assert stop.call_count == 3
    alarm.assert_called_once_with(5)


@pytest.mark.parametrize(
    "operation", ["jobs_inventory", "job_info", "job_logs", "registry_lookup", "request_load"]
)
def test_settlement_io_operation_preserves_error_and_control_timeout(
    bound_retry_jobs, monkeypatch, operation
):
    from django_ray.models import TaskInputPayload
    from django_ray.runner import ray_job
    from qualification.application.run_rq2 import Diagnostics

    row, client, _infos, _jobs, _requests, _locators, load = bound_retry_jobs
    diagnostic = Diagnostics()
    diagnostic.enter("exception_materialization")
    diagnostic.exception_progress("job_settlement")
    monkeypatch.setattr(ray_job, "_bounded_control_requests", client.fixture_control_requests)
    setattr(client, ray_job._REQUEST_TIMEOUT_ATTRIBUTE, 1.25)
    error = OSError("operation-private-canary")
    events = []

    def mark(value):
        events.append(value)
        diagnostic.exception_operation(value)

    def fail(*_args, **_kwargs):
        if operation in {"jobs_inventory", "job_info", "job_logs"}:
            assert getattr(client, ray_job._REQUEST_TIMEOUT_ATTRIBUTE) == 5.0
        raise error

    if operation == "jobs_inventory":
        client.list_jobs.side_effect = fail
    elif operation == "job_info":
        client.get_job_info.side_effect = fail
    elif operation == "job_logs":
        client.get_job_logs.side_effect = fail
    elif operation == "registry_lookup":
        monkeypatch.setattr(TaskInputPayload.objects, "get", fail)
    else:
        load.side_effect = fail
    with pytest.raises(OSError) as caught:
        probe._job_observation(row, client, deadline=100, operation=mark)
    assert caught.value is error and events[-1] == operation
    assert getattr(client, ray_job._REQUEST_TIMEOUT_ATTRIBUTE) == 1.25
    diagnostic.failed(caught.value)
    value = diagnostic.snapshot()
    assert value["failure_type"] == "os_error"
    assert value["exception_observation_operation"] == operation
    assert "operation-private-canary" not in json.dumps(value)


@pytest.mark.parametrize("late_failure", [False, True])
def test_successful_job_io_clears_operation_before_later_validation(bound_retry_jobs, late_failure):
    from qualification.application.run_rq2 import Diagnostics

    row, client, infos, _jobs, _requests, _locators, _load = bound_retry_jobs
    diagnostic = Diagnostics()
    diagnostic.enter("exception_materialization")
    diagnostic.exception_progress("job_settlement")
    if late_failure:
        next(iter(infos.values())).driver_exit_code = 1
        with pytest.raises(probe.ObservationError) as caught:
            probe._job_observation(
                row, client, deadline=100, operation=diagnostic.exception_operation
            )
        diagnostic.failed(caught.value)
    else:
        assert probe._job_observation(
            row, client, deadline=100, operation=diagnostic.exception_operation
        )[0]
        diagnostic.failed(OSError("later-private-canary"))
    assert "exception_observation_operation" not in diagnostic.snapshot()


@pytest.mark.parametrize("operation", ["manager_liveness", "terminal_snapshot"])
def test_actual_settlement_outer_boundary_is_retained_on_failure(
    observer_seams, monkeypatch, operation
):
    from qualification.application.run_rq2 import Diagnostics

    _clock, row, _task, _raw, presentation, cleanup, manager, _jobs = observer_seams
    diagnostic = Diagnostics()
    diagnostic.enter("exception_materialization")
    error = OSError("outer-operation-private-canary")

    def external(*_args, **_kwargs):
        if diagnostic._exception_operation == operation:
            raise error
        return (
            None
            if operation == "manager_liveness"
            else (row, [SimpleNamespace(), SimpleNamespace()])
        )

    if operation == "manager_liveness":
        manager.poll.side_effect = external
    else:
        monkeypatch.setattr(probe, "_terminal_snapshot", external)
    with pytest.raises(OSError) as caught:
        probe.observe(
            transport="rq2_job",
            token="fixed-test-token",
            deadline=diagnostic.work_deadline,
            manager=manager,
            progress=diagnostic.exception_progress,
            operation=diagnostic.exception_operation,
        )
    assert caught.value is error
    diagnostic.failed(caught.value, manager)
    assert diagnostic.snapshot()["exception_observation_operation"] == operation
    presentation.assert_not_called()
    cleanup.assert_not_called()


def test_core_observation_does_not_call_settlement_operation(observer_seams):
    _clock, _row, _task, _raw, _presentation, _cleanup, _manager, _jobs = observer_seams
    operation = Mock(side_effect=AssertionError("Core must not enter Jobs settlement I/O"))
    probe.observe(
        transport="ray_core",
        token="fixed-test-token",
        deadline=100,
        manager_prefix="owned-core",
        operation=operation,
    )
    operation.assert_not_called()
