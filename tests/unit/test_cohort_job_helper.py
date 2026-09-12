"""The fixed dispatcher never executes request-selected Python code."""

from types import SimpleNamespace

import pytest

from django_ray.runner import cohort_job_helper as helper


@pytest.fixture
def transport(monkeypatch, tmp_path):
    state = SimpleNamespace(
        payload={"command": "inspect", "arguments": {"marker": "private"}},
        calls=[],
        writes=[],
        directory=str(tmp_path),
    )
    monkeypatch.setattr(
        helper,
        "read_cohort_process_request",
        lambda _directory: SimpleNamespace(operation_id="a" * 32, payload=state.payload),
    )
    monkeypatch.setattr(
        helper,
        "write_cohort_process_response",
        lambda *args, **kwargs: state.writes.append((args, kwargs)),
    )
    from django_ray.runner import cohort_job_control

    def execute(command, arguments):
        state.calls.append((command, arguments))
        return {"pending": True}

    monkeypatch.setattr(cohort_job_control, "execute_cohort_job_control", execute)
    return state


@pytest.mark.parametrize(
    "command", ["prepare", "submit", "inspect", "stop", "discover-client", "inspect-driver"]
)
def test_fixed_command_executes_once_and_returns_bound_response(transport, capsys, command):
    transport.payload["command"] = command
    assert helper.main([transport.directory]) == 0
    assert transport.calls == [(command, {"marker": "private"})]
    assert transport.writes == [
        (
            (transport.directory,),
            {"operation_id": "a" * 32, "outcome": "ok", "payload": {"pending": True}},
        )
    ]
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "os.system", "arguments": {}},
        {"command": "submit", "arguments": {}, "nonce": "private"},
        {"command": ["submit"], "arguments": {}},
        {"command": "submit", "arguments": "private"},
        {},
        None,
    ],
)
def test_invalid_command_is_not_dispatched_or_echoed(transport, capsys, payload):
    transport.payload = payload
    assert helper.main([transport.directory]) == 1
    assert not transport.calls
    assert transport.writes[0][1] == {
        "operation_id": "a" * 32,
        "outcome": "failed",
        "payload": {"reason": "operation_refused"},
    }
    assert capsys.readouterr() == ("", "")


def test_failed_operation_never_returns_exception_text(transport, monkeypatch, capsys):
    def fail(_payload):
        raise RuntimeError("https://private-endpoint?token=private")

    monkeypatch.setattr(helper, "_execute", fail)
    assert helper.main([transport.directory]) == 1
    assert transport.writes[0][1]["payload"] == {"reason": "operation_refused"}
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("arguments", [[], ["relative"], [1], ["/one", "/two"], "/one"])
def test_invalid_argv_never_reads_ipc(monkeypatch, arguments):
    monkeypatch.setattr(
        helper, "read_cohort_process_request", lambda _directory: pytest.fail("Unexpected IPC")
    )
    assert helper.main(arguments) == 2


@pytest.mark.parametrize("stage", ["read", "write"])
def test_transport_error_emits_no_untrusted_text(transport, monkeypatch, capsys, stage):
    def fail(*_args, **_kwargs):
        raise OSError("private filesystem path")

    name = "read_cohort_process_request" if stage == "read" else "write_cohort_process_response"
    monkeypatch.setattr(helper, name, fail)
    assert helper.main([transport.directory]) == 2
    assert len(transport.calls) == (stage == "write")
    assert capsys.readouterr() == ("", "")


def test_signal_interruption_is_not_a_success_response(transport, monkeypatch):
    def interrupt(_payload):
        raise KeyboardInterrupt

    monkeypatch.setattr(helper, "_execute", interrupt)
    with pytest.raises(KeyboardInterrupt):
        helper.main([transport.directory])
    assert not transport.writes
