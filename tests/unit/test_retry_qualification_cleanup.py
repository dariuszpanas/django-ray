"""Observe asynchronous fixture actor removal without starting Ray."""

from types import SimpleNamespace

import pytest

from testproject.apps.cluster_tasks.retry_qualification import remove_counter


@pytest.mark.parametrize("initial", ["alive", "pending", "dead", "never"])
def test_counter_cleanup_waits_for_observed_death_with_a_deadline(monkeypatch, initial):
    import ray
    from ray.exceptions import GetTimeoutError, RayActorError

    counter = SimpleNamespace(read=SimpleNamespace(remote=lambda: "read-ref"))
    killed = []
    calls = []
    clock = iter([0, 0, 11] if initial == "never" else [0, 0, 0.1])
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr(ray, "kill", lambda actor, **kwargs: killed.append((actor, kwargs)))

    def get(ref, *, timeout):
        assert ref == "read-ref"
        assert 0 < timeout <= 2
        calls.append(ref)
        if initial == "dead" or len(calls) > 1:
            raise RayActorError()
        if initial == "pending":
            raise GetTimeoutError("pending")
        return 42

    monkeypatch.setattr(ray, "get", get)
    if initial == "never":
        with pytest.raises(AssertionError, match="survived cleanup"):
            remove_counter(counter)
    else:
        remove_counter(counter)
    assert killed == [(counter, {"no_restart": True})]
    assert len(calls) == (1 if initial in {"dead", "never"} else 2)
