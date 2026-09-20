"""Pressure evidence must prove actual loss mitigation and terminal detail."""

import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.runtime.remote import WorkflowProgressActor
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


@pytest.mark.parametrize("reject_event", [False, True])
def test_deployed_probe_bounds_actors_and_observes_cleanup(monkeypatch, reject_event):
    class ActorDeadError(Exception):
        pass

    class Actor:
        def __init__(self, initialization, limits):
            self.collector = WorkflowProgressActor(initialization, limits=limits)
            self.killed = False
            self.ingest = SimpleNamespace(
                remote=lambda event: False if reject_event else self.collector.ingest(event)
            )
            self.snapshot = SimpleNamespace(
                remote=lambda: ActorDeadError() if self.killed else self.collector.snapshot()
            )

    actors = []
    options_seen = []
    connections = []
    shutdowns = []
    timeouts = []

    class ActorType:
        def options(self, **options):
            if options.get("runtime_env", {}).get("working_dir", "").startswith("/"):
                raise ValueError("Local working_dir is only supported at job initialization")
            options_seen.append(options)
            return self

        def remote(self, initialization, *, limits):
            assert all(actor.killed for actor in actors)
            actor = Actor(initialization, limits)
            actors.append(actor)
            return actor

    def remote(**options):
        assert options == {"num_cpus": 0.25, "max_restarts": 0, "max_task_retries": 0}
        return lambda cls: ActorType()

    def get(value, *, timeout):
        timeouts.append(timeout)
        if isinstance(value, Exception):
            raise value
        return value

    def kill(actor, *, no_restart):
        assert no_restart is True
        actor.killed = True

    fake = SimpleNamespace(
        is_initialized=lambda: False,
        init=lambda **kwargs: connections.append(kwargs),
        remote=remote,
        get=get,
        kill=kill,
        shutdown=lambda: shutdowns.append(True),
        exceptions=SimpleNamespace(RayActorError=ActorDeadError),
    )
    monkeypatch.setitem(sys.modules, "ray", fake)
    monkeypatch.setitem(sys.modules, "ray.exceptions", fake.exceptions)
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    if reject_event:
        with pytest.raises(ValueError, match="rejected lifecycle"):
            verify_deployed_pressure()
        assert len(actors) == 1
    else:
        result = verify_deployed_pressure()
        assert result["actors_removed"] is True
        assert result["scope"] == "collector_and_preparation"
        assert len(result["cases"]) == len(actors) == 4
    assert all(actor.killed for actor in actors)
    assert all(0 < timeout <= 20 for timeout in timeouts)
    assert options_seen == []
    assert connections == [
        {
            "address": "ray://ray-head:10001",
            "runtime_env": {"working_dir": "/runtime/recovery.zip"},
            "logging_level": "ERROR",
        }
    ]
    assert shutdowns == [True]
