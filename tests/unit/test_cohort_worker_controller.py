"""Parent controller progress without native calls, processes or database access."""

import builtins
import sys
from copy import deepcopy
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace

import pytest

from django_ray.runner import cohort_connection
from django_ray.runner import cohort_worker as module
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.target.cohort_claim import CohortRunnerFamily
from tests.unit.test_cohort_connection import FakeThread, SteppedQueue


class FakeLifecycle:
    def __init__(self, lease, package, runtime, family, **kwargs):
        self.lease = lease
        self.outstanding = None
        self.invalidated = False

    def configure_aliases(self, aliases):
        self.aliases = aliases

    def invalidate(self):
        self.invalidated = True


class FakeCoreAdapter:
    def __init__(self, lifecycle):
        self.lifecycle = lifecycle
        self.calls = []
        self.qualified = ()
        self.poll_error = False

    def begin(self, digest, epoch):
        assert self.lifecycle.outstanding is None
        self.calls.append(("begin", digest, epoch))
        self.lifecycle.outstanding = object()

    def poll(self):
        self.calls.append(("poll", self.lifecycle.outstanding))
        if self.poll_error:
            raise RuntimeError("private provider diagnostics")

    def qualified_aliases(self):
        return self.qualified


class FakeJobsAdapter:
    def __init__(self, lifecycle, configuration, **kwargs):
        self.lifecycle = lifecycle
        self.outstanding = None
        self.phase = module.JobsCohortPhase.PREPARING
        self.calls = []
        self.qualified = ()
        self.poll_error = False
        self.cleanup_error = False
        self.finish_on_poll = False

    def begin(self, alias):
        assert self.outstanding is None
        self.outstanding = object()
        self.phase = module.JobsCohortPhase.PREPARING
        self.calls.append(("begin", alias, self.outstanding))

    def poll(self, ticket):
        assert ticket is self.outstanding
        self.calls.append(("poll", ticket))
        if self.poll_error:
            self.phase = module.JobsCohortPhase.BLOCKED
            raise RuntimeError("private provider diagnostics")
        if self.finish_on_poll:
            self.outstanding = None
            self.finish_on_poll = False

    def begin_cleanup(self, ticket):
        assert ticket is self.outstanding
        self.calls.append(("cleanup", ticket))
        if self.cleanup_error:
            raise RuntimeError("private cleanup diagnostics")
        self.poll_error = False
        self.phase = module.JobsCohortPhase.STOPPING

    def qualified_aliases(self):
        return self.qualified

    def invalidate(self):
        self.calls.append(("invalidate", self.outstanding))
        self.lifecycle.invalidate()
        if self.outstanding is not None:
            self.phase = module.JobsCohortPhase.BLOCKED
            self.poll_error = True


@pytest.fixture
def case(monkeypatch):
    state = SimpleNamespace(now=10.0, threads=[], connections=[], claims=[])
    ray = ModuleType("ray")
    ray.__dict__["__version__"] = "2.58.0"
    monkeypatch.setitem(sys.modules, "ray", ray)

    def thread(**kwargs):
        instance = FakeThread(**kwargs)
        state.threads.append(instance)
        return instance

    def claim(identity, **kwargs):
        state.claims.append((identity, kwargs))
        return ("owned-claim",)

    monkeypatch.setattr(cohort_connection, "Thread", thread)
    monkeypatch.setattr(cohort_connection, "Queue", SteppedQueue)
    monkeypatch.setattr(module, "CohortQualificationLifecycle", FakeLifecycle)
    monkeypatch.setattr(module, "CoreCohortManagerAdapter", FakeCoreAdapter)
    monkeypatch.setattr(module, "JobsCohortManagerAdapter", FakeJobsAdapter)
    monkeypatch.setattr(module, "claim_cohort_tasks", claim)
    state.identity = WorkerLeaseIdentity("worker", "host", 12, datetime.now(UTC))
    state.arguments = {
        "validated_aliases": ("first", "second", "jobs", "other"),
        "selected_queues": (" queue 日本語 ",),
        "tasks": {
            "first": {
                "OPTIONS": {"RAY_ADDRESS": "http://first:8265"},
                "QUEUES": [" queue 日本語 "],
            },
            "second": {
                "OPTIONS": {"RAY_ADDRESS": "http://second:8265"},
                "QUEUES": [" queue 日本語 "],
            },
            "jobs": {"OPTIONS": {"RAY_JOB_ONLY": True}, "QUEUES": [" queue 日本語 "]},
            "other": {"OPTIONS": {}, "QUEUES": ["different"]},
        },
        "manager_settings": {"RAY_ADDRESS": "auto", "RAY_RUNTIME_ENV": {}},
        "django_settings_module": "project.settings",
        "monotonic": lambda: state.now,
    }
    yield state
    for instance in state.threads:
        instance.close()


def controller(case, mode="cluster"):
    return module.CohortWorkerController(
        case.identity,
        **case.arguments,
        execution_mode=mode,
        core_address="ray://selected:10001" if mode == "cluster" else None,
        connect=lambda: case.connections.append("connected"),
    )


def connect(case, value):
    value.tick()
    assert len(case.threads) == 1
    case.threads[0].run()
    value.tick()


def qualification(alias):
    return SimpleNamespace(configuration=SimpleNamespace(alias=alias))


def test_local_sdk_mutation_cannot_replace_the_declared_connection(case, monkeypatch):
    from copy import deepcopy

    from django.conf import settings

    from django_ray.management.commands import django_ray_worker as worker

    command = worker.Command()
    command.lease_identity = case.identity
    command.execution_mode = "local"
    command.cluster_address = None
    command._ray_backend_queue_configuration = SimpleNamespace(
        aliases=case.arguments["validated_aliases"]
    )
    monkeypatch.setattr(settings, "TASKS", case.arguments["tasks"])
    monkeypatch.setattr(worker, "get_settings", lambda: case.arguments["manager_settings"])
    sdk = sys.modules["ray"]
    received = []

    def initialize(**options):
        received.append(options)
        # Ray Node.validate_external_storage adds these values in place to
        # the supplied nonempty system configuration during local startup.
        options["_system_config"].update(
            automatic_object_spilling_enabled=True,
            object_spilling_config='{"type":"filesystem"}',
            is_external_storage_type_fs=True,
        )
        options["runtime_env"]["env_vars"]["RAY_SDK_LOCAL_VALUE"] = "observed"

    monkeypatch.setattr(sdk, "is_initialized", lambda: False, raising=False)
    monkeypatch.setattr(sdk, "init", initialize, raising=False)
    command._initialize_cohort_execution(case.arguments["selected_queues"])
    controller = command._cohort_controller
    declared = controller._configuration_arguments["core_control_settings"]
    original = deepcopy(declared)
    controller.tick()
    case.threads[0].run()
    controller.tick()

    assert len(received) == 1 and controller.connected
    assert controller.check_configuration(
        tasks=case.arguments["tasks"], manager_settings=case.arguments["manager_settings"]
    )
    assert declared == original and received[0]["_system_config"] is not declared["_system_config"]
    assert not controller.stopped

    changed = deepcopy(case.arguments["tasks"])
    changed["first"]["OPTIONS"]["RAY_ADDRESS"] = "http://replacement:8265"
    assert not controller.check_configuration(
        tasks=changed, manager_settings=case.arguments["manager_settings"]
    )
    assert controller.reason == "configuration_changed" and controller.stopped
    assert controller.connection_ticket is not None and len(received) == 1


@pytest.mark.parametrize("mode", ["sync", "ray", "cluster"])
def test_retirement_does_not_begin_connection_or_probe_or_claim(case, mode):
    value = controller(case, mode)
    value.request_retirement()
    value.tick()
    assert value.retirement_ready
    assert value.qualifications() == () and value.claim(limit=1) == ()
    assert not case.threads and not case.claims
    if value.adapter is not None:
        assert not [call for call in value.adapter.calls if call[0] == "begin"]


def test_retirement_waits_for_existing_core_probe_without_starting_another(case):
    value = controller(case)
    connect(case, value)
    ticket = value.lifecycle.outstanding
    assert ticket is not None
    value.request_retirement()
    value.tick()
    assert value.adapter.calls[-1] == ("poll", ticket)
    assert not value.retirement_ready and value.claim(limit=1) == ()
    value.lifecycle.outstanding = None
    value.tick()
    assert value.retirement_ready
    assert len([call for call in value.adapter.calls if call[0] == "begin"]) == 1


def test_retirement_waits_for_existing_jobs_cleanup_and_never_starts_sibling(case):
    value = controller(case, "ray")
    value.tick()
    value.request_retirement()
    value.adapter.poll_error = True
    value.tick()
    assert value.adapter.calls[-1][0] == "cleanup"
    assert not value.retirement_ready
    value.adapter.finish_on_poll = True
    value.tick()
    value.tick()
    assert value.retirement_ready
    assert len([call for call in value.adapter.calls if call[0] == "begin"]) == 1


def test_sync_never_initializes_ray_and_preserves_exact_selected_queue(case, monkeypatch):
    original = builtins.__import__

    def no_ray(name, *args, **kwargs):
        if name == "ray":
            pytest.fail("Sync controller imported Ray")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_ray)
    value = controller(case, "sync")
    for _ in range(3):
        value.tick()
    assert value.family is CohortRunnerFamily.SYNC
    assert value.adapter is value.connection is value.lifecycle is None
    assert not value.connected
    assert [alias.alias for alias in value.aliases] == ["first", "second"]
    assert all(alias.queues == (" queue 日本語 ",) for alias in value.aliases)
    assert value.claim(limit=3) == ("owned-claim",)
    assert case.claims[0][1]["qualifications"] == ()
    assert case.claims[0][1]["runner_family"] is CohortRunnerFamily.SYNC
    assert not case.connections and not case.threads


@pytest.mark.parametrize("mode", ["sync", "cluster"])
def test_task_environment_changes_do_not_invalidate_sync_or_core_admission(case, mode):
    value = controller(case, mode)
    changed = deepcopy(case.arguments["manager_settings"])
    changed["RAY_RUNTIME_ENV"] = {"pip": ["different-task-package==1"]}
    assert value.check_configuration(tasks=case.arguments["tasks"], manager_settings=changed)


@pytest.mark.parametrize("mode", ["sync", "cluster", "ray"])
def test_observed_configuration_change_stops_claims_without_resurrecting_on_aba(case, mode):
    value = controller(case, mode)
    changed = deepcopy(case.arguments["tasks"])
    changed["first"]["OPTIONS"]["RAY_ADDRESS"] = "http://changed:8265"
    assert not value.check_configuration(
        tasks=changed, manager_settings=case.arguments["manager_settings"]
    )
    assert value.reason == "configuration_changed"
    assert not value.check_configuration(
        tasks=case.arguments["tasks"], manager_settings=case.arguments["manager_settings"]
    )
    assert value.claim(limit=1) == () and value.qualifications() == ()
    value.tick()
    assert not case.claims and not case.connections and not case.threads
    if value.lifecycle is not None:
        assert value.lifecycle.invalidated


def test_pending_core_connection_keeps_one_owner_and_never_calls_probe(case):
    value = controller(case)
    for _ in range(20):
        value.tick()
    assert len(case.threads) == 1 and case.threads[0].is_alive()
    assert case.connections == [] and value.adapter.calls == []
    assert not value.connected
    case.threads[0].run()
    value.tick()
    assert value.connected and case.connections == ["connected"]
    assert value.adapter.calls == [("begin", value.configuration.core_configuration_digest, 1)]


def test_core_connection_deadline_cannot_accept_late_success_or_start_a_probe(case):
    value = controller(case)
    value.tick()
    ticket = value.connection_ticket
    case.now += 60
    value.tick()
    assert value.reason == "connection_unavailable"
    assert not value.connected
    assert value.connection.outstanding is ticket
    case.threads[0].run()
    for _ in range(3):
        value.tick()
    assert not value.connected
    assert value.connection.outstanding is ticket
    assert len(case.threads) == 1 and value.adapter.calls == []


@pytest.mark.parametrize("mode", ["sync", "cluster", "ray"])
def test_unreadable_current_configuration_withholds_claims_and_retains_owned_operations(case, mode):
    value = controller(case, mode)
    value.tick()
    original_ticket = value.adapter.outstanding if mode == "ray" else value.connection_ticket
    assert not value.check_configuration(
        tasks={}, manager_settings=case.arguments["manager_settings"]
    )
    assert value.reason == "configuration_changed"
    assert value.claim(limit=1) == ()
    if mode == "ray":
        assert value.adapter.outstanding is original_ticket
    elif mode == "cluster":
        assert value.connection.outstanding is original_ticket
        assert case.threads[0].is_alive()


def test_jobs_manager_profile_change_invalidates_qualification_even_with_same_declaration(case):
    value = controller(case, "ray")
    value.tick()
    ticket = value.adapter.outstanding
    changed = deepcopy(case.arguments["manager_settings"])
    changed["RAY_RUNTIME_ENV"] = {"pip": ["changed-control-package==1"]}
    assert not value.check_configuration(tasks=case.arguments["tasks"], manager_settings=changed)
    assert value.adapter.outstanding is ticket
    assert value.qualifications() == ()


def test_pending_core_probe_keeps_connection_and_operation_until_owned_completion(case):
    value = controller(case)
    connect(case, value)
    ticket = value.lifecycle.outstanding
    for _ in range(5):
        value.tick()
    assert value.lifecycle.outstanding is ticket
    assert value.adapter.calls[1:] == [("poll", ticket)] * 5
    assert len(case.threads) == 1
    value.lifecycle.outstanding = None
    value.adapter.qualified = (qualification("first"), qualification("second"))
    case.now += 2
    value.tick()
    assert len([call for call in value.adapter.calls if call[0] == "begin"]) == 1
    value.adapter.qualified = ()
    value.tick()
    assert len([call for call in value.adapter.calls if call[0] == "begin"]) == 2
    assert len(case.threads) == 1


def test_failed_core_probe_retains_slot_and_redacts_provider_failure(case):
    value = controller(case)
    connect(case, value)
    ticket = value.lifecycle.outstanding
    value.adapter.poll_error = True
    for _ in range(4):
        value.tick()
    assert value.reason == "qualification_unavailable"
    assert value.lifecycle.outstanding is ticket
    assert len(case.threads) == 1
    assert len([call for call in value.adapter.calls if call[0] == "begin"]) == 1


def test_jobs_serial_round_robin_skips_qualified_aliases_and_respects_retry_spacing(case):
    value = controller(case, "ray")
    value.adapter.qualified = (qualification("first"),)
    value.tick()
    first = value.adapter.outstanding
    assert value.adapter.calls == [("begin", "second", first)]
    for _ in range(4):
        value.tick()
    assert value.adapter.calls[1:] == [("poll", first)] * 4
    value.adapter.finish_on_poll = True
    value.tick()
    value.tick()
    assert len([call for call in value.adapter.calls if call[0] == "begin"]) == 1
    case.now += 2
    value.tick()
    assert value.adapter.calls[-1][:2] == ("begin", "jobs")
    assert not case.threads and not case.connections


def test_failed_jobs_operation_owns_one_cleanup_and_keeps_polling_it(case):
    value = controller(case, "ray")
    value.tick()
    ticket = value.adapter.outstanding
    value.adapter.poll_error = True
    value.tick()
    assert value.adapter.calls[-1] == ("cleanup", ticket)
    for _ in range(5):
        value.tick()
    assert value.adapter.outstanding is ticket
    assert value.adapter.calls.count(("cleanup", ticket)) == 1
    assert value.adapter.calls.count(("poll", ticket)) == 6
    value.adapter.finish_on_poll = True
    value.tick()
    assert value.adapter.outstanding is None
    case.now += 2
    value.tick()
    assert value.adapter.outstanding is not ticket
    value.adapter.poll_error = True
    value.tick()
    assert value.adapter.calls[-1] == ("cleanup", value.adapter.outstanding)


def test_unconfirmed_jobs_cleanup_does_not_detach_or_replace_operation(case):
    value = controller(case, "ray")
    value.tick()
    ticket = value.adapter.outstanding
    value.adapter.poll_error = value.adapter.cleanup_error = True
    for _ in range(3):
        value.tick()
    assert value.reason == "cleanup_unconfirmed"
    assert value.adapter.outstanding is ticket
    assert value.adapter.calls.count(("cleanup", ticket)) == 3
    assert len([call for call in value.adapter.calls if call[0] == "begin"]) == 1


def test_stopped_jobs_controller_continues_exact_cleanup_without_admitting_work(case):
    value = controller(case, "ray")
    value.tick()
    ticket = value.adapter.outstanding
    value.invalidate("lease_lost")
    value.tick()
    assert value.claim(limit=2) == ()
    value.poll_stopped_cleanup()
    assert value.adapter.calls[-1] == ("cleanup", ticket)
    value.poll_stopped_cleanup()
    assert value.adapter.calls[-1] == ("poll", ticket)
    value.adapter.finish_on_poll = True
    value.poll_stopped_cleanup()
    assert value.adapter.outstanding is None
    value.poll_stopped_cleanup()
    assert not case.claims
    assert len([call for call in value.adapter.calls if call[0] == "begin"]) == 1


def test_qualification_failure_withholds_candidates_and_claim_batch_is_bounded(case, monkeypatch):
    value = controller(case, "ray")

    def failed():
        raise RuntimeError("private provider diagnostics")

    monkeypatch.setattr(value.adapter, "qualified_aliases", failed)
    assert value.claim(limit=500) == ("owned-claim",)
    assert case.claims[-1][1]["qualifications"] == ()
    assert case.claims[-1][1]["limit"] == 100
    assert value.reason == "qualification_unavailable"
    count = len(case.claims)
    assert value.claim(limit=0) == ()
    assert len(case.claims) == count
