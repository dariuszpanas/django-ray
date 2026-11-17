"""Exercise only the nested live observer with fake Ray and a fake clock."""

from __future__ import annotations

import ast
import copy
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest


class _GetTimeoutError(Exception):
    pass


class _RayTaskError(Exception):
    pass


class _ActorTimeoutError(_RayTaskError, _GetTimeoutError):
    """Model RayTaskError.as_instanceof_cause's dual inheritance."""


class _Clock:
    def __init__(self):
        self.nanoseconds = 0
        self.sleeps: list[tuple[float, float]] = []

    def monotonic(self) -> float:
        return self.nanoseconds / 1_000_000_000

    def advance(self, seconds: float) -> None:
        self.nanoseconds += round(seconds * 1_000_000_000)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append((self.monotonic(), seconds))
        self.advance(seconds)


type Action = Callable[[_Harness, object, float], object]
type Observer = Callable[[object, Callable[[object], bool]], object]


class _Harness:
    def __init__(self, actions: Sequence[Action] = (), *, repeat: Action | None = None):
        self.clock = _Clock()
        self.actions = deque(actions)
        self.repeat = repeat
        self.refs: list[object] = []
        self.pending: set[object] = set()
        self.max_pending = 0
        self.gets: list[tuple[object, float, float]] = []
        self.submission_error: Exception | None = None
        self.submission_delay = 0.0
        self.witness = SimpleNamespace(snapshot=SimpleNamespace(remote=self.remote))

    def remote(self) -> object:
        if self.submission_error is not None:
            raise self.submission_error
        self.clock.advance(self.submission_delay)
        ref = object()
        self.refs.append(ref)
        self.pending.add(ref)
        self.max_pending = max(self.max_pending, len(self.pending))
        return ref

    def get(self, ref: object, *, timeout: float) -> object:
        self.gets.append((ref, self.clock.monotonic(), timeout))
        action = self.actions.popleft() if self.actions else self.repeat
        assert action is not None, "unexpected extra snapshot read"
        result = action(self, ref, timeout)
        self.pending.remove(ref)
        return result

    def observe(self, predicate: Callable[[object], bool]) -> object:
        source = Path(__file__).parents[1] / "integration/test_live_failure_injection.py"
        module = ast.parse(source.read_text())
        cls = next(
            n
            for n in module.body
            if isinstance(n, ast.ClassDef) and n.name == "TestLiveFailureInjection"
        )
        method = next(
            n
            for n in cls.body
            if isinstance(n, ast.FunctionDef)
            and n.name == "test_cancellation_finalizes_live_pending_task"
        )
        observers = [
            n for n in method.body if isinstance(n, ast.FunctionDef) and n.name == "observe"
        ]
        assert len(observers) == 1
        selected = ast.Module(body=[copy.deepcopy(observers[0])], type_ignores=[])
        namespace = {
            "ray": SimpleNamespace(get=self.get),
            "time": SimpleNamespace(monotonic=self.clock.monotonic, sleep=self.clock.sleep),
            "GetTimeoutError": _GetTimeoutError,
            "RayTaskError": _RayTaskError,
        }
        exec(compile(selected, "<live-witness-observe>", "exec"), namespace)
        observer = cast(Observer, namespace["observe"])
        return observer(self.witness, predicate)


def _progress(state: object) -> bool:
    return cast(dict[str, int], state)["progress"] >= 2


def _state(value: object, *, elapsed: float = 0.0) -> Action:
    def action(harness: _Harness, ref: object, timeout: float) -> object:
        harness.clock.advance(elapsed)
        return value

    return action


def _error(error: Exception, *, elapsed: float = 0.0) -> Action:
    def action(harness: _Harness, ref: object, timeout: float) -> object:
        harness.clock.advance(elapsed)
        raise error

    return action


def _expire_get(harness: _Harness, ref: object, timeout: float) -> object:
    harness.clock.advance(timeout)
    raise _GetTimeoutError("snapshot wait expired")


def _assert_budget(harness: _Harness) -> None:
    assert all(0 < timeout <= min(2, 15 - started) + 1e-9 for _, started, timeout in harness.gets)
    assert all(
        0 < delay <= min(0.05, 15 - started) + 1e-9 for started, delay in harness.clock.sleeps
    )
    assert all(started < 15 for _, started, _ in harness.gets)


def test_ready_snapshot_returns_original_state():
    state = {"progress": 2}
    harness = _Harness([_state(state)])
    assert harness.observe(_progress) is state
    assert len(harness.refs) == len(harness.gets) == 1
    assert harness.gets[0][2] == 2
    assert harness.clock.sleeps == []


def test_completed_false_predicate_requests_new_snapshot():
    state = {"progress": 2}
    harness = _Harness([_state({"progress": 0}), _state(state)])
    assert harness.observe(_progress) is state
    assert len(harness.refs) == 2
    assert harness.gets[0][0] is not harness.gets[1][0]
    assert harness.max_pending == 1
    assert harness.clock.sleeps == [(0.0, 0.05)]


@pytest.mark.parametrize("timeouts", [1, 2, 3])
def test_wait_timeouts_reuse_pending_snapshot_until_ready(timeouts):
    state = {"progress": 2}
    harness = _Harness(
        [_error(_GetTimeoutError("not ready")) for _ in range(timeouts)] + [_state(state)]
    )
    assert harness.observe(_progress) is state
    assert len(harness.refs) == 1 and harness.max_pending == 1
    assert len(harness.gets) == timeouts + 1
    assert all(ref is harness.refs[0] for ref, _, _ in harness.gets)
    assert len(harness.clock.sleeps) == timeouts
    _assert_budget(harness)


def test_persistent_timeouts_preserve_original_deadline():
    harness = _Harness(repeat=_expire_get)
    with pytest.raises(AssertionError, match="Owned cancellation observation deadline expired"):
        harness.observe(_progress)
    assert harness.clock.monotonic() == 15
    assert len(harness.refs) == 1 and harness.max_pending == 1
    assert len(harness.gets) == 8
    assert harness.gets[-1][2] == pytest.approx(0.65)
    assert len(harness.clock.sleeps) == 7
    _assert_budget(harness)


def test_early_timeouts_poll_same_reference_without_hot_spin():
    harness = _Harness(repeat=_error(_GetTimeoutError("early timeout")))
    with pytest.raises(AssertionError, match="Owned cancellation observation deadline expired"):
        harness.observe(_progress)
    assert harness.clock.monotonic() == 15
    assert len(harness.gets) == len(harness.clock.sleeps) == 300
    assert len(harness.refs) == 1 and harness.max_pending == 1
    assert all(ref is harness.refs[0] for ref, _, _ in harness.gets)
    _assert_budget(harness)


def test_get_timeout_shrinks_to_remaining_budget():
    harness = _Harness([_state({"progress": 0}, elapsed=13.9), _state({"progress": 2})])
    assert harness.observe(_progress) == {"progress": 2}
    assert harness.gets[1][2] == pytest.approx(1.05)
    _assert_budget(harness)


def test_poll_sleep_shrinks_to_remaining_budget():
    harness = _Harness([_state({"progress": 0}, elapsed=14.98)])
    with pytest.raises(AssertionError, match="Owned cancellation observation deadline expired"):
        harness.observe(_progress)
    assert len(harness.gets) == len(harness.refs) == 1
    assert harness.clock.sleeps[0][1] == pytest.approx(0.02)
    assert harness.clock.monotonic() == 15
    _assert_budget(harness)


def test_late_completed_read_is_rejected_before_predicate():
    harness = _Harness([_state({"progress": 2}, elapsed=15)])
    seen = []

    def predicate(state):
        seen.append(state)
        return True

    with pytest.raises(AssertionError, match="Owned cancellation observation deadline expired"):
        harness.observe(predicate)
    assert seen == [] and harness.clock.sleeps == []
    assert len(harness.gets) == len(harness.refs) == 1


@pytest.mark.parametrize("matched", [False, True])
def test_late_predicate_result_is_rejected(matched):
    harness = _Harness([_state({"progress": 2})])

    def predicate(state):
        harness.clock.advance(15)
        return matched

    with pytest.raises(AssertionError, match="Owned cancellation observation deadline expired"):
        harness.observe(predicate)
    assert len(harness.gets) == len(harness.refs) == 1
    assert harness.clock.sleeps == []


def test_submission_consuming_deadline_starts_no_get():
    harness = _Harness([_state({"progress": 2})])
    harness.submission_delay = 15
    with pytest.raises(AssertionError, match="Owned cancellation observation deadline expired"):
        harness.observe(_progress)
    assert len(harness.refs) == 1 and harness.gets == [] and harness.clock.sleeps == []


def test_wait_timeout_consuming_deadline_starts_no_sleep_or_retry():
    harness = _Harness([_error(_GetTimeoutError("expired"), elapsed=15)])
    with pytest.raises(AssertionError, match="Owned cancellation observation deadline expired"):
        harness.observe(_progress)
    assert len(harness.refs) == len(harness.gets) == 1
    assert harness.clock.sleeps == []


@pytest.mark.parametrize(
    "error",
    [
        _RayTaskError("actor failure"),
        _ActorTimeoutError("remote timeout cause"),
        ConnectionError("transport"),
        ValueError("bad response"),
    ],
)
def test_non_wait_get_errors_preserve_identity_without_retry(error):
    harness = _Harness([_error(error)])
    with pytest.raises(type(error)) as caught:
        harness.observe(_progress)
    assert caught.value is error
    assert len(harness.gets) == len(harness.refs) == 1
    assert harness.clock.sleeps == []


@pytest.mark.parametrize(
    "error", [_GetTimeoutError("submission"), _RayTaskError("actor"), ConnectionError("transport")]
)
def test_submission_errors_are_not_wait_timeouts(error):
    harness = _Harness()
    harness.submission_error = error
    with pytest.raises(type(error)) as caught:
        harness.observe(_progress)
    assert caught.value is error
    assert harness.refs == harness.gets == [] and harness.clock.sleeps == []


@pytest.mark.parametrize(
    "error", [_GetTimeoutError("predicate"), _RayTaskError("predicate"), KeyError("missing")]
)
def test_predicate_errors_are_not_wait_timeouts(error):
    harness = _Harness([_state({"progress": 2})])

    def predicate(state):
        raise error

    with pytest.raises(type(error)) as caught:
        harness.observe(predicate)
    assert caught.value is error
    assert len(harness.gets) == len(harness.refs) == 1
    assert harness.clock.sleeps == []


@pytest.mark.parametrize(
    "state,exception", [(None, TypeError), ({}, KeyError), ({"progress": "bad"}, TypeError)]
)
def test_malformed_states_remain_visible(state, exception):
    harness = _Harness([_state(state)])
    with pytest.raises(exception):
        harness.observe(_progress)
    assert len(harness.gets) == len(harness.refs) == 1
    assert harness.clock.sleeps == []
