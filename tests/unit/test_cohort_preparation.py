"""Owned preparation stays responsive without passing ORM state to callbacks."""

from dataclasses import asdict, replace
from datetime import timedelta
from threading import Event, Thread, get_ident
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from django_ray.runner import cohort_dispatch as dispatch
from django_ray.runner import cohort_preparation as module
from django_ray.runner.cohort_claims import ClaimedCohortTask
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.runtime.runtime_env import normalize_runtime_env
from django_ray.target.cohort_claim import (
    CohortClaimDisposition,
    cohort_claim_facts_digest,
    cohort_task_runtime_env_snapshot_digest,
)
from django_ray.target.cohort_claim_storage import CohortClaimRecord
from tests.unit.test_cohort_claim import NOW, facts

Reason = module.CohortPreparationReason


class FakeThread:
    def __init__(self, *, target, args, name, daemon):
        self.target, self.args, self.name, self.daemon = target, args, name, daemon
        self.alive = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def run(self):
        assert self.alive
        try:
            self.target(*self.args)
        finally:
            self.alive = False

    def join(self, *args, **kwargs):
        pytest.fail("parent must never join a preparation callback")


def _source():
    environment = normalize_runtime_env({})
    value = replace(
        facts(),
        runtime_env_profile=environment.profile,
        runtime_env_hash=environment.digest,
        runtime_env_snapshot_digest=cohort_task_runtime_env_snapshot_digest(
            profile=environment.profile,
            serialized=environment.serialized,
            digest=environment.digest,
        ),
    )
    owner = WorkerLeaseIdentity(
        value.worker_lease_id,
        value.worker_lease_hostname,
        value.worker_lease_pid,
        value.worker_lease_started_at,
    )
    record = CohortClaimRecord(
        1,
        value,
        cohort_claim_facts_digest(value),
        1,
        CohortClaimDisposition.OPEN,
        owner,
        None,
        None,
    )
    task = dispatch._PreparationTask(
        1,
        "task",
        1,
        1,
        3,
        "RUNNING",
        owner.worker_id,
        "0.5.0",
        "tests.tasks.add",
        "[]",
        "{}",
        None,
        environment.profile,
        environment.serialized,
        environment.digest,
    )
    return dispatch.CohortPreparationInput(
        task,
        record,
        dispatch._contract(record),
        None,
        environment.profile,
        environment.serialized,
        environment.digest,
        "{}",
    )


@pytest.fixture
def state(monkeypatch):
    source = _source()
    prepared = dispatch.prepare_captured_cohort_execution(source)
    value = SimpleNamespace(
        wall=NOW,
        mono=10.0,
        threads=[],
        captures=[],
        preparations=[],
        commits=[],
        source=source,
        prepared=prepared,
        claimed=ClaimedCohortTask(SimpleNamespace(**asdict(source.task)), source.claim, None),
    )

    def thread(**kwargs):
        instance = FakeThread(**kwargs)
        value.threads.append(instance)
        return instance

    def capture(claimed, *, transport=None):
        value.captures.append((get_ident(), claimed))
        return replace(
            source,
            claim=claimed.claim,
            task=dispatch._preparation_task(claimed.execution),
            transport=transport,
        )

    def prepare(captured):
        value.preparations.append(captured)
        return prepared

    def commit(claimed, captured, result, *, now=None):
        value.commits.append((get_ident(), claimed, captured, result, now))
        return dispatch.PreparedCohortDispatch(
            claimed.execution, replace(claimed.claim, revision=2), result
        )

    monkeypatch.setattr(module, "Thread", thread)
    monkeypatch.setattr(module, "_clock", lambda: (value.wall, value.mono))
    # These callback-controller cases own no database. Retained test-runner
    # connections from preceding transactional modules are not their context.
    monkeypatch.setattr(module.connections, "all", lambda **kwargs: [])
    monkeypatch.setattr(dispatch, "capture_claimed_cohort_preparation", capture)
    monkeypatch.setattr(dispatch, "prepare_captured_cohort_execution", prepare)
    monkeypatch.setattr(dispatch, "commit_claimed_cohort_preparation", commit)
    value.controller = module.CohortPreparationController(max_pending=2)
    return value


def _refuses(reason, callback):
    with pytest.raises(module.CohortPreparationError) as caught:
        callback()
    assert caught.value.reason is reason
    assert "private" not in str(caught.value)


def _begin(state, **kwargs):
    return state.controller.begin(state.claimed, **kwargs)


def _other(state):
    identity = replace(state.claimed.claim.facts.identity, task_execution_pk=2, task_id="other")
    record = replace(
        state.claimed.claim, claim_id=2, facts=replace(state.source.claim.facts, identity=identity)
    )
    task = SimpleNamespace(**(asdict(state.source.task) | {"pk": 2, "task_id": "other"}))
    return ClaimedCohortTask(task, record, None)


def test_capture_precedes_thread_and_parent_commit_is_once(state):
    ticket = _begin(state)
    assert (
        ticket.claim is state.claimed.claim and ticket.identity == state.source.claim.facts.identity
    )
    assert len(state.captures) == 1 and state.preparations == []
    for _ in range(4):
        result = state.controller.poll(ticket)
        assert result.stage == "preparing" and not result.local_cleanup_complete
    assert state.commits == []
    _refuses(Reason.BUSY, lambda: state.controller.commit(ticket))
    state.threads[0].run()
    assert state.controller.poll(ticket).stage == "ready"
    assert state.threads[0].args[0] is state.preparations[0]
    assert type(state.threads[0].args[0]) is dispatch.CohortPreparationInput
    result = state.controller.commit(ticket, now=state.wall)
    assert result.claim.revision == 2
    assert state.commits[0][0] == state.captures[0][0] == get_ident()
    _refuses(Reason.BUSY, lambda: state.controller.commit(ticket))
    assert len(state.commits) == 1


def test_bounded_retained_identities_one_active_and_idempotent_begin(state):
    first = _begin(state)
    assert _begin(state) is first and len(state.captures) == 1
    assert state.controller.begin(_other(state)) is None
    assert len(state.captures) == 1 and state.controller.busy
    state.threads[0].run()
    second = state.controller.begin(_other(state))
    assert second is not None and state.controller.pending_count == 2
    state.controller.abort(first)
    state.controller.retire(first)
    _refuses(Reason.STALE, lambda: state.controller.poll(first))
    assert state.controller.retire(second) is False
    state.threads[1].run()
    state.controller.abort(second)
    assert state.controller.retire(second) is True
    assert state.controller.pending_count == 0


def test_completed_tickets_still_count_toward_finite_retention(state):
    first = _begin(state)
    state.threads[0].run()
    second = state.controller.begin(_other(state))
    state.threads[1].run()
    assert not state.controller.busy and state.controller.pending_count == 2
    third = replace(
        _other(state),
        claim=replace(
            _other(state).claim,
            facts=replace(
                _other(state).claim.facts,
                identity=replace(second.identity, task_execution_pk=3, task_id="third"),
            ),
        ),
    )
    assert state.controller.begin(third) is None
    assert len(state.captures) == 2
    assert state.controller.retire(first) is True


@pytest.mark.parametrize("identity", [None, [], {"task_id": "private"}])
def test_invalid_identity_is_bounded_refusal_before_capture(state, identity):
    claimed = replace(
        state.claimed,
        claim=replace(
            state.claimed.claim, facts=replace(state.claimed.claim.facts, identity=identity)
        ),
    )
    _refuses(Reason.INVALID, lambda: state.controller.begin(claimed))
    assert state.captures == []


@pytest.mark.parametrize("action", ["poll", "commit", "abort", "retire"])
def test_forged_equal_ticket_is_not_owned(state, action):
    ticket = _begin(state)
    _refuses(Reason.STALE, lambda: getattr(state.controller, action)(replace(ticket)))
    assert state.controller.busy


@pytest.mark.parametrize("change", ["revision", "transport"])
def test_duplicate_identity_cannot_cross_original_claim_or_transport(state, change):
    _begin(state)
    claimed, transport = state.claimed, None
    if change == "revision":
        claimed = replace(claimed, claim=replace(claimed.claim, revision=2))
    else:
        transport = "ray-job"
    _refuses(Reason.STALE, lambda: state.controller.begin(claimed, transport=transport))
    assert len(state.threads) == 1


@pytest.mark.parametrize("change", ["deadline", "wall_regression", "mono_regression", "clock"])
def test_failure_is_sticky_and_retirement_requires_actual_exit(state, change):
    ticket = _begin(state, timeout_seconds=5)
    if change == "deadline":
        state.mono += 5
        reason = Reason.DEADLINE
    elif change == "wall_regression":
        state.wall -= timedelta(microseconds=1)
        reason = Reason.CLOCK_REGRESSION
    elif change == "mono_regression":
        state.mono -= 1
        reason = Reason.CLOCK_REGRESSION
    else:
        state.mono = float("nan")
        reason = Reason.CLOCK_FAILED
    assert state.controller.poll(ticket).reason is reason
    state.wall, state.mono = NOW, 11.0
    assert state.controller.retire(ticket) is False
    state.threads[0].run()
    assert state.controller.poll(ticket).stage == "blocked"
    _refuses(reason, lambda: state.controller.commit(ticket))
    state.controller.retire(ticket)
    assert not state.controller.busy and state.commits == []


def test_abort_discards_mailbox_success_without_replaying(state):
    ticket = _begin(state)
    state.controller.abort(ticket)
    state.threads[0].run()
    assert state.controller.poll(ticket).reason is Reason.ABORTED
    assert _begin(state) is ticket
    _refuses(Reason.ABORTED, lambda: state.controller.commit(ticket))
    assert len(state.threads) == 1


@pytest.mark.parametrize("started", [False, True])
def test_start_exception_retains_any_started_callback(state, monkeypatch, started):
    def start(thread):
        thread.alive = started
        raise RuntimeError("private thread error")

    monkeypatch.setattr(FakeThread, "start", start)
    ticket = _begin(state)
    assert state.controller.poll(ticket).reason is Reason.START_FAILED
    if started:
        assert state.controller.retire(ticket) is False
        state.threads[0].run()
    state.controller.retire(ticket)
    assert state.commits == []


@pytest.mark.parametrize("bad", [None, "private result", SystemExit("private failure")])
def test_callback_failure_never_transports_diagnostics_or_accepts_untyped_result(
    state, monkeypatch, bad
):
    def prepare(source):
        if isinstance(bad, BaseException):
            raise bad
        return bad

    monkeypatch.setattr(dispatch, "prepare_captured_cohort_execution", prepare)
    ticket = _begin(state)
    state.threads[0].run()
    result = state.controller.poll(ticket)
    assert result.reason is Reason.PREPARATION_FAILED and "private" not in repr(result)


def test_post_exit_clock_check_discards_late_positive_mailbox(state, monkeypatch):
    ticket = _begin(state, timeout_seconds=2)
    state.threads[0].run()
    mailbox = state.controller._current(ticket).mailbox
    get = mailbox.get_nowait

    def delayed_read():
        value = get()
        state.mono += 2
        return value

    monkeypatch.setattr(mailbox, "get_nowait", delayed_read)
    assert state.controller.poll(ticket).reason is Reason.DEADLINE
    assert state.commits == []


def test_capture_crossing_deadline_does_not_start_callback(state, monkeypatch):
    capture = dispatch.capture_claimed_cohort_preparation

    def slow_capture(*args, **kwargs):
        value = capture(*args, **kwargs)
        state.mono += 30
        return value

    monkeypatch.setattr(dispatch, "capture_claimed_cohort_preparation", slow_capture)
    ticket = _begin(state)
    assert state.threads == []
    assert state.controller.poll(ticket).reason is Reason.DEADLINE
    assert state.controller.retire(ticket) is True


def test_capture_exception_has_no_callback_or_retained_identity(state, monkeypatch):
    def capture(*args, **kwargs):
        raise RuntimeError("private capture diagnostics")

    monkeypatch.setattr(dispatch, "capture_claimed_cohort_preparation", capture)
    _refuses(Reason.CAPTURE_FAILED, lambda: _begin(state))
    assert state.threads == [] and state.controller.pending_count == 0


def test_unknown_liveness_never_allows_replacement_or_retirement(state, monkeypatch):
    ticket = _begin(state)

    def unknown():
        raise RuntimeError("private local ownership")

    monkeypatch.setattr(state.threads[0], "is_alive", unknown)
    assert state.controller.abort(ticket).local_cleanup_complete is False
    assert state.controller.retire(ticket) is False
    assert state.controller.begin(_other(state)) is None


def test_ready_result_must_still_be_fresh_when_parent_commits(state):
    ticket = _begin(state)
    state.threads[0].run()
    assert state.controller.poll(ticket).stage == "ready"
    state.wall += timedelta(seconds=31)
    _refuses(Reason.DEADLINE, lambda: state.controller.commit(ticket))
    assert state.commits == []


def test_successful_database_commit_returns_revision_even_after_deadline(state, monkeypatch):
    original = dispatch.commit_claimed_cohort_preparation

    def slow_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        state.mono += 100
        return result

    monkeypatch.setattr(dispatch, "commit_claimed_cohort_preparation", slow_commit)
    ticket = _begin(state)
    state.threads[0].run()
    result = state.controller.commit(ticket)
    assert result.claim.revision == 2
    assert state.controller.poll(ticket).stage == "committed"
    assert state.controller.abort(ticket).stage == "committed"
    assert state.controller._current(ticket).committed is result


def test_commit_exception_is_sticky_and_never_retries_the_database(state, monkeypatch):
    calls = []

    def refused(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("private changed claim")

    monkeypatch.setattr(dispatch, "commit_claimed_cohort_preparation", refused)
    ticket = _begin(state)
    state.threads[0].run()
    _refuses(Reason.COMMIT_FAILED, lambda: state.controller.commit(ticket))
    _refuses(Reason.COMMIT_FAILED, lambda: state.controller.commit(ticket))
    assert calls == [1]


@pytest.mark.parametrize("active,autocommit", [(True, True), (False, False)])
def test_parent_entry_refuses_transactions_before_capture(state, monkeypatch, active, autocommit):
    connection = SimpleNamespace(in_atomic_block=active, connection=object(), autocommit=autocommit)
    monkeypatch.setattr(module.connections, "all", lambda **kwargs: [connection])
    _refuses(Reason.TRANSACTION, lambda: _begin(state))
    assert state.captures == [] and state.threads == []


def test_polling_an_initialized_connection_never_reconnects(state, monkeypatch):
    ticket = _begin(state)

    def forbidden_io():
        pytest.fail("local polling must not ensure or open a database connection")

    connection = SimpleNamespace(
        in_atomic_block=False,
        connection=object(),
        autocommit=True,
        get_autocommit=forbidden_io,
        ensure_connection=forbidden_io,
    )

    def initialized_connections(*, initialized_only):
        assert initialized_only is True
        return [connection]

    monkeypatch.setattr(module.connections, "all", initialized_connections)
    dispatch._outside_transaction()
    assert state.controller.poll(ticket).stage == "preparing"
    assert state.controller.busy and state.controller.pending_count == 1
    state.threads[0].run()
    assert state.controller.poll(ticket).stage == "ready"
    assert state.controller.retire(ticket) is True


@pytest.mark.parametrize("value", [False, 0, -1, 101, 1.0])
def test_retained_capacity_is_strict_and_bounded(value):
    _refuses(Reason.INVALID, lambda: module.CohortPreparationController(max_pending=value))


@pytest.mark.parametrize("value", [False, 0, -1, 61, float("nan"), float("inf")])
def test_callback_deadline_is_strict_and_bounded(state, value):
    _refuses(Reason.INVALID, lambda: _begin(state, timeout_seconds=value))
    assert state.captures == [] and state.threads == []


def test_other_thread_and_reentrant_parent_cannot_control_ticket(state, monkeypatch):
    ticket = _begin(state)
    original_owner = state.controller._parent_thread
    state.controller._parent_thread = original_owner + 1
    try:
        _refuses(Reason.WRONG_OWNER, lambda: state.controller.abort(ticket))
    finally:
        state.controller._parent_thread = original_owner
    state.controller._accessing = True
    try:
        _refuses(Reason.BUSY, lambda: state.controller.abort(ticket))
    finally:
        state.controller._accessing = False


def test_real_blocked_callback_keeps_parent_heartbeat_and_immutable_inputs(state, monkeypatch):
    entered, release = Event(), Event()
    callbacks = []
    monkeypatch.setattr(module, "Thread", Thread)

    def prepare(source):
        callbacks.append((get_ident(), source))
        entered.set()
        assert release.wait(2)
        return state.prepared

    monkeypatch.setattr(dispatch, "prepare_captured_cohort_execution", prepare)
    ticket = _begin(state)
    try:
        assert entered.wait(1)
        state.claimed.execution.args_json = '["private mutation"]'
        heartbeats = []
        for _ in range(4):
            assert state.controller.poll(ticket).stage == "preparing"
            heartbeats.append(get_ident())
        assert heartbeats == [get_ident()] * 4
        assert callbacks[0][0] != get_ident()
        assert callbacks[0][1].task.args_json == "[]"
        assert callbacks[0][1].task is not state.claimed.execution
        assert "private mutation" not in repr(ticket)
    finally:
        release.set()
        deadline = monotonic() + 2
        while state.controller.busy:
            assert monotonic() < deadline
            sleep(0.005)
        state.controller.abort(ticket)
        state.controller.retire(ticket)
