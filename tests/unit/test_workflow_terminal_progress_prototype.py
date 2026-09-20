"""Test transport semantics before selecting a production admission design."""

from __future__ import annotations

import pytest

from scripts.workflow_terminal_progress_prototype import MAX_METADATA_BYTES, execute_with_metadata


def test_admitted_capture_keeps_one_bounded_latest_value():
    def callback(*, report):
        assert report(b"first")
        assert report(b"last")
        assert not report(b"x" * (MAX_METADATA_BYTES + 1))
        return 42

    assert execute_with_metadata(callback, admitted=True) == (42, b"last")


def test_unadmitted_capture_preserves_the_one_result_path():
    def callback(*, report):
        assert report(b"unused") is False
        return {"value": 42}

    assert execute_with_metadata(callback, admitted=False) == {"value": 42}


@pytest.mark.parametrize("failure", ["exception", "oversize", "type"])
def test_metadata_preparation_cannot_replace_a_success(failure):
    calls = []

    def callback(*, report):
        calls.append(True)
        report(b"last")
        return 42

    def encoder(_value):
        if failure == "exception":
            raise ValueError("encoder failed")
        return b"x" * (MAX_METADATA_BYTES + 1) if failure == "oversize" else None

    assert execute_with_metadata(callback, admitted=True, encoder=encoder) == (42, None)
    assert calls == [True]


@pytest.mark.parametrize("admitted", [False, True])
def test_original_callback_exception_is_preserved(admitted):
    failure = ValueError("callable failed")

    def callback(*, report):
        report(b"before failure")
        raise failure

    with pytest.raises(ValueError) as raised:
        execute_with_metadata(callback, admitted=admitted)
    assert raised.value is failure


@pytest.mark.real_ray
def test_real_ray_secondary_metadata_preserves_downstream_dependency(ray_cluster):
    def callback(value, *, report):
        report(b"first")
        report(b"last")
        return value + 1

    remote = ray_cluster.remote(num_cpus=0.25, max_retries=0)(execute_with_metadata)
    refs = []
    try:
        value, metadata = remote.options(num_returns=2).remote(callback, 40, admitted=True)
        refs.extend([value, metadata])
        following = remote.remote(callback, value, admitted=False)
        refs.append(following)
        assert ray_cluster.get([following, metadata], timeout=30) == [42, b"last"]
        assert value.task_id() == metadata.task_id()
    finally:
        for ref in refs:
            ray_cluster.cancel(ref, force=True, recursive=True)


@pytest.mark.real_ray
@pytest.mark.parametrize("max_retries", [1, -1])
@pytest.mark.parametrize("metadata_failure", [False, True])
def test_real_ray_secondary_metadata_preserves_configured_retry(
    ray_cluster, max_retries, metadata_failure
):
    class Attempts:
        def __init__(self):
            self.count = 0

        def next(self):
            self.count += 1
            return self.count

        def value(self):
            return self.count

    counter = ray_cluster.remote(num_cpus=0, max_restarts=0)(Attempts).remote()

    def callback(counter, *, report):
        import ray

        attempt = ray.get(counter.next.remote(), timeout=10)
        report(f"attempt-{attempt}".encode())
        if attempt == 1:
            raise ValueError("fixed first-attempt failure")
        return 42

    remote = ray_cluster.remote(
        num_cpus=0.25, num_returns=2, max_retries=max_retries, retry_exceptions=True
    )(execute_with_metadata)
    refs = []

    def encoder(wire):
        if metadata_failure:
            raise RuntimeError("fixed encoder failure")
        return wire

    try:
        refs = list(remote.remote(callback, counter, admitted=True, encoder=encoder))
        assert ray_cluster.get(refs, timeout=30) == [
            42,
            None if metadata_failure else b"attempt-2",
        ]
        assert ray_cluster.get(counter.value.remote(), timeout=10) == 2
    finally:
        for ref in refs:
            ray_cluster.cancel(ref, force=True, recursive=True)
        ray_cluster.kill(counter, no_restart=True)


@pytest.mark.real_ray
def test_real_ray_failed_callable_fails_both_results(ray_cluster):
    def callback(*, report):
        report(b"before failure")
        raise ValueError("fixed callable failure")

    remote = ray_cluster.remote(num_cpus=0.25, num_returns=2, max_retries=0)(execute_with_metadata)
    refs = list(remote.remote(callback, admitted=True))
    try:
        for ref in refs:
            with pytest.raises(ValueError, match="fixed callable failure"):
                ray_cluster.get(ref, timeout=30)
    finally:
        for ref in refs:
            ray_cluster.cancel(ref, force=True, recursive=True)
