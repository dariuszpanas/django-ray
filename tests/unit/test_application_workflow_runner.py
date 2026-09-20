"""The workflow runner must fail closed without leaking fixture diagnostics."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from qualification.application import run_workflows as runner
from qualification.application.api import (
    EXPECTED_TASK_STATUS_INPUT_MAX_BYTES,
    EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES,
    TASK_STATUS_BY_STATE,
)


@pytest.fixture
def fixture_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr("django.setup", lambda: None)
    monkeypatch.setattr(
        "django_ray.conf.settings.get_settings",
        lambda: {"WORKFLOW_PROGRESS_SCHEMA_V3_PILOT": True},
    )
    monkeypatch.setattr(runner, "ApplicationHttp", Mock())
    monkeypatch.setattr(runner, "read_token", lambda _path: "fixture-token")
    execute = Mock(return_value=[{"state": "observed"}])
    monkeypatch.setattr(runner, "execute_case", execute)
    receipt = tmp_path / "receipt.json"
    args = ["--token-file", str(tmp_path / "token"), "--receipt", str(receipt)]
    return args, receipt, execute


def test_success_records_all_serial_cases_without_claiming_complete_gate(fixture_runner, capsys):
    args, receipt, execute = fixture_runner
    assert runner.main(args) == 0
    value = json.loads(receipt.read_text())
    assert value == json.loads(capsys.readouterr().out)
    assert value["status"] == "passed"
    assert value["complete_workflow_gate"] is False
    assert value["publisher"] == "pilot"
    assert [call.kwargs["case"] for call in execute.call_args_list] == list(runner.workflow_cases())


@pytest.mark.parametrize("failure_index", range(len(runner.workflow_cases())))
@pytest.mark.parametrize("module_name", ["qualification.application.run_workflows", "__main__"])
def test_failed_case_stops_submissions_and_omits_raw_error(
    fixture_runner, capsys, failure_index, module_name, monkeypatch
):
    monkeypatch.setattr(runner, "__name__", module_name)
    args, receipt, execute = fixture_runner
    execute.side_effect = [[]] * failure_index + [ValueError("secret diagnostic")]
    assert runner.main(args) == 1
    assert execute.call_count == failure_index + 1
    assert not receipt.exists()
    output = capsys.readouterr().out
    assert "secret diagnostic" not in output
    value = json.loads(output)
    assert value["failed_stage"] == runner.workflow_cases()[failure_index].name
    assert "observations" not in value
    location = value["failed_location"]
    assert set(location) == {"module", "function", "line"}
    assert location["module"] == "qualification.application.run_workflows"
    assert location["function"] == "main"
    assert type(location["line"]) is int


def test_receipt_cannot_overwrite_existing_evidence(fixture_runner, capsys):
    args, receipt, _ = fixture_runner
    receipt.write_text("existing")
    assert runner.main(args) == 1
    assert receipt.read_text() == "existing"
    assert json.loads(capsys.readouterr().out)["failed_stage"] == "receipt"


def test_wrong_settings_cannot_submit(fixture_runner, monkeypatch, capsys):
    args, receipt, execute = fixture_runner
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
    assert runner.main(args) == 1
    execute.assert_not_called()
    assert not receipt.exists()
    assert json.loads(capsys.readouterr().out)["failed_stage"] == "configuration"


def test_default_publisher_is_not_misrepresented_as_pilot(fixture_runner, monkeypatch):
    args, receipt, execute = fixture_runner
    monkeypatch.setattr(
        "django_ray.conf.settings.get_settings",
        lambda: {"WORKFLOW_PROGRESS_SCHEMA_V3_PILOT": False},
    )
    assert runner.main(args) == 1
    execute.assert_not_called()
    assert not receipt.exists()


@pytest.mark.parametrize("case", runner.workflow_cases(), ids=lambda case: case.name)
def test_execute_observes_each_durable_attempt_after_one_submission(monkeypatch, case):
    task_id = "f717c512-17d7-4b5e-b778-d614fb14427c"
    identities = [
        {"schema_version": 1, "run_id": task_id, "attempt_number": n, "execution_generation": n}
        for n in range(1, len(case.states) + 1)
    ]
    history = [
        {
            "attempt_number": identity["attempt_number"],
            "state": state,
            "workflow_progress_summary_json": json.dumps(
                {"run_identity": {**identity, "task_execution_pk": 12}}
            ),
        }
        for identity, state in zip(identities, case.states, strict=True)
    ]
    if case.policy == "disabled":
        for item in history:
            item["workflow_progress_summary_json"] = None
    attempts = MagicMock()
    attempts.annotate.return_value.filter.return_value.order_by.return_value.values.return_value.__getitem__.return_value = history
    row = SimpleNamespace(
        pk=12,
        state=case.states[-1],
        attempt_number=len(case.states),
        callable_path=case.callable_path,
        attempts=attempts,
    )
    manager = Mock()
    manager.only.return_value.get.return_value = row
    monkeypatch.setattr("django_ray.models.RayTaskExecution.objects", manager)
    overflow = Mock()
    monkeypatch.setattr(runner, "verify_plan_overflow_storage", overflow)
    storage = Mock()
    monkeypatch.setattr(runner, "verify_no_workflow_detail", storage)
    disabled_storage = Mock()
    monkeypatch.setattr(runner, "verify_no_disabled_publication", disabled_storage)
    submitted = {"task_id": task_id, "args": [], "kwargs": dict(case.options)}
    enqueue = Mock(return_value=SimpleNamespace(id=task_id, args=[], kwargs=dict(case.options)))
    monkeypatch.setattr("testproject.admission.enqueue_sample", enqueue)
    monkeypatch.setattr(
        runner, "qualification_admin_session", lambda: nullcontext("fixture-cookie")
    )
    graph = Mock(side_effect=lambda *_args, **_kwargs: {"observed": True})
    monkeypatch.setattr(runner, "read_full_workflow_graph", graph)
    monkeypatch.setattr(runner, "read_disabled_workflow_graph", graph)
    admin = Mock(return_value={"admin_workflow": "verified"})
    monkeypatch.setattr(runner, "observe_admin_contract", admin)
    polling = {
        "task_id": task_id,
        "state": case.states[-1],
        "status": TASK_STATUS_BY_STATE[case.states[-1]],
        "attempt_number": len(case.states),
        "execution_generation": len(case.states),
        "input_max_bytes": EXPECTED_TASK_STATUS_INPUT_MAX_BYTES,
        "response_max_bytes": EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES,
        "input_omission_reason": None,
        "args": [],
        "kwargs": dict(case.options),
    }
    responses = [(200, json.dumps(polling).encode()), (302, b""), (401, b"")]
    if case.name == "recovery":
        responses.insert(0, (200, json.dumps(submitted).encode()))
    request = Mock(side_effect=responses)
    expected = {"observed": True}
    if case.policy != "disabled":
        expected["admin_contract"] = {"admin_workflow": "verified"}
    assert runner.execute_case(request, token="fixture-token", case=case) == [expected] * len(
        case.states
    )
    if case.policy == "disabled":
        disabled_storage.assert_called_once_with(12)
        storage.assert_called_once_with(12)
        admin.assert_not_called()
        graph.assert_called_once()
        assert graph.call_args.kwargs["expected_state"] == case.states[0]
        return
    disabled_storage.assert_not_called()
    assert enqueue.call_count == (0 if case.name == "recovery" else 1)
    assert sum(call.kwargs["method"] == "POST" for call in request.call_args_list) == (
        1 if case.name == "recovery" else 0
    )
    assert [call.kwargs["run_identity"] for call in graph.call_args_list] == identities
    assert [call.kwargs["expected_state"] for call in graph.call_args_list] == list(case.states)
    assert [call.kwargs["fixture"] for call in graph.call_args_list] == [
        case.name if case.name in {"recovery", "plan-overflow"} else "complex"
    ] * len(case.states)
    assert [call.kwargs["attempt"] for call in admin.call_args_list] == [
        *range(1, len(case.states)),
        None,
    ]
    assert storage.call_count == (1 if case.policy == "terminal_only" else 0)
    assert overflow.call_count == (1 if case.name == "plan-overflow" else 0)


@pytest.mark.parametrize("retained", [None, 0, 1, 2])
def test_terminal_only_checks_all_detail_tables(monkeypatch, retained):
    models = (
        "WorkflowProgressNodeDetail",
        "WorkflowProgressTopologyManifest",
        "WorkflowProgressTopologyPage",
    )
    managers = []
    for index, model in enumerate(models):
        manager = Mock()
        manager.filter.return_value.exists.return_value = retained == index
        monkeypatch.setattr(f"django_ray.models.{model}.objects", manager)
        managers.append(manager)
    if retained is None:
        runner.verify_no_workflow_detail(12)
        for manager in managers:
            manager.filter.assert_called_once_with(run_storage__execution_id=12)
    else:
        with pytest.raises(ValueError, match="retained graph storage"):
            runner.verify_no_workflow_detail(12)


@pytest.mark.django_db
@pytest.mark.parametrize("retained", [None, "current", "attempt", "staged"])
def test_disabled_publication_checks_current_history_and_staging(retained):
    from django_ray.models import RayTaskExecution, TaskAttempt, WorkflowProgressRunStorage

    task = RayTaskExecution.objects.create(task_id="disabled-storage-check", callable_path="unused")
    if retained == "current":
        task.progress_data = '{"unexpected":true}'
        task.save(update_fields=["progress_data"])
    elif retained == "attempt":
        TaskAttempt.objects.create(
            execution=task,
            attempt_number=1,
            state="FAILED",
            workflow_progress_summary_json='{"unexpected":true}',
        )
    elif retained == "staged":
        WorkflowProgressRunStorage.objects.create(
            execution=task,
            attempt_number=1,
            execution_generation=1,
            run_id="00000000-0000-0000-0000-000000000563",
        )
    if retained:
        with pytest.raises(ValueError, match="retained publication storage"):
            runner.verify_no_disabled_publication(task.pk)
    else:
        runner.verify_no_disabled_publication(task.pk)
