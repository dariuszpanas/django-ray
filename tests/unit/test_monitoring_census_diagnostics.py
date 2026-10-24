"""Resource-free contracts for failure-only final census diagnostics."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from qualification.monitoring import census_diagnostics as diagnostic
from qualification.monitoring import run

IMAGES = {
    name: name + "@sha256:" + "a" * 64
    for name in ("application", "postgres", "prometheus", "grafana", "ray", "importer")
}


@pytest.fixture
def payload():
    groups = [
        [("django-web", "application")],
        [("postgres", "postgres")],
        [("prometheus", "prometheus")],
        [("grafana", "grafana")],
        [("ray-head", "ray"), ("dashboard-importer", "importer")],
        [("ray-worker", "ray")],
    ]
    return {
        "items": [
            {
                "spec": {
                    "containers": [{"name": name, "image": IMAGES[image]} for name, image in group]
                },
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [
                        {
                            "name": name,
                            "ready": True,
                            "restartCount": 0,
                            "imageID": "sha256:" + "a" * 64,
                        }
                        for name, _ in group
                    ],
                },
            }
            for group in groups
        ]
    }


class Snapshot:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def get(self, resource):
        self.calls.append(resource)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_success_retains_the_same_snapshot_and_has_no_diagnostic(payload):
    kube, receipt = Snapshot(payload), {}
    expected = run.census(deepcopy(payload), IMAGES)
    assert run.final_pod_census(kube, IMAGES, receipt) is payload
    assert receipt == {"containers": expected}
    assert len(expected) == 7 and kube.calls == ["pods"]


def test_diagnostics_preserve_existing_status_map_and_truthiness_acceptance(payload):
    pod = payload["items"][0]
    status = pod["status"]["containerStatuses"][0]
    pod["status"]["containerStatuses"].insert(0, {**status, "restartCount": 4})
    pod["status"]["containerStatuses"].append({"name": "ignored-extra-status"})
    status.update(ready=1, restartCount=False)
    receipt = {}
    run.final_pod_census(Snapshot(payload), IMAGES, receipt)
    assert len(receipt["containers"]) == 7 and "census_diagnostic" not in receipt


@pytest.mark.parametrize(
    "reason",
    [
        "pod_count",
        "pod_phase",
        "pod_ready_condition",
        "unexpected_component",
        "duplicate_component",
        "spec_image_mismatch",
        "missing_container_status",
        "container_restart_count",
        "container_not_ready",
        "image_id_invalid",
        "init_restart_count",
        "init_not_successful",
        "component_set_incomplete",
    ],
)
def test_each_existing_failed_check_has_a_closed_reason(payload, reason):
    pod = payload["items"][0]
    status = pod["status"]["containerStatuses"][0]
    if reason == "pod_count":
        payload["items"].pop()
    elif reason == "pod_phase":
        pod["status"]["phase"] = "private-phase-canary"
    elif reason == "pod_ready_condition":
        pod["status"]["conditions"] = []
    elif reason == "unexpected_component":
        pod["spec"]["containers"][0]["name"] = "private-name-canary"
    elif reason == "duplicate_component":
        payload["items"][-1] = deepcopy(pod)
    elif reason == "spec_image_mismatch":
        pod["spec"]["containers"][0]["image"] = "private-image-canary"
    elif reason == "missing_container_status":
        pod["status"]["containerStatuses"] = []
    elif reason == "container_restart_count":
        status["restartCount"] = "private-restart-canary"
    elif reason == "container_not_ready":
        status["ready"] = False
    elif reason == "image_id_invalid":
        status["imageID"] = "private-image-id-canary"
    elif reason in {"init_restart_count", "init_not_successful"}:
        pod["status"]["initContainerStatuses"] = [
            {
                "restartCount": 1 if reason == "init_restart_count" else 0,
                "state": {"terminated": {"exitCode": 1}},
            }
        ]
    else:
        payload["items"][4]["spec"]["containers"].pop()
    kube, receipt = Snapshot(payload), {}
    with pytest.raises(diagnostic.CensusError) as caught:
        run.final_pod_census(kube, IMAGES, receipt)
    assert caught.value.reason == reason
    assert receipt == {"census_diagnostic": diagnostic.record("pod_census", reason)}
    assert "private-" not in json.dumps(receipt)
    assert kube.calls == ["pods"]


@pytest.mark.parametrize(
    "field",
    [
        "root",
        "items",
        "pod",
        "status",
        "spec",
        "containers",
        "container",
        "statuses",
        "status_name",
        "init",
        "init_state",
    ],
)
def test_malformed_snapshot_never_exports_its_contents(payload, field):
    pod = payload["items"][0]
    if field == "root":
        payload = None
    elif field == "items":
        payload["items"] = None
    elif field == "pod":
        payload["items"][0] = None
    elif field == "status":
        pod["status"] = None
    elif field == "spec":
        del pod["spec"]
    elif field == "containers":
        pod["spec"]["containers"] = None
    elif field == "container":
        pod["spec"]["containers"][0] = None
    elif field == "statuses":
        pod["status"]["containerStatuses"] = None
    elif field == "status_name":
        pod["status"]["containerStatuses"][0] = {"private-key-canary": "private-value-canary"}
    elif field == "init":
        pod["status"]["initContainerStatuses"] = [None]
    else:
        pod["status"]["initContainerStatuses"] = [{"restartCount": 0, "state": None}]
    receipt, kube = {}, Snapshot(payload)
    with pytest.raises((AttributeError, KeyError, TypeError, IndexError)):
        run.final_pod_census(kube, IMAGES, receipt)
    assert receipt == {"census_diagnostic": diagnostic.record("pod_census", "malformed_payload")}
    assert kube.calls == ["pods"]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("private-query-canary"),
        TimeoutError("private-timeout-canary"),
        json.JSONDecodeError("private-json-canary", "private-body-canary", 0),
    ],
)
def test_query_failure_preserves_exception_without_census(monkeypatch, error):
    monkeypatch.setattr(run, "census", lambda *_: pytest.fail("census reached after query failure"))
    receipt, kube = {}, Snapshot(error)
    with pytest.raises(type(error)) as caught:
        run.final_pod_census(kube, IMAGES, receipt)
    assert caught.value is error and kube.calls == ["pods"]
    assert receipt == {"census_diagnostic": diagnostic.record("pod_fetch", "query_failed")}


def test_unclassified_failure_preserves_exception_and_closed_fallback(monkeypatch, payload):
    error = RuntimeError("private-exception-canary")

    def fail(*_):
        raise error

    monkeypatch.setattr(run, "census", fail)
    receipt = {}
    with pytest.raises(RuntimeError) as caught:
        run.final_pod_census(Snapshot(payload), IMAGES, receipt)
    assert caught.value is error
    assert receipt == {"census_diagnostic": diagnostic.record("pod_census", "unclassified")}


def test_check_order_preserves_first_failure(payload):
    first = payload["items"][0]
    first["status"]["conditions"] = []
    payload["items"][1]["status"]["phase"] = "Pending"
    with pytest.raises(diagnostic.CensusError) as caught:
        run.census(payload, IMAGES)
    assert caught.value.reason == "pod_ready_condition"
    first["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    payload["items"][1]["status"]["phase"] = "Running"
    first["spec"]["containers"][0]["image"] = "wrong-image"
    first["status"]["containerStatuses"] = []
    with pytest.raises(diagnostic.CensusError) as caught:
        run.census(payload, IMAGES)
    assert caught.value.reason == "spec_image_mismatch"


@pytest.mark.parametrize(
    "stage,reason",
    [
        (stage, reason)
        for stage, reasons in diagnostic.REASONS.items()
        for reason in sorted(reasons)
    ],
)
def test_every_record_is_closed_and_bounded(stage, reason):
    value = diagnostic.record(stage, reason)
    assert diagnostic.valid(value)
    assert set(value) == {"schema_version", "stage", "reason"}
    assert len(json.dumps(value).encode()) <= diagnostic.MAX_BYTES


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"stage": "unknown"},
        {"stage": []},
        {"reason": []},
        {"reason": "x" * 513},
        {"reason": "query_failed"},
        {"extra": "private-canary"},
    ],
)
def test_record_validation_rejects_unknown_malformed_or_contradictory_fields(change):
    value = diagnostic.record("pod_census", "pod_count") | change
    assert not diagnostic.valid(value)


def test_record_constructor_never_echoes_invalid_inputs():
    with pytest.raises(ValueError, match="^Invalid monitoring census diagnostic$"):
        diagnostic.record("private-stage-canary", "private-reason-canary")
