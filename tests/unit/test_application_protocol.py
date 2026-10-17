"""Finite orchestration, cleanup, redaction and receipt contract checks."""

import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from qualification.application import api
from qualification.application import run_protocol as protocol


def passed_receipt():
    return {
        "schema_version": 1,
        "layer": protocol.LAYER,
        "status": "passed",
        "complete_application_gate": False,
        "generation": "before",
        "budget_seconds": 120,
        "assertions": dict.fromkeys(protocol.FACTS, True),
        "failure_phase": None,
        "failure_type": None,
        "cleanup_failed": False,
    }


@pytest.mark.parametrize("fact", protocol.FACTS)
def test_each_protocol_fact_is_mandatory(fact):
    value = passed_receipt()
    protocol.validate_receipt(value)
    value["assertions"][fact] = False
    with pytest.raises(ValueError):
        protocol.validate_receipt(value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema_version", True),
        ("layer", "application_core"),
        ("status", "failed"),
        ("generation", "unknown"),
        ("budget_seconds", 121),
        ("complete_application_gate", True),
        ("failure_phase", "metrics"),
        ("failure_type", "timeout"),
        ("cleanup_failed", True),
        ("assertions", {**dict.fromkeys(protocol.FACTS, True), "foreign": True}),
        ("assertions", dict.fromkeys(protocol.FACTS, 1)),
    ],
)
def test_protocol_receipt_refuses_partial_or_wrong_contract(key, value):
    receipt = passed_receipt()
    receipt[key] = value
    with pytest.raises(ValueError):
        protocol.validate_receipt(receipt)


@pytest.mark.parametrize("field", sorted(protocol.RECEIPT_FIELDS))
def test_protocol_receipt_requires_even_nullable_fields(field):
    receipt = passed_receipt()
    receipt.pop(field)
    with pytest.raises(ValueError):
        protocol.validate_receipt(receipt)


def test_protocol_receipt_refuses_unreviewed_exported_fields():
    receipt = {**passed_receipt(), "exception": "must not be exported"}
    with pytest.raises(ValueError):
        protocol.validate_receipt(receipt)


def metrics(count):
    name = api.EXPECTED_EXECUTION_PROTOCOL_METRIC
    return (
        "\n".join(
            [
                f"# HELP {name} Total tasks by bounded execution-protocol bucket and state",
                f"# TYPE {name} gauge",
                *[
                    f'{name}{{protocol="{version}",state="{state}"}} '
                    f"{count if (version, state) == ('other', 'QUEUED') else 0}"
                    for version in api.EXPECTED_EXECUTION_PROTOCOL_METRIC_PROTOCOLS
                    for state in api.EXPECTED_EXECUTION_PROTOCOL_METRIC_STATES
                ],
            ]
        )
        + "\n"
    ).encode()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    clock = [0.0]
    events = []
    faults = {}
    task_id = str(uuid4())
    owned = SimpleNamespace(task_id=task_id, revision=None, pk=123)

    def event(name):
        events.append(name)
        if name in faults:
            fault = faults[name]
            if callable(fault):
                fault()
            else:
                raise fault

    def seed(checkpoint):
        checkpoint()
        owned.revision = 1
        event("seed")

    def observe():
        event("observe")
        return True

    def cleanup(checkpoint):
        checkpoint()
        event("database_cleanup")

    owned.seed = seed
    owned.observe = observe
    owned.status = lambda: event("status")
    owned.cleanup = cleanup
    monkeypatch.setattr(protocol, "Fixture", lambda generation: owned)

    def prepare(root, control, end):
        event("prepare")
        (root / "observed").write_bytes(control)

    remote = SimpleNamespace(
        prepare=prepare,
        reject=lambda fixture, end: event("reject"),
        cleanup=lambda end: event("remote_cleanup"),
    )
    monkeypatch.setattr(protocol, "Remote", lambda: remote)
    monkeypatch.setattr(
        protocol,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    monkeypatch.setattr(protocol, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr(protocol, "MOUNT_ROOT", tmp_path)
    signals = SimpleNamespace(
        SIGTERM=15, SIGALRM=14, ITIMER_REAL=0, signal=Mock(), setitimer=Mock()
    )
    monkeypatch.setattr(protocol, "signal", signals)
    monkeypatch.setattr("django.setup", lambda: event("bootstrap"))
    connection = SimpleNamespace(
        vendor="postgresql",
        in_atomic_block=False,
        cursor=lambda: nullcontext(SimpleNamespace(execute=Mock())),
        close=lambda: event("close"),
    )
    monkeypatch.setattr("django.db.connection", connection)
    token_file = tmp_path / "token"
    token_file.write_text("not-exported-token" * 4)
    payload = {
        "task_id": task_id,
        "state": "QUEUED",
        "status": api.TASK_STATUS_BY_STATE["QUEUED"],
        "attempt_number": 1,
        "execution_generation": 0,
        "input_max_bytes": api.EXPECTED_TASK_STATUS_INPUT_MAX_BYTES,
        "response_max_bytes": api.EXPECTED_TASK_STATUS_RESPONSE_MAX_BYTES,
        "input_omission_reason": None,
        "args": [],
        "kwargs": {},
        "execution_protocol_version": 2,
        "created_with_django_ray_version": "0.6.0",
        "managed_with_django_ray_version": None,
        "executor_django_ray_version": None,
        "protocol_compatible_worker_available": False,
        "queue_capacity_attested": False,
    }
    metric_calls = []

    def client(base_url, **kwargs):
        assert 0 < kwargs["request_timeout"] <= 5
        assert kwargs["observer_deadline"] == 90

        def request(path, **options):
            assert options["method"] == "GET" and options["response_limit"] == 65536
            assert options["headers"] == {"Authorization": "Bearer " + "not-exported-token" * 4}
            if path == "/api/metrics":
                event("metrics")
                count = len(metric_calls)
                metric_calls.append(path)
                return 200, metrics(count)
            if path.startswith("/api/tasks/"):
                event("task_status")
                return 200, json.dumps(payload).encode()
            event("execution_list")
            return 200, json.dumps({"tasks": [payload]}).encode()

        return request

    monkeypatch.setattr(protocol, "ApplicationHttp", client)
    return SimpleNamespace(
        clock=clock,
        events=events,
        faults=faults,
        owned=owned,
        remote=remote,
        token=token_file,
        root=tmp_path,
        signals=signals,
        payload=payload,
    )


def invoke(harness):
    return protocol.run("before", "http://django-web:8000", harness.token)


def test_complete_ordered_phase_emits_only_finite_facts_after_cleanup(harness):
    receipt = invoke(harness)
    protocol.validate_receipt(receipt)
    assert harness.events == [
        "bootstrap",
        "metrics",
        "seed",
        "prepare",
        "observe",
        "reject",
        "task_status",
        "execution_list",
        "metrics",
        "status",
        "observe",
        "remote_cleanup",
        "database_cleanup",
        "close",
    ]
    assert not list(harness.root.glob("protocol-*"))
    serialized = json.dumps(receipt)
    assert harness.owned.task_id not in serialized and "not-exported-token" not in serialized
    assert len(serialized) < 2048
    assert harness.signals.setitimer.call_args.args == (0, 0)


@pytest.mark.parametrize("failed_phase", ["seed", "prepare", "reject", "task_status", "status"])
def test_work_failure_preserves_first_fixed_category_and_runs_independent_cleanup(
    harness, failed_phase
):
    harness.faults[failed_phase] = RuntimeError("secret token and unbounded error" * 1000)
    receipt = invoke(harness)
    assert receipt["status"] == "failed" and receipt["failure_type"] == "unavailable"
    assert receipt["cleanup_failed"] is False
    assert harness.events[-3:] == ["remote_cleanup", "database_cleanup", "close"]
    assert receipt["assertions"]["admission_restored"]
    assert receipt["assertions"]["owned_fixture_removed"]
    assert not list(harness.root.glob("protocol-*"))
    assert "secret" not in json.dumps(receipt)


def test_uncertain_remote_cleanup_still_restores_database_but_retains_sentinel(harness):
    harness.faults["remote_cleanup"] = ValueError("unknown remote state")
    receipt = invoke(harness)
    assert receipt["status"] == "failed" and receipt["cleanup_failed"]
    assert receipt["failure_phase"] == "remote_cleanup"
    assert receipt["assertions"]["admission_restored"]
    assert not receipt["assertions"]["owned_remote_cleanup"]
    assert not receipt["assertions"]["owned_fixture_removed"]
    assert "database_cleanup" in harness.events
    assert harness.events[-1] == "close"
    assert len(list(harness.root.glob("protocol-*"))) == 1


def test_foreign_sentinel_refuses_removal_but_still_closes_connection(harness):
    def introduce_foreign_file():
        [directory] = harness.root.glob("protocol-*")
        (directory / "foreign").write_text("foreign")

    harness.faults["database_cleanup"] = introduce_foreign_file
    receipt = invoke(harness)
    assert receipt["status"] == "failed" and receipt["cleanup_failed"]
    assert receipt["failure_phase"] == "files_cleanup"
    assert harness.events[-1] == "close"
    assert not receipt["assertions"]["owned_fixture_removed"]
    assert len(list(harness.root.glob("protocol-*/foreign"))) == 1


def test_early_failure_cleanup_has_own_25_second_cap(harness):
    harness.faults["seed"] = ValueError("early failure")
    harness.faults["remote_cleanup"] = lambda: harness.clock.__setitem__(0, 26)
    receipt = invoke(harness)
    assert receipt["failure_phase"] == "seed"
    assert receipt["cleanup_failed"] and receipt["status"] == "failed"
    assert "database_cleanup" not in harness.events and "close" not in harness.events
    assert not receipt["assertions"]["owned_fixture_removed"]


def test_database_cleanup_failure_does_not_suppress_confirmed_remote_file_cleanup(harness):
    harness.faults["database_cleanup"] = ValueError("foreign revision")
    receipt = invoke(harness)
    assert receipt["status"] == "failed" and receipt["cleanup_failed"]
    assert receipt["failure_phase"] == "database_cleanup"
    assert not receipt["assertions"]["admission_restored"]
    assert not receipt["assertions"]["owned_fixture_removed"]
    assert not list(harness.root.glob("protocol-*"))
    assert harness.events[-1] == "close"


def test_work_deadline_prevents_mutation_and_keeps_cleanup_reserve(harness):
    harness.faults["metrics"] = lambda: harness.clock.__setitem__(0, 90)
    receipt = invoke(harness)
    assert receipt["failure_phase"] == "baseline" and receipt["failure_type"] == "timeout"
    assert "seed" not in harness.events
    assert harness.events[-3:] == ["remote_cleanup", "database_cleanup", "close"]
    assert not list(harness.root.glob("protocol-*"))


def test_late_cleanup_cannot_emit_success_or_start_later_cleanup_mutations(harness):
    harness.faults["remote_cleanup"] = lambda: harness.clock.__setitem__(0, 115)
    receipt = invoke(harness)
    assert receipt["status"] == "failed" and receipt["cleanup_failed"]
    assert receipt["failure_type"] == "timeout"
    assert "database_cleanup" not in harness.events
    assert not receipt["assertions"]["owned_remote_cleanup"]
    assert not receipt["assertions"]["owned_fixture_removed"]
    assert len(list(harness.root.glob("protocol-*"))) == 1


@pytest.mark.parametrize("name,contents", [("foreign", b"data"), ("control", b"wrong")])
def test_file_cleanup_refuses_foreign_content_without_partial_removal(tmp_path, name, contents):
    (tmp_path / "observed").write_bytes(b"owned")
    (tmp_path / name).write_bytes(contents)
    with pytest.raises(ValueError):
        protocol.remove_files(tmp_path, b"owned")
    assert (tmp_path / "observed").read_bytes() == b"owned"


def test_main_only_writes_valid_pass_to_owned_exclusive_receipt_destination(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(protocol, "RECEIPT_ROOT", tmp_path)
    monkeypatch.setattr(protocol, "run", lambda *args: copy.deepcopy(passed_receipt()))
    destination = tmp_path / "before-protocol.json"
    argv = ["--generation", "before", "--token-file", "owned-token", "--receipt", str(destination)]
    assert protocol.main(argv) == 0
    protocol.validate_receipt(json.loads(destination.read_text()))
    assert protocol.main(argv) == 1
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[-1])["failure_phase"] == "receipt"
    protocol.validate_receipt(json.loads(destination.read_text()))
