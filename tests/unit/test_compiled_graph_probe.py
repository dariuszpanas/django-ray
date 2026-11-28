"""Tests for subprocess containment and Compiled Graph probe classifications."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from django_ray.runtime import compiled_graph_probe as probe
from django_ray.runtime.compiled_graph import (
    CompiledGraphCapabilityDecision,
    CompiledGraphReason,
    CompiledGraphRuntimeIdentity,
    CompiledGraphSubmissionTransport,
    CompiledGraphTopology,
    CompiledGraphTransport,
)
from django_ray.runtime.compiled_graph_probe import (
    CompiledGraphProbeOutcome,
    CompiledGraphProbeRequest,
    CompiledGraphProbeStatus,
    run_compiled_graph_probe,
)


def _decision(
    *,
    eligible: bool = True,
    reason: CompiledGraphReason = CompiledGraphReason.ELIGIBLE,
) -> CompiledGraphCapabilityDecision:
    return CompiledGraphCapabilityDecision(
        eligible=eligible,
        reason=reason,
        message="test decision",
        topology=CompiledGraphTopology.DIRECT_DRIVER.value,
        submission_transport=CompiledGraphSubmissionTransport.DIRECT_RAY_CORE.value,
        transport=CompiledGraphTransport.CPU_SHARED_MEMORY.value,
        runtime=CompiledGraphRuntimeIdentity(
            ray_version="2.56.1",
            python_version="3.12.12",
            operating_system="linux",
            architecture="x86_64",
            python_implementation="cpython",
            python_abi="cpython-312-x86_64-linux-gnu",
            dependency_profile="ray=2.56.1;numpy=2.5.1",
            platform_profile="linux-profile",
            libc_profile="glibc-2.39",
            container_profile="kubernetes:gha-ubuntu-24.04",
            deployment_profile=f"sha256:{'a' * 64}",
            shared_memory_profile="posix-dev-shm:size=68719476736:mount=tmpfs",
            object_store_profile="ray-plasma:memory=2147483648:spill=disabled",
        ),
        candidate=eligible,
        verified=eligible,
        capability_set="test-capability" if eligible else None,
    )


def _request(
    *,
    candidate_native: bool = False,
    unsafe_native: bool = False,
) -> CompiledGraphProbeRequest:
    return CompiledGraphProbeRequest(
        topology=CompiledGraphTopology.DIRECT_DRIVER,
        candidate_native=candidate_native,
        unsafe_native=unsafe_native,
    )


def _record_command(
    record: dict[str, object],
    *,
    exit_code: int = 0,
    prefix_output: str = "",
) -> list[str]:
    encoded_record = json.dumps(record, sort_keys=True, separators=(",", ":"))
    script = (
        "import os,pathlib; "
        f"print({prefix_output!r}, end='', flush=True); "
        f"pathlib.Path(os.environ[{probe._CHILD_RECORD_PATH_ENV!r}]).write_text("
        f"{encoded_record!r}, encoding='utf-8'); "
        f"raise SystemExit({exit_code})"
    )
    return [sys.executable, "-c", script]


def _force_supported(monkeypatch) -> CompiledGraphCapabilityDecision:
    decision = _decision()
    monkeypatch.setattr(probe.sys, "platform", "linux")
    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", lambda *_a, **_k: decision)
    return decision


def test_request_round_trip_is_versioned() -> None:
    request = _request(candidate_native=True, unsafe_native=True)

    assert CompiledGraphProbeRequest.fromdict(request.asdict()) == request
    with pytest.raises(ValueError, match="schema"):
        CompiledGraphProbeRequest.fromdict({"schema_version": 99})


def test_unsupported_guard_returns_without_spawning(monkeypatch) -> None:
    decision = _decision(
        eligible=False,
        reason=CompiledGraphReason.UNSUPPORTED_OPERATING_SYSTEM,
    )
    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", lambda *_a, **_k: decision)
    monkeypatch.setattr(
        probe.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("guarded probe must not spawn"),
    )

    outcome = run_compiled_graph_probe(_request())

    assert outcome.status is CompiledGraphProbeStatus.UNSUPPORTED_GUARD
    assert outcome.exit_code is None
    assert outcome.decision is decision


def test_unsafe_probe_requires_second_environment_acknowledgement(monkeypatch) -> None:
    decision = _force_supported(monkeypatch)
    monkeypatch.setattr(
        probe.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("unacknowledged probe must not spawn"),
    )

    outcome = run_compiled_graph_probe(_request(unsafe_native=True), _environment={})

    assert outcome.status is CompiledGraphProbeStatus.PYTHON_FAILURE
    assert outcome.error_type == "UnsafeProbeNotAcknowledged"
    assert probe.UNSAFE_PROBE_ENV in (outcome.error_message or "")
    assert outcome.decision is decision


def test_unsafe_probe_cannot_bypass_topology_or_transport_contract(monkeypatch) -> None:
    decision = _decision(eligible=False, reason=CompiledGraphReason.UNSUPPORTED_TRANSPORT)
    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", lambda *_a, **_k: decision)
    monkeypatch.setattr(
        probe.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("semantic rejection must not spawn"),
    )

    outcome = run_compiled_graph_probe(_request(unsafe_native=True), _environment={})

    assert outcome.status is CompiledGraphProbeStatus.UNSUPPORTED_GUARD


def test_candidate_requires_explicit_canary_opt_in(monkeypatch) -> None:
    decision = _decision(
        eligible=False,
        reason=CompiledGraphReason.CANDIDATE_REQUIRES_SMOKE,
    )
    decision = replace(decision, candidate=True)
    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", lambda *_a, **_k: decision)
    monkeypatch.setattr(
        probe.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("candidate without opt-in must not spawn"),
    )

    outcome = run_compiled_graph_probe(_request())

    assert outcome.status is CompiledGraphProbeStatus.UNSUPPORTED_GUARD


@pytest.mark.skipif(sys.platform != "linux", reason="probe subprocess execution requires Linux")
def test_candidate_canary_runs_without_unsafe_acknowledgement(monkeypatch) -> None:
    decision = replace(
        _decision(
            eligible=False,
            reason=CompiledGraphReason.CANDIDATE_REQUIRES_SMOKE,
        ),
        candidate=True,
    )
    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", lambda *_a, **_k: decision)
    command = _record_command({"status": "success"})

    outcome = run_compiled_graph_probe(
        _request(candidate_native=True),
        _command=command,
        _environment={},
    )

    assert outcome.status is CompiledGraphProbeStatus.SUCCESS
    assert outcome.decision.candidate is True


@pytest.mark.skipif(sys.platform != "linux", reason="probe subprocess execution requires Linux")
def test_incomplete_candidate_canary_runs_only_with_explicit_opt_in(monkeypatch) -> None:
    decision = replace(
        _decision(
            eligible=False,
            reason=CompiledGraphReason.INCOMPLETE_CAPABILITY_CONTEXT,
        ),
        candidate=True,
    )
    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", lambda *_a, **_k: decision)

    outcome = run_compiled_graph_probe(
        _request(candidate_native=True),
        _command=_record_command({"status": "success"}),
        _environment={},
    )

    assert outcome.status is CompiledGraphProbeStatus.SUCCESS
    assert outcome.decision.eligible is False


@pytest.mark.skipif(sys.platform != "linux", reason="probe subprocess execution requires Linux")
def test_success_record_is_parsed_and_output_is_bounded(monkeypatch) -> None:
    decision = _force_supported(monkeypatch)
    command = _record_command(
        {
            "status": "success",
            "details": {"ray_version": "2.56.1", "result_verified": True},
        },
        prefix_output="x" * 20_000 + "\n",
    )

    outcome = run_compiled_graph_probe(_request(), _command=command)

    assert outcome.status is CompiledGraphProbeStatus.SUCCESS
    assert outcome.successful is True
    assert outcome.exit_code == 0
    assert outcome.decision is decision
    assert outcome.details == {"ray_version": "2.56.1", "result_verified": True}
    assert 0 < len(outcome.stdout_tail) <= probe._MAX_OUTPUT_CHARS
    assert json.loads(json.dumps(outcome.asdict()))["successful"] is True


@pytest.mark.skipif(sys.platform != "linux", reason="probe subprocess execution requires Linux")
def test_output_is_drained_into_a_bounded_tail_while_child_runs(monkeypatch) -> None:
    _force_supported(monkeypatch)
    script = (
        "import json,os,pathlib,sys; "
        "[sys.stdout.write('x' * 4096) for _ in range(256)]; "
        f"pathlib.Path(os.environ[{probe._CHILD_RECORD_PATH_ENV!r}]).write_text("
        "json.dumps({'status': 'success'}), encoding='utf-8')"
    )

    outcome = run_compiled_graph_probe(
        _request(),
        _command=[sys.executable, "-c", script],
    )

    assert outcome.status is CompiledGraphProbeStatus.SUCCESS
    assert len(outcome.stdout_tail) <= probe._MAX_OUTPUT_CHARS


def test_bounded_tail_buffer_never_retains_more_than_limit() -> None:
    buffer = probe._BoundedTailBuffer(limit=32)

    for _ in range(1_000):
        buffer.append("0123456789")

    assert buffer.value() == "89012345678901234567890123456789"


@pytest.mark.skipif(sys.platform != "linux", reason="probe subprocess execution requires Linux")
def test_python_failure_record_survives_nonzero_child_exit(monkeypatch) -> None:
    _force_supported(monkeypatch)
    command = _record_command(
        {
            "status": "python_failure",
            "error_type": "ValueError",
            "error_message": "compile failed",
            "traceback_tail": "trace",
            "details": "not-an-object",
        },
        exit_code=2,
    )

    outcome = run_compiled_graph_probe(_request(), _command=command)

    assert outcome.status is CompiledGraphProbeStatus.PYTHON_FAILURE
    assert outcome.exit_code == 2
    assert outcome.error_type == "ValueError"
    assert outcome.error_message == "compile failed"
    assert outcome.traceback_tail == "trace"
    assert outcome.details is None


@pytest.mark.skipif(sys.platform != "linux", reason="probe subprocess execution requires Linux")
def test_maximum_escaped_failure_record_cannot_be_truncated_by_human_logs(monkeypatch) -> None:
    _force_supported(monkeypatch)
    script = (
        "import os; "
        "from pathlib import Path; "
        "from django_ray.runtime import compiled_graph_probe as probe; "
        "unit=chr(0x1f4a5)+chr(34)+chr(92)+chr(10); "
        "value=(unit*((probe._MAX_ERROR_CHARS//len(unit))+1))[:probe._MAX_ERROR_CHARS]; "
        "print('human-log-' + ('x'*20000), flush=True); "
        "probe._write_child_record("
        "Path(os.environ[probe._CHILD_RECORD_PATH_ENV]), "
        "{'status':'python_failure','error_type':'EscapedFailure',"
        "'error_message':value,'traceback_tail':value}); "
        "raise SystemExit(2)"
    )
    expected_unit = chr(0x1F4A5) + '"\\\n'
    expected = (expected_unit * ((probe._MAX_ERROR_CHARS // len(expected_unit)) + 1))[
        : probe._MAX_ERROR_CHARS
    ]

    outcome = run_compiled_graph_probe(
        _request(),
        _command=[sys.executable, "-c", script],
    )

    assert outcome.status is CompiledGraphProbeStatus.PYTHON_FAILURE
    assert outcome.error_type == "EscapedFailure"
    assert outcome.error_message == expected
    assert outcome.traceback_tail == expected
    assert len(outcome.stdout_tail) == probe._MAX_OUTPUT_CHARS


@pytest.mark.skipif(sys.platform != "linux", reason="probe subprocess execution requires Linux")
def test_native_worker_crash_record_survives_nonzero_child_exit(monkeypatch) -> None:
    _force_supported(monkeypatch)
    command = _record_command(
        {
            "status": "native_crash",
            "error_type": "WorkerCrashedError",
            "error_message": "worker died unexpectedly",
        },
        exit_code=2,
    )

    outcome = run_compiled_graph_probe(_request(), _command=command)

    assert outcome.status is CompiledGraphProbeStatus.NATIVE_CRASH
    assert outcome.error_type == "WorkerCrashedError"


@pytest.mark.skipif(sys.platform != "linux", reason="probe subprocess execution requires Linux")
def test_invalid_child_record_is_a_native_crash(monkeypatch) -> None:
    _force_supported(monkeypatch)
    command = _record_command({"status": "future-status"})

    outcome = run_compiled_graph_probe(_request(), _command=command)

    assert outcome.status is CompiledGraphProbeStatus.NATIVE_CRASH
    assert outcome.error_type == "InvalidChildRecord"


@pytest.mark.skipif(sys.platform != "linux", reason="probe subprocess execution requires Linux")
def test_abrupt_exit_is_classified_with_native_exit_code(monkeypatch) -> None:
    _force_supported(monkeypatch)
    command = [sys.executable, "-c", "import os; os._exit(7)"]

    outcome = run_compiled_graph_probe(_request(), _command=command)

    assert outcome.status is CompiledGraphProbeStatus.NATIVE_CRASH
    assert outcome.exit_code == 7
    assert outcome.native_exit_code == "0x00000007"
    assert outcome.error_type == "AbruptProcessExit"


@pytest.mark.skipif(sys.platform != "linux", reason="probe subprocess execution requires Linux")
def test_abrupt_root_exit_cleans_real_grandchild(monkeypatch, tmp_path: Path) -> None:
    _force_supported(monkeypatch)
    ready_pid_file = tmp_path / "grandchild.pid"
    sentinel = tmp_path / "leaked-grandchild.txt"
    grandchild = (
        "import os,pathlib,time; "
        f"p=pathlib.Path({str(ready_pid_file)!r}); "
        "tmp=p.with_suffix('.tmp'); "
        "tmp.write_text(str(os.getpid()), encoding='utf-8'); "
        "tmp.replace(p); "
        "time.sleep(10.0); "
        f"pathlib.Path({str(sentinel)!r}).write_text('leaked', encoding='utf-8')"
    )
    root = (
        "import os,pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        f"ready=pathlib.Path({str(ready_pid_file)!r}); "
        "deadline=time.monotonic()+5.0; "
        "\nwhile not ready.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "os._exit(7 if ready.exists() else 9)"
    )

    outcome = run_compiled_graph_probe(
        _request(),
        _command=[sys.executable, "-c", root],
    )

    assert outcome.status is CompiledGraphProbeStatus.NATIVE_CRASH
    assert outcome.exit_code == 7
    assert ready_pid_file.exists()
    grandchild_pid = int(ready_pid_file.read_text(encoding="utf-8").strip())

    deadline = time.monotonic() + 3.0
    reaped = False
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            reaped = True
            break
        except PermissionError:  # pragma: no cover - PID reused by another user
            reaped = True
            break
        time.sleep(0.02)

    assert reaped, f"grandchild process {grandchild_pid} survived abrupt root exit"
    assert sentinel.exists() is False


@pytest.mark.skipif(sys.platform != "linux", reason="probe subprocess execution requires Linux")
def test_timeout_terminates_and_reaps_probe_process(monkeypatch) -> None:
    _force_supported(monkeypatch)
    command = [sys.executable, "-c", "import time; time.sleep(5)"]

    outcome = run_compiled_graph_probe(
        _request(),
        timeout_seconds=0.1,
        _command=command,
    )

    assert outcome.status is CompiledGraphProbeStatus.TIMEOUT
    assert outcome.error_type == "TimeoutExpired"
    assert "0.1s" in (outcome.error_message or "")
    assert outcome.exit_code is not None


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        run_compiled_graph_probe(_request(), timeout_seconds=0)


@pytest.mark.parametrize(
    "timeout",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param(10**400, id="integer-float-overflow"),
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(None, id="none"),
        pytest.param("1", id="string"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
    ],
)
def test_timeout_validation_precedes_policy_and_side_effects(timeout, monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid timeout reached policy, clock, allocation or process setup")

    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", forbidden)
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=forbidden))
    monkeypatch.setattr(probe, "tempfile", SimpleNamespace(TemporaryDirectory=forbidden))
    monkeypatch.setattr(probe, "subprocess", SimpleNamespace(Popen=forbidden))
    with pytest.raises(ValueError, match="^timeout_seconds must be finite and greater than zero$"):
        run_compiled_graph_probe(
            _request(candidate_native=True, unsafe_native=True),
            timeout_seconds=timeout,
            _environment={probe.UNSAFE_PROBE_ENV: "1"},
        )


@pytest.mark.parametrize("timeout", [1, 0.5, 60.0])
def test_finite_timeout_preserves_guarded_outcome(timeout, monkeypatch) -> None:
    decision = _decision(eligible=False, reason=CompiledGraphReason.UNSUPPORTED_OPERATING_SYSTEM)

    def forbidden(*_args, **_kwargs):
        pytest.fail("a finite timeout bypassed the existing admission guard")

    clock = iter([1.0, 1.25])
    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", lambda *_a, **_k: decision)
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(probe, "tempfile", SimpleNamespace(TemporaryDirectory=forbidden))
    monkeypatch.setattr(probe, "subprocess", SimpleNamespace(Popen=forbidden))
    outcome = run_compiled_graph_probe(_request(), timeout_seconds=timeout)
    assert outcome.status is CompiledGraphProbeStatus.UNSUPPORTED_GUARD
    assert outcome.decision is decision
    assert outcome.duration_seconds == 0.25


def test_linux_process_group_cleanup_runs_after_root_already_exited(monkeypatch) -> None:
    class Process:
        pid = 123

        def poll(self) -> int:
            return 7

    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        probe.os,
        "killpg",
        lambda pid, sig: killpg_calls.append((pid, sig)),
        raising=False,
    )
    monkeypatch.setattr(probe.signal, "SIGKILL", 9, raising=False)

    probe._terminate_process_tree(Process())  # type: ignore[arg-type]

    assert killpg_calls == [(123, 9)]
    assert not hasattr(probe, "_WindowsJob")


def test_child_start_gate_release_failure_terminates_and_reaps_process_tree(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Process:
        pid = 456

        def poll(self) -> int | None:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            events.append("wait")
            return 0

    def fail_gate(_path: Path) -> None:
        events.append("gate-failed")
        raise OSError("gate unwritable")

    monkeypatch.setattr(probe, "_release_child_start_gate", fail_gate)
    monkeypatch.setattr(
        probe,
        "_terminate_process_tree",
        lambda _process: events.append("terminate"),
    )

    with pytest.raises(OSError, match="gate unwritable"):
        probe._arm_child_process(Process(), tmp_path / "gate")  # type: ignore[arg-type]

    assert events == ["gate-failed", "terminate", "wait"]


def test_unsafe_probe_and_subprocess_boundary_reject_non_linux(monkeypatch) -> None:
    os_decision = _decision(
        eligible=False,
        reason=CompiledGraphReason.UNSUPPORTED_OPERATING_SYSTEM,
    )
    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", lambda *_a, **_k: os_decision)
    monkeypatch.setattr(
        probe.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("non-Linux probe must not spawn"),
    )
    monkeypatch.setenv(probe.UNSAFE_PROBE_ENV, "1")

    parent_os_rejected = run_compiled_graph_probe(
        _request(unsafe_native=True),
        _environment={probe.UNSAFE_PROBE_ENV: "1"},
    )
    child_os_rejected = probe._execute_child_request(_request(unsafe_native=True))

    assert parent_os_rejected.status is CompiledGraphProbeStatus.UNSUPPORTED_GUARD
    assert child_os_rejected["status"] == "unsupported_guard"

    eligible_decision = _decision(eligible=True, reason=CompiledGraphReason.ELIGIBLE)
    monkeypatch.setattr(
        probe,
        "evaluate_compiled_graph_support",
        lambda *_a, **_k: eligible_decision,
    )
    monkeypatch.setattr(probe.sys, "platform", "win32")

    parent_platform_rejected = run_compiled_graph_probe(
        _request(unsafe_native=True),
        _environment={probe.UNSAFE_PROBE_ENV: "1"},
    )
    child_platform_rejected = probe._execute_child_request(_request(unsafe_native=True))

    assert parent_platform_rejected.status is CompiledGraphProbeStatus.UNSUPPORTED_GUARD
    assert "requires Linux" in (parent_platform_rejected.error_message or "")
    assert child_platform_rejected["status"] == "unsupported_guard"
    assert "requires Linux" in child_platform_rejected["error_message"]


def test_child_start_gate_blocks_until_parent_releases_it(monkeypatch, tmp_path: Path) -> None:
    gate_path = tmp_path / "child-start-ready"
    entered = threading.Event()
    finished = threading.Event()
    monkeypatch.setenv(probe._CHILD_START_GATE_PATH_ENV, str(gate_path))

    def wait_for_gate() -> None:
        entered.set()
        probe._wait_for_parent_start_gate(timeout_seconds=1)
        finished.set()

    waiter = threading.Thread(target=wait_for_gate)
    waiter.start()

    assert entered.wait(timeout=1)
    assert finished.wait(timeout=0.05) is False
    probe._release_child_start_gate(gate_path)
    assert finished.wait(timeout=1)
    waiter.join(timeout=1)


def test_signal_outcome_is_distinct_from_native_exit() -> None:
    outcome = probe._abrupt_process_outcome(
        _decision(),
        duration_seconds=1.0,
        exit_code=-signal.SIGTERM,
        stdout="out",
        stderr="err",
    )

    assert outcome.status is CompiledGraphProbeStatus.SIGNAL
    assert outcome.termination_signal == signal.SIGTERM
    assert outcome.native_exit_code is None


@pytest.mark.skipif(sys.platform != "linux", reason="probe subprocess execution requires Linux")
def test_unsafe_runtime_bypass_runs_after_acknowledgement(monkeypatch) -> None:
    decision = _decision(
        eligible=False,
        reason=CompiledGraphReason.UNSUPPORTED_RAY_VERSION,
    )
    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", lambda *_a, **_k: decision)
    command = _record_command({"status": "success"})

    outcome = run_compiled_graph_probe(
        _request(unsafe_native=True),
        _command=command,
        _environment={probe.UNSAFE_PROBE_ENV: "1"},
    )

    assert outcome.status is CompiledGraphProbeStatus.SUCCESS
    assert outcome.decision.eligible is False


def test_child_guard_does_not_call_native_probe(monkeypatch) -> None:
    decision = _decision(
        eligible=False,
        reason=CompiledGraphReason.UNSUPPORTED_OPERATING_SYSTEM,
    )
    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", lambda *_a, **_k: decision)
    monkeypatch.setattr(
        probe,
        "_run_native_probe",
        lambda *_a: pytest.fail("guarded child must not call Ray"),
    )

    record = probe._execute_child_request(_request())

    assert record["status"] == "unsupported_guard"


def test_child_incomplete_candidate_requires_canary_opt_in(monkeypatch) -> None:
    monkeypatch.setattr(probe.sys, "platform", "linux")
    decision = replace(
        _decision(
            eligible=False,
            reason=CompiledGraphReason.INCOMPLETE_CAPABILITY_CONTEXT,
        ),
        candidate=True,
    )
    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", lambda *_a, **_k: decision)
    monkeypatch.setattr(probe, "_run_native_probe", lambda _topology: {"ran": True})

    guarded = probe._execute_child_request(_request())
    canary = probe._execute_child_request(_request(candidate_native=True))

    assert guarded["status"] == "unsupported_guard"
    assert canary == {"status": "success", "details": {"ran": True}}


def test_child_unsafe_path_requires_environment_acknowledgement(monkeypatch) -> None:
    _force_supported(monkeypatch)
    monkeypatch.delenv(probe.UNSAFE_PROBE_ENV, raising=False)
    monkeypatch.setattr(
        probe,
        "_run_native_probe",
        lambda *_a: pytest.fail("unacknowledged child must not call Ray"),
    )

    record = probe._execute_child_request(_request(unsafe_native=True))

    assert record["status"] == "python_failure"
    assert record["error_type"] == "UnsafeProbeNotAcknowledged"


def test_child_unsafe_path_cannot_bypass_transport_contract(monkeypatch) -> None:
    decision = _decision(eligible=False, reason=CompiledGraphReason.UNSUPPORTED_TRANSPORT)
    monkeypatch.setattr(probe, "evaluate_compiled_graph_support", lambda *_a, **_k: decision)
    monkeypatch.setenv(probe.UNSAFE_PROBE_ENV, "1")
    monkeypatch.setattr(
        probe,
        "_run_native_probe",
        lambda *_a: pytest.fail("unsupported transport must not run a CPU probe"),
    )

    record = probe._execute_child_request(_request(unsafe_native=True))

    assert record["status"] == "unsupported_guard"


def test_child_classifies_python_and_native_worker_failures(monkeypatch) -> None:
    _force_supported(monkeypatch)

    def python_failure(_topology):
        raise ValueError("bad graph")

    monkeypatch.setattr(probe, "_run_native_probe", python_failure)
    python_record = probe._execute_child_request(_request())

    class WorkerCrashedError(RuntimeError):
        pass

    def native_failure(_topology):
        raise WorkerCrashedError("worker died unexpectedly")

    monkeypatch.setattr(probe, "_run_native_probe", native_failure)
    native_record = probe._execute_child_request(_request())

    assert python_record["status"] == "python_failure"
    assert python_record["error_type"] == "ValueError"
    assert "ValueError" in python_record["traceback_tail"]
    assert native_record["status"] == "native_crash"
    assert native_record["details"] == {"native_worker_crash": True}


def test_child_success_returns_native_details(monkeypatch) -> None:
    _force_supported(monkeypatch)
    monkeypatch.setattr(
        probe,
        "_run_native_probe",
        lambda topology: {"topology": topology.value},
    )

    record = probe._execute_child_request(_request())

    assert record == {
        "status": "success",
        "details": {"topology": "direct-driver"},
    }


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("worker crashed"),
        RuntimeError("unexpected SYSTEM ERROR in worker"),
    ],
)
def test_native_worker_crash_message_detection(error: BaseException) -> None:
    assert probe._looks_like_native_worker_crash(error) is True


def test_regular_exception_is_not_native_worker_crash() -> None:
    assert probe._looks_like_native_worker_crash(ValueError("application failure")) is False


def test_payload_decode_rejects_invalid_encoding_and_non_object() -> None:
    with pytest.raises(ValueError, match="Invalid"):
        probe._decode_child_payload("not-base64")

    encoded_list = probe.base64.urlsafe_b64encode(b"[]").decode("ascii")
    with pytest.raises(ValueError, match="must be an object"):
        probe._decode_child_payload(encoded_list)


def test_child_command_round_trips_request() -> None:
    request = _request(candidate_native=True, unsafe_native=True)

    command = probe._child_command(request, python_executable="python-test")

    assert command[:4] == (
        "python-test",
        "-m",
        "django_ray.runtime.compiled_graph_probe",
        "--child-payload-b64",
    )
    assert probe._decode_child_payload(command[4]) == request


@pytest.mark.parametrize("remainder", [1, 2])
def test_child_payload_requires_base64_padding(remainder: int) -> None:
    request = _request()
    payload = json.dumps(request.asdict()).encode("ascii")
    payload += b" " * ((remainder - len(payload)) % 3)
    encoded = probe.base64.urlsafe_b64encode(payload).decode("ascii")

    assert encoded.endswith("=")
    assert probe._decode_child_payload(encoded) == request
    with pytest.raises(ValueError, match="Invalid Compiled Graph probe child payload"):
        probe._decode_child_payload(encoded.rstrip("="))


def test_private_control_record_round_trips_and_helpers_handle_bytes(tmp_path: Path) -> None:
    record_path = tmp_path / "record.json"

    probe._write_child_record(record_path, {"status": "success"})

    assert probe._read_child_record(record_path) == {"status": "success"}
    assert probe._join_output(b"one", b"one-two") == "one-two"
    assert probe._join_output("one", "two") == "onetwo"
    assert probe._tail(None) == ""


def test_main_parent_honors_require_success(monkeypatch, capsys) -> None:
    outcome = CompiledGraphProbeOutcome(
        status=CompiledGraphProbeStatus.UNSUPPORTED_GUARD,
        decision=_decision(
            eligible=False,
            reason=CompiledGraphReason.UNSUPPORTED_OPERATING_SYSTEM,
        ),
        duration_seconds=0.1,
    )
    monkeypatch.setattr(probe, "run_compiled_graph_probe", lambda *_a, **_k: outcome)

    exit_code = probe.main(["--require-success"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "unsupported_guard"


def test_main_child_writes_private_structured_record(monkeypatch, capsys, tmp_path: Path) -> None:
    request = _request()
    command = probe._child_command(request, python_executable="python")
    record_path = tmp_path / "record.json"
    events: list[str] = []
    monkeypatch.setenv(probe._CHILD_RECORD_PATH_ENV, str(record_path))
    monkeypatch.setattr(probe, "_wait_for_parent_start_gate", lambda: events.append("gate"))

    def execute_request(_request) -> dict[str, object]:
        events.append("execute")
        return {"status": "success", "details": {}}

    monkeypatch.setattr(probe, "_execute_child_request", execute_request)

    exit_code = probe.main(["--child-payload-b64", command[-1]])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == ""
    assert events == ["gate", "execute"]
    assert probe._read_child_record(record_path) == {"status": "success", "details": {}}


def test_main_child_converts_decode_error_to_python_failure(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    record_path = tmp_path / "record.json"
    gate_path = tmp_path / "child-start-ready"
    monkeypatch.setenv(probe._CHILD_RECORD_PATH_ENV, str(record_path))
    monkeypatch.setenv(probe._CHILD_START_GATE_PATH_ENV, str(gate_path))
    probe._release_child_start_gate(gate_path)

    exit_code = probe.main(["--child-payload-b64", "bad"])
    captured = capsys.readouterr()
    record = probe._read_child_record(record_path)

    assert exit_code == 2
    assert captured.out == ""
    assert record is not None
    assert record["status"] == "python_failure"
    assert record["error_type"] == "ValueError"


def test_outcome_defaults_details_to_json_object() -> None:
    outcome = CompiledGraphProbeOutcome(
        status=CompiledGraphProbeStatus.SUCCESS,
        decision=_decision(),
        duration_seconds=1 / 3,
    )

    assert outcome.asdict()["duration_seconds"] == 0.333333
    assert outcome.asdict()["details"] == {}


def test_programmatic_probe_outcome_serialization_is_end_to_end_bounded() -> None:
    oversized = "\U0001f4a5" * 100_000
    base_decision = _decision()
    decision = replace(
        base_decision,
        message=oversized,
        topology=oversized,
        submission_transport=oversized,
        transport=oversized,
        runtime=replace(base_decision.runtime, container_profile=oversized),
    )
    outcome = CompiledGraphProbeOutcome(
        status=CompiledGraphProbeStatus.PYTHON_FAILURE,
        decision=decision,
        duration_seconds=1,
        native_exit_code=oversized,
        error_type=oversized,
        error_message=oversized,
        traceback_tail=oversized,
        stdout_tail=oversized,
        stderr_tail=oversized,
        details={"oversized": oversized},
    )

    serialized = outcome.asdict()
    encoded = json.dumps(serialized, ensure_ascii=True, sort_keys=True)

    assert serialized["decision"]["runtime"]["container_profile"].startswith("<oversized:")
    assert serialized["decision"]["message"].startswith("<oversized:")
    assert len(serialized["native_exit_code"]) == probe._MAX_STATUS_CHARS
    assert len(serialized["error_type"]) == probe._MAX_ERROR_TYPE_CHARS
    assert len(serialized["error_message"]) == probe._MAX_ERROR_CHARS
    assert len(serialized["traceback_tail"]) == probe._MAX_ERROR_CHARS
    assert len(serialized["stdout_tail"]) == probe._MAX_OUTPUT_CHARS
    assert len(serialized["stderr_tail"]) == probe._MAX_OUTPUT_CHARS
    assert serialized["details"]["control_record_details_truncated"] is True
    assert len(serialized["details"]["sha256"]) == 64
    assert len(encoded) < 1_000_000


@pytest.mark.parametrize(
    "killpg_error",
    [PermissionError("pid reused under another uid"), ProcessLookupError("already exited")],
)
def test_terminate_process_tree_ignores_pid_reuse_permission_and_lookup_errors(
    monkeypatch,
    killpg_error: OSError,
) -> None:
    kill_calls: list[str] = []

    class FakeProcess:
        def __init__(self, *, exit_code: int | None) -> None:
            self.pid = 4242
            self._exit_code = exit_code

        def poll(self) -> int | None:
            return self._exit_code

        def kill(self) -> None:
            kill_calls.append("kill")

    def fake_killpg(_pid: int, _sig: int) -> None:
        raise killpg_error

    monkeypatch.setattr(probe.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(probe.signal, "SIGKILL", 9, raising=False)

    exited_process = FakeProcess(exit_code=0)
    probe._terminate_process_tree(exited_process)  # type: ignore[arg-type]
    assert kill_calls == []

    running_process = FakeProcess(exit_code=None)
    probe._terminate_process_tree(running_process)  # type: ignore[arg-type]
    assert kill_calls == ["kill"]


def test_native_probe_helpers_cover_driver_nested_and_teardown_paths(monkeypatch) -> None:
    import logging
    import types

    events: list[tuple[str, object]] = []
    compile_behavior = {"result": "django-ray-compiled-graph-probe", "fail_compile": False}
    initialized = {"state": False}

    class FakeInputNode:
        def __enter__(self) -> str:
            return "input-node"

        def __exit__(self, *_exc: object) -> None:
            return None

    class FakeCompiledGraph:
        def execute(self, value: str) -> tuple[str, str]:
            events.append(("execute", value))
            return ("ref", str(compile_behavior["result"]))

        def teardown(self, *, kill_actors: bool = False) -> None:
            events.append(("teardown", kill_actors))

    class FakeBoundNode:
        def __init__(self, arg: object) -> None:
            self.arg = arg

        def experimental_compile(self) -> FakeCompiledGraph:
            if compile_behavior["fail_compile"]:
                raise RuntimeError("compile boom")
            events.append(("compile", self.arg))
            return FakeCompiledGraph()

    class FakeMethodHandle:
        def __init__(self, fn: Any) -> None:
            self._fn = fn

        def bind(self, graph_input: object) -> FakeBoundNode:
            assert self._fn("ping") == "ping"
            return FakeBoundNode(graph_input)

    class FakeActorHandle:
        def __init__(self, cls: type[Any]) -> None:
            instance = cls()
            self.echo = FakeMethodHandle(instance.echo)

    class FakeRemoteDecorator:
        def __init__(self, target: Any, **options: Any) -> None:
            self._target = target
            self._options = options

        def remote(self) -> Any:
            if isinstance(self._target, type):
                return FakeActorHandle(self._target)
            events.append(("nested-remote", self._options))
            return ("nested-ref", self._target())

    def fake_remote(target: Any = None, **options: Any) -> Any:
        if target is None:
            return lambda fn: FakeRemoteDecorator(fn, **options)
        return FakeRemoteDecorator(target, **options)

    def fake_init(**kwargs: Any) -> None:
        initialized["state"] = True
        events.append(("init", kwargs))

    def fake_shutdown() -> None:
        initialized["state"] = False
        events.append(("shutdown", None))

    def fake_get(ref: tuple[str, Any], *, timeout: int | None = None) -> Any:
        events.append(("get", timeout))
        return ref[1]

    def fake_kill(actor: Any, *, no_restart: bool = False) -> None:
        events.append(("kill", (type(actor).__name__, no_restart)))

    fake_ray = types.ModuleType("ray")
    fake_ray.__version__ = "2.60.0-test"  # type: ignore[attr-defined]
    fake_ray.is_initialized = lambda: initialized["state"]  # type: ignore[attr-defined]
    fake_ray.init = fake_init  # type: ignore[attr-defined]
    fake_ray.shutdown = fake_shutdown  # type: ignore[attr-defined]
    fake_ray.remote = fake_remote  # type: ignore[attr-defined]
    fake_ray.get = fake_get  # type: ignore[attr-defined]
    fake_ray.kill = fake_kill  # type: ignore[attr-defined]

    fake_dag = types.ModuleType("ray.dag")
    fake_dag.InputNode = FakeInputNode  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setitem(sys.modules, "ray.dag", fake_dag)

    # 1. DIRECT_DRIVER initializes local 2-CPU Ray, runs graph, and shuts down.
    direct = probe._run_native_probe(CompiledGraphTopology.DIRECT_DRIVER)
    assert direct == {"ray_version": "2.60.0-test", "result_verified": True}
    assert (
        "init",
        {"num_cpus": 2, "include_dashboard": False, "logging_level": logging.ERROR},
    ) in events
    assert ("teardown", True) in events
    assert ("shutdown", None) in events

    # 2. RAY_JOB_DRIVER initializes with address="auto".
    events.clear()
    job_driver = probe._run_native_probe(CompiledGraphTopology.RAY_JOB_DRIVER)
    assert job_driver == {"ray_version": "2.60.0-test", "result_verified": True}
    assert (
        "init",
        {"address": "auto", "include_dashboard": False, "logging_level": logging.ERROR},
    ) in events

    # 3. NESTED_RAY_TASK dispatches through _nested_compiler_owner and shuts down.
    events.clear()
    nested = probe._run_native_probe(CompiledGraphTopology.NESTED_RAY_TASK)
    assert nested == {"ray_version": "2.60.0-test", "result_verified": True}
    assert ("nested-remote", {"max_retries": 0}) in events
    assert ("shutdown", None) in events

    # 4. Unsupported topology fails closed before touching Ray.
    with pytest.raises(ValueError, match="does not implement topology"):
        probe._run_native_probe(CompiledGraphTopology.RAY_CLIENT_DRIVER)

    # 5. Pre-initialized Ray is not re-initialized or shut down, and result mismatch raises.
    events.clear()
    initialized["state"] = True
    compile_behavior["result"] = "unexpected"
    with pytest.raises(AssertionError, match="Unexpected Compiled Graph result"):
        probe._run_driver_probe(CompiledGraphTopology.DIRECT_DRIVER)
    assert not any(kind in {"init", "shutdown"} for kind, _ in events)
    assert ("teardown", True) in events

    # 6. Compile failure falls back to ray.kill(actor, no_restart=True).
    events.clear()
    compile_behavior["fail_compile"] = True
    with pytest.raises(RuntimeError, match="compile boom"):
        probe._execute_compiled_graph_once()
    assert ("kill", ("FakeActorHandle", True)) in events


def test_compiled_graph_modules_declare_experimental_quarantine() -> None:
    from django_ray.runtime import compiled_graph, compiled_graph_lifecycle

    for module in (compiled_graph, compiled_graph_lifecycle, probe):
        doc = module.__doc__ or ""
        assert "Experimental quarantined module" in doc
