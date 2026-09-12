"""Owned fake helpers; these tests never initialize Ray or invoke a network SDK."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from django_ray.runner import cohort_jobs as module
from django_ray.runner.cohort_configuration import prepare_cohort_worker_configuration
from django_ray.runner.cohort_process import (
    CohortProcessCompletion,
    CohortProcessOperation,
    CohortProcessReason,
)
from django_ray.runner.cohort_qualification import CohortQualificationLifecycle
from django_ray.runtime.cohort_job import CohortProbeJobLease
from django_ray.target.attestation import RayRunnerFamily
from django_ray.target.cohort_contract import _timestamp
from django_ray.target.cohort_job_control import cohort_probe_submitted_runtime_env_digest
from tests.unit.test_cohort_qualification import NOW, RUNTIME


class Clock:
    def __init__(self):
        self.now = NOW

    def wall(self):
        return self.now

    def monotonic(self):
        return 100 + (self.now - NOW).total_seconds()


class FakeSupervisor:
    def __init__(self, clock):
        self.clock = clock
        self.outstanding = None
        self.calls = []
        self.result = None
        self.cancelled = []
        self.handler = self.default
        self.hang = False

    def default(self, payload):
        if payload["command"] == "prepare":
            source = payload["arguments"]
            environment = json.loads(source["control_runtime_env_json"])
            return {
                "jobs_endpoint": source["ray_address"]
                if source["ray_address"].startswith(("http://", "https://"))
                else "http://ray-head:8265",
                "submitted_runtime_env_json": source["control_runtime_env_json"],
                "source_control_profile_digest": source["source_control_profile_digest"],
                "submitted_runtime_env_digest": cohort_probe_submitted_runtime_env_digest(
                    environment
                ),
            }
        if payload["command"] == "stop":
            return {"terminal": True}
        if payload["command"] == "inspect":
            return {"pending": False, "inspected_at": _timestamp(self.clock.now)}
        raise AssertionError("Unexpected command")

    def start(self, payload, *, timeout_seconds):
        assert self.outstanding is None
        assert timeout_seconds > 0
        assert "nonce" not in json.dumps(payload)
        self.calls.append(payload)
        self.outstanding = CohortProcessOperation(
            f"{len(self.calls):032x}", self.clock.monotonic() + timeout_seconds
        )
        return self.outstanding

    def cancel(self, ticket):
        assert ticket is self.outstanding
        self.cancelled.append(ticket)

    def poll(self, ticket):
        assert ticket is self.outstanding
        if self.hang:
            return None
        try:
            value = self.handler(self.calls[-1])
        except Exception:
            return CohortProcessCompletion(ticket.operation_id, CohortProcessReason.HELPER_FAILED)
        finally:
            self.outstanding = None
        return self.result or CohortProcessCompletion(ticket.operation_id, None, value)


def configuration(*, address="http://ray-head:8265", count=2):
    names = [f"alias{index}" for index in range(count)]
    return prepare_cohort_worker_configuration(
        tasks={name: {"OPTIONS": {"RAY_ADDRESS": address}} for name in names},
        validated_aliases=names,
        selected_queues=["default"],
        manager_settings={},
        django_settings_module="tests.settings",
        runner_family=RayRunnerFamily.RAY_JOB,
        execution_mode="ray",
    )


def controller(config=None):
    clock = Clock()
    lifecycle = CohortQualificationLifecycle(
        CohortProbeJobLease("worker", "host", 1, NOW - timedelta(seconds=10)),
        "0.5.0",
        RUNTIME,
        RayRunnerFamily.RAY_JOB,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    helper = FakeSupervisor(clock)
    manager = module.JobsCohortManagerAdapter(
        lifecycle,
        config or configuration(),
        supervisor=helper,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    return SimpleNamespace(manager=manager, helper=helper, lifecycle=lifecycle, clock=clock)


@pytest.fixture
def case(monkeypatch):
    value = controller()
    monkeypatch.setattr(module.connections, "all", lambda: ())
    monkeypatch.setattr(value.manager, "_lease", lambda now: SimpleNamespace(last_heartbeat_at=now))
    monkeypatch.setattr(value.manager, "_withdraw_unused_capabilities", lambda now: None)
    return value


def test_configuration_is_immutable_and_records_are_finite():
    case = controller(configuration(count=64))
    assert len(case.manager._records) == 64
    with pytest.raises(AttributeError):
        case.manager.configuration = configuration(count=1)
    with pytest.raises(module.JobsCohortAdapterError):
        module.JobsCohortManagerAdapter(
            case.lifecycle, replace(case.manager.configuration, job_addresses=())
        )


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), 601])
def test_bad_deadline_refuses_before_helper(case, timeout):
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.begin("alias0", timeout_seconds=timeout)
    assert case.helper.calls == []


def test_first_prepare_uses_only_configured_control_profile_and_exact_address(case):
    ticket = case.manager.begin("alias0")
    payload = case.helper.calls[0]
    assert payload["command"] == "prepare"
    assert payload["arguments"]["ray_address"] == "http://ray-head:8265"
    assert (
        payload["arguments"]["source_control_profile_digest"]
        == case.manager.configuration.control_profile_digest
    )
    assert case.manager.outstanding is ticket
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.begin("alias1")
    assert len(case.helper.calls) == 1


def test_crossed_ticket_is_refused_without_signalling(case):
    ticket = case.manager.begin("alias0")
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.poll(replace(ticket))
    assert not case.helper.cancelled


def test_invalidation_retains_helper_and_alias_records(case):
    ticket = case.manager.begin("alias0")
    case.manager.invalidate()
    assert case.manager.outstanding is ticket
    assert case.manager.qualified_aliases() == ()
    assert len(case.manager._records) == 2
    assert case.helper.cancelled
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.begin_cleanup(ticket)


def test_expired_operation_never_accepts_late_preparation(case, monkeypatch):
    reserved = []
    monkeypatch.setattr(case.manager, "_reserve_and_submit", lambda *a, **k: reserved.append(True))
    ticket = case.manager.begin("alias0", timeout_seconds=1)
    case.clock.now += timedelta(seconds=1)
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.poll(ticket)
    assert reserved == [] and case.manager.outstanding is ticket
    case.manager.begin_cleanup(ticket)
    assert case.manager.outstanding is None


@pytest.mark.parametrize(
    "changed", ["source_control_profile_digest", "submitted_runtime_env_digest", "extra"]
)
def test_crossed_preparation_cannot_reserve_or_submit(case, monkeypatch, changed):
    original = case.helper.default

    def crossed(payload):
        result = original(payload)
        result[changed] = "sha256:" + "f" * 64
        return result

    case.helper.handler = crossed
    reserved = []
    monkeypatch.setattr(case.manager, "_reserve_and_submit", lambda *a, **k: reserved.append(True))
    ticket = case.manager.begin("alias0")
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.poll(ticket)
    assert reserved == [] and len(case.helper.calls) == 1


@pytest.fixture
def client_case(monkeypatch):
    from django_ray.runner import cohort_client_discovery as discovery
    from tests.unit.test_cohort_client_discovery import observation

    value = controller(configuration(address="ray://ray-head:10001"))
    monkeypatch.setattr(module.connections, "all", lambda: ())
    monkeypatch.setattr(value.manager, "_lease", lambda now: SimpleNamespace(last_heartbeat_at=now))
    monkeypatch.setattr(value.manager, "_withdraw_unused_capabilities", lambda now: None)
    monkeypatch.setattr(discovery, "_runtime", lambda *args: None)
    value.driver_terminal = True

    def execute(payload):
        command, arguments = payload["command"], payload["arguments"]
        if command == "discover-client":
            return observation(
                arguments,
                observed_at=_timestamp(value.clock.now),
                jobs_endpoint="https://ray-head:8265",
            )
        if command == "inspect-driver":
            return {
                "schema_version": 1,
                **arguments,
                "terminal": value.driver_terminal,
                "inspected_at": _timestamp(value.clock.now),
            }
        return value.helper.default(payload)

    value.helper.handler = execute
    return value


def test_client_driver_terminal_inspection_precedes_upload_or_job(client_case):
    case = client_case
    ticket = case.manager.begin("alias0")
    assert [call["command"] for call in case.helper.calls] == ["discover-client"]
    case.manager.poll(ticket)
    assert [call["command"] for call in case.helper.calls] == ["discover-client", "inspect-driver"]
    case.manager.poll(ticket)
    assert [call["command"] for call in case.helper.calls] == [
        "discover-client",
        "inspect-driver",
        "prepare",
    ]
    assert case.helper.calls[-1]["arguments"]["ray_address"] == "https://ray-head:8265"
    assert case.manager.configuration.job_addresses[0][1] == "ray://ray-head:10001"


def test_nonterminal_client_driver_retains_operation_and_allows_only_cleanup_read(client_case):
    case = client_case
    case.driver_terminal = False
    ticket = case.manager.begin("alias0")
    case.manager.poll(ticket)
    assert case.manager.poll(ticket) is None
    assert len(case.helper.calls) == 3 and case.manager.outstanding is ticket
    case.manager.cancel(ticket)
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.poll(ticket)
    case.clock.now += timedelta(seconds=301)
    case.driver_terminal = True
    case.manager.begin_cleanup(ticket)
    case.manager.poll(ticket)
    assert case.manager.outstanding is None
    assert [call["command"] for call in case.helper.calls] == ["discover-client"] + [
        "inspect-driver"
    ] * 3


def test_lost_client_discovery_response_cannot_be_cleared_as_no_job(client_case):
    case = client_case
    ticket = case.manager.begin("alias0")
    case.helper.result = CohortProcessCompletion(
        case.helper.outstanding.operation_id, CohortProcessReason.HELPER_FAILED
    )
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.poll(ticket)
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.begin_cleanup(ticket)
    assert case.manager.outstanding is ticket
    assert len(case.helper.calls) == 1


@pytest.mark.parametrize("failure", ["regression", "malformed", "raising"])
def test_persistent_parent_clock_failure_reaps_local_helper_without_accepting_discovery(
    client_case, failure
):
    case = client_case
    ticket = case.manager.begin("alias0")
    child = case.helper.outstanding
    original = case.manager._wall_clock

    def broken():
        if failure == "raising":
            raise RuntimeError("private clock diagnostic")
        return case.clock.now - timedelta(seconds=1) if failure == "regression" else None

    case.manager._wall_clock = broken
    case.helper.hang = True
    with pytest.raises(module.JobsCohortAdapterError) as refused:
        case.manager.poll(ticket)
    assert refused.value.reason is module.JobsCohortAdapterReason.DEADLINE
    assert case.helper.outstanding is child
    case.helper.hang = False
    with pytest.raises(module.JobsCohortAdapterError) as refused:
        case.manager.poll(ticket)
    assert refused.value.reason is module.JobsCohortAdapterReason.DEADLINE
    assert case.helper.outstanding is None
    assert case.helper.cancelled and all(item is child for item in case.helper.cancelled)
    assert case.manager.outstanding is ticket
    assert case.manager._operation.discovery_observation is None
    assert len(case.helper.calls) == 1
    case.manager._wall_clock = original
    with pytest.raises(module.JobsCohortAdapterError) as refused:
        case.manager.begin_cleanup(ticket)
    assert refused.value.reason is module.JobsCohortAdapterReason.CLEANUP_UNCONFIRMED


@pytest.mark.parametrize("phase", ["discover-client", "inspect-driver"])
def test_future_client_helper_timestamp_is_not_fresh(client_case, phase):
    case = client_case
    original = case.helper.handler

    def future(payload):
        result = original(payload)
        if payload["command"] == phase:
            result["observed_at" if phase == "discover-client" else "inspected_at"] = _timestamp(
                case.clock.now + timedelta(seconds=1)
            )
        return result

    case.helper.handler = future
    ticket = case.manager.begin("alias0")
    if phase == "inspect-driver":
        case.manager.poll(ticket)
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.poll(ticket)
    assert not any(call["command"] == "prepare" for call in case.helper.calls)


def test_crossed_client_driver_cleanup_result_remains_owned(client_case):
    case = client_case
    case.driver_terminal = False
    ticket = case.manager.begin("alias0")
    case.manager.poll(ticket)
    case.manager.cancel(ticket)
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.poll(ticket)
    case.manager.begin_cleanup(ticket)
    original = case.helper.handler

    def crossed(payload):
        result = original(payload)
        result["observation"] = dict(result["observation"], native_job_id="ffffffff")
        return result

    case.helper.handler = crossed
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.poll(ticket)
    assert case.manager.outstanding is ticket


def test_preparation_cannot_cross_explicit_configured_endpoint(case, monkeypatch):
    original = case.helper.default
    case.helper.handler = lambda payload: dict(original(payload), jobs_endpoint="http://other:8265")
    reserved = []
    monkeypatch.setattr(case.manager, "_reserve_and_submit", lambda *a, **k: reserved.append(True))
    ticket = case.manager.begin("alias0")
    with pytest.raises(module.JobsCohortAdapterError):
        case.manager.poll(ticket)
    assert reserved == []
