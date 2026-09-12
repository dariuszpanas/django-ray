"""Owned cancellation effects stay distinct from exact-ref terminal evidence."""

import threading
from dataclasses import replace
from datetime import UTC, datetime
from threading import BoundedSemaphore
from types import SimpleNamespace

import pytest

from django_ray.runner import ray_core
from django_ray.runner.cancellation import CancellationOutcomeStatus
from django_ray.target.cohort_transport import CohortExecutionResult, encode_cohort_execution_result
from tests.unit.test_cohort_execution import completed, prepared
from tests.unit.test_ray_core_runner import _FakeObjectRef, _install_fake_ray


@pytest.fixture
def control(monkeypatch):
    fake = _install_fake_ray(monkeypatch)
    clock = SimpleNamespace(value=10.0)
    monkeypatch.setattr(ray_core, "time", SimpleNamespace(monotonic=lambda: clock.value))
    monkeypatch.setattr(ray_core, "_RAY_CORE_CANCEL_SLOT", BoundedSemaphore(1))
    threads = []

    class ControlledThread:
        def __init__(self, *, target, name, daemon):
            assert name == "django-ray-cohort-cancel" and daemon is True
            self.target = target
            self.alive = False
            threads.append(self)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def finish(self):
            try:
                self.target()
            finally:
                self.alive = False

    monkeypatch.setattr(ray_core, "Thread", ControlledThread)
    calls = []
    monkeypatch.setattr(fake, "cancel", lambda ref, **kwargs: calls.append((ref, kwargs)))
    runner = ray_core.RayCoreRunner()
    value = prepared()

    def handle(pk=1):
        result = ray_core.RayCoreHandle(
            pk,
            _FakeObjectRef(str(pk) * 56),
            datetime.now(UTC),
            "task",
            attempt_number=value.identity.attempt_number,
            execution_generation=value.identity.execution_generation,
            cohort_prepared=value,
        )
        runner._pending_tasks[pk] = result
        return result

    state = SimpleNamespace(
        fake=fake,
        clock=clock,
        threads=threads,
        calls=calls,
        runner=runner,
        handle=handle,
        value=value,
    )
    yield state
    for thread in threads:
        if thread.is_alive():
            thread.finish()
    assert not runner.cohort_cancellation_busy


def test_one_owned_callback_requests_recursive_stop_without_retiring(control):
    state = control
    handle = state.handle()
    ticket = state.runner.begin_cohort_cancellation(handle)
    assert ticket is not None and ticket.handle is handle
    assert state.runner.poll_cohort_cancellation(ticket) is None
    assert state.runner.cohort_cancellation_busy
    assert state.runner.begin_cohort_cancellation(handle) is ticket
    assert state.runner.begin_cohort_cancellation(replace(handle)) is None
    with pytest.raises(ValueError, match="Unknown"):
        state.runner.poll_cohort_cancellation(replace(ticket))
    state.threads[0].finish()
    result = state.runner.poll_cohort_cancellation(ticket)
    assert result.status is ray_core.RayCoreCohortCancellationStatus.REQUESTED
    assert state.calls == [(handle.object_ref, {"force": False, "recursive": True})]
    assert not state.runner.cohort_cancellation_busy
    assert (
        state.runner.get_pending_handle(
            1,
            attempt_number=handle.attempt_number,
            execution_generation=handle.execution_generation,
        )
        is handle
    )
    assert state.runner.begin_cohort_cancellation(handle) is ticket
    assert len(state.threads) == 1


def test_actual_callback_runs_outside_parent_and_retains_handle(control, monkeypatch):
    monkeypatch.setattr(ray_core, "Thread", threading.Thread)
    entered, release = threading.Event(), threading.Event()
    caller_thread = threading.get_ident()
    observed = []
    handle = control.handle()

    def cancel(ref, *, force, recursive):
        observed.append((threading.get_ident(), ref, force, recursive))
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(control.fake, "cancel", cancel)
    ticket = control.runner.begin_cohort_cancellation(handle)
    callback = control.runner._cohort_cancellation_active.thread
    try:
        assert entered.wait(2)
        assert control.runner.poll_cohort_cancellation(ticket) is None
        assert control.runner.cohort_cancellation_busy
    finally:
        release.set()
        callback.join(2)
    assert not callback.is_alive()
    result = control.runner.poll_cohort_cancellation(ticket)
    assert result.status is ray_core.RayCoreCohortCancellationStatus.REQUESTED
    assert observed == [(observed[0][0], handle.object_ref, False, True)]
    assert observed[0][0] != caller_thread
    assert control.runner._pending_tasks[1] is handle
    assert control.runner.retire_pending_handle(handle)
    assert not control.runner._cohort_cancellations


def test_occupied_slot_never_starts_another_handle_and_can_advance_after_exit(control):
    first, second = control.handle(), control.handle(2)
    ticket = control.runner.begin_cohort_cancellation(first)
    assert control.runner.begin_cohort_cancellation(second) is None
    assert len(control.threads) == 1
    control.threads[0].finish()
    assert control.runner.begin_cohort_cancellation(second) is not None
    assert control.runner.begin_cohort_cancellation(first) is ticket
    assert len(control.threads) == 2


def test_legacy_global_slot_occupancy_preserves_cohort_handle_without_ticket(control):
    handle = control.handle()
    slot = ray_core._RAY_CORE_CANCEL_SLOT
    assert slot.acquire(blocking=False)
    try:
        assert control.runner.begin_cohort_cancellation(handle) is None
        assert not control.threads
        assert control.runner._pending_tasks[1] is handle
    finally:
        slot.release()
    assert control.runner.begin_cohort_cancellation(handle) is not None


def test_deadline_is_sticky_and_does_not_hide_late_ordinary_completion(control):
    handle = control.handle()
    ticket = control.runner.begin_cohort_cancellation(handle, timeout_seconds=1)
    control.clock.value = 11.0
    assert control.runner.poll_cohort_cancellation(ticket).uncertainty == "deadline"
    assert control.runner.cohort_cancellation_busy
    control.threads[0].finish()
    assert control.runner.poll_cohort_cancellation(ticket).uncertainty == "deadline"
    assert control.runner.begin_cohort_cancellation(handle) is ticket
    assert len(control.threads) == 1
    value = control.value
    control.fake.ready_refs.add(handle.object_ref)
    control.fake.values[handle.object_ref] = encode_cohort_execution_result(
        CohortExecutionResult(
            value.identity,
            value.request_digest,
            value.contract_digest,
            completion_json=completed(value),
        )
    )
    observed = control.runner.poll_cohort_completed([handle])[0]
    assert observed.result.completion_json == completed(value)
    assert not observed.terminal_cancelled and observed.uncertainty is None
    assert control.runner._pending_tasks[1] is handle


@pytest.mark.parametrize("bad_clock", [9.0, float("nan"), float("inf"), True, "10"])
def test_bad_parent_clock_preserves_callback_then_allows_local_cleanup(control, bad_clock):
    handle = control.handle()
    ticket = control.runner.begin_cohort_cancellation(handle)
    control.clock.value = bad_clock
    assert control.runner.poll_cohort_cancellation(ticket).uncertainty == "clock_unavailable"
    assert control.runner.cohort_cancellation_busy
    control.threads[0].finish()
    assert not control.runner.cohort_cancellation_busy
    control.clock.value = 10.5
    assert control.runner.poll_cohort_cancellation(ticket).uncertainty == "clock_unavailable"
    assert control.runner.begin_cohort_cancellation(handle) is ticket


def test_retirement_does_not_hide_callback_or_allow_connection_teardown(control):
    first = control.handle()
    control.runner.begin_cohort_cancellation(first)
    assert control.runner.retire_pending_handle(first)
    assert control.runner.cohort_cancellation_busy
    replacement = control.handle()
    assert control.runner.begin_cohort_cancellation(replacement) is None
    assert len(control.threads) == 1
    control.threads[0].finish()
    assert not control.runner.cohort_cancellation_busy
    assert not control.runner._cohort_cancellations
    replacement_ticket = control.runner.begin_cohort_cancellation(replacement)
    assert replacement_ticket.handle is replacement


def test_callback_start_failure_releases_slot_but_same_handle_never_replays(control, monkeypatch):
    handle = control.handle()
    original = ray_core.Thread.start
    monkeypatch.setattr(
        ray_core.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError())
    )
    ticket = control.runner.begin_cohort_cancellation(handle)
    assert control.runner.poll_cohort_cancellation(ticket).uncertainty == "callback_start_failed"
    assert not control.runner.cohort_cancellation_busy
    assert control.runner.begin_cohort_cancellation(handle) is ticket
    monkeypatch.setattr(ray_core.Thread, "start", original)
    assert control.runner.begin_cohort_cancellation(control.handle(2)) is not None


def test_backend_failure_is_uncertain_and_legacy_cancel_cannot_pop_cohort(control, monkeypatch):
    handle = control.handle()
    monkeypatch.setattr(
        control.fake, "cancel", lambda *_args, **_kw: (_ for _ in ()).throw(RuntimeError())
    )
    ticket = control.runner.begin_cohort_cancellation(handle)
    control.threads[0].finish()
    assert control.runner.poll_cohort_cancellation(ticket).uncertainty == "request_uncertain"
    assert (
        control.runner.cancel_pending_with_status(handle).status
        is CancellationOutcomeStatus.NOT_APPLICABLE
    )
    assert control.runner._pending_tasks[1] is handle


@pytest.mark.parametrize("kind", ["outer", "nested_subclass", "cause", "text"])
def test_only_direct_outer_ray_cancellation_is_terminal(control, kind):
    handle = control.handle()
    cancelled_type = control.fake.exceptions.TaskCancelledError
    if kind == "outer":
        error = cancelled_type()
    elif kind == "nested_subclass":

        class NestedRayTaskError(RuntimeError, cancelled_type):
            pass

        error = NestedRayTaskError()
    else:
        error = RuntimeError("TaskCancelledError")
        if kind == "cause":
            error.__cause__ = cancelled_type()
    control.fake.ready_refs.add(handle.object_ref)
    control.fake.values[handle.object_ref] = error
    observed = control.runner.poll_cohort_completed([handle])[0]
    assert observed.terminal_cancelled is (kind == "outer")
    assert observed.result is None
    assert observed.uncertainty == (None if kind == "outer" else "transport_uncertain")
    assert control.runner._pending_tasks[1] is handle


@pytest.mark.parametrize("timeout", [False, 0, -1, 5.01, float("inf"), float("nan"), "1"])
def test_invalid_budget_cannot_start_cancellation(control, timeout):
    with pytest.raises(ValueError, match="timeout"):
        control.runner.begin_cohort_cancellation(control.handle(), timeout_seconds=timeout)
    assert not control.threads and not control.calls
