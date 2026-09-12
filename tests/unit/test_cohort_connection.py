"""Resource-free connection ownership; no Ray, database or native init calls."""

from dataclasses import replace
from queue import Queue
from threading import Event, Thread, get_ident, local
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from django_ray.runner import cohort_connection as module

DIGEST = "sha256:" + "a" * 64
Phase = module.CoreConnectionPhase
Reason = module.CoreConnectionReason


_scheduler = local()


class _StoppedTestThread(BaseException):
    pass


class SteppedQueue(Queue):
    """Pause owned callbacks at their real blocking command-mailbox boundary."""

    def get(self, *args, **kwargs):
        owner = getattr(_scheduler, "owner", None)
        if owner is not None:
            owner.checkpoint()
        return super().get(*args, **kwargs)


class FakeThread:
    """Explicit test scheduling of a persistent owner, with no native work."""

    def __init__(self, *, target, name, daemon):
        self.target, self.name, self.daemon = target, name, daemon
        self.proceed, self.paused, self.stopped, self.finished = (Event() for _ in range(4))
        self.thread = None

    def checkpoint(self):
        self.paused.set()
        if not self.proceed.wait(timeout=5):
            raise AssertionError("Test did not release its owned callback")
        self.proceed.clear()
        self.paused.clear()
        if self.stopped.is_set():
            raise _StoppedTestThread

    def start(self):
        def execute():
            _scheduler.owner = self
            try:
                self.checkpoint()
                self.target()
            except _StoppedTestThread:
                pass
            finally:
                self.finished.set()
                self.paused.set()

        self.thread = Thread(target=execute, name=self.name, daemon=self.daemon)
        self.thread.start()
        assert self.paused.wait(timeout=2)

    def is_alive(self):
        return self.thread is not None and self.thread.is_alive()

    def run(self):
        assert self.is_alive() and self.paused.is_set()
        self.paused.clear()
        self.proceed.set()
        assert self.paused.wait(timeout=2), "Owned test callback did not reach its next boundary"
        if self.finished.is_set():
            self.thread.join(timeout=2)
            assert not self.is_alive()

    def close(self):
        # Stopped resource-free fixture cleanup, never product disconnect proof.
        self.stopped.set()
        self.proceed.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
            assert not self.is_alive()

    def join(self, *args, **kwargs):
        pytest.fail("Parent lifecycle must never join a thread")


@pytest.fixture
def case(monkeypatch):
    value = SimpleNamespace(now=10.0, threads=[], calls=[])

    def thread(**kwargs):
        instance = FakeThread(**kwargs)
        value.threads.append(instance)
        return instance

    monkeypatch.setattr(module, "Thread", thread)
    monkeypatch.setattr(module, "Queue", SteppedQueue)
    value.lifecycle = module.CoreConnectionLifecycle(monotonic=lambda: value.now)
    yield value
    for instance in value.threads:
        instance.close()


def begin(case, **changes):
    args = {"connect": lambda: case.calls.append("connect"), "timeout_seconds": 5}
    args.update(changes)
    return case.lifecycle.begin(DIGEST, **args)


def refuses(reason, call):
    with pytest.raises(module.CoreConnectionError) as error:
        call()
    assert error.value.reason is reason


def reap(case, ticket):
    """A cleanup receipt releases ownership only after the creator really exits."""
    assert case.lifecycle.outstanding is ticket
    assert case.lifecycle.poll(ticket).phase is Phase.CLEANING
    refuses(Reason.BUSY, lambda: begin(case))
    case.threads[-1].run()
    assert case.lifecycle.poll(ticket).phase is Phase.CLEANED
    assert case.lifecycle.outstanding is None


def test_begin_and_poll_leave_parent_responsive_until_exact_callback_exit(case):
    ticket = begin(case)
    assert case.calls == []
    assert case.threads[0].daemon and case.threads[0].name == "django-ray-core-connection"
    for _ in range(4):
        snapshot = case.lifecycle.poll(ticket)
        assert snapshot.phase is Phase.CONNECTING and snapshot.callback_running
    assert case.calls == []
    # The same-process callback can initialize a context used by its parent;
    # returning it as a transferable subprocess payload is not this contract.
    case.threads[0].run()
    assert case.calls == ["connect"]
    snapshot = case.lifecycle.poll(ticket)
    assert snapshot.phase is Phase.CONNECTED and not snapshot.callback_running
    assert case.threads[0].is_alive()
    case.now = 100
    assert case.lifecycle.poll(ticket).phase is Phase.CONNECTED
    refuses(Reason.BUSY, lambda: begin(case))


@pytest.mark.parametrize("action", ["poll", "invalidate", "begin_cleanup", "confirm_cleanup"])
def test_reconstructed_or_old_ticket_never_controls_current_epoch(case, action):
    ticket = begin(case)
    forged = replace(ticket)
    kwargs = {"cleanup": lambda: True} if action == "begin_cleanup" else {}
    if action == "confirm_cleanup":
        kwargs = {"independently_confirmed": True}
    refuses(Reason.STALE, lambda: getattr(case.lifecycle, action)(forged, **kwargs))
    case.threads[0].run()
    case.lifecycle.confirm_cleanup(ticket, independently_confirmed=True)
    reap(case, ticket)
    second = begin(case)
    assert second.epoch == ticket.epoch + 1
    refuses(Reason.STALE, lambda: getattr(case.lifecycle, action)(ticket, **kwargs))
    assert case.lifecycle.outstanding is second


@pytest.mark.parametrize("clock", [15.0, 100.0])
def test_deadline_discards_late_success_and_prevents_replacement(case, clock):
    ticket = begin(case)
    case.now = clock
    snapshot = case.lifecycle.poll(ticket)
    assert snapshot.phase is Phase.BLOCKED and snapshot.reason is Reason.DEADLINE
    case.threads[0].run()
    assert case.lifecycle.poll(ticket).phase is Phase.BLOCKED
    assert case.threads[0].is_alive()
    refuses(Reason.BUSY, lambda: begin(case))
    assert len(case.threads) == 1


@pytest.mark.parametrize("result", [False, True, 1, "raw credentials", object()])
def test_connection_callback_has_no_unbounded_result_transport(case, result):
    ticket = begin(case, connect=lambda: result)
    case.threads[0].run()
    snapshot = case.lifecycle.poll(ticket)
    assert snapshot.phase is Phase.BLOCKED and snapshot.reason is Reason.CONNECT_FAILED
    assert case.threads[0].is_alive()
    assert "raw credentials" not in repr(snapshot)


@pytest.mark.parametrize("exception", [RuntimeError("raw credentials"), SystemExit(7)])
def test_callback_exceptions_are_fixed_redacted_failure(case, exception):
    def fail():
        raise exception

    ticket = begin(case, connect=fail)
    case.threads[0].run()
    snapshot = case.lifecycle.poll(ticket)
    assert snapshot.reason is Reason.CONNECT_FAILED and "credentials" not in repr(snapshot)
    assert case.threads[0].is_alive()


@pytest.mark.parametrize("action", ["begin_cleanup", "confirm_cleanup"])
def test_cleanup_never_overlaps_an_uncertain_connect_callback(case, action):
    ticket = begin(case)
    case.now = 20
    case.lifecycle.poll(ticket)
    kwargs = (
        {"cleanup": lambda: True}
        if action == "begin_cleanup"
        else {"independently_confirmed": True}
    )
    refuses(Reason.BUSY, lambda: getattr(case.lifecycle, action)(ticket, **kwargs))
    assert len(case.threads) == 1 and case.lifecycle.outstanding is ticket


def test_exact_successful_cleanup_releases_epoch_without_calling_task_cleanup(case):
    ticket = begin(case)
    case.threads[0].run()
    case.lifecycle.poll(ticket)
    case.lifecycle.begin_cleanup(ticket, cleanup=lambda: True)
    assert len(case.threads) == 1
    assert case.lifecycle.poll(ticket).phase is Phase.CLEANING
    refuses(Reason.BUSY, lambda: case.lifecycle.begin_cleanup(ticket, cleanup=lambda: True))
    refuses(
        Reason.BUSY, lambda: case.lifecycle.confirm_cleanup(ticket, independently_confirmed=True)
    )
    case.threads[0].run()
    assert case.lifecycle.poll(ticket).phase is Phase.CLEANING
    reap(case, ticket)
    assert case.lifecycle.outstanding is None and case.calls == ["connect"]
    assert begin(case).epoch == 2


@pytest.mark.parametrize("result", [None, False, 1, "yes"])
def test_cleanup_requires_strict_independently_verified_true(case, result):
    ticket = begin(case)
    case.threads[0].run()
    case.lifecycle.begin_cleanup(ticket, cleanup=lambda: result)
    case.threads[0].run()
    assert case.lifecycle.poll(ticket).reason is Reason.CLEANUP_FAILED
    assert case.threads[0].is_alive() and len(case.threads) == 1
    refuses(
        Reason.CLEANUP_UNCONFIRMED,
        lambda: case.lifecycle.confirm_cleanup(ticket, independently_confirmed=result),
    )
    assert case.lifecycle.outstanding is ticket


def test_late_cleanup_stays_quarantined_until_independent_confirmation(case):
    ticket = begin(case)
    case.threads[0].run()
    case.lifecycle.begin_cleanup(ticket, cleanup=lambda: True, timeout_seconds=2)
    case.now = 12
    assert case.lifecycle.poll(ticket).reason is Reason.DEADLINE
    refuses(
        Reason.BUSY, lambda: case.lifecycle.confirm_cleanup(ticket, independently_confirmed=True)
    )
    case.threads[0].run()
    assert case.lifecycle.poll(ticket).phase is Phase.BLOCKED
    assert (
        case.lifecycle.confirm_cleanup(ticket, independently_confirmed=True).phase is Phase.CLEANING
    )
    reap(case, ticket)
    assert begin(case).epoch == 2


@pytest.mark.parametrize(
    "clock,reason",
    [
        (9, Reason.CLOCK_REGRESSION),
        (float("nan"), Reason.CLOCK_FAILED),
        (True, Reason.CLOCK_FAILED),
        (None, Reason.CLOCK_FAILED),
        (-1, Reason.CLOCK_FAILED),
    ],
)
def test_bad_clock_retains_ownership_but_cannot_prevent_exact_local_cleanup(case, clock, reason):
    ticket = begin(case)
    case.now = clock
    assert case.lifecycle.poll(ticket).reason is reason
    case.threads[0].run()
    case.lifecycle.begin_cleanup(ticket, cleanup=lambda: True)
    assert len(case.threads) == 1
    case.threads[0].run()
    assert case.lifecycle.poll(ticket).phase is Phase.BLOCKED
    case.lifecycle.confirm_cleanup(ticket, independently_confirmed=True)
    reap(case, ticket)
    refuses(reason, lambda: begin(case))
    case.now = 11
    assert begin(case).epoch == 2


def test_raising_clock_cannot_accept_completed_callback(case):
    ticket = begin(case)
    case.threads[0].run()
    case.lifecycle._monotonic = lambda: (_ for _ in ()).throw(RuntimeError("secret"))
    snapshot = case.lifecycle.poll(ticket)
    assert snapshot.reason is Reason.CLOCK_FAILED and "secret" not in repr(snapshot)
    case.lifecycle.confirm_cleanup(ticket, independently_confirmed=True)
    reap(case, ticket)
    assert case.lifecycle.outstanding is None


@pytest.mark.parametrize("phase", ["connect", "cleanup"])
def test_clock_is_rechecked_after_mailbox_read_before_success(case, phase):
    ticket = begin(case)
    case.threads[0].run()
    if phase == "cleanup":
        case.lifecycle.begin_cleanup(ticket, cleanup=lambda: True, timeout_seconds=5)
        case.threads[0].run()
    observations = iter([10.0, 15.0])
    case.lifecycle._monotonic = lambda: next(observations)
    snapshot = case.lifecycle.poll(ticket)
    assert snapshot.phase is Phase.BLOCKED and snapshot.reason is Reason.DEADLINE
    assert case.lifecycle.outstanding is ticket


def test_invalidation_discards_readiness_without_detaching_callback(case):
    ticket = begin(case)
    assert case.lifecycle.invalidate(ticket).reason is Reason.STALE
    case.threads[0].run()
    assert case.lifecycle.poll(ticket).phase is Phase.BLOCKED
    refuses(Reason.BUSY, lambda: begin(case))


def test_clock_cannot_reenter_and_retire_current_operation(case):
    ticket = begin(case)
    case.threads[0].run()

    def reenter():
        refuses(
            Reason.BUSY,
            lambda: case.lifecycle.confirm_cleanup(ticket, independently_confirmed=True),
        )
        return case.now

    case.lifecycle._monotonic = reenter
    assert case.lifecycle.poll(ticket).phase is Phase.CONNECTED
    assert case.lifecycle.outstanding is ticket


def test_parent_owner_identity_is_required(case, monkeypatch):
    ticket = begin(case)
    with monkeypatch.context() as wrong_owner:
        wrong_owner.setattr(module.threading, "get_ident", lambda: -1)
        refuses(Reason.WRONG_OWNER, lambda: case.lifecycle.poll(ticket))
    assert case.lifecycle.poll(ticket).phase is Phase.CONNECTING


@pytest.mark.parametrize(
    "changes",
    [
        {"configuration_digest": "secret"},
        {"configuration_digest": "sha256:" + "A" * 64},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": 601},
        {"timeout_seconds": float("inf")},
        {"connect": None},
    ],
)
def test_invalid_begin_refuses_before_any_thread(case, changes):
    arguments = {"configuration_digest": DIGEST, "connect": lambda: None}
    arguments.update(changes)
    refuses(Reason.INVALID, lambda: case.lifecycle.begin(**arguments))
    assert not case.threads and case.lifecycle.outstanding is None


def test_thread_start_failure_still_retains_exact_local_cleanup_ownership(case, monkeypatch):
    def fail(_thread):
        raise RuntimeError("raw credentials")

    monkeypatch.setattr(FakeThread, "start", fail)
    ticket = begin(case)
    assert case.lifecycle.poll(ticket).reason is Reason.CONNECT_FAILED
    refuses(Reason.BUSY, lambda: begin(case))
    case.lifecycle.confirm_cleanup(ticket, independently_confirmed=True)
    assert case.lifecycle.outstanding is None


def test_failed_cleanup_can_retry_on_same_creator_without_releasing_context(case):
    ticket = begin(case)
    case.threads[0].run()
    case.lifecycle.begin_cleanup(ticket, cleanup=lambda: False)
    case.threads[0].run()
    assert case.lifecycle.poll(ticket).reason is Reason.CLEANUP_FAILED
    refuses(Reason.BUSY, lambda: begin(case))
    case.lifecycle.begin_cleanup(ticket, cleanup=lambda: True)
    assert len(case.threads) == 1
    case.threads[0].run()
    reap(case, ticket)


def test_unexpected_creator_exit_invalidates_ready_context_and_requires_cleanup(case):
    ticket = begin(case)
    case.threads[0].run()
    assert case.lifecycle.poll(ticket).phase is Phase.CONNECTED
    case.threads[0].close()
    snapshot = case.lifecycle.poll(ticket)
    assert snapshot.phase is Phase.BLOCKED and snapshot.reason is Reason.CONNECT_FAILED
    refuses(Reason.BUSY, lambda: begin(case))
    case.lifecycle.begin_cleanup(ticket, cleanup=lambda: True)
    assert len(case.threads) == 2 and not case.threads[0].is_alive()
    case.threads[1].run()
    reap(case, ticket)


def test_dead_owner_cannot_replay_a_previously_queued_cleanup(case):
    ticket = begin(case)
    case.threads[0].run()

    def obsolete_cleanup():
        pytest.fail("The dead creator's queued callback cannot be replayed")

    case.lifecycle.begin_cleanup(ticket, cleanup=obsolete_cleanup)
    case.threads[0].close()
    assert case.lifecycle.poll(ticket).reason is Reason.CLEANUP_FAILED
    calls = []

    def current_cleanup():
        calls.append("current-cleanup")
        return True

    case.lifecycle.begin_cleanup(ticket, cleanup=current_cleanup)
    case.threads[1].run()
    reap(case, ticket)
    assert calls == ["current-cleanup"]


def test_confirmed_disconnect_keeps_exact_ticket_until_owner_reaped(case):
    ticket = begin(case)
    case.threads[0].run()
    snapshot = case.lifecycle.confirm_cleanup(ticket, independently_confirmed=True)
    assert snapshot.phase is Phase.CLEANING and snapshot.callback_running
    for _ in range(20):
        assert case.lifecycle.poll(ticket).phase is Phase.CLEANING
        refuses(Reason.BUSY, lambda: begin(case))
    refuses(
        Reason.BUSY, lambda: case.lifecycle.confirm_cleanup(ticket, independently_confirmed=True)
    )
    assert len(case.threads) == 1
    reap(case, ticket)


def test_real_connection_callback_preserves_process_context_and_parent_heartbeat(monkeypatch):
    entered, release, cleaning, cleaned = (Event() for _ in range(4))
    context, threads = {}, []

    def thread(**kwargs):
        instance = Thread(**kwargs)
        threads.append(instance)
        return instance

    def connect():
        context["connector_thread"] = get_ident()
        entered.set()
        if not release.wait(timeout=2):
            raise TimeoutError
        context["initialized"] = True

    def cleanup():
        context["cleanup_thread"] = get_ident()
        cleaning.set()
        if not cleaned.wait(timeout=2):
            raise TimeoutError
        context["initialized"] = False
        return True

    def wait_for(phase):
        deadline = monotonic() + 2
        snapshot = lifecycle.poll(ticket)
        while snapshot.phase is not phase and monotonic() < deadline:
            sleep(0.001)
            snapshot = lifecycle.poll(ticket)
        assert snapshot.phase is phase
        return snapshot

    monkeypatch.setattr(module, "Thread", thread)
    lifecycle = module.CoreConnectionLifecycle()
    ticket = lifecycle.begin(DIGEST, connect=connect, timeout_seconds=5)
    try:
        assert entered.wait(timeout=1)
        heartbeats = 0
        for _ in range(3):
            assert lifecycle.poll(ticket).phase is Phase.CONNECTING
            heartbeats += 1  # The parent can perform its ordinary lease heartbeat.
        assert heartbeats == 3 and context["connector_thread"] != get_ident()
        assert "initialized" not in context
        release.set()
        snapshot = wait_for(Phase.CONNECTED)
        assert not snapshot.callback_running and context["initialized"] is True
        assert len(threads) == 1 and threads[0].is_alive()
        lifecycle.begin_cleanup(ticket, cleanup=cleanup)
        assert cleaning.wait(timeout=1)
        assert context["cleanup_thread"] == context["connector_thread"]
        for _ in range(3):
            snapshot = lifecycle.poll(ticket)
            assert snapshot.phase is Phase.CLEANING and snapshot.callback_running
            refuses(Reason.BUSY, lambda: lifecycle.begin(DIGEST, connect=connect))
            heartbeats += 1
        assert heartbeats == 6 and context["initialized"] is True
        assert len(threads) == 1 and threads[0].is_alive()
        cleaned.set()
        wait_for(Phase.CLEANED)
        assert not threads[0].is_alive() and context["initialized"] is False
        assert lifecycle.outstanding is None
    finally:
        release.set()
        cleaned.set()
        deadline = monotonic() + 2
        while threads[0].is_alive() and monotonic() < deadline:
            if lifecycle.outstanding is ticket:
                snapshot = lifecycle.poll(ticket)
                if not snapshot.callback_running:
                    # Release only this resource-free test's simulated context.
                    context["initialized"] = False
                    lifecycle.confirm_cleanup(ticket, independently_confirmed=True)
            sleep(0.001)
        for instance in threads:
            instance.join(timeout=3)
            assert not instance.is_alive()
