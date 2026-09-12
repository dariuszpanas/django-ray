"""The private Core adapter never changes or guesses the selected connection."""

from dataclasses import replace
from datetime import timedelta

import pytest

from django_ray.runner import cohort_core as adapter
from django_ray.runner.leasing import WorkerLeaseIdentity
from tests.unit.test_cohort_qualification import DIGEST, LEASE, RUNTIME


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "not-initialized",
        "falsey-initialized",
        "nondefault",
        "other-context",
        "boolean-count",
        "unknown-connected",
        "runtime",
    ],
)
def test_connection_guard_refuses_unsupported_contexts_without_connecting(monkeypatch, change):
    import ray
    import ray.util.client as client

    monkeypatch.setattr(ray, "__version__", "2.57.0" if change == "version" else "2.58.0")
    initialized = (
        False if change == "not-initialized" else 0 if change == "falsey-initialized" else True
    )
    monkeypatch.setattr(ray, "is_initialized", lambda: initialized)
    monkeypatch.setattr(client.ray, "is_default", lambda: change != "nondefault")
    monkeypatch.setattr(
        client.ray, "is_connected", lambda: None if change == "unknown-connected" else False
    )
    monkeypatch.setattr(
        client,
        "num_connected_contexts",
        lambda: True if change == "boolean-count" else 1 if change == "other-context" else 0,
    )
    monkeypatch.setattr(
        adapter,
        "_local_runtime",
        lambda _ray: ("other" if change == "runtime" else "0.5.0", RUNTIME),
    )
    monkeypatch.setattr(ray, "init", lambda **kwargs: pytest.fail("Guard must never connect"))
    with pytest.raises(adapter.CoreCohortAdapterError) as error:
        adapter._supported_connection("0.5.0", RUNTIME)
    assert error.value.reason is adapter.CoreCohortAdapterReason.UNSUPPORTED_CONNECTION


@pytest.mark.parametrize("connected", [False, True])
def test_guard_accepts_only_existing_native_or_single_default_client(monkeypatch, connected):
    import ray
    import ray.util.client as client

    monkeypatch.setattr(ray, "__version__", "2.58.0")
    monkeypatch.setattr(ray, "is_initialized", lambda: True)
    monkeypatch.setattr(client.ray, "is_default", lambda: True)
    monkeypatch.setattr(client.ray, "is_connected", lambda: connected)
    monkeypatch.setattr(client, "num_connected_contexts", lambda: int(connected))
    monkeypatch.setattr(adapter, "_local_runtime", lambda _ray: ("0.5.0", RUNTIME))
    monkeypatch.setattr(
        ray, "get_runtime_context", lambda: pytest.fail("Parent guard must not make session RPCs")
    )
    assert adapter._supported_connection("0.5.0", RUNTIME) is ray


def test_challenge_digest_binds_descriptor_epoch_and_lease_incarnation():
    identity = WorkerLeaseIdentity(LEASE.worker_id, LEASE.hostname, LEASE.pid, LEASE.started_at)
    original = adapter._challenge_digest(identity, DIGEST, 1)
    assert original == adapter._challenge_digest(identity, DIGEST, 1)
    assert (
        len(
            {
                original,
                adapter._challenge_digest(identity, DIGEST, 2),
                adapter._challenge_digest(identity, "sha256:" + "b" * 64, 1),
                adapter._challenge_digest(
                    replace(identity, started_at=identity.started_at + timedelta(seconds=1)),
                    DIGEST,
                    1,
                ),
            }
        )
        == 4
    )
