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
    assert value["schema_version"] == 2
    assert value["source_role"] is None
    assert value["inventory_reason"] == (
        "inventory_read" if phase in {"inventory", "final_inventory"} else None
    )
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


@pytest.mark.parametrize(
    "change,reason,role",
    [
        ("missing-pod", "pod_count", "manager"),
        ("duplicate-pod", "pod_count", "manager"),
        ("spec", "container_spec_count", "manager"),
        ("status", "container_status_count", "manager"),
        ("deleting", "pod_deleting", "manager"),
        ("image", "declared_image", "manager"),
        ("image-id", "observed_image", "manager"),
        ("restart", "container_restarted", "manager"),
        ("boolean-restart", "container_restarted", "manager"),
        ("running", "container_not_running", "manager"),
        ("ready", "container_not_ready", "manager"),
        ("coordinate", "source_coordinates", "manager"),
        ("exit", "generation_exit", "assertions"),
    ],
)
def test_inventory_failure_identifies_exact_role_and_predicate_without_source_values(
    change, reason, role
):
    underlying, calls = host_fixture()

    def run(argv, **kwargs):
        result = underlying(argv, **kwargs)
        value = json.loads(result.stdout)
        items = value["items"]
        manager = items[1]
        status = manager["status"]["containerStatuses"][0]
        if change == "missing-pod":
            items.pop(1)
        elif change == "duplicate-pod":
            items.append(manager)
        elif change == "spec":
            manager["spec"]["containers"] = []
        elif change == "status":
            manager["status"]["containerStatuses"] = []
        elif change == "deleting":
            manager["metadata"]["deletionTimestamp"] = "private-canary"
        elif change == "image":
            manager["spec"]["containers"][0]["image"] = "private-canary"
        elif change == "image-id":
            status["imageID"] = "private-canary"
        elif change in {"restart", "boolean-restart"}:
            status["restartCount"] = 1 if change == "restart" else False
        elif change == "running":
            status["state"] = {}
        elif change == "ready":
            status["ready"] = False
        elif change == "coordinate":
            del manager["metadata"]["uid"]
        elif change == "exit":
            items[2]["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] = 1
        return subprocess.CompletedProcess(argv, 0, json.dumps(value).encode(), b"")

    with pytest.raises((ValueError, KeyError)) as caught:
        observe(run)
    encoded = diagnostics.diagnostic(caught.value, "host")
    value = json.loads(encoded)
    assert value["failure_phase"] == "inventory"
    assert value["source_role"] == role
    assert value["inventory_reason"] == reason
    assert len(calls) == 1  # Failed inventory never reads or compares private logs.
    assert "private-canary" not in encoded
    assert len(encoded.encode()) + 1 <= diagnostics.MAX_BYTES


@pytest.mark.parametrize("change", ["ready", "identity"])
def test_final_inventory_rechecks_readiness_and_immutable_identity(change):
    underlying, calls = host_fixture()
    inventories = 0

    def run(argv, **kwargs):
        nonlocal inventories
        result = underlying(argv, **kwargs)
        if "get" in argv:
            inventories += 1
            if inventories == 2:
                value = json.loads(result.stdout)
                manager = value["items"][1]
                if change == "ready":
                    manager["status"]["containerStatuses"][0]["ready"] = False
                else:
                    manager["metadata"]["uid"] = "private-canary"
                return subprocess.CompletedProcess(argv, 0, json.dumps(value).encode(), b"")
        return result

    with pytest.raises(ValueError) as caught:
        observe(run)
    encoded = diagnostics.diagnostic(caught.value, "host")
    value = json.loads(encoded)
    assert value["failure_phase"] == "final_inventory"
    assert value["source_role"] == "manager"
    assert value["inventory_reason"] == (
        "container_not_ready" if change == "ready" else "source_changed"
    )
    assert "private-canary" not in encoded
    assert len(calls) == 7


@pytest.mark.parametrize("field", ["source_role", "inventory_reason"])
@pytest.mark.parametrize("invalid", ["private-canary", {"message": "private-canary"}])
def test_forged_inventory_attribution_is_dropped(field, invalid):
    error = ValueError("private-canary")
    value = json.loads(diagnostics.diagnostic(error, "host"))
    value.update(
        failure_phase="inventory", source_role="manager", inventory_reason="container_not_ready"
    )
    value[field] = invalid
    error.__dict__["_protected_log_failure"] = value
    encoded = diagnostics.diagnostic(error, "host")
    value = json.loads(encoded)
    assert value["failure_phase"] == "unavailable"
    assert value["source_role"] is None
    assert value["inventory_reason"] is None
    assert "private-canary" not in encoded


@pytest.mark.parametrize("role", ["django-manager", "django-web", "ray-head", "ray-worker"])
def test_pod_and_event_roles_correlate_without_retaining_identity_or_probe_output(role):
    from qualification.application import safe_diagnostics as safe

    name = role + "-67b96f8ff-2k4jz"
    timestamp = "2026-10-21T02:10:03.575737Z"
    pod = {
        "metadata": {"name": name, "uid": "private-canary"},
        "status": {
            "phase": "Running",
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "lastTransitionTime": timestamp,
                    "message": "private-canary",
                },
                {"type": "private-canary", "status": "False"},
            ],
            "containerStatuses": [
                {"name": "manager", "ready": False, "restartCount": 0, "state": {"running": {}}}
            ],
        },
    }
    event = {
        "involvedObject": {"kind": "Pod", "name": name, "uid": "private-canary"},
        "reason": "Unhealthy",
        "type": "Warning",
        "count": 111,
        "message": "Readiness probe failed: private-canary timed out",
        "firstTimestamp": timestamp,
        "lastTimestamp": "2026-10-21T02:11:03Z",
        "eventTime": "2026-10-21T02:11:03.123456789Z",
        "series": {"lastObservedTime": "2026-10-21T02:11:04Z"},
    }
    observed_pod = safe.resources("pods", [pod])[0]
    observed_event = safe.resources("events", [event])[0]
    assert observed_pod["role"] == observed_event["role"] == role
    assert observed_pod["conditions"] == [
        {"type": "Ready", "status": "False", "last_transition_time": timestamp}
    ]
    assert observed_pod["containers"][0]["role"] == "manager"
    assert observed_event["probe_failure"] == "readiness_failed"
    assert observed_event["count"] == 111
    assert observed_event["first_timestamp"] == timestamp
    assert observed_event["last_timestamp"] == event["lastTimestamp"]
    assert observed_event["event_time"] == event["eventTime"]
    assert observed_event["series_last_observed_time"] == event["series"]["lastObservedTime"]
    for record in (observed_pod, observed_event):
        assert safe._timestamp(record["observed_at"]) is not None
        encoded = json.dumps(record)
        assert "private-canary" not in encoded
        assert name not in encoded
        assert "timed out" not in encoded


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Liveness probe failed: private-canary", "liveness_failed"),
        ("Startup probe failed: private-canary", "startup_failed"),
        (
            "Readiness probe errored and resulted in unknown state: private-canary",
            "readiness_errored",
        ),
        ("private-canary Readiness probe failed: timed out", "unavailable"),
        ("private-canary timeout", "unavailable"),
        ({"message": "private-canary"}, "unavailable"),
    ],
)
def test_event_probe_category_requires_a_known_kubelet_prefix(message, expected):
    from qualification.application.safe_diagnostics import resources

    event = {
        "message": message,
        "reason": "Unhealthy",
        "involvedObject": {"kind": "Pod", "name": "django-manager-private-canary"},
    }
    assert resources("events", [event])[0]["probe_failure"] == expected
    event["reason"] = "Failed"
    assert resources("events", [event])[0]["probe_failure"] == "unavailable"


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "private-canary",
        "2026-99-21T02:10:03Z",
        "2026-10-21T02:10:03+01:00",
        {"message": "private-canary"},
    ],
)
def test_untrusted_event_coordinates_and_timestamps_do_not_escape_projection(invalid):
    from qualification.application.safe_diagnostics import resources

    event = {
        "involvedObject": {"kind": "Pod", "name": invalid},
        "firstTimestamp": invalid,
        "lastTimestamp": invalid,
        "eventTime": invalid,
        "series": {"lastObservedTime": invalid},
    }
    clean = resources("events", [event])[0]
    assert clean["role"] == "unavailable"
    assert clean["first_timestamp"] is None
    assert clean["last_timestamp"] is None
    assert clean["event_time"] is None
    assert clean["series_last_observed_time"] is None
    assert "private-canary" not in json.dumps(clean)
