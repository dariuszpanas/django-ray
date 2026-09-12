"""The host never launches native helpers: subprocess/clock/files are controlled."""

from __future__ import annotations

import errno
import json
import math
import signal
import stat
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from django_ray.runner import cohort_process as module
from django_ray.runner.cohort_process import (
    COHORT_PROCESS_REQUEST_MAX_BYTES,
    COHORT_PROCESS_RESPONSE_MAX_BYTES,
    CohortProcessError,
    CohortProcessPhase,
    CohortProcessReason,
    CohortProcessSupervisor,
    decode_cohort_process_request,
    encode_cohort_process_request,
    encode_cohort_process_response,
)

OPERATION_ID = "a" * 32
PAYLOAD = {"command": "inspect", "arguments": {"value": "private-request"}}


class Harness:
    def __init__(self, monkeypatch):
        self.now = 100.0
        self.exited = False
        self.other_members = False
        self.group_exists = False
        self.wait_error = None
        self.wait_identity = 123
        self.process = SimpleNamespace(pid=123, poll=Mock(return_value=0))
        self.popen = Mock(return_value=self.process)
        self.killpg = Mock()
        self.write = Mock()
        self.read = Mock()
        self.discard = Mock(return_value=True)
        self.directory = "/private/helper"
        self.mkdir = Mock(return_value=self.directory)
        monkeypatch.setattr(module.sys, "platform", "linux")
        for name, value in {"P_PID": 1, "WEXITED": 4, "WNOHANG": 1, "WNOWAIT": 16}.items():
            monkeypatch.setattr(module.os, name, value, raising=False)
        monkeypatch.setattr(module.signal, "SIGKILL", 9, raising=False)
        monkeypatch.setattr(module.tempfile, "mkdtemp", self.mkdir)
        monkeypatch.setattr(module, "_directory_fd", Mock(return_value=12))
        monkeypatch.setattr(module, "_write_file", self.write)
        monkeypatch.setattr(module, "_read_file", self.read)
        monkeypatch.setattr(module.subprocess, "Popen", self.popen)
        monkeypatch.setattr(module.os, "waitid", self.waitid, raising=False)
        monkeypatch.setattr(module.os, "killpg", self.killpg, raising=False)
        monkeypatch.setattr(module, "_other_group_members", lambda pid: self.other_members)
        monkeypatch.setattr(module, "_group_exists", lambda pid: self.group_exists)
        monkeypatch.setattr(CohortProcessSupervisor, "_discard_directory", self.discard)
        self.supervisor = CohortProcessSupervisor(monotonic=lambda: self.now)

    def waitid(self, kind, pid, flags):
        assert (kind, pid, flags) == (1, 123, 21)
        if self.wait_error:
            raise self.wait_error
        return SimpleNamespace(si_pid=self.wait_identity) if self.exited else None

    def start(self, timeout=10):
        ticket = self.supervisor.start(PAYLOAD, timeout_seconds=timeout)
        self.read.return_value = encode_cohort_process_response(
            ticket.operation_id, outcome="ok", payload={"value": "private-response"}
        )
        return ticket


@pytest.fixture
def harness(monkeypatch):
    return Harness(monkeypatch)


def assert_reason(reason, operation, *args, **kwargs):
    with pytest.raises(CohortProcessError) as caught:
        operation(*args, **kwargs)
    assert caught.value.reason is reason
    assert str(caught.value) == f"Cohort helper refused: {reason.value}"
    assert "private-" not in str(caught.value)


@pytest.mark.parametrize("command", ["prepare", "submit", "inspect", "stop"])
def test_request_round_trip(command):
    payload = {
        "command": command,
        "arguments": {"unicode": "zażółć", "values": [None, True, 1, 2.5]},
    }
    encoded = encode_cohort_process_request(OPERATION_ID, payload)
    decoded = decode_cohort_process_request(encoded)
    assert decoded.operation_id == OPERATION_ID
    assert decoded.payload == payload
    assert OPERATION_ID not in repr(decoded)
    assert "zażółć" not in repr(decoded)
    assert encode_cohort_process_request(decoded.operation_id, decoded.payload) == encoded


@pytest.mark.parametrize("operation_id", [None, True, 1, "", "a" * 31, "A" * 32, "z" * 32])
def test_request_rejects_bad_operation_identity(operation_id):
    assert_reason(CohortProcessReason.INVALID, encode_cohort_process_request, operation_id, PAYLOAD)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"command": "shell", "arguments": {}},
        {"command": True, "arguments": {}},
        {"command": "stop", "arguments": []},
        {"command": "stop", "arguments": {}, "extra": "secret"},
        {"command": "stop", "arguments": {1: "secret"}},
        {"command": "stop", "arguments": {"bad": object()}},
        {"command": "stop", "arguments": {"bad": math.nan}},
        {"command": "stop", "arguments": {"bad": math.inf}},
        {"command": "stop", "arguments": {"bad": b"bytes"}},
        {"command": "stop", "arguments": {"bad": "\ud800"}},
    ],
)
def test_request_rejects_noncanonical_values(payload):
    assert_reason(CohortProcessReason.INVALID, encode_cohort_process_request, OPERATION_ID, payload)


@pytest.mark.parametrize(
    "encoded",
    [
        None,
        bytearray(b"{}"),
        b"{",
        b"\xff",
        b"[]",
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        pytest.param(b" " * (COHORT_PROCESS_REQUEST_MAX_BYTES + 1), id="oversized"),
    ],
)
def test_decoder_rejects_malformed_or_oversized_json(encoded):
    assert_reason(CohortProcessReason.INVALID, decode_cohort_process_request, encoded)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "old-schema"),
        ("schema_version", True),
        ("schema_version", 2),
        ("operation_id", "b"),
        ("payload", []),
        ("extra", None),
    ],
)
def test_decoder_rejects_wrong_envelope(field, value):
    wire = json.loads(encode_cohort_process_request(OPERATION_ID, PAYLOAD))
    wire[field] = value
    assert_reason(
        CohortProcessReason.INVALID, decode_cohort_process_request, json.dumps(wire).encode()
    )


def test_depth_cycle_and_separate_byte_caps():
    nested = {}
    for _ in range(33):
        nested = {"child": nested}
    assert_reason(
        CohortProcessReason.INVALID,
        encode_cohort_process_request,
        OPERATION_ID,
        {"command": "stop", "arguments": nested},
    )
    cyclic = {}
    cyclic["self"] = cyclic
    assert_reason(
        CohortProcessReason.INVALID,
        encode_cohort_process_request,
        OPERATION_ID,
        {"command": "stop", "arguments": cyclic},
    )
    large = {"receipt": "x" * (COHORT_PROCESS_RESPONSE_MAX_BYTES + 1)}
    assert (
        decode_cohort_process_request(
            encode_cohort_process_request(OPERATION_ID, {"command": "inspect", "arguments": large})
        ).payload["arguments"]
        == large
    )
    assert_reason(
        CohortProcessReason.INVALID,
        encode_cohort_process_response,
        OPERATION_ID,
        outcome="ok",
        payload=large,
    )
    assert_reason(
        CohortProcessReason.INVALID,
        encode_cohort_process_request,
        OPERATION_ID,
        {"command": "inspect", "arguments": {"receipt": "x" * COHORT_PROCESS_REQUEST_MAX_BYTES}},
    )


@pytest.mark.parametrize("outcome,payload", [(None, {}), (True, {}), ("unknown", {}), ("ok", [])])
def test_response_writer_rejects_invalid_values(outcome, payload):
    assert_reason(
        CohortProcessReason.INVALID,
        encode_cohort_process_response,
        OPERATION_ID,
        outcome=outcome,
        payload=payload,
    )


def test_fixed_exec_and_redacted_exact_operation(harness):
    ticket = harness.start()
    assert harness.supervisor.phase is CohortProcessPhase.RUNNING
    assert harness.supervisor.outstanding is ticket
    assert ticket.deadline == 110
    assert ticket.operation_id not in repr(ticket)
    args, kwargs = harness.popen.call_args
    assert args == (
        [module.sys.executable, "-m", "django_ray.runner.cohort_job_helper", "/private/helper"],
    )
    assert kwargs == {
        "stdin": module.subprocess.DEVNULL,
        "stdout": module.subprocess.DEVNULL,
        "stderr": module.subprocess.DEVNULL,
        "start_new_session": True,
        "close_fds": True,
    }
    assert "private-request" not in repr(args)
    assert decode_cohort_process_request(harness.write.call_args.args[2]).payload == PAYLOAD
    assert harness.supervisor.poll(ticket) is None
    harness.process.poll.assert_not_called()
    harness.read.assert_not_called()
    harness.killpg.assert_not_called()


@pytest.mark.parametrize("timeout", [None, True, 0, -1, 601, math.nan, math.inf])
def test_invalid_timeout_never_creates_file_or_process(harness, timeout):
    assert_reason(CohortProcessReason.INVALID, harness.start, timeout)
    harness.mkdir.assert_not_called()
    harness.popen.assert_not_called()


def test_invalid_payload_and_platform_never_spawn(harness, monkeypatch):
    assert_reason(
        CohortProcessReason.INVALID,
        harness.supervisor.start,
        {"command": "run", "arguments": {}},
        timeout_seconds=1,
    )
    monkeypatch.setattr(module.sys, "platform", "win32")
    assert_reason(CohortProcessReason.PLATFORM_UNSUPPORTED, harness.start)
    harness.mkdir.assert_not_called()


def test_busy_exact_ticket_owner_and_late_ticket(harness, monkeypatch):
    ticket = harness.start()
    assert_reason(CohortProcessReason.BUSY, harness.start)
    assert_reason(CohortProcessReason.STALE, harness.supervisor.poll, replace(ticket))
    monkeypatch.setattr(module.threading, "current_thread", lambda: object())
    assert_reason(CohortProcessReason.WRONG_OWNER, harness.supervisor.poll, ticket)
    assert_reason(CohortProcessReason.WRONG_OWNER, lambda: harness.supervisor.outstanding)
    assert_reason(CohortProcessReason.WRONG_OWNER, lambda: harness.supervisor.phase)
    harness.killpg.assert_not_called()


def test_success_requires_clean_exit_and_group_gone_then_releases(harness):
    ticket = harness.start()
    harness.exited = True
    harness.other_members = True
    assert harness.supervisor.poll(ticket) is None
    harness.process.poll.assert_not_called()
    harness.other_members = False
    result = harness.supervisor.poll(ticket)
    assert result.reason is None
    assert result.response == {"value": "private-response"}
    assert "private-response" not in repr(result)
    assert ticket.operation_id not in repr(result)
    assert harness.supervisor.outstanding is None
    assert harness.supervisor.phase is CohortProcessPhase.IDLE
    harness.process.poll.assert_called_once_with()
    harness.discard.assert_called_once()
    assert_reason(CohortProcessReason.STALE, harness.supervisor.poll, ticket)
    assert harness.start().operation_id != ticket.operation_id


def test_deadline_term_kill_and_late_success_is_discarded(harness):
    ticket = harness.start()
    harness.now = 110
    assert harness.supervisor.poll(ticket) is None
    assert harness.supervisor.phase is CohortProcessPhase.TERMINATING
    harness.killpg.assert_called_once_with(123, signal.SIGTERM)
    harness.now = 111.99
    harness.supervisor.poll(ticket)
    assert harness.killpg.call_count == 1
    harness.now = 112
    harness.supervisor.poll(ticket)
    assert harness.killpg.call_args.args == (123, signal.SIGKILL)
    assert harness.supervisor.phase is CohortProcessPhase.KILLING
    harness.now = 114
    harness.supervisor.poll(ticket)
    assert harness.supervisor.phase is CohortProcessPhase.QUARANTINED
    assert_reason(CohortProcessReason.BUSY, harness.start)
    harness.exited = True
    result = harness.supervisor.poll(ticket)
    assert result.reason is CohortProcessReason.DEADLINE
    assert result.response is None
    harness.read.assert_not_called()
    assert harness.killpg.call_count == 2


def test_exited_leader_is_retained_until_descendant_cleanup(harness):
    ticket = harness.start()
    harness.exited = True
    harness.other_members = True
    harness.now = 110
    harness.supervisor.poll(ticket)
    harness.now = 112
    harness.supervisor.poll(ticket)
    harness.now = 114
    harness.supervisor.poll(ticket)
    harness.process.poll.assert_not_called()
    assert harness.supervisor.outstanding is ticket
    assert harness.supervisor.phase is CohortProcessPhase.QUARANTINED
    harness.other_members = False
    assert harness.supervisor.poll(ticket).reason is CohortProcessReason.DEADLINE


def test_cancel_never_accepts_success(harness):
    ticket = harness.start()
    harness.supervisor.cancel(ticket)
    harness.exited = True
    result = harness.supervisor.poll(ticket)
    assert result.reason is CohortProcessReason.CANCELLED
    assert result.response is None
    harness.read.assert_not_called()


@pytest.mark.parametrize("bad_clock", [99, None, True, math.nan, math.inf, -1])
def test_bad_clock_stops_helper_and_retains_original_failure(harness, bad_clock):
    ticket = harness.start()
    harness.now = bad_clock
    assert harness.supervisor.poll(ticket) is None
    harness.killpg.assert_called_once_with(123, signal.SIGTERM)
    harness.now = 103
    harness.exited = True
    assert harness.supervisor.poll(ticket).reason is CohortProcessReason.CLOCK_FAILED


def test_clock_exception_and_cancel_clock_failure_are_redacted(harness):
    ticket = harness.start()
    harness.supervisor._monotonic = Mock(side_effect=RuntimeError("private-clock"))
    harness.supervisor.cancel(ticket)
    harness.exited = True
    result = harness.supervisor.poll(ticket)
    assert result.reason is CohortProcessReason.CANCELLED
    assert "private-clock" not in repr(result)


@pytest.mark.parametrize(
    "error",
    [
        ChildProcessError(errno.ECHILD, "private-pid"),
        PermissionError(errno.EPERM, "private-permission"),
    ],
)
def test_lost_child_identity_never_signals_or_reaps_replacement(harness, error):
    ticket = harness.start()
    harness.wait_error = error
    assert harness.supervisor.poll(ticket) is None
    harness.now = 112
    assert harness.supervisor.poll(ticket) is None
    harness.supervisor.cancel(ticket)
    harness.killpg.assert_not_called()
    harness.process.poll.assert_not_called()
    assert harness.supervisor.phase is CohortProcessPhase.QUARANTINED


def test_lost_identity_before_first_timeout_signal_is_not_signalled(harness):
    ticket = harness.start()
    harness.now = 110
    harness.wait_error = ChildProcessError(errno.ECHILD, "private-pid")
    assert harness.supervisor.poll(ticket) is None
    harness.killpg.assert_not_called()
    assert harness.supervisor.outstanding is ticket


def test_signal_uses_captured_owned_pid(harness):
    ticket = harness.start()
    harness.process.pid = 999
    harness.supervisor.cancel(ticket)
    harness.killpg.assert_called_once_with(123, signal.SIGTERM)


def test_wait_interrupted_is_retryable_but_wrong_identity_is_not(harness):
    ticket = harness.start()
    harness.wait_error = InterruptedError(errno.EINTR, "private-signal")
    assert harness.supervisor.poll(ticket) is None
    assert harness.supervisor.phase is CohortProcessPhase.RUNNING
    harness.wait_error = None
    harness.exited = True
    harness.wait_identity = 999
    assert harness.supervisor.poll(ticket) is None
    assert harness.supervisor.phase is CohortProcessPhase.QUARANTINED
    harness.process.poll.assert_not_called()


def test_unknown_group_scan_retains_leader_then_cleans_with_fixed_failure(harness):
    ticket = harness.start()
    harness.exited = True
    harness.other_members = None
    assert harness.supervisor.poll(ticket) is None
    harness.process.poll.assert_not_called()
    harness.other_members = False
    result = harness.supervisor.poll(ticket)
    assert result.reason is CohortProcessReason.OWNERSHIP_UNCERTAIN
    harness.read.assert_not_called()


@pytest.mark.parametrize("returncode", [None, "error"])
def test_uncertain_reap_never_retries_or_signals_numeric_pid(harness, returncode):
    ticket = harness.start()
    harness.exited = True
    if returncode == "error":
        harness.process.poll.side_effect = RuntimeError("private-reap")
    else:
        harness.process.poll.return_value = returncode
    assert harness.supervisor.poll(ticket) is None
    harness.now = 112
    harness.supervisor.poll(ticket)
    harness.supervisor.cancel(ticket)
    harness.process.poll.assert_called_once()
    harness.killpg.assert_not_called()
    assert harness.supervisor.outstanding is ticket


def test_reaped_group_still_present_quarantines_without_signalling(harness):
    ticket = harness.start()
    harness.exited = True
    harness.group_exists = True
    assert harness.supervisor.poll(ticket) is None
    harness.now = 112
    assert harness.supervisor.poll(ticket) is None
    harness.killpg.assert_not_called()
    harness.process.poll.assert_called_once()
    assert_reason(CohortProcessReason.BUSY, harness.start)
    harness.group_exists = False
    assert harness.supervisor.poll(ticket).reason is CohortProcessReason.OWNERSHIP_UNCERTAIN


@pytest.mark.parametrize(
    "response",
    [
        b"{",
        pytest.param(b"x" * (COHORT_PROCESS_RESPONSE_MAX_BYTES + 1), id="oversized"),
        encode_cohort_process_response("b" * 32, outcome="ok", payload={}),
        encode_cohort_process_response(
            OPERATION_ID, outcome="failed", payload={"private": "secret"}
        ),
    ],
)
def test_malformed_oversized_crossed_response_is_fixed_failure(harness, response):
    ticket = harness.start()
    harness.exited = True
    harness.read.return_value = response
    result = harness.supervisor.poll(ticket)
    assert result.reason is CohortProcessReason.INVALID_RESPONSE
    assert result.response is None


@pytest.mark.parametrize("exitcode,outcome", [(1, "ok"), (-9, "ok"), (0, "failed")])
def test_failed_exit_or_outcome_never_exposes_response(harness, exitcode, outcome):
    ticket = harness.start()
    harness.exited = True
    harness.process.poll.return_value = exitcode
    harness.read.return_value = encode_cohort_process_response(
        ticket.operation_id, outcome=outcome, payload={"private": "failure"}
    )
    result = harness.supervisor.poll(ticket)
    assert result.reason is CohortProcessReason.HELPER_FAILED
    assert result.response is None


def test_failed_local_file_cleanup_holds_slot_and_no_success_payload(harness):
    ticket = harness.start()
    harness.exited = True
    harness.discard.return_value = False
    assert harness.supervisor.poll(ticket) is None
    assert harness.supervisor.phase is CohortProcessPhase.QUARANTINED
    assert_reason(CohortProcessReason.BUSY, harness.start)
    harness.discard.return_value = True
    result = harness.supervisor.poll(ticket)
    assert result.reason is CohortProcessReason.LOCAL_CLEANUP_FAILED
    assert result.response is None


@pytest.mark.parametrize("error", [ProcessLookupError(), PermissionError("private-signal")])
def test_group_signal_errors_are_fixed_and_do_not_release_slot(harness, error):
    ticket = harness.start()
    harness.killpg.side_effect = error
    harness.supervisor.cancel(ticket)
    assert harness.supervisor.outstanding is ticket
    assert harness.supervisor.phase in (
        CohortProcessPhase.TERMINATING,
        CohortProcessPhase.QUARANTINED,
    )


def test_spawn_failure_discards_only_known_request(harness, monkeypatch):
    harness.popen.side_effect = OSError("private-spawn")
    unlink, close, rmdir = Mock(), Mock(), Mock()
    monkeypatch.setattr(module.os, "unlink", unlink)
    monkeypatch.setattr(module.os, "close", close)
    monkeypatch.setattr(module.os, "rmdir", rmdir)
    assert_reason(CohortProcessReason.SPAWN_FAILED, harness.start)
    unlink.assert_called_once_with("request.json", dir_fd=12)
    close.assert_called_once_with(12)
    rmdir.assert_called_once_with("/private/helper")
    assert harness.supervisor.outstanding is None


def fake_file_stat(**changes):
    values = {
        "st_mode": stat.S_IFREG | 0o600,
        "st_uid": 100,
        "st_nlink": 1,
        "st_size": 2,
        "st_mtime_ns": 1,
        "st_ctime_ns": 1,
        "st_dev": 10,
        "st_ino": 20,
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture
def file_os(monkeypatch):
    for name, value in {
        "O_DIRECTORY": 0x10000,
        "O_NOFOLLOW": 0x20000,
        "O_CLOEXEC": 0x40000,
        "O_NONBLOCK": 0x80000,
    }.items():
        monkeypatch.setattr(module.os, name, value, raising=False)
    state = SimpleNamespace(
        open=Mock(return_value=25),
        close=Mock(),
        fstat=Mock(return_value=fake_file_stat()),
        read=Mock(side_effect=[b"{}", b""]),
        write=Mock(side_effect=lambda fd, value: len(value)),
        fsync=Mock(),
        replace=Mock(),
        unlink=Mock(),
        rmdir=Mock(),
        stat=Mock(return_value=fake_file_stat()),
    )
    for name in vars(state):
        monkeypatch.setattr(module.os, name, getattr(state, name))
    monkeypatch.setattr(module.os, "getuid", lambda: 100, raising=False)
    return state


def test_private_file_read_is_bounded_regular_and_nofollow(file_os):
    assert module._read_file(12, "response.json", 20) == b"{}"
    flags = file_os.open.call_args.args[1]
    assert flags & module.os.O_NOFOLLOW
    assert flags & module.os.O_NONBLOCK
    assert file_os.open.call_args.kwargs == {"dir_fd": 12}
    assert all(call.args[1] <= 21 for call in file_os.read.call_args_list)
    file_os.close.assert_called_once_with(25)


@pytest.mark.parametrize(
    "change",
    [
        {"st_mode": stat.S_IFLNK | 0o600},
        {"st_mode": stat.S_IFIFO | 0o600},
        {"st_mode": stat.S_IFREG | 0o644},
        {"st_uid": 101},
        {"st_nlink": 2},
        {"st_size": 21},
    ],
)
def test_private_file_rejects_symlink_fifo_permissions_owner_links_size(file_os, change):
    file_os.fstat.return_value = fake_file_stat(**change)
    assert_reason(CohortProcessReason.INVALID, module._read_file, 12, "response.json", 20)
    file_os.read.assert_not_called()
    file_os.close.assert_called_once_with(25)


@pytest.mark.parametrize("change", [{"st_size": 3}, {"st_mtime_ns": 2}, {"st_ctime_ns": 2}])
def test_private_file_rejects_concurrent_change(file_os, change):
    file_os.fstat.side_effect = [fake_file_stat(), fake_file_stat(**change)]
    assert_reason(CohortProcessReason.INVALID, module._read_file, 12, "response.json", 20)


def test_private_file_rejects_growth_past_cap(file_os):
    file_os.read.side_effect = [b"x" * 21]
    assert_reason(CohortProcessReason.INVALID, module._read_file, 12, "response.json", 20)


@pytest.mark.parametrize(
    "change",
    [{"st_mode": stat.S_IFREG | 0o700}, {"st_mode": stat.S_IFDIR | 0o755}, {"st_uid": 101}],
)
def test_private_directory_rejects_nonprivate_identity(file_os, change):
    file_os.fstat.return_value = fake_file_stat(st_mode=stat.S_IFDIR | 0o700)
    vars(file_os.fstat.return_value).update(change)
    assert_reason(
        CohortProcessReason.INVALID, module._directory_fd, module.os.path.abspath("private")
    )
    file_os.close.assert_called_once_with(25)


def test_private_directory_requires_absolute_nofollow_path(file_os):
    assert_reason(CohortProcessReason.INVALID, module._directory_fd, "relative")
    file_os.open.assert_not_called()
    file_os.fstat.return_value = fake_file_stat(st_mode=stat.S_IFDIR | 0o700)
    assert module._directory_fd(module.os.path.abspath("private")) == 25
    assert file_os.open.call_args.args[1] & module.os.O_NOFOLLOW


def test_private_file_writer_exclusive_mode_and_partial_writes(file_os):
    file_os.write.side_effect = [1, 1]
    module._write_file(12, "request.json", b"{}")
    args = file_os.open.call_args.args
    assert args[1] & module.os.O_EXCL and args[1] & module.os.O_NOFOLLOW
    assert args[2] == 0o600
    file_os.fsync.assert_called_once_with(25)
    file_os.close.assert_called_once_with(25)


def test_private_writer_zero_progress_is_fixed_failure(file_os):
    file_os.write.side_effect = [0]
    assert_reason(CohortProcessReason.INVALID, module._write_file, 12, "request.json", b"{}")
    file_os.close.assert_called_once_with(25)


def test_helper_reads_shared_codec_and_writes_atomically(file_os, monkeypatch):
    monkeypatch.setattr(module, "_directory_fd", Mock(return_value=12))
    monkeypatch.setattr(
        module,
        "_read_file",
        Mock(return_value=encode_cohort_process_request(OPERATION_ID, PAYLOAD)),
    )
    request = module.read_cohort_process_request("/private/helper")
    assert request.operation_id == OPERATION_ID and request.payload == PAYLOAD
    module.write_cohort_process_response(
        "/private/helper",
        operation_id=request.operation_id,
        outcome="ok",
        payload={"prepared": True},
    )
    assert file_os.open.call_args.args[0] == "response.tmp"
    file_os.replace.assert_called_once_with(
        "response.tmp", "response.json", src_dir_fd=12, dst_dir_fd=12
    )
    assert file_os.close.call_args.args == (12,)


@pytest.mark.parametrize(
    "operation",
    [
        module.read_cohort_process_request,
        lambda directory: module.write_cohort_process_response(
            directory, operation_id=OPERATION_ID, outcome="ok", payload={}
        ),
    ],
)
def test_helper_file_failures_do_not_echo_diagnostics(operation, monkeypatch):
    monkeypatch.setattr(module, "_directory_fd", Mock(side_effect=OSError("private-path")))
    assert_reason(CohortProcessReason.INVALID, operation, "/private/helper")


def test_discard_requires_same_directory_inode_and_deletes_only_fixed_names(file_os):
    supervisor = CohortProcessSupervisor()
    running = SimpleNamespace(directory="/private/helper", directory_fd=12)
    assert supervisor._discard_directory(running)
    assert [call.args for call in file_os.unlink.call_args_list] == [
        ("request.json",),
        ("response.json",),
        ("response.tmp",),
    ]
    assert all(call.kwargs == {"dir_fd": 12} for call in file_os.unlink.call_args_list)
    file_os.rmdir.assert_called_once_with("/private/helper")
    file_os.close.assert_called_once_with(12)


@pytest.mark.parametrize("failure", ["identity", "unlink", "rmdir"])
def test_discard_refuses_replaced_or_unclean_directory(file_os, failure):
    if failure == "identity":
        file_os.stat.return_value = fake_file_stat(st_ino=999)
    else:
        getattr(file_os, failure).side_effect = OSError("private-file")
    assert not CohortProcessSupervisor()._discard_directory(
        SimpleNamespace(directory="/private/helper", directory_fd=12)
    )
    file_os.close.assert_not_called()
    if failure == "identity":
        file_os.unlink.assert_not_called()


def test_discard_missing_files_is_safe(file_os):
    file_os.unlink.side_effect = FileNotFoundError()
    assert CohortProcessSupervisor()._discard_directory(
        SimpleNamespace(directory="/private/helper", directory_fd=12)
    )


class Entries:
    def __init__(self, names):
        self.names = names

    def __enter__(self):
        return (SimpleNamespace(name=name) for name in self.names)

    def __exit__(self, *args):
        pass


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"456 (helper name) S 1 123 123", True),
        (b"456 (other) S 1 456 456", False),
        (b"malformed", None),
        (b"456 (helper) S 1 bad 123", None),
        (b"x" * 4097, None),
    ],
)
def test_group_scan_is_bounded_and_handles_odd_process_names(file_os, monkeypatch, raw, expected):
    monkeypatch.setattr(module.os, "scandir", lambda path: Entries(["self", "123", "456"]))
    file_os.read.side_effect = [raw]
    assert module._other_group_members(123) is expected
    file_os.open.assert_called_once()
    assert file_os.open.call_args.args[0] == "/proc/456/stat"
    file_os.read.assert_called_once_with(25, 4097)


def test_group_scan_limits_even_unrelated_entries(file_os, monkeypatch):
    monkeypatch.setattr(module, "_GROUP_SCAN_LIMIT", 3)
    monkeypatch.setattr(module.os, "scandir", lambda path: Entries(["self"] * 4))
    assert module._other_group_members(123) is None
    file_os.open.assert_not_called()


@pytest.mark.parametrize(
    "error,expected", [(FileNotFoundError(), False), (PermissionError(), None)]
)
def test_group_scan_races_or_denials_never_invent_membership(file_os, monkeypatch, error, expected):
    monkeypatch.setattr(module.os, "scandir", lambda path: Entries(["456"]))
    file_os.open.side_effect = error
    assert module._other_group_members(123) is expected


@pytest.mark.parametrize(
    "error,expected", [(None, True), (ProcessLookupError(), False), (PermissionError(), True)]
)
def test_group_existence_uses_only_signal_zero(monkeypatch, error, expected):
    killpg = Mock(side_effect=error)
    monkeypatch.setattr(module.os, "killpg", killpg, raising=False)
    assert module._group_exists(123) is expected
    killpg.assert_called_once_with(123, 0)


@pytest.mark.parametrize("items", [[None] * 8193, {str(i): None for i in range(4100)}])
def test_encoder_rejects_excess_nodes_before_serializing(monkeypatch, items):
    dumps = Mock(side_effect=AssertionError("must refuse before allocation"))
    monkeypatch.setattr(module.json, "dumps", dumps)
    assert_reason(
        CohortProcessReason.INVALID,
        encode_cohort_process_request,
        OPERATION_ID,
        {"command": "inspect", "arguments": {"items": items}},
    )
    dumps.assert_not_called()


@pytest.mark.parametrize("wire", [b"[" + b"0," * 8192 + b"0]", b"[" * 34 + b"0" + b"]" * 34])
def test_decoder_rejects_expansion_before_loading(monkeypatch, wire):
    loads = Mock(side_effect=AssertionError("must refuse before allocation"))
    monkeypatch.setattr(module.json, "loads", loads)
    assert_reason(CohortProcessReason.INVALID, decode_cohort_process_request, wire)
    loads.assert_not_called()


def test_encoder_preflights_cumulative_utf8_string_bytes(monkeypatch):
    dumps = Mock(side_effect=AssertionError("must refuse before allocation"))
    monkeypatch.setattr(module.json, "dumps", dumps)
    assert_reason(
        CohortProcessReason.INVALID,
        encode_cohort_process_response,
        OPERATION_ID,
        outcome="ok",
        payload={"first": "ą" * 65536, "second": "ą" * 65536},
    )
    dumps.assert_not_called()


def test_wire_scanner_ignores_escaped_json_inside_strings():
    payload = {"command": "inspect", "arguments": {"receipt": '[{"nested": "\\\\""}]' * 1000}}
    assert (
        decode_cohort_process_request(encode_cohort_process_request(OPERATION_ID, payload)).payload
        == payload
    )


@pytest.mark.parametrize("when", ["decode", "discard"])
@pytest.mark.parametrize(
    "clock,reason",
    [
        (110, CohortProcessReason.DEADLINE),
        (None, CohortProcessReason.CLOCK_FAILED),
        (99, CohortProcessReason.CLOCK_FAILED),
    ],
)
def test_success_rechecks_deadline_after_file_decode_and_cleanup(harness, when, clock, reason):
    ticket = harness.start()
    harness.exited = True

    def late(*args):
        harness.now = clock
        return harness.read.return_value if when == "decode" else True

    (harness.read if when == "decode" else harness.discard).side_effect = late
    result = harness.supervisor.poll(ticket)
    assert result.reason is reason
    assert result.response is None
    harness.killpg.assert_not_called()
    assert harness.supervisor.outstanding is None


@pytest.mark.skipif(
    sys.platform != "linux", reason="Linux exec/file/group plumbing; no Ray initialization"
)
def test_linux_fixed_helper_reaps_refused_inspect_and_removes_private_files(tmp_path, monkeypatch):
    """A real fixed helper rejects absent inspect fields before any native work."""
    original_mkdtemp = module.tempfile.mkdtemp
    monkeypatch.setattr(
        module.tempfile, "mkdtemp", lambda *, prefix: original_mkdtemp(prefix=prefix, dir=tmp_path)
    )
    supervisor = CohortProcessSupervisor()
    ticket = supervisor.start({"command": "inspect", "arguments": {}}, timeout_seconds=15)
    running = supervisor._running
    assert running is not None
    directory, pid = running.directory, running.pid
    completion = None
    try:
        until = time.monotonic() + 15
        while time.monotonic() < until and completion is None:
            completion = supervisor.poll(ticket)
            if completion is None:
                time.sleep(0.01)
    finally:
        if supervisor.outstanding is not None:
            supervisor.cancel(ticket)
            cleanup_until = time.monotonic() + 6
            while time.monotonic() < cleanup_until and supervisor.outstanding is not None:
                supervisor.poll(ticket)
                time.sleep(0.01)
    assert completion is not None
    assert completion.reason is CohortProcessReason.HELPER_FAILED
    assert completion.response is None
    assert supervisor.outstanding is None
    assert not module.os.path.lexists(directory)
    with pytest.raises(ProcessLookupError):
        module.os.killpg(pid, 0)
