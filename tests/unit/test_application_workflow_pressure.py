"""Pressure evidence must prove actual loss mitigation and terminal detail."""

import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.runtime.remote import WorkflowProgressActor
from qualification.application import workflow_pressure
from qualification.application.workflow_pressure import (
    pressure_case,
    verify_deployed_pressure,
    verify_pressure_snapshot,
)

IDENTITY = WorkflowRunIdentity(9, 1, 1, "00000000-0000-0000-0000-000000000572")


def snapshot_for(*, mapped=False, failed=False):
    limits, initialization, events = pressure_case(IDENTITY, mapped=mapped, failed=failed)
    actor = WorkflowProgressActor(initialization, limits=limits)
    for event in events:
        assert actor.ingest(event)
    return actor.snapshot(), limits.combined_max_decoded_bytes


@pytest.mark.parametrize("mapped", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_pressure_fixture_prepares_real_terminal_detail(mapped, failed):
    snapshot, limit = snapshot_for(mapped=mapped, failed=failed)
    assert verify_pressure_snapshot(
        IDENTITY, snapshot, mapped=mapped, failed=failed, byte_limit=limit
    ) == {
        "state": "FAILED" if failed else "SUCCEEDED",
        "mapped": mapped,
        "byte_accounting": True,
        "prepared_detail": True,
    }


@pytest.mark.parametrize("fault", ["bytes", "budget", "pressure", "rejection", "identity", "state"])
def test_pressure_fixture_rejects_incomplete_evidence(fault):
    snapshot, limit = snapshot_for()
    snapshot = deepcopy(snapshot)
    identity = IDENTITY
    if fault == "bytes":
        snapshot["ingress"]["retained_bytes"] += 1
    elif fault == "budget":
        limit = 1
    elif fault == "pressure":
        snapshot["ingress"]["replaceable"] = dict.fromkeys(snapshot["ingress"]["replaceable"], 0)
    elif fault == "rejection":
        snapshot["ingress"]["rejected"] = 1
    elif fault == "identity":
        identity = WorkflowRunIdentity(9, 2, 1, IDENTITY.run_id)
    else:
        snapshot["graph"]["nodes"][0]["state"] = "RUNNING"
    with pytest.raises(ValueError):
        verify_pressure_snapshot(identity, snapshot, mapped=False, failed=False, byte_limit=limit)


@pytest.fixture
def deployed_probe(monkeypatch):
    class RayActorError(Exception):
        pass

    class ActorDiedError(RayActorError):
        pass

    class ActorUnavailableError(RayActorError):
        pass

    probe = SimpleNamespace(
        now=0.0,
        elapsed_before_cleanup=0.0,
        reject_event=False,
        successful_cleanup_snapshots=0,
        cleanup_error=None,
        actors=[],
        sleeps=[],
        timeouts=[],
        errors=SimpleNamespace(
            RayActorError=RayActorError,
            ActorDiedError=ActorDiedError,
            ActorUnavailableError=ActorUnavailableError,
        ),
    )

    class Actor:
        def __init__(self, initialization, limits):
            self.collector = WorkflowProgressActor(initialization, limits=limits)
            self.killed = False
            self.death_observed = False
            self.cleanup_snapshots = 0
            self.ingest = SimpleNamespace(
                remote=lambda event: False if probe.reject_event else self.collector.ingest(event)
            )
            self.snapshot = SimpleNamespace(remote=self.read_snapshot)

        def read_snapshot(self):
            if self.killed:
                self.cleanup_snapshots += 1
                if probe.cleanup_error is not None:
                    return probe.cleanup_error
                if self.cleanup_snapshots > probe.successful_cleanup_snapshots:
                    self.death_observed = True
                    return ActorDiedError()
            return self.collector.snapshot()

    options_seen = []
    connections = []
    shutdowns = []

    class ActorType:
        def options(self, **options):
            if options.get("runtime_env", {}).get("working_dir", "").startswith("/"):
                raise ValueError("Local working_dir is only supported at job initialization")
            options_seen.append(options)
            return self

        def remote(self, initialization, *, limits):
            assert all(actor.death_observed for actor in probe.actors)
            actor = Actor(initialization, limits)
            probe.actors.append(actor)
            return actor

    def remote(**options):
        assert options == {"num_cpus": 0.25, "max_restarts": 0, "max_task_retries": 0}
        return lambda cls: ActorType()

    def get(value, *, timeout):
        probe.timeouts.append(timeout)
        assert 0 < timeout <= min(20, 120 - probe.now)
        if isinstance(value, Exception):
            raise value
        return value

    def kill(actor, *, no_restart):
        assert no_restart is True
        actor.killed = True
        probe.now += probe.elapsed_before_cleanup

    def sleep(delay):
        assert 0 < delay <= min(0.1, 120 - probe.now)
        probe.sleeps.append(delay)
        probe.now += delay

    fake = SimpleNamespace(
        is_initialized=lambda: False,
        init=lambda **kwargs: connections.append(kwargs),
        remote=remote,
        get=get,
        kill=kill,
        shutdown=lambda: shutdowns.append(True),
        exceptions=probe.errors,
    )
    monkeypatch.setitem(sys.modules, "ray", fake)
    monkeypatch.setitem(sys.modules, "ray.exceptions", fake.exceptions)
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr(workflow_pressure.time, "monotonic", lambda: probe.now)
    monkeypatch.setattr(workflow_pressure.time, "sleep", sleep)
    yield probe
    assert all(actor.killed for actor in probe.actors)
    assert all(0 < timeout <= 20 for timeout in probe.timeouts)
    assert options_seen == []
    assert connections == [
        {
            "address": "ray://ray-head:10001",
            "runtime_env": {"working_dir": "/runtime/recovery.zip"},
            "logging_level": "ERROR",
        }
    ]
    assert shutdowns == [True]


@pytest.mark.parametrize("reject_event", [False, True])
@pytest.mark.parametrize("successful_cleanup_snapshots", [0, 2])
def test_deployed_probe_bounds_actors_and_observes_cleanup(
    deployed_probe, reject_event, successful_cleanup_snapshots
):
    deployed_probe.reject_event = reject_event
    deployed_probe.successful_cleanup_snapshots = successful_cleanup_snapshots
    if reject_event:
        with pytest.raises(ValueError, match="rejected lifecycle"):
            verify_deployed_pressure()
        assert len(deployed_probe.actors) == 1
    else:
        result = verify_deployed_pressure()
        assert result["actors_removed"] is True
        assert result["scope"] == "collector_and_preparation"
        assert len(result["cases"]) == len(deployed_probe.actors) == 4
    assert all(actor.death_observed for actor in deployed_probe.actors)
    assert all(
        actor.cleanup_snapshots == successful_cleanup_snapshots + 1
        for actor in deployed_probe.actors
    )
    assert len(deployed_probe.sleeps) == len(deployed_probe.actors) * successful_cleanup_snapshots


def test_deployed_probe_rejects_actor_alive_at_original_deadline(deployed_probe):
    deployed_probe.successful_cleanup_snapshots = float("inf")
    deployed_probe.elapsed_before_cleanup = 119.75
    with pytest.raises(ValueError, match="actor remained available at deadline"):
        verify_deployed_pressure()
    assert len(deployed_probe.actors) == 1
    assert not deployed_probe.actors[0].death_observed
    assert deployed_probe.now == 120
    assert sum(deployed_probe.sleeps) == pytest.approx(0.25)
    assert deployed_probe.sleeps[-1] == pytest.approx(0.05)


@pytest.mark.parametrize(
    "error_type", ["RayActorError", "ActorUnavailableError", "runtime", "timeout"]
)
def test_deployed_probe_propagates_cleanup_errors(deployed_probe, error_type):
    errors = {"runtime": RuntimeError, "timeout": TimeoutError, **vars(deployed_probe.errors)}
    error = errors[error_type]("cleanup probe failed")
    deployed_probe.cleanup_error = error
    with pytest.raises(type(error)) as caught:
        verify_deployed_pressure()
    assert caught.value is error
    assert len(deployed_probe.actors) == 1
    assert not deployed_probe.actors[0].death_observed
    assert deployed_probe.sleeps == []
