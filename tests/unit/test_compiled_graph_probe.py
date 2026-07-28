"""Tests for subprocess containment and Compiled Graph probe classifications."""

from __future__ import annotations

import json
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

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


def test_invalid_child_record_is_a_native_crash(monkeypatch) -> None:
    _force_supported(monkeypatch)
    command = _record_command({"status": "future-status"})

    outcome = run_compiled_graph_probe(_request(), _command=command)

    assert outcome.status is CompiledGraphProbeStatus.NATIVE_CRASH
    assert outcome.error_type == "InvalidChildRecord"


def test_abrupt_exit_is_classified_with_native_exit_code(monkeypatch) -> None:
    _force_supported(monkeypatch)
    command = [sys.executable, "-c", "import os; os._exit(7)"]

    outcome = run_compiled_graph_probe(_request(), _command=command)

    assert outcome.status is CompiledGraphProbeStatus.NATIVE_CRASH
    assert outcome.exit_code == 7
    assert outcome.native_exit_code == "0x00000007"
    assert outcome.error_type == "AbruptProcessExit"


def test_abrupt_root_exit_cleans_real_grandchild(monkeypatch, tmp_path: Path) -> None:
    _force_supported(monkeypatch)
    sentinel = tmp_path / "leaked-grandchild.txt"
    grandchild = (
        "import pathlib,time; "
        "time.sleep(0.75); "
        f"pathlib.Path({str(sentinel)!r}).write_text('leaked', encoding='utf-8')"
    )
    root = (
        "import os,subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "os._exit(7)"
    )

    outcome = run_compiled_graph_probe(
        _request(),
        _command=[sys.executable, "-c", root],
    )
    time.sleep(1)

    assert outcome.status is CompiledGraphProbeStatus.NATIVE_CRASH
    assert sentinel.exists() is False


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


def test_windows_job_cleanup_runs_after_root_already_exited(monkeypatch) -> None:
    class Process:
        pid = 123

        def poll(self) -> int:
            return 7

    class Job:
        terminated = False

        def terminate(self) -> None:
            self.terminated = True

    job = Job()
    monkeypatch.setattr(probe.os, "name", "nt")

    probe._terminate_process_tree(Process(), windows_job=job)

    assert job.terminated is True


def test_windows_job_is_assigned_before_child_start_gate_is_released(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Process:
        pass

    class Job:
        pass

    job = Job()

    def assign_job(_process) -> Job:
        events.append("job-assigned")
        return job

    def release_gate(_path: Path) -> None:
        events.append("gate-released")

    monkeypatch.setattr(probe.os, "name", "nt")
    monkeypatch.setattr(probe, "_WindowsJob", assign_job)
    monkeypatch.setattr(probe, "_release_child_start_gate", release_gate)

    result = probe._arm_child_process(Process(), tmp_path / "gate")

    assert result is job
    assert events == ["job-assigned", "gate-released"]


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
