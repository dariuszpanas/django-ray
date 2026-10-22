"""Resource-free refusal/cleanup tests; simulated Kubernetes is not deployed proof."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from qualification.application import worker_image_data as data_probe
from qualification.application import worker_images as probe

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "example/current@sha256:" + "a" * 64
OWNER = "a" * 24


def source():
    return b"\n---\n".join(
        (ROOT / f"k8s/overlays/kuberay-kind/worker-{mode}.yaml").read_bytes()
        for mode in probe.MODES
    )


def original():
    steps = yaml.safe_load((ROOT / "qualification/application/core.yaml").read_text())["spec"][
        "steps"
    ]
    for step in steps:
        for operation in step.get("try", []):
            item = operation.get("apply", operation.get("create", {})).get("resource", {})
            if item.get("kind") == "Deployment" and item["metadata"]["name"] == "django-manager":
                item = copy.deepcopy(item)
                item["metadata"]["uid"] = "original-manager"
                item["metadata"]["generation"] = 1
                item["status"] = {"observedGeneration": 1}
                item["spec"]["template"]["spec"]["containers"][0]["image"] = IMAGE
                return item
    raise AssertionError("Missing actual source manager")


def pod(manifest, name):
    template = manifest["spec"]["template"]
    return {
        "metadata": {"name": name, "uid": name + "-uid", **template["metadata"]},
        "spec": copy.deepcopy(template["spec"]),
        "status": {
            "containerStatuses": [
                {
                    "ready": True,
                    "restartCount": 0,
                    "imageID": "containerd://sha256:" + "a" * 64,
                    "state": {"running": {}},
                }
            ]
        },
    }


@pytest.mark.parametrize("image_id", [None, ""])
def test_pending_container_without_image_id_waits_for_real_readiness(monkeypatch, image_id):
    # Kubernetes emitted this state for the restored manager in the failed
    # hosted observation. It has not started a container whose digest can be read.
    pending = pod(original(), "pending-manager")
    status = pending["status"]["containerStatuses"][0]
    pending["status"]["phase"] = "Pending"
    status.update(ready=False, state={"waiting": {"reason": "ContainerCreating"}})
    if image_id is None:
        status.pop("imageID")
    else:
        status["imageID"] = image_id
    running = pod(original(), "pending-manager")
    clock = [0.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        probe.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    observations = iter([pending, pending, running])
    observer = probe.Observer(None, [], "owned", 2)
    assert observer.wait(lambda: probe.ready([next(observations)], IMAGE)) is running
    assert clock[0] == 0.5


def test_pending_image_identity_does_not_extend_observation_deadline(monkeypatch):
    pending = pod(original(), "pending-manager")
    pending["status"]["containerStatuses"][0].update(
        ready=False, imageID="", state={"waiting": {"reason": "ContainerCreating"}}
    )
    clock = [0.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        probe.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    observer = probe.Observer(None, [], "owned", 1.5)
    with pytest.raises(TimeoutError) as caught:
        observer.wait(lambda: probe.ready([pending], IMAGE))
    assert clock[0] == 0.5
    diagnostics = probe._Diagnostics()
    diagnostics.capture(caught.value)
    assert diagnostics.receipt()["timeout_source"] == "poll_reserve_reached"


@pytest.mark.parametrize("remaining", [0, -0.001])
def test_command_deadline_refuses_execution_and_records_its_bound(monkeypatch, remaining):
    monkeypatch.setattr(probe.time, "monotonic", lambda: 75 - remaining)

    def checked(*_args, **_kwargs):
        pytest.fail("An exhausted deadline must not execute a command")

    observer = probe.Observer(checked, [], "owned", 75)
    with pytest.raises(TimeoutError) as caught:
        observer.call("get", "pods")
    diagnostics = probe._Diagnostics()
    diagnostics.capture(caught.value)
    assert diagnostics.receipt()["timeout_source"] == "deadline_before_command"


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (3, "remaining_budget_limit"),
        (10, "remaining_budget_limit"),
        (10.001, "command_limit"),
        (17, "command_limit"),
    ],
)
def test_command_timeout_records_selected_bound_not_later_clock(monkeypatch, remaining, expected):
    clock = [75 - remaining]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])
    calls = []

    def checked(argv, *, data, timeout):
        calls.append((data, timeout))
        clock[0] = 121  # Attribution must not be recomputed after command failure.
        raise subprocess.TimeoutExpired(
            argv, timeout, output=b"private output", stderr=b"private stderr"
        )

    observer = probe.Observer(checked, [], "owned", 75)
    with pytest.raises(TimeoutError) as caught:
        observer.call("private command")
    diagnostics = probe._Diagnostics()
    diagnostics.phase = "stopped_ml"
    diagnostics.capture(caught.value)
    value = diagnostics.receipt()
    probe.validate_failure_receipt(value)
    assert calls == [(None, min(10, remaining))]
    assert value["timeout_source"] == expected
    assert "private" not in str(caught.value) + json.dumps(value)


def test_first_timeout_bound_and_independent_cleanup_bound_survive(monkeypatch):
    clock = [72.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])

    def checked(argv, *, data, timeout):
        raise subprocess.TimeoutExpired(argv, timeout, stderr=b"private failure")

    observer = probe.Observer(checked, [], "owned", 75)
    diagnostics = probe._Diagnostics()
    diagnostics.phase = "stopped_ml"
    with pytest.raises(TimeoutError) as initial:
        observer.call("private initial command")
    diagnostics.capture(initial.value)
    clock[0] = 80
    observer.deadline = 120
    diagnostics.phase = "cleanup_restore"
    with pytest.raises(TimeoutError) as cleanup:
        observer.call("private cleanup command")
    diagnostics.capture(cleanup.value, cleanup=True)
    diagnostics.facts.update(dict.fromkeys(probe.CLEANUP_FACTS, True))
    diagnostics.phase = "receipt"
    diagnostics.capture(ValueError("private later wrapper failure"))
    value = diagnostics.receipt()
    probe.validate_failure_receipt(value)
    assert (value["phase"], value["category"], value["timeout_source"]) == (
        "stopped_ml",
        "timeout",
        "remaining_budget_limit",
    )
    assert (value["cleanup_phase"], value["cleanup_category"], value["cleanup_timeout_source"]) == (
        "cleanup_restore",
        "timeout",
        "command_limit",
    )
    assert all(value[key] is True for key in probe.CLEANUP_FACTS)
    assert "private" not in json.dumps(value)


@pytest.mark.parametrize(
    "change", ["declared-image", "observed-image", "restart", "boolean-restart", "running", "ready"]
)
def test_pending_readiness_preserves_identity_and_state_refusals(change):
    pending = pod(original(), "pending-manager")
    status = pending["status"]["containerStatuses"][0]
    status.update(ready=False, imageID="", state={"waiting": {"reason": "ContainerCreating"}})
    if change == "declared-image":
        pending["spec"]["containers"][0]["image"] = "unexpected"
    elif change == "observed-image":
        status["imageID"] = "containerd://sha256:" + "b" * 64
    elif change == "restart":
        status["restartCount"] = 1
    elif change == "boolean-restart":
        status["restartCount"] = False
    elif change == "running":
        status["state"] = {"running": {}}
    else:
        status["ready"] = True
    with pytest.raises(ValueError):
        probe.ready([pending], IMAGE)


def test_source_derived_commands_and_admitted_template_are_preserved():
    commands = probe.source_commands(source())
    base = original()
    saved = copy.deepcopy(base)
    for mode in probe.MODES:
        value = probe.deployment(base, commands, OWNER, mode)
        container = value["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == commands[mode][0]
        assert container["args"][-2:] == ["--concurrency", "20"]
        assert (
            container["resources"] == base["spec"]["template"]["spec"]["containers"][0]["resources"]
        )
        assert container["image"] == IMAGE
        assert {"name": "DJANGO_RAY_QUEUES", "value": mode} in container["env"]
        assert (
            "os.kill(1,signal.SIGINT)" in container["lifecycle"]["preStop"]["exec"]["command"][-1]
        )
        if mode == "ml":
            assert "ray://ray-head:10001" in container["args"]
    assert base == saved


@pytest.mark.parametrize("change", ["command", "queue", "concurrency", "container", "oversize"])
def test_changed_source_contract_refuses_before_mutation(change):
    raw = source()
    if change == "oversize":
        raw = b"x" * (1024 * 1024 + 1)
    else:
        rows = list(yaml.safe_load_all(raw))
        item = rows[0]["spec"]["template"]["spec"]["containers"][0]
        if change == "command":
            item["command"] = ["unreviewed"]
        elif change == "queue":
            item["args"][2] = "other"
        elif change == "concurrency":
            item["args"][-1] = "21"
        else:
            rows[0]["spec"]["template"]["spec"]["containers"].append(copy.deepcopy(item))
        raw = yaml.safe_dump_all(rows).encode()
    with pytest.raises(ValueError):
        probe.source_commands(raw)


def receipt():
    return {
        "schema_version": 1,
        "modes": {mode: dict.fromkeys(probe.FACTS, True) for mode in probe.MODES},
        "substitutions": list(probe.SUBSTITUTIONS),
        "rows_removed": True,
        "history_preserved": True,
        "original_restored": True,
    }


@pytest.mark.parametrize("mode", probe.MODES)
@pytest.mark.parametrize("fact", probe.FACTS)
def test_mode_receipt_requires_every_literal_true_fact(mode, fact):
    value = receipt()
    value["modes"][mode][fact] = 1
    with pytest.raises(ValueError):
        probe.validate_receipt(value)


@pytest.mark.parametrize("fact", ["rows_removed", "history_preserved", "original_restored"])
def test_missing_cleanup_or_preservation_is_not_success(fact):
    value = receipt()
    value[fact] = False
    with pytest.raises(ValueError):
        probe.validate_receipt(value)


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "initialize",
        "original-controller",
        "prepare",
        "create",
        "process",
        "pod-replaced",
        "deployment-replaced",
        "pod-restarted",
        "task",
        "task-and-restore",
        "stopped",
        "remove",
        "controller-remains",
        "replicaset-remains",
        "data-cleanup",
        "missing-state",
        "malformed-cleanup",
        "ownership",
        "restore",
        "delayed-cleanup",
        "work-deadline",
        "delayed-readiness-success",
        "ready-deadline",
        "phase-deadline",
        "total-deadline",
    ],
)
def test_simulated_application_flow_is_sequential_and_restores_after_failure(monkeypatch, failure):
    clock = [0.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        probe.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(probe.secrets, "token_hex", lambda _count: OWNER)
    base = original()
    live = {"django-manager": base}
    pods = {"django-manager": pod(base, "django-manager-original")}
    calls = []
    created = []

    def checked(argv, *, data=None, timeout=40):
        assert 0 < timeout <= 10
        assert clock[0] + timeout <= 180
        calls.append(argv)
        if "kustomize" in argv:
            return source()
        args = argv[argv.index("owned") + 1 :]
        command = args[0]
        if command == "get":
            if args[1] == "replicasets":
                if failure == "original-controller" and "app=django-manager" in args:
                    return b'{"items":[{"spec":{"replicas":1}}]}'
                return (
                    b'{"items":[{}]}'
                    if failure == "replicaset-remains" and created
                    else b'{"items":[]}'
                )
            if args[1] == "pods":
                selector = args[args.index("-l") + 1]
                if (
                    failure == "total-deadline"
                    and selector == "app=django-manager"
                    and pods.get("django-manager", {}).get("metadata", {}).get("name")
                    == "django-manager-restored"
                ):
                    clock[0] = 180.001
                labels = dict(item.split("=", 1) for item in selector.split(","))
                if selector == "app=django-web":
                    return json.dumps({"items": [{"metadata": {"name": "web-owned"}}]}).encode()
                selected = [
                    item
                    for item in pods.values()
                    if all(
                        item["metadata"]["labels"].get(key) == value
                        for key, value in labels.items()
                    )
                ]
                if failure in {"delayed-readiness-success", "ready-deadline"} and selected:
                    mode = labels.get("image-mode")
                    if (mode == "sync" and clock[0] < 40) or (
                        mode == "ml" and (failure == "ready-deadline" or clock[0] < 130)
                    ):
                        selected = copy.deepcopy(selected)
                        selected[0]["status"]["containerStatuses"][0]["ready"] = False
                return json.dumps({"items": selected}).encode()
            item = copy.deepcopy(live.get(args[2]))
            if item and failure == "ownership" and created and args[2] == "django-manager":
                item["metadata"]["uid"] = "foreign"
            return b"" if item is None else json.dumps(item).encode()
        if command == "scale":
            if "--replicas=0" in args:
                pods.pop("django-manager")
                base["spec"]["replicas"] = 0
                base["metadata"]["generation"] = 2
                base["status"]["observedGeneration"] = 2
            else:
                if failure == "restore":
                    raise RuntimeError("private restore failure")
                if failure == "task-and-restore":
                    raise TimeoutError("private later restoration timeout")
                pods["django-manager"] = pod(base, "django-manager-restored")
                if failure == "phase-deadline":
                    clock[0] = 180.001
            return b""
        if command == "create":
            assert not pods, "Never overlap original or temporary managers"
            manifest = json.loads(data)
            name = manifest["metadata"]["name"]
            manifest["metadata"]["uid"] = name + "-uid"
            live[name] = manifest
            pods[name] = pod(manifest, name + "-pod")
            created.append(name)
            if failure == "create":
                raise RuntimeError("Ambiguous creation response with private content")
            return json.dumps(manifest).encode()
        if command == "delete":
            name = args[2]
            if failure == "remove":
                raise RuntimeError("private removal failure")
            if failure != "controller-remains":
                live.pop(name)
            pods.pop(name, None)
            return b""
        assert command == "exec"
        if "qualification.application.worker_image_data" in args:
            action = args[args.index(OWNER) + 1]
            if action == "initialize":
                if failure == "initialize":
                    raise RuntimeError("private initialization failure")
                return b'{"initialized":true}'
            if action == "prepare":
                if failure == "prepare":
                    raise RuntimeError("private prepare failure")
                return b'{"prepared":true}'
            if action == "observe":
                assert (
                    args[args.index("--hostname") + 1]
                    == next(iter(pods.values()))["metadata"]["name"]
                )
                if failure == "pod-replaced":
                    next(iter(pods.values()))["metadata"]["uid"] = "replacement"
                if failure == "deployment-replaced":
                    live[created[-1]]["metadata"]["uid"] = "replacement"
                if failure == "pod-restarted":
                    next(iter(pods.values()))["status"]["containerStatuses"][0]["restartCount"] = 1
                if failure in {"task", "delayed-cleanup", "task-and-restore"}:
                    raise RuntimeError("private task failure")
                if failure == "work-deadline":
                    clock[0] = 135
                    return b'{"complete":false}'
                return b'{"complete":true,"one_attempt":true,"sentinel_unchanged":true}'
            if action == "stopped":
                if failure == "stopped":
                    raise RuntimeError("private stop failure")
                return b'{"inactive_lease":true,"sentinel_unchanged":true}'
            if failure == "data-cleanup":
                raise RuntimeError("private row cleanup failure")
            if failure == "missing-state":
                return b'{"rows_removed":true,"history_preserved":false}'
            if failure == "malformed-cleanup":
                return b'{"rows_removed":true,"history_preserved":true,"unexpected":true}'
            if failure == "delayed-cleanup":
                clock[0] = 154
            if failure == "phase-deadline":
                clock[0] = 179
            return b'{"rows_removed":true,"history_preserved":true}'
        item = next(item for item in pods.values() if item["metadata"]["name"] == args[1])
        container = item["spec"]["containers"][0]
        value = container["command"] + container["args"]
        if failure == "process":
            value = ["unrelated"]
        return json.dumps(value).encode()

    if failure in {None, "delayed-readiness-success"}:
        value = probe.run(
            checked=checked,
            kubectl=["kubectl"],
            namespace="owned",
            image=IMAGE,
            root=ROOT,
        )
        probe.validate_receipt(value)
        assert created == [f"mode-{OWNER}-{mode}" for mode in probe.MODES]
        if failure == "delayed-readiness-success":
            # Sequential polling completes beyond the former work and total caps.
            assert clock[0] == 130
    else:
        with pytest.raises(probe.WorkerImageObservationError) as error:
            probe.run(
                checked=checked,
                kubectl=["kubectl"],
                namespace="owned",
                image=IMAGE,
                root=ROOT,
            )
        assert "private" not in str(error.value)
        diagnostic = error.value.diagnostic()
        probe.validate_failure_receipt(diagnostic)
        assert "private" not in json.dumps(diagnostic)
        expected_phases = {
            "initialize": "initialize",
            "original-controller": "quiescence",
            "prepare": "prepare_sync",
            "create": "create_sync",
            "process": "process_sync",
            "pod-replaced": "observe_sync",
            "deployment-replaced": "remove_sync",
            "pod-restarted": "observe_sync",
            "task": "observe_sync",
            "task-and-restore": "observe_sync",
            "stopped": "stopped_sync",
            "remove": "remove_sync",
            "controller-remains": "remove_sync",
            "replicaset-remains": "remove_sync",
            "data-cleanup": "cleanup_rows",
            "missing-state": "cleanup_rows",
            "malformed-cleanup": "cleanup_rows",
            "ownership": "cleanup_restore",
            "restore": "cleanup_restore",
            "delayed-cleanup": "observe_sync",
            "work-deadline": "observe_sync",
            "ready-deadline": "ready_ml",
            "phase-deadline": "cleanup_restore",
            "total-deadline": "deadline",
        }
        assert diagnostic["phase"] == expected_phases[failure]
        if failure == "remove":
            assert diagnostic["category"] == "command"
            assert diagnostic["cleanup_phase"] == "cleanup_remove_sync"
            assert diagnostic["cleanup_category"] == "command"
            assert diagnostic["temporary_removed"] is False
            assert diagnostic["original_restored"] is False
        if failure == "missing-state":
            assert diagnostic["rows_removed"] is True
            assert diagnostic["history_preserved"] is False
            assert diagnostic["original_restored"] is True
        if failure == "malformed-cleanup":
            assert diagnostic["rows_removed"] is False
            assert diagnostic["history_preserved"] is False
            assert diagnostic["original_restored"] is True
        if failure == "task-and-restore":
            assert diagnostic["category"] == "command"
            assert diagnostic["cleanup_phase"] == "cleanup_restore"
            assert diagnostic["cleanup_category"] == "timeout"
            assert diagnostic["cleanup_timeout_source"] == "unclassified"
            assert diagnostic["temporary_removed"] is True
            assert diagnostic["original_restored"] is False
        if failure in {"work-deadline", "ready-deadline", "phase-deadline", "total-deadline"}:
            assert diagnostic["category"] == "timeout"
            assert (
                diagnostic["timeout_source"]
                == {
                    "work-deadline": "poll_reserve_reached",
                    "ready-deadline": "poll_reserve_reached",
                    "phase-deadline": "deadline_before_command",
                    "total-deadline": "total_phase_limit",
                }[failure]
            )
        if failure == "ready-deadline":
            assert clock[0] == 134  # At most one second remains for another poll.
            assert created == [f"mode-{OWNER}-{mode}" for mode in probe.MODES]
            assert diagnostic["cleanup_phase"] is None
            assert all(diagnostic[key] is True for key in probe.CLEANUP_FACTS)
        # The caller gets a copy; a poisoned consumer cannot mutate the exception.
        diagnostic["phase"] = "private consumer content"
        assert error.value.diagnostic()["phase"] == expected_phases[failure]
    assert any("cleanup" in call for call in calls)
    if failure == "work-deadline":
        assert created == [f"mode-{OWNER}-sync"]
        assert clock[0] == 135  # The reserved cleanup/restoration path remained available.
    if failure == "original-controller":
        assert created == []
    restore_calls = [call for call in calls if "--replicas=1" in call]
    assert bool(restore_calls) is (
        failure
        not in {
            "ownership",
            "remove",
            "controller-remains",
            "replicaset-remains",
            "deployment-replaced",
        }
    )
    if failure not in {
        "remove",
        "ownership",
        "restore",
        "task-and-restore",
        "controller-remains",
        "replicaset-remains",
        "deployment-replaced",
    }:
        assert list(pods) == ["django-manager"]


@pytest.mark.parametrize(
    ("error", "category", "timeout_source"),
    [
        (TimeoutError("private timeout"), "timeout", "unclassified"),
        (subprocess.TimeoutExpired("private argv", 10), "timeout", "command_limit"),
        (RuntimeError("private command"), "command", None),
        (ValueError("private source"), "validation", None),
        (OSError("private unavailable"), "unavailable", None),
    ],
)
def test_preflight_failure_is_fixed_and_does_not_claim_cleanup(error, category, timeout_source):
    def checked(*_args, **_kwargs):
        raise error

    with pytest.raises(probe.WorkerImageObservationError) as caught:
        probe.run(checked=checked, kubectl=["kubectl"], namespace="owned", image=IMAGE, root=ROOT)
    value = caught.value.diagnostic()
    assert value == {
        "schema_version": 2,
        "phase": "source_render",
        "category": category,
        "timeout_source": timeout_source,
        "cleanup_phase": None,
        "cleanup_category": None,
        "cleanup_timeout_source": None,
        **dict.fromkeys(probe.CLEANUP_FACTS, False),
    }
    assert "private" not in str(caught.value) + json.dumps(value)


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "phase",
        "category",
        "timeout_source",
        "cleanup_phase",
        "cleanup_category",
        "cleanup_timeout_source",
        *probe.CLEANUP_FACTS,
        "raw",
    ],
)
def test_failure_receipt_rejects_unbounded_or_malformed_fields(field):
    value = {
        "schema_version": 2,
        "phase": "ready_sync",
        "category": "validation",
        "timeout_source": None,
        "cleanup_phase": "cleanup_restore",
        "cleanup_category": "timeout",
        "cleanup_timeout_source": "unclassified",
        **dict.fromkeys(probe.CLEANUP_FACTS, False),
    }
    value[field] = "private arbitrary response"
    with pytest.raises(ValueError, match="Invalid worker image failure receipt"):
        probe.WorkerImageObservationError(value)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 1},
        {"timeout_source": None},
        {"timeout_source": "private text"},
        {"timeout_source": False},
        {"category": "command"},
        {"cleanup_timeout_source": None},
        {"cleanup_timeout_source": "private text"},
        {"cleanup_timeout_source": False},
        {"cleanup_category": "command"},
        {"cleanup_phase": None, "cleanup_category": None},
    ],
)
def test_timeout_sources_must_match_failure_and_cleanup_categories(change):
    value = {
        "schema_version": 2,
        "phase": "stopped_ml",
        "category": "timeout",
        "timeout_source": "remaining_budget_limit",
        "cleanup_phase": "cleanup_restore",
        "cleanup_category": "timeout",
        "cleanup_timeout_source": "command_limit",
        **dict.fromkeys(probe.CLEANUP_FACTS, False),
    }
    probe.validate_failure_receipt(value)
    value.update(change)
    with pytest.raises(ValueError, match="Invalid worker image failure receipt"):
        probe.WorkerImageObservationError(value)


def test_independent_phase_starts_once_after_late_cold_success_without_admission_drift(monkeypatch):
    clock = [1800.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])

    def checked(*_args, **_kwargs):
        clock[0] += 0.001
        return source()

    def get(observer, *_args):
        assert observer.deadline == 1935
        assert clock[0] == 1800.001
        raise ValueError("stopped before mutation")

    monkeypatch.setattr(probe.Observer, "get", get)
    with pytest.raises(probe.WorkerImageObservationError) as caught:
        probe.run(
            checked=checked,
            kubectl=[],
            namespace="owned",
            image=IMAGE,
            root=ROOT,
        )
    assert caught.value.diagnostic()["phase"] == "original_observation"
    assert caught.value.diagnostic()["category"] == "validation"
    assert "stopped before mutation" not in str(caught.value)


@pytest.mark.parametrize("elapsed", [135, 180])
def test_source_preflight_consumes_phase_budget_and_exhaustion_prevents_mutation(
    monkeypatch, elapsed
):
    clock = [100.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])
    calls = []

    def checked(argv, **kwargs):
        calls.append(argv)
        assert "kustomize" in argv
        assert kwargs["timeout"] == 10
        clock[0] += elapsed
        return source()

    with pytest.raises(probe.WorkerImageObservationError) as caught:
        probe.run(checked=checked, kubectl=[], namespace="owned", image=IMAGE, root=ROOT)
    assert len(calls) == 1
    assert caught.value.diagnostic()["phase"] == "original_observation"
    assert caught.value.diagnostic()["category"] == "timeout"
    assert caught.value.diagnostic()["timeout_source"] == "deadline_before_command"


def test_host_installs_yaml_for_manifest_rendering_within_existing_tool_budget():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/application-qualification.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["application-core"]["steps"]
    tools = next(item for item in steps if item.get("name") == "Install pinned public test tools")
    execution = next(
        item for item in steps if item.get("name") == "Run the native application core test"
    )
    assert tools["timeout-minutes"] == 5
    assert "python -m pip install PyYAML==6.0.3" in tools["run"].splitlines()
    assert steps.index(tools) < steps.index(execution)


def test_hosted_step_accounts_for_catalogue_and_worker_phases_with_outer_allowance():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/application-qualification.yml").read_text()
    )
    job = workflow["jobs"]["application-core"]
    step = next(
        item for item in job["steps"] if item.get("name") == "Run the native application core test"
    )
    assert job["timeout-minutes"] == 130
    assert step["timeout-minutes"] * 60 - 2820 - probe.PHASE_SECONDS == 300
    assert probe.PHASE_SECONDS == 180
    assert probe.CLEANUP_SECONDS == 45
    assert probe.PHASE_SECONDS - probe.CLEANUP_SECONDS == 135


@pytest.fixture
def database_observer(monkeypatch, tmp_path, settings):
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease

    monkeypatch.setattr(data_probe, "state_path", lambda _owner: tmp_path / "state.json")
    monkeypatch.setattr(data_probe, "database_limits", lambda: None)
    tasks = copy.deepcopy(settings.TASKS)
    tasks["default"]["QUEUES"] = ["default", "sync", "ml", "low-priority"]
    settings.TASKS = tasks

    def snapshot():
        return {
            model.__name__: json.loads(
                json.dumps(list(model.objects.order_by("pk").values()), default=str)
            )
            for model in (RayTaskExecution, TaskAttempt, TaskWorkerLease)
        }

    monkeypatch.setattr(data_probe, "snapshot", snapshot)
    old = RayTaskExecution.objects.create(
        task_id="old", callable_path="removed.task", state="SUCCEEDED"
    )
    TaskWorkerLease.objects.create(worker_id="old", hostname="old", pid=999, is_active=False)
    before = snapshot()
    return SimpleNamespace(old=old, before=before, snapshot=snapshot)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "sentinel",
        "result",
        "generation",
        "attempt",
        "foreign-worker",
        "other-incarnation",
        "pid",
        "active-cleanup",
        "history",
    ],
)
def test_data_observer_exact_identity_preservation_and_cleanup(database_observer, failure):
    from django.utils import timezone

    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease

    assert data_probe.run(OWNER, "initialize") == {"initialized": True}
    assert data_probe.run(OWNER, "prepare", "sync") == {"prepared": True}
    expected_hostname = f"mode-{OWNER}-sync-pod"
    assert data_probe.run(OWNER, "observe", "sync", expected_hostname) == {"complete": False}
    row = RayTaskExecution.objects.get(queue_name="sync")
    row.state = "SUCCEEDED"
    row.execution_generation = 2 if failure == "generation" else 1
    row.result_data = "6" if failure == "result" else "5"
    row.claimed_by_worker = "owned"
    row.save()
    TaskAttempt.objects.create(execution=row, attempt_number=1, state="SUCCEEDED")
    if failure == "attempt":
        TaskAttempt.objects.create(execution=row, attempt_number=2, state="FAILED")
    hostname = "foreign" if failure == "foreign-worker" else f"mode-{OWNER}-sync-pod"
    if failure == "other-incarnation":
        hostname += "-replacement"
    lease = TaskWorkerLease.objects.create(
        worker_id="owned", hostname=hostname, pid=2 if failure == "pid" else 1, queue_name="sync"
    )
    if failure == "sentinel":
        RayTaskExecution.objects.filter(queue_name="low-priority").update(error_message="changed")
    if failure in {
        "sentinel",
        "result",
        "generation",
        "attempt",
        "foreign-worker",
        "other-incarnation",
        "pid",
    }:
        with pytest.raises((ValueError, TaskAttempt.MultipleObjectsReturned)):
            data_probe.run(OWNER, "observe", "sync", expected_hostname)
    else:
        assert data_probe.run(OWNER, "observe", "sync", expected_hostname) == {
            "complete": True,
            "one_attempt": True,
            "sentinel_unchanged": True,
        }
    if failure == "active-cleanup":
        with pytest.raises(ValueError, match="still active"):
            data_probe.run(OWNER, "cleanup")
        assert RayTaskExecution.objects.count() == 3
    lease.is_active = False
    lease.stopped_at = timezone.now()
    lease.save()
    if failure is None:
        assert data_probe.run(OWNER, "stopped", "sync", expected_hostname) == {
            "inactive_lease": True,
            "sentinel_unchanged": True,
        }
    if failure in {"other-incarnation", "pid"}:
        with pytest.raises(ValueError, match="another worker"):
            data_probe.run(OWNER, "stopped", "sync", expected_hostname)
    if failure == "foreign-worker":
        lease.delete()  # Test-owned foreign fixture, never deleted by the observer.
    if failure == "history":
        RayTaskExecution.objects.filter(pk=database_observer.old.pk).update(error_message="changed")
        with pytest.raises(ValueError, match="preservation"):
            data_probe.run(OWNER, "cleanup")
    else:
        assert data_probe.run(OWNER, "cleanup") == {"rows_removed": True, "history_preserved": True}
        assert database_observer.snapshot() == database_observer.before


@pytest.mark.django_db(transaction=True)
def test_ownership_write_failure_rolls_back_enqueued_rows(database_observer, monkeypatch):
    data_probe.run(OWNER, "initialize")
    monkeypatch.setattr(data_probe, "save", lambda *_args: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        data_probe.run(OWNER, "prepare", "sync")
    assert database_observer.snapshot() == database_observer.before


@pytest.mark.parametrize("owner", ["", "../file", "a" * 23, "A" * 24, "a" * 25])
def test_state_path_rejects_unowned_names(owner):
    with pytest.raises(ValueError):
        data_probe.state_path(owner)


def test_state_read_and_write_have_finite_limits(tmp_path):
    path = tmp_path / "state.json"
    with pytest.raises(ValueError):
        data_probe.save(path, {"large": "x" * data_probe.MAX_STATE_BYTES})
    assert not path.exists()
    path.write_bytes(b"x" * (data_probe.MAX_STATE_BYTES + 1))
    with pytest.raises(ValueError):
        data_probe.read(path)
