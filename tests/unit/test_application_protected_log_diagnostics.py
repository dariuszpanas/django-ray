"""Finite failure attribution without private text or native execution."""

import io
import json
import subprocess
from types import SimpleNamespace

import pytest

from qualification.application import protected_log_diagnostics as diagnostics
from qualification.application import protected_logs as logs
from tests.unit.test_application_protected_surfaces import IMAGE, host_fixture


def child_record():
    return {
        "schema_version": 1,
        "layer": "application_protected_logs",
        "status": "failed",
        "failure_scope": "child",
        "failure_phase": "django_setup",
        "failure_type": "timeout",
        "child_phase": None,
    }


def observe(run):
    return logs.observe(
        context="owned-context",
        kubeconfig_paths="/original/config",
        namespace="django-ray-core-owned",
        image=IMAGE,
        generation="before",
        run=run,
    )


@pytest.mark.parametrize(
    "position,phase",
    list(
        enumerate(
            (
                "inventory",
                "core_receipt",
                "status_check",
                "web_log",
                "manager_log",
                "private_compare",
                "final_inventory",
            )
        )
    ),
)
def test_each_host_timeout_retains_only_its_phase(position, phase):
    underlying, _ = host_fixture()
    calls = 0

    def run(argv, **kwargs):
        nonlocal calls
        current, calls = calls, calls + 1
        if current == position:
            raise subprocess.TimeoutExpired(
                ["private-canary"],
                kwargs["timeout"],
                output=b"private-canary",
                stderr=b"private-canary",
            )
        return underlying(argv, **kwargs)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        observe(run)
    encoded = diagnostics.diagnostic(caught.value, "host")
    value = json.loads(encoded)
    assert value["failure_phase"] == phase
    assert value["failure_type"] == "timeout"
    assert value["child_phase"] is None
    assert "private-canary" not in encoded
    assert len(encoded.encode()) + 1 <= diagnostics.MAX_BYTES
    assert calls == position + 1


def test_host_propagates_only_validated_child_phase_on_failed_exec():
    underlying, calls = host_fixture()

    def run(argv, **kwargs):
        if "--inspect-status" in argv:
            return subprocess.CompletedProcess(
                argv, 1, json.dumps(child_record()).encode(), b"private-canary wrapper stderr"
            )
        return underlying(argv, **kwargs)

    with pytest.raises(diagnostics.ChildFailureError) as caught:
        observe(run)
    value = json.loads(diagnostics.diagnostic(caught.value, "host"))
    assert value["failure_phase"] == "status_check"
    assert value["failure_type"] == "timeout"
    assert value["child_phase"] == "django_setup"
    assert "private-canary" not in json.dumps(value)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "phase",
        "type",
        "scope",
        "version-bool",
        "child-phase",
        "duplicate",
        "trailing",
        "oversized",
        "invalid-utf8",
        "nested",
        "nonfinite",
    ],
)
def test_malformed_private_diagnostics_never_propagate(mutation):
    value = child_record()
    if mutation == "extra":
        value["message"] = "private-canary"
    elif mutation == "phase":
        value["failure_phase"] = "private-canary"
    elif mutation == "type":
        value["failure_type"] = "private-canary"
    elif mutation == "scope":
        value["failure_scope"] = "host"
    elif mutation == "version-bool":
        value["schema_version"] = True
    elif mutation == "child-phase":
        value["child_phase"] = "django_setup"
    elif mutation == "nested":
        value["failure_phase"] = {"message": "private-canary"}
    elif mutation == "nonfinite":
        value["schema_version"] = float("nan")
    raw = json.dumps(value).encode()
    if mutation == "duplicate":
        raw = raw[:-1] + b',"schema_version":1}'
    elif mutation == "trailing":
        raw += b" private-canary"
    elif mutation == "oversized":
        raw += b" " * diagnostics.MAX_BYTES
    elif mutation == "invalid-utf8":
        raw = b"\xffprivate-canary"
    assert diagnostics.parse_child(raw) is None
    underlying, _ = host_fixture()

    def run(argv, **kwargs):
        if "--inspect-status" in argv:
            return subprocess.CompletedProcess(argv, 1, raw, b"private-canary")
        return underlying(argv, **kwargs)

    with pytest.raises(ValueError) as caught:
        observe(run)
    encoded = diagnostics.diagnostic(caught.value, "host")
    assert json.loads(encoded)["child_phase"] is None
    assert "private-canary" not in encoded


def test_failure_object_on_successful_exit_cannot_be_a_success_receipt():
    underlying, _ = host_fixture()

    def run(argv, **kwargs):
        if "--inspect-status" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(child_record()).encode(), b"")
        return underlying(argv, **kwargs)

    with pytest.raises(ValueError) as caught:
        observe(run)
    value = json.loads(diagnostics.diagnostic(caught.value, "host"))
    assert value["failure_phase"] == "status_check"
    assert value["child_phase"] is None


def test_actual_child_database_timeout_is_a_bounded_private_failure(monkeypatch, capsys):
    packet = {
        "protected_runtime": {
            "api_clear": True,
            "row_ids": [1, 2, 3],
            "started_at": "2026-10-17T00:00:00Z",
        }
    }
    monkeypatch.setattr(logs.sys, "platform", "linux")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setattr(
        logs.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(packet).encode()))
    )
    monkeypatch.setattr(logs.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(logs.signal, "signal", lambda *args: None)
    alarms = []
    monkeypatch.setattr(logs.signal, "alarm", alarms.append, raising=False)
    monkeypatch.setattr(
        logs,
        "read_owned_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("private-canary")),
    )
    assert logs.main(["--inspect-status"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert diagnostics.parse_child(captured.out.encode()) is not None
    assert json.loads(captured.out) == {**child_record(), "failure_phase": "read_only_db"}
    assert "private-canary" not in captured.out
    assert alarms == [4]


@pytest.mark.parametrize(
    "error,kind",
    [
        (ValueError("private-canary"), "invalid"),
        (OSError("private-canary"), "unavailable"),
        (TimeoutError("private-canary"), "timeout"),
    ],
)
def test_unknown_cli_errors_and_malicious_attached_fields_are_suppressed(
    tmp_path, monkeypatch, capsys, error, kind
):
    error.__dict__["_protected_log_failure"] = {**child_record(), "message": "private-canary"}
    monkeypatch.setattr(logs, "observe", lambda **kwargs: (_ for _ in ()).throw(error))
    output = tmp_path / "receipt.json"
    assert (
        logs.main(
            [
                "--context=owned",
                "--kubeconfig-paths=/owned",
                "--namespace=django-ray-core-owned",
                "--image=" + IMAGE,
                "--generation=before",
                "--output=" + str(output),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    value = json.loads(captured.err)
    assert captured.out == ""
    assert "private-canary" not in captured.err
    assert value["failure_scope"] == "host"
    assert value["failure_phase"] == "unavailable"
    assert value["failure_type"] == kind
    assert not output.exists()


def test_http_socket_timeout_keeps_the_source_owned_timeout_category():
    from qualification.application.run_api import (
        ApplicationHttpError,
        HttpFailureCode,
        HttpTransportFailure,
    )

    error = ApplicationHttpError(
        "private-canary",
        code=HttpFailureCode.REQUEST,
        status=None,
        transport_failure=HttpTransportFailure.TIMEOUT,
    )
    assert diagnostics.failure_type(error) == "timeout"


@pytest.mark.parametrize(
    "module",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(ApplicationHttpError="private-canary"),
        SimpleNamespace(ApplicationHttpError=ValueError, HttpTransportFailure=None),
        SimpleNamespace(ApplicationHttpError=ValueError, HttpTransportFailure=object),
    ],
)
def test_partial_http_import_cannot_break_or_leak_failure_diagnostics(monkeypatch, module):
    monkeypatch.setitem(diagnostics.sys.modules, "qualification.application.run_api", module)
    encoded = diagnostics.diagnostic(ImportError("private-canary"), "child")
    assert json.loads(encoded)["failure_type"] == "unavailable"
    assert "private-canary" not in encoded
