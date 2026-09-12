"""Local effect semantics only; these tests provide no native upgrade evidence."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import ray
import ray._private.worker

import django_ray
from django_ray.runtime.context import durable_task_execution
from tests.unit.test_upgrade_runtime_settings import environment as environment
from tests.unit.test_upgrade_runtime_settings import module as module


@pytest.fixture
def tasks(settings, module):
    settings.TASKS = module["TASKS"]
    settings.DJANGO_RAY = module["DJANGO_RAY"]
    return importlib.import_module("qualification.upgrade.runtime_tasks")


def test_fresh_interpreter_imports_tasks_under_exact_finite_upgrade_settings(environment):
    pytest.importorskip("psycopg", reason="Fresh PostgreSQL settings import needs its driver only")
    child_environment = dict(os.environ)
    child_environment.update(environment)
    child_environment["DJANGO_SETTINGS_MODULE"] = "qualification.upgrade.runtime_settings"
    code = """
import sys
import django
from django.db.backends.base.base import BaseDatabaseWrapper
def forbidden(*args, **kwargs):
    raise AssertionError("task import must not connect to the database")
BaseDatabaseWrapper.connect = forbidden
django.setup()
assert "qualification.upgrade.runtime_tasks" not in sys.modules
from qualification.upgrade import runtime_tasks
for name in ("value", "gated_effect", "failed", "retried"):
    task = getattr(runtime_tasks, name)
    assert task.backend == "default" and task.queue_name == "upgrade-core"
    routed = task.using(backend="jobs", queue_name="upgrade-jobs")
    assert routed.backend == "jobs" and routed.queue_name == "upgrade-jobs"
assert "ray" not in sys.modules
print("finite-upgrade-task-import-passed")
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=child_environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "finite-upgrade-task-import-passed"


@pytest.fixture
def workflow(monkeypatch):
    from django_ray import workflows

    state = SimpleNamespace(steps=[], chains=[], policies=[], runs=[], result=42, on_run=None)

    def step(callable_obj, **kwargs):
        signature = object()
        state.steps.append((callable_obj, kwargs, signature))
        return signature

    def run(*args, **kwargs):
        state.runs.append((args, kwargs))
        if state.on_run is not None:
            state.on_run()
        return state.result

    def reporting(policy):
        state.policies.append(policy)
        return SimpleNamespace(run=run)

    def chain(*signatures):
        state.chains.append(signatures)
        return SimpleNamespace(with_progress_reporting=reporting)

    monkeypatch.setattr(workflows, "step", step)
    monkeypatch.setattr(workflows, "chain", chain)
    return state


def assert_workflow_controls(tasks, workflow):
    assert [(leaf, options) for leaf, options, _ in workflow.steps] == [
        (leaf, {"django": True, "ray_options": {"num_cpus": 0.1, "max_retries": 0}})
        for leaf in (tasks.workflow_increment, tasks.workflow_double)
    ]
    assert workflow.chains == [tuple(signature for _, _, signature in workflow.steps)]
    assert workflow.policies == ["full"]
    assert workflow.runs == [((20,), {"use_ray": True})]


@pytest.fixture
def effects(tmp_path, monkeypatch, tasks, workflow):
    directory = tmp_path / "runtime-effects"
    directory.mkdir()
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_ARTIFACT_ROOT", str(tmp_path.resolve()))
    monkeypatch.setattr(ray, "is_initialized", lambda: True)
    monkeypatch.setattr(
        ray, "get_runtime_context", lambda: SimpleNamespace(get_job_id=lambda: "01000000")
    )
    monkeypatch.setattr(ray, "init", lambda **kwargs: pytest.fail("current connection is owned"))
    monkeypatch.setattr(ray, "shutdown", lambda: pytest.fail("current connection is owned"))
    return directory


def released_config(context):
    original = {
        "task_execution_pk": context.task_pk,
        "task_id": context.task_id,
        "attempt_number": context.attempt_number,
        "execution_generation": context.execution_generation,
    }
    digest = hashlib.sha256(
        json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    submission_id = "raysubmit_django_ray_v1_" + digest
    return {
        "runtime_env": {},
        "metadata": {
            "job_submission_id": submission_id,
            "job_name": submission_id,
            "django_ray_task_id": str(context.task_pk),
            "django_ray_attempt_number": str(context.attempt_number),
            "django_ray_execution_generation": str(context.execution_generation),
        },
    }


@pytest.fixture
def released(effects, monkeypatch):
    from django_ray.runtime import context as runtime_context

    # Shape from v0.4.0 DurableTaskContext: no protocol/strict/cohort fields.
    context = SimpleNamespace(
        task_pk=41,
        task_id="rehearsal-task",
        attempt_number=1,
        execution_generation=1,
        ray_job_driver=True,
    )
    worker = SimpleNamespace(core_worker=None)
    state = SimpleNamespace(initialized=False, events=[], context=context, worker=worker)
    monkeypatch.setattr(django_ray, "__version__", "0.4.0")
    monkeypatch.setattr(runtime_context, "get_current_task_context", lambda: context)
    monkeypatch.setattr(ray._private.worker, "global_worker", worker)
    monkeypatch.setattr(ray, "is_initialized", lambda: state.initialized)

    def initialize(**kwargs):
        state.events.append(("init", kwargs))
        worker.core_worker = object()
        state.initialized = True

    def shutdown():
        state.events.append(("shutdown", None))
        worker.core_worker = None
        state.initialized = False

    monkeypatch.setattr(ray, "init", initialize)
    monkeypatch.setattr(ray, "shutdown", shutdown)
    monkeypatch.setenv("RAY_ADDRESS", "10.0.0.7:6379")
    monkeypatch.setenv("RAY_JOB_CONFIG_JSON_ENV_VAR", json.dumps(released_config(context)))
    return state


@contextmanager
def identity(*, attempt=1, protocol=3):
    with durable_task_execution(
        41,
        task_id="rehearsal-task",
        attempt_number=attempt,
        execution_generation=attempt,
        execution_protocol_version=protocol,
        ray_job_driver=True,
    ):
        yield


def test_value_retains_actual_context_and_exposes_duplicate_invocation(tasks, effects, workflow):
    def during_workflow():
        assert (effects / "current-core.1.started.json").is_file()
        assert not (effects / "current-core.1.committed.json").exists()
        assert ray.is_initialized() is True

    workflow.on_run = during_workflow
    with identity():
        result = tasks.value.func("current-core", "\u00e9" * 8192)
        committed = (effects / "current-core.1.committed.json").read_bytes()
        with pytest.raises(tasks.FixtureTerminalError, match="duplicate-application-effect"):
            tasks.value.func("current-core")
    marker = json.loads(committed)
    assert marker["identity"] == result["identity"]
    assert marker["identity"]["task_pk"] == 41
    assert marker["identity"]["native_job_id"] == "01000000"
    assert marker["identity"]["context_protocol"] == 3
    assert result["payload"] == "\u00e9" * 8192
    assert result["workflow_result"] == 42
    assert_workflow_controls(tasks, workflow)
    assert (effects / "current-core.1.committed.json").read_bytes() == committed
    assert len(list(effects.iterdir())) == 2
    assert len(committed) < tasks.MAX_MARKER_BYTES


@pytest.mark.parametrize(
    "case,payload",
    [([], "42"), ("old-gated", "42"), ("old-success", None), ("old-success", "\u00e9" * 16385)],
    ids=["case-type", "case-route", "payload-type", "payload-bytes"],
)
def test_invalid_input_cannot_create_an_effect(tasks, effects, case, payload):
    with identity(), pytest.raises(tasks.FixtureTerminalError):
        tasks.value.func(case, payload)
    assert list(effects.iterdir()) == []


def test_missing_artifact_root_is_not_created(tasks, tmp_path, monkeypatch):
    missing = tmp_path / "absent"
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_ARTIFACT_ROOT", str(missing))
    with pytest.raises(tasks.FixtureTerminalError, match="artifact-root-unavailable"):
        tasks.value.func("old-success")
    assert not missing.exists()


def test_native_and_durable_context_are_required_before_any_effect(tasks, effects, monkeypatch):
    with pytest.raises(tasks.FixtureTerminalError, match="native-context-unavailable"):
        tasks.value.func("current-core")
    monkeypatch.setattr(ray, "is_initialized", lambda: False)
    with identity(), pytest.raises(tasks.FixtureTerminalError, match="native-context-unavailable"):
        tasks.value.func("current-core")
    assert list(effects.iterdir()) == []


def test_released_context_does_not_invent_a_protocol_marker(tasks, effects, released, workflow):
    def during_workflow():
        assert released.initialized and released.worker.core_worker is not None
        assert [event for event, _ in released.events] == ["init"]
        assert (effects / "old-success.1.started.json").is_file()
        assert not (effects / "old-success.1.committed.json").exists()

    workflow.on_run = during_workflow
    result = tasks.value.func("old-success")
    assert result["identity"]["context_protocol"] is None
    assert result["identity"]["native_job_id"] == "01000000"
    assert released.events == [
        ("init", {"address": "10.0.0.7:6379", "log_to_driver": False}),
        ("shutdown", None),
    ]
    assert not released.initialized
    assert result["payload"] == "42" and result["workflow_result"] == 42
    assert_workflow_controls(tasks, workflow)


def test_plain_workflow_leaves_report_progress_and_compute_without_outer_effects(
    tasks, effects, monkeypatch
):
    from django_ray import workflows

    reports = []
    monkeypatch.setattr(workflows, "report_progress", lambda *a, **k: reports.append((a, k)))
    assert tasks.workflow_double(tasks.workflow_increment(20)) == 42
    assert reports == [
        ((1, 1), {"message": "upgrade-increment-complete"}),
        ((1, 1), {"message": "upgrade-double-complete"}),
    ]
    assert not list(effects.iterdir())


@pytest.mark.parametrize("failure", ["raises", "wrong-result"])
def test_failed_workflow_cannot_commit_and_released_connection_is_closed(
    tasks, effects, released, workflow, failure
):
    if failure == "raises":

        def fail():
            raise ValueError("fixture-leaf-failed")

        workflow.on_run = fail
    else:
        workflow.result = 41
    with pytest.raises(ValueError):
        tasks.value.func("old-success")
    assert_workflow_controls(tasks, workflow)
    assert not (effects / "old-success.1.committed.json").exists()
    assert [event for event, _ in released.events] == ["init", "shutdown"]
    assert not released.initialized


def test_gate_requires_exact_release_bytes_before_committing(tasks, effects):
    (effects / "current-jobs.release").write_bytes(b"release\n")
    with identity():
        result = tasks.gated_effect.func("current-jobs")
    assert (
        json.loads((effects / "current-jobs.1.committed.json").read_bytes())["identity"] == result
    )


@pytest.mark.parametrize("contents", [b"", b"release", b"release\nextra"])
def test_invalid_gate_does_not_commit(tasks, effects, contents):
    (effects / "current-jobs.release").write_bytes(contents)
    with identity(), pytest.raises(tasks.FixtureTerminalError, match="invalid-upgrade-gate"):
        tasks.gated_effect.func("current-jobs")
    assert (effects / "current-jobs.1.started.json").is_file()
    assert not (effects / "current-jobs.1.committed.json").exists()


def test_nonregular_gate_is_refused_before_open(tasks, effects):
    (effects / "current-jobs.release").mkdir()
    with identity(), pytest.raises(tasks.FixtureTerminalError, match="invalid-upgrade-gate"):
        tasks.gated_effect.func("current-jobs")
    assert not (effects / "current-jobs.1.committed.json").exists()


def test_unreleased_gate_times_out_without_an_effect(tasks, effects, monkeypatch):
    clock = iter([0.0, 0.0, float(tasks.GATE_TIMEOUT_SECONDS)])
    sleeps = []
    monkeypatch.setattr(tasks.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(tasks.time, "sleep", sleeps.append)
    with identity(), pytest.raises(tasks.FixtureTerminalError, match="upgrade-gate-timeout"):
        tasks.gated_effect.func("current-jobs")
    assert sleeps == [0.1]
    assert not (effects / "current-jobs.1.committed.json").exists()


def test_failure_and_retry_have_distinct_effects_for_actual_attempt_context(
    tasks, effects, released, monkeypatch
):
    # Direct calls model task bodies, not manager retry or terminal transitions.
    with pytest.raises(tasks.FixtureTerminalError, match="application-failure"):
        tasks.failed.func()
    # A second actual task has a distinct durable identity and submission ID.
    released.context.task_pk = 42
    released.context.task_id = "retry-task"
    monkeypatch.setenv("RAY_JOB_CONFIG_JSON_ENV_VAR", json.dumps(released_config(released.context)))
    with pytest.raises(ValueError, match="first-attempt-failure"):
        tasks.retried.func()
    released.context.attempt_number = released.context.execution_generation = 2
    monkeypatch.setenv("RAY_JOB_CONFIG_JSON_ENV_VAR", json.dumps(released_config(released.context)))
    result = tasks.retried.func()
    assert result["attempt"] == result["generation"] == 2
    assert sorted(path.name for path in effects.iterdir()) == [
        "old-failure.1.started.json",
        "old-retry.1.started.json",
        "old-retry.2.committed.json",
        "old-retry.2.started.json",
    ]
    assert [event for event, _ in released.events] == ["init", "shutdown"] * 3


@pytest.mark.parametrize(
    "address",
    [
        "",
        "auto",
        "local",
        "http://head:8265",
        "ray://head:10001",
        "head",
        "head:0",
        "head:65536",
        "user@head:6379",
    ],
)
def test_released_connection_requires_exact_injected_gcs_before_init(
    tasks, effects, released, monkeypatch, address
):
    monkeypatch.setenv("RAY_ADDRESS", address)
    with pytest.raises(tasks.FixtureTerminalError, match="released-driver-unavailable"):
        tasks.value.func("old-success")
    assert not released.events
    assert not list(effects.iterdir())


@pytest.mark.parametrize(
    "field",
    [
        "job_submission_id",
        "job_name",
        "django_ray_task_id",
        "django_ray_attempt_number",
        "django_ray_execution_generation",
    ],
)
def test_released_metadata_is_bound_to_original_task_before_init(
    tasks, effects, released, monkeypatch, field
):
    config = released_config(released.context)
    config["metadata"][field] = "crossed"
    monkeypatch.setenv("RAY_JOB_CONFIG_JSON_ENV_VAR", json.dumps(config))
    with pytest.raises(tasks.FixtureTerminalError, match="released-driver-unavailable"):
        tasks.value.func("old-success")
    assert not released.events
    assert not list(effects.iterdir())


@pytest.mark.parametrize(
    "changes",
    [
        {"task_id": None},
        {"task_pk": True},
        {"execution_generation": 0},
        {"attempt_number": 3},
        {"ray_job_driver": False},
        {"execution_protocol_version": 3},
    ],
)
def test_released_context_is_validated_before_connection(tasks, effects, released, changes):
    for key, value in changes.items():
        setattr(released.context, key, value)
    with pytest.raises(tasks.FixtureTerminalError):
        tasks.value.func("old-success")
    assert not released.events
    assert not list(effects.iterdir())


def test_released_connection_never_reuses_or_closes_an_existing_context(tasks, effects, released):
    released.initialized = True
    original = released.worker.core_worker = object()
    with pytest.raises(tasks.FixtureTerminalError, match="released-driver-unavailable"):
        tasks.value.func("old-success")
    assert released.worker.core_worker is original
    assert not released.events
    assert not list(effects.iterdir())


@pytest.mark.parametrize("partial", [False, True])
def test_startup_failure_leaves_no_effect_and_does_not_invent_cleanup(
    tasks, effects, released, monkeypatch, partial
):
    def initialize(**kwargs):
        if partial:
            released.initialized = True
            released.worker.core_worker = object()
        raise OSError("private endpoint diagnostic")

    monkeypatch.setattr(ray, "init", initialize)
    reason = "released-cleanup-unconfirmed" if partial else "released-startup-unconfirmed"
    with pytest.raises(tasks.FixtureTerminalError, match=reason):
        tasks.value.func("old-success")
    assert not released.events
    assert not list(effects.iterdir())


@pytest.mark.parametrize("mode", ["raises", "still-connected", "replacement"])
def test_late_cleanup_failure_preserves_committed_effect_and_refuses_success(
    tasks, effects, released, monkeypatch, mode
):
    original_publish = tasks._publish
    replacement = object()

    def publish(directory, case, phase, identity):
        original_publish(directory, case, phase, identity)
        if mode == "replacement" and phase == "committed":
            released.worker.core_worker = replacement

    def shutdown():
        released.events.append(("shutdown", None))
        if mode == "raises":
            raise OSError("private cleanup diagnostic")

    monkeypatch.setattr(tasks, "_publish", publish)
    monkeypatch.setattr(ray, "shutdown", shutdown)
    with pytest.raises(tasks.FixtureTerminalError, match="^upgrade-released-cleanup-unconfirmed$"):
        tasks.value.func("old-success")
    assert (effects / "old-success.1.committed.json").is_file()
    assert sum(event == "init" for event, _ in released.events) == 1
    if mode == "replacement":
        assert released.worker.core_worker is replacement
        assert [event for event, _ in released.events] == ["init"]


def test_released_native_connection_remains_live_through_gate(
    tasks, effects, released, monkeypatch
):
    def release(seconds):
        assert released.initialized and released.worker.core_worker is not None
        assert [event for event, _ in released.events] == ["init"]
        (effects / "old-gated.release").write_bytes(b"release\n")

    monkeypatch.setattr(tasks.time, "sleep", release)
    result = tasks.gated_effect.func("old-gated")
    assert result["native_job_id"] == "01000000"
    assert not released.initialized
    assert (effects / "old-gated.1.committed.json").is_file()


def test_gate_read_crossing_deadline_does_not_commit(tasks, effects, monkeypatch):
    (effects / "current-jobs.release").write_bytes(b"release\n")
    clock = iter([0.0, 0.0, float(tasks.GATE_TIMEOUT_SECONDS)])
    monkeypatch.setattr(tasks.time, "monotonic", lambda: next(clock))
    with identity(), pytest.raises(tasks.FixtureTerminalError, match="upgrade-gate-timeout"):
        tasks.gated_effect.func("current-jobs")
    assert (effects / "current-jobs.1.started.json").is_file()
    assert not (effects / "current-jobs.1.committed.json").exists()
