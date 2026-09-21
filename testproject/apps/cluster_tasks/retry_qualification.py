"""Fixed retry fixtures for the disposable workflow qualification workload."""

from __future__ import annotations


class RetryCounter:
    """One owned counter, independent of worker invocation and retry lifetime."""

    def __init__(self):
        self.count = 0

    def increment(self):
        self.count += 1
        return self.count

    def read(self):
        return self.count


def retry_leaf(counter, failures):
    import ray

    from django_ray.runtime.context import report_workflow_progress

    invocation = ray.get(counter.increment.remote(), timeout=10)
    report_workflow_progress(
        1, 2, message="retry qualification", metrics={"invocation": invocation}
    )
    if invocation <= failures:
        raise ValueError("retry qualification exhausted")
    return 42


def preview(value):
    return {"answer": value}


def identity(value):
    return value


def remove_counter(counter):
    """Require observed removal after Ray's asynchronous kill request."""
    import time

    import ray
    from ray.exceptions import GetTimeoutError, RayActorError

    ray.kill(counter, no_restart=True)
    deadline = time.monotonic() + 10
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("Retry qualification counter survived cleanup")
        try:
            ray.get(counter.read.remote(), timeout=min(2, remaining))
        except RayActorError:
            return
        except GetTimeoutError:
            pass
        time.sleep(0.01)


def run_retry_qualification(*, exhausted: bool = False, unlimited: bool = False):
    """Run finite or eventually settling unlimited retries, then remove the actor."""
    import ray
    from ray.exceptions import RayTaskError

    from django_ray.workflows import chain, step

    failures = 2 if exhausted or unlimited else 1
    counter = ray.remote(num_cpus=0, max_restarts=0)(RetryCounter).remote()
    try:
        leaf = (
            step(retry_leaf, counter, failures)
            .with_options(
                num_cpus=0.25,
                max_retries=-1 if unlimited else 1,
                retry_exceptions=True,
            )
            .with_output_preview(preview)
        )
        signature = leaf if exhausted else chain(leaf, step(identity).with_options(num_cpus=0.25))
        try:
            result = signature.with_progress_reporting("full").run(use_ray=True)
        except RayTaskError:
            if not exhausted or ray.get(counter.read.remote(), timeout=10) != 2:
                raise AssertionError("Retry qualification invocation count differs") from None
            raise
        if exhausted or result != 42 or ray.get(counter.read.remote(), timeout=10) != failures + 1:
            raise AssertionError("Retry qualification result or invocation count differs")
        return result
    finally:
        remove_counter(counter)
