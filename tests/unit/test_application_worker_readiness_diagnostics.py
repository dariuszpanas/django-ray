"""Resource-free readiness diagnostics; all process, descriptor and ORM seams are fake."""

import json
import subprocess
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qualification.application import protected_log_diagnostics as diagnostics
from qualification.application import protected_logs as logs
from qualification.application import worker_readiness_diagnostics as readiness
from tests.unit.test_application_protected_surfaces import IMAGE, host_fixture


def report(status="ready", reason="live_lease"):
    return {"schema_version": 1, "scope": "worker_lease", "status": status, "reason": reason}


def progress(status="ready", reason="live_lease"):
    return b"".join(readiness.MILESTONES) + readiness._report_frame(status, reason)


@pytest.mark.parametrize(
    "status,reason,code",
    [
        ("ready", "live_lease", 0),
        ("not_ready", "ambiguous_lease", 1),
        ("not_ready", "no_live_lease", 1),
        ("error", "lease_policy_mismatch", 2),
    ],
)
def test_exact_worker_report_requires_matching_exit(status, reason, code):
    raw = json.dumps(report(status, reason)).encode()
    assert readiness.parse(raw, code) == {"status": status, "reason": reason}
    assert readiness.parse(raw, (code + 1) % 3) is None
    assert readiness.parse(raw, bool(code)) is None


@pytest.mark.parametrize(
    "change", ["extra", "duplicate", "reason", "shape", "bool", "huge", "utf8", "trailing"]
)
def test_private_or_malformed_worker_output_is_never_retained(change):
    value = report()
    if change == "extra":
        value["private"] = "private-canary"
    elif change == "reason":
        value["reason"] = "private-canary"
    elif change == "shape":
        value["status"] = {"private": "private-canary"}
    elif change == "bool":
        value["schema_version"] = True
    raw = json.dumps(value).encode()
    if change == "duplicate":
        raw = raw[:-1] + b',"reason":"private-canary"}'
    elif change == "huge":
        raw += b" " * readiness.MAX_BYTES
    elif change == "utf8":
        raw = b"\xffprivate-canary"
    elif change == "trailing":
        raw += b"private-canary"
    assert readiness.parse(raw, 0) is None


@pytest.mark.parametrize(
    "field", ["outcome", "phase", "status", "reason", "exit_code", "elapsed_ms"]
)
@pytest.mark.parametrize("invalid", [{"private": "canary"}, ["private-canary"], True])
def test_nested_or_boolean_diagnostic_fields_are_rejected_without_raising(field, invalid):
    for outcome in ("observed", "incomplete"):
        value = readiness.empty(outcome)
        if outcome == "observed":
            value.update(phase="complete", status="ready", reason="live_lease", exit_code=0)
        value[field] = invalid
        assert not readiness.valid(value)
    for field in ("status", "reason"):
        value = report()
        value[field] = invalid
        assert readiness.parse(json.dumps(value).encode(), 0) is None
    assert readiness.parse(json.dumps(report()).encode(), invalid) is None


def test_every_readiness_reason_fits_existing_failure_output_ceiling():
    for status, reasons in readiness.REASONS.items():
        for reason in reasons:
            value = readiness.empty("observed")
            value.update(
                phase="complete",
                status=status,
                reason=reason,
                exit_code=readiness.EXITS[status],
                elapsed_ms=10000,
            )
            failure = diagnostics._record(
                "host", "final_inventory", "invalid", None, "manager", "container_not_ready", value
            )
            assert diagnostics._valid(failure, "host")
            assert (
                len(json.dumps(failure, sort_keys=True, separators=(",", ":")).encode()) + 1
                <= diagnostics.MAX_BYTES
            )


def observe(**changes):
    return readiness.observe(
        prefix=["kubectl", "--context", "owned", "-n", "owned"],
        pod="private-manager",
        environment={"KUBECONFIG": "private-config"},
        **{"deadline": 40, **changes},
    )


@pytest.mark.parametrize("platform,deadline", [("win32", 40), ("darwin", 40), ("linux", 13.9)])
def test_unsupported_or_insufficient_budget_never_spawns(monkeypatch, platform, deadline):
    monkeypatch.setattr(readiness.sys, "platform", platform)
    monkeypatch.setattr(readiness.time, "monotonic", lambda: 10)
    capture = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(readiness, "_capture", capture)
    assert observe(deadline=deadline) == readiness.empty()
    capture.assert_not_called()


@pytest.mark.parametrize(
    "raw,code,outcome,status",
    [
        (progress(), 0, "observed", "ready"),
        (progress("not_ready", "no_live_lease"), 1, "observed", "not_ready"),
        (progress(), 2, "incomplete", "unknown"),
        (b"private-canary", 3, "error", "unknown"),
        (b"", 127, "incomplete", "unknown"),
        (b"".join(readiness.MILESTONES), 137, "timeout", "unknown"),
    ],
)
def test_one_observation_clips_deadline_and_retains_only_fixed_facts(
    monkeypatch, raw, code, outcome, status
):
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    times = iter([10, 11.25])
    monkeypatch.setattr(readiness.time, "monotonic", lambda: next(times))
    capture = Mock(return_value=(raw, code))
    monkeypatch.setattr(readiness, "_capture", capture)
    value = observe(deadline=17)
    assert value["outcome"] == outcome
    assert value["status"] == status
    assert value["elapsed_ms"] == 1250
    assert readiness.valid(value)
    assert "private" not in json.dumps(value)
    argv = capture.call_args.args[0]
    assert argv[-6:] == [
        "timeout",
        "--signal=KILL",
        "4s",
        "python",
        "-m",
        "qualification.application.worker_readiness_diagnostics",
    ]
    assert capture.call_args.kwargs["deadline"] == 16
    capture.assert_called_once()


@pytest.mark.parametrize(
    "error,outcome",
    [
        (TimeoutError("private"), "timeout"),
        (OSError("private"), "error"),
        (ValueError("private"), "error"),
    ],
)
def test_capture_errors_cannot_leak_text(monkeypatch, error, outcome):
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    monkeypatch.setattr(readiness.time, "monotonic", lambda: 10)
    monkeypatch.setattr(readiness, "_capture", Mock(side_effect=error))
    assert observe() == readiness.empty(outcome)


@pytest.mark.parametrize("phase", ["initial", "final"])
@pytest.mark.parametrize("outcome", ["ready", "timeout", "raises", "forged"])
def test_unready_manager_remains_failed_with_no_subsequent_log_read(monkeypatch, phase, outcome):
    underlying, calls = host_fixture()
    inventories = 0

    def run(argv, **kwargs):
        nonlocal inventories
        result = underlying(argv, **kwargs)
        if "get" in argv:
            inventories += 1
            if inventories == (1 if phase == "initial" else 2):
                value = json.loads(result.stdout)
                value["items"][1]["status"]["containerStatuses"][0]["ready"] = False
                return subprocess.CompletedProcess(argv, 0, json.dumps(value).encode(), b"")
        return result

    value = readiness.empty("timeout" if outcome == "timeout" else "incomplete")
    if outcome == "ready":
        value.update(
            outcome="observed", phase="complete", status="ready", reason="live_lease", exit_code=0
        )
    elif outcome == "forged":
        value["reason"] = "private-canary"
    probe = Mock(
        return_value=value, side_effect=OSError("private-canary") if outcome == "raises" else None
    )
    monkeypatch.setattr(readiness, "observe", probe)
    with pytest.raises(ValueError, match="source is not live and ready") as caught:
        logs.observe(
            context="owned",
            kubeconfig_paths="private",
            namespace="owned",
            image=IMAGE,
            generation="before",
            run=run,
        )
    recorded = json.loads(diagnostics.diagnostic(caught.value, "host"))
    assert recorded["failure_phase"] == ("inventory" if phase == "initial" else "final_inventory")
    assert recorded["inventory_reason"] == "container_not_ready"
    assert recorded["source_role"] == "manager"
    assert recorded["status"] == "failed"
    assert "private-canary" not in json.dumps(recorded)
    assert len(json.dumps(recorded, separators=(",", ":")).encode()) + 1 <= diagnostics.MAX_BYTES
    assert len(calls) == (1 if phase == "initial" else 7)
    probe.assert_called_once()


@pytest.mark.parametrize("failure", [None, "overflow", "timeout", "reaped", "wait-timeout"])
def test_capture_caps_actual_reads_and_signals_before_reaping(monkeypatch, failure):
    events = []
    stream = SimpleNamespace(fileno=lambda: 77, close=lambda: events.append("close"))

    def wait(**kwargs):
        events.append("wait")
        assert 0 <= kwargs["timeout"] <= 1
        if failure == "wait-timeout":
            raise subprocess.TimeoutExpired("private-canary", 1)

    process = SimpleNamespace(stdout=stream, pid=123, returncode=None, wait=wait)

    class Selector:
        active = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def register(self, *args):
            pass

        def unregister(self, *args):
            self.active = False

        def get_map(self):
            return {77: True} if self.active else {}

        def select(self, timeout):
            return [(SimpleNamespace(fd=77, fileobj=stream), None)]

    chunks = iter([b"x" * 513] if failure == "overflow" else [b"{}", b""])
    sizes = []

    def read(fd, size):
        sizes.append(size)
        return next(chunks)

    monkeypatch.setattr(readiness.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(readiness.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(readiness.os, "set_blocking", lambda *args: None)
    monkeypatch.setattr(readiness.os, "read", read)
    monkeypatch.setattr(
        readiness.os, "killpg", lambda *args: events.append("signal"), raising=False
    )
    monkeypatch.setattr(readiness.signal, "SIGKILL", 9, raising=False)

    def peek(process):
        events.append("peek")
        if failure == "reaped":
            raise ChildProcessError("private-canary")
        return 0

    monkeypatch.setattr(readiness, "_peek", peek)
    monkeypatch.setattr(readiness.time, "monotonic", lambda: 9 if failure == "timeout" else 0)
    if failure is None:
        assert readiness._capture(["kubectl"], environment={}, deadline=10) == (b"{}", 0)
    else:
        with pytest.raises(
            (ValueError, TimeoutError, ChildProcessError, subprocess.TimeoutExpired)
        ):
            readiness._capture(["kubectl"], environment={}, deadline=10)
    assert all(size <= 513 for size in sizes)
    if failure == "reaped":
        assert "signal" not in events
        assert "wait" not in events
    else:
        assert events[-4:] == ["peek", "signal", "wait", "close"]
    kwargs = readiness.subprocess.Popen.call_args.kwargs
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True


@pytest.mark.parametrize("status,expected", [(None, None), ((1, 7), 7), ((2, 9), -9)])
def test_peek_observes_exit_without_reaping(monkeypatch, status, expected):
    for name, value in {
        "P_PID": 1,
        "WEXITED": 4,
        "WNOHANG": 1,
        "WNOWAIT": 16,
        "CLD_EXITED": 1,
    }.items():
        monkeypatch.setattr(readiness.os, name, value, raising=False)
    observed = None if status is None else SimpleNamespace(si_code=status[0], si_status=status[1])
    waitid = Mock(return_value=observed)
    monkeypatch.setattr(readiness.os, "waitid", waitid, raising=False)
    process = SimpleNamespace(returncode=None, pid=123)
    assert readiness._peek(process) == expected
    waitid.assert_called_once_with(1, 123, 4 | 1 | 16)


def test_peek_refuses_already_reaped_process_without_touching_numeric_pid(monkeypatch):
    waitid = Mock(side_effect=AssertionError("must not inspect a reused pid"))
    monkeypatch.setattr(readiness.os, "waitid", waitid, raising=False)
    with pytest.raises(ChildProcessError):
        readiness._peek(SimpleNamespace(returncode=0, pid=123))
    waitid.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_private_child_suppresses_descriptors_before_django_and_only_emits_fixed_json(
    monkeypatch, failure
):
    events = []
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr(readiness.os, "dup", lambda fd: 9)
    monkeypatch.setattr(readiness.os, "dup2", lambda source, target: events.append(target))
    monkeypatch.setattr(readiness.os, "close", lambda fd: events.append("close"))
    output = []
    monkeypatch.setattr(readiness.os, "write", lambda fd, raw: (output.append(raw), len(raw))[1])
    monkeypatch.setattr(
        "builtins.open", lambda *args: nullcontext(SimpleNamespace(fileno=lambda: 7))
    )

    def command(checkpoint):
        assert events == [1, 2]
        assert output == [readiness.MILESTONES[0]]
        if failure:
            raise RuntimeError("private-canary")
        checkpoint("django_setup")
        checkpoint("worker_ready")
        return json.dumps(report("not_ready", "no_live_lease")).encode(), 1

    monkeypatch.setattr(readiness, "_command", command)
    assert readiness.main() == (3 if failure else 1)
    assert output == (
        [readiness.MILESTONES[0]]
        if failure
        else [
            *readiness.MILESTONES,
            b'{"schema_version":1,"scope":"worker_lease","status":"not_ready","reason":"no_live_lease"}\n',
        ]
    )
    assert events[-1] == "close"


@pytest.mark.parametrize(
    "platform,settings",
    [
        ("win32", "testproject.settings_qualification"),
        ("darwin", "testproject.settings_qualification"),
        ("linux", "private-settings"),
    ],
)
def test_private_child_rejects_wrong_platform_or_settings_before_effects(
    monkeypatch, platform, settings
):
    monkeypatch.setattr(readiness.sys, "platform", platform)
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", settings)
    duplicate = Mock(side_effect=AssertionError("must not change descriptors"))
    command = Mock(side_effect=AssertionError("must not initialize Django"))
    monkeypatch.setattr(readiness.os, "dup", duplicate)
    monkeypatch.setattr(readiness, "_command", command)
    assert readiness.main() == 3
    duplicate.assert_not_called()
    command.assert_not_called()


def test_child_capture_refuses_oversized_command_output():
    output = readiness._Output()
    output.write("x" * readiness.MAX_BYTES)
    with pytest.raises(ValueError):
        output.write("x")
    assert len(output.getvalue()) == readiness.MAX_BYTES


@pytest.mark.parametrize(
    "status,reason,code",
    [
        ("ready", "live_lease", 0),
        ("not_ready", "no_live_lease", 1),
        ("error", "lease_policy_invalid", 2),
    ],
)
def test_child_calls_existing_command_with_exact_fixture_coordinates(
    monkeypatch, status, reason, code
):
    import django
    from django.core import management

    monkeypatch.setattr(django, "setup", Mock())
    monkeypatch.setenv("DJANGO_RAY_QUEUES", "default")
    monkeypatch.setenv("DJANGO_RAY_POD_NAME", "private-manager")

    def call_command(name, **kwargs):
        assert name == "django_ray_worker_ready"
        assert kwargs["queue"] == "default"
        assert kwargs["hostname"] == "private-manager"
        assert kwargs["as_json"] is True
        kwargs["stdout"].write(json.dumps(report(status, reason)))
        if code:
            raise management.CommandError("private-canary", returncode=code)

    monkeypatch.setattr(management, "call_command", call_command)
    checkpoints = []
    raw, exit_code = readiness._command(checkpoints.append)
    assert checkpoints == ["django_setup", "worker_ready"]
    assert readiness.parse(raw, exit_code) == {"status": status, "reason": reason}
    django.setup.assert_called_once()


@pytest.mark.parametrize(
    "status,reason",
    [(status, reason) for status, reasons in readiness.REASONS.items() for reason in reasons],
)
def test_every_fixed_stream_and_interrupted_byte_prefix_has_only_completed_checkpoints(
    status, reason
):
    raw = progress(status, reason)
    assert len(raw) <= readiness.MAX_BYTES
    ends = [len(b"".join(readiness.MILESTONES[:index])) for index in range(1, 4)]
    for cut in range(len(raw)):
        phase, result = readiness._progress(raw[:cut], interrupted=True)
        completed = sum(cut >= end for end in ends)
        assert phase == (readiness.CHECKPOINTS[completed - 1] if completed else "unknown")
        assert result is None
    assert readiness._progress(raw, interrupted=False) == (
        "worker_ready",
        {"status": status, "reason": reason},
    )


@pytest.mark.parametrize(
    "raw",
    [
        readiness.MILESTONES[0] + b"private-canary",
        readiness.MILESTONES[0] * 2,
        readiness.MILESTONES[1],
        readiness.MILESTONES[0] + b'{"v":true,"phase":"django_setup"}\n',
        readiness.MILESTONES[0] + b'{"v":1,"phase":{}}\n',
        b"".join(readiness.MILESTONES) + b'{"schema_version":1,"private":',
        progress() + b"x",
        progress() + b"\n",
        progress() + readiness._report_frame("ready", "live_lease"),
        b"x" * 513,
    ],
)
def test_invalid_progress_discards_even_an_earlier_checkpoint(raw):
    with pytest.raises(ValueError, match="Invalid readiness progress"):
        readiness._progress(raw, interrupted=True)


@pytest.mark.parametrize("code", [True, False, None, [], {}, "0"])
def test_noninteger_capture_exit_cannot_accept_report_or_leak(monkeypatch, code):
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    monkeypatch.setattr(readiness.time, "monotonic", lambda: 10)
    monkeypatch.setattr(readiness, "_capture", Mock(return_value=(progress(), code)))
    assert observe() == readiness.empty("error")


@pytest.mark.parametrize("code", [1, 2, 3, 124, 137, -9, 127])
def test_complete_ready_report_cannot_override_contradictory_exit(monkeypatch, code):
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    monkeypatch.setattr(readiness.time, "monotonic", lambda: 10)
    monkeypatch.setattr(readiness, "_capture", Mock(return_value=(progress(), code)))
    value = observe()
    assert value["outcome"] == ("timeout" if code in {124, 137} else "incomplete")
    assert value["phase"] == "worker_ready"
    assert value["status"] == value["reason"] == "unknown"
    assert readiness.valid(value)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (readiness.MILESTONES[0], "helper_entry"),
        (readiness.MILESTONES[0] + readiness.MILESTONES[1][:9], "helper_entry"),
        (progress(), "worker_ready"),
    ],
)
@pytest.mark.parametrize("cleanup", ["clean", "ownership_lost", "wait_timeout", "close_error"])
def test_actual_capture_timeout_preserves_only_safe_progress_after_successful_cleanup(
    monkeypatch, raw, expected, cleanup
):
    clock = SimpleNamespace(now=0)
    events = []

    def close():
        events.append("close")
        if cleanup == "close_error":
            raise OSError("private-canary")

    stream = SimpleNamespace(fileno=lambda: 77, close=close)

    def wait(**kwargs):
        events.append("wait")
        if cleanup == "wait_timeout":
            raise subprocess.TimeoutExpired("private-canary", 1)

    process = SimpleNamespace(stdout=stream, pid=123, returncode=None, wait=wait)

    class Selector:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def register(self, *args):
            pass

        def get_map(self):
            return {77: True}

        def select(self, timeout):
            return [(SimpleNamespace(fd=77, fileobj=stream), None)]

    def read(fd, size):
        assert size == 513
        clock.now = 9
        return raw

    def peek(process):
        events.append("peek")
        if cleanup == "ownership_lost":
            raise ChildProcessError("private-canary")
        return None

    monkeypatch.setattr(readiness.sys, "platform", "linux")
    monkeypatch.setattr(readiness.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(readiness.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(readiness.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(readiness.os, "set_blocking", lambda *args: None)
    monkeypatch.setattr(readiness.os, "read", read)
    monkeypatch.setattr(readiness, "_peek", peek)
    monkeypatch.setattr(
        readiness.os, "killpg", lambda *args: events.append("signal"), raising=False
    )
    monkeypatch.setattr(readiness.signal, "SIGKILL", 9, raising=False)
    value = observe()
    assert value["phase"] == (expected if cleanup == "clean" else "unknown")
    assert value["outcome"] == ("timeout" if cleanup in {"clean", "wait_timeout"} else "error")
    assert value["status"] == value["reason"] == "unknown"
    assert value["exit_code"] is None
    assert readiness.valid(value)
    assert "private-canary" not in json.dumps(value)
    assert events == (
        ["peek", "close"] if cleanup == "ownership_lost" else ["peek", "signal", "wait", "close"]
    )


def test_partial_carrier_never_renders_output_bytes():
    error = readiness._CaptureDeadlineError(b"private-canary")
    assert error.raw == b"private-canary"
    assert str(error) == "Readiness command timed out"
    assert "private-canary" not in repr(error)


@pytest.mark.parametrize("stage", ["import", "setup", "command"])
def test_child_checkpoints_precede_exact_setup_and_command_boundary(monkeypatch, stage):
    import builtins

    import django
    from django.core import management

    checkpoints = []
    original_import = builtins.__import__

    def fail():
        raise RuntimeError("private-canary")

    def import_module(name, *args, **kwargs):
        if name == "django" and stage == "import":
            fail()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    monkeypatch.setattr(django, "setup", fail if stage == "setup" else lambda: None)
    monkeypatch.setattr(management, "call_command", lambda *args, **kwargs: fail())
    with pytest.raises(RuntimeError, match="private-canary"):
        readiness._command(checkpoints.append)
    assert (
        checkpoints
        == {"import": [], "setup": ["django_setup"], "command": ["django_setup", "worker_ready"]}[
            stage
        ]
    )
