"""Final workflow node outcomes must agree with retrying callable outcomes."""

from types import SimpleNamespace

import pytest

from tests.unit.test_remote import (
    _WORKFLOW_RUN_IDENTITY,
    _execute_bound_workflow_step,
    _progress_actor,
)


@pytest.mark.parametrize("failures", [0, 1, 2])
def test_bound_leaf_success_after_retry_preserves_final_graph_outcome(monkeypatch, failures):
    """Exercise real strict dispatch and ingestion; emulate only Ray retry scheduling."""
    collector = _progress_actor()
    actor = SimpleNamespace(ingest=SimpleNamespace(remote=collector.ingest))
    calls = 0
    original_error = ValueError("transient workflow failure")

    def callback():
        nonlocal calls
        calls += 1
        if calls <= failures:
            raise original_error
        return 42

    monkeypatch.setattr("django_ray.runtime.import_utils.import_callable", lambda _path: callback)

    def invoke():
        return _execute_bound_workflow_step(
            "tests.unit.test_remote.workflow_target",
            False,
            (),
            {},
            {},
            _WORKFLOW_RUN_IDENTITY["task_execution_pk"],
            actor,
            "0.0",
            workflow_run_identity=_WORKFLOW_RUN_IDENTITY,
            return_outcome_marker=True,
        )

    for _ in range(failures):
        with pytest.raises(ValueError) as caught:
            invoke()
        assert caught.value is original_error
    value, marker = invoke()
    assert value == 42
    assert type(marker) is bytes
    outcome_ref = object()

    def get_marker(ref, *, timeout):
        assert ref is outcome_ref
        assert timeout == 0
        return marker

    coordinator_ray = SimpleNamespace(
        wait=lambda refs, **kwargs: (refs, []),
        get=get_marker,
    )
    _settle_outcome(coordinator_ray, actor, outcome_ref)
    assert calls == failures + 1
    snapshot = collector.snapshot()
    node = snapshot["graph"]["nodes"][0]
    assert node["state"] == "SUCCEEDED"
    assert node["error"] is None
    assert snapshot["state"] == "SUCCEEDED"


def retry_settlement_target(counter, failures):
    """Importable native fixture with external attempt state, independent of workers."""
    import ray

    attempt = ray.get(counter.increment.remote(), timeout=10)
    if attempt <= failures:
        raise ValueError("transient workflow failure")
    return 42


@pytest.mark.real_ray
@pytest.mark.parametrize("max_retries,failures", [(1, 1), (-1, 2), (1, 2)])
@pytest.mark.parametrize("following", [False, True])
def test_native_bound_leaf_graph_matches_final_retry_outcome(
    ray_cluster, max_retries, failures, following
):
    """Run the strict leaf boundary with actual Ray retries and actual ingestion."""
    from django_ray.runtime.remote import WorkflowProgressActor
    from django_ray.workflow.progress.protocol import WorkflowProgressEventKind as Kind
    from tests.unit.test_remote import _WORKFLOW_PLAN, _progress_wire

    class Counter:
        def __init__(self):
            self.calls = 0

        def increment(self):
            self.calls += 1
            return self.calls

        def count(self):
            return self.calls

    counter = ray_cluster.remote(num_cpus=0, max_restarts=0)(Counter).remote()
    collector = ray_cluster.remote(num_cpus=0.25, max_restarts=0)(WorkflowProgressActor).remote(
        _progress_wire(Kind.INITIALIZED, {"plan": _WORKFLOW_PLAN})
    )
    ref = marker_ref = downstream = downstream_marker = None
    try:
        from django_ray.runtime.context import DurableTaskContext, WorkflowRunIdentity
        from django_ray.runtime.runtime_env import normalize_runtime_env
        from django_ray.workflow.plans import runtime_env_plan_identity
        from django_ray.workflows import Step, _RayExecutor

        executor = _RayExecutor()
        executor.task_execution_pk = _WORKFLOW_RUN_IDENTITY["task_execution_pk"]
        executor.task_context = DurableTaskContext(
            task_pk=executor.task_execution_pk,
            task_id="workflow-retry-fixture",
            attempt_number=_WORKFLOW_RUN_IDENTITY["attempt_number"],
            execution_generation=_WORKFLOW_RUN_IDENTITY["execution_generation"],
            execution_protocol_version=1,
            strict_execution_request=True,
            runtime_env_plan_identity=runtime_env_plan_identity(
                normalize_runtime_env({})
            ).as_transport_dict(),
        )
        executor.workflow_run_identity = WorkflowRunIdentity(
            **{
                key: value
                for key, value in _WORKFLOW_RUN_IDENTITY.items()
                if key != "schema_version"
            }
        )
        executor.progress_actor = collector
        ref = executor.submit_step(
            Step(
                "tests.unit.test_workflow_retry_settlement.retry_settlement_target",
                bound_args=(counter, failures),
                ray_options={
                    "num_cpus": 0.25,
                    "max_retries": max_retries,
                    "retry_exceptions": True,
                },
            ),
            (),
            {},
            "0.0",
            (),
        )
        marker_ref = next(iter(executor._pending_leaf_outcomes))
        if following:
            downstream = executor.submit_step(
                Step(
                    "testproject.apps.cluster_tasks.retry_qualification.identity",
                    ray_options={"num_cpus": 0.25},
                ),
                (ref,),
                {},
                "0.1",
                ("0.0",),
            )
            downstream_marker = next(
                key for key in executor._pending_leaf_outcomes if key != marker_ref
            )
        succeeds = max_retries == -1 or failures <= max_retries
        if succeeds:
            assert ray_cluster.get(downstream if following else ref, timeout=30) == 42
        else:
            with pytest.raises(ray_cluster.exceptions.RayTaskError, match="transient"):
                ray_cluster.get(downstream if following else ref, timeout=30)
        expected_calls = failures + 1 if succeeds else max_retries + 1
        assert ray_cluster.get(counter.count.remote(), timeout=10) == expected_calls

        # A coordinator snapshot is not an ordering barrier for a worker handle.
        # Wait for the exact number of terminal events, never sleep and assume.
        import time

        deadline = time.monotonic() + 10
        while True:
            snapshot = ray_cluster.get(collector.snapshot.remote(), timeout=10)
            decoded = snapshot["ingress"]["cost"]["ingest"]["decoded_by_kind"]
            if decoded["completed"] + decoded["failed"] == expected_calls + int(
                following and succeeds
            ):
                break
            assert time.monotonic() < deadline, "leaf lifecycle delivery did not complete"
            time.sleep(0.05)
        while executor._pending_leaf_outcomes:
            executor._poll_leaf_outcomes()
            assert executor.progress_actor is collector
            assert time.monotonic() < deadline, "final outcome metadata did not settle"
            time.sleep(0.01)
        snapshot = ray_cluster.get(collector.snapshot.remote(), timeout=10)
        node = next(node for node in snapshot["graph"]["nodes"] if node["node_id"] == "0.0")
        if following:
            following_node = next(
                node for node in snapshot["graph"]["nodes"] if node["node_id"] == "0.1"
            )
            assert following_node["state"] == ("SUCCEEDED" if succeeds else "PENDING")
        assert node["state"] == ("SUCCEEDED" if succeeds else "FAILED")
        assert snapshot["state"] == node["state"]
        if succeeds:
            assert node["error"] is None
        else:
            assert "transient workflow failure" in node["error"]
    finally:
        for owned_ref in (ref, marker_ref, downstream, downstream_marker):
            if owned_ref is not None:
                ray_cluster.cancel(owned_ref, force=True, recursive=True)
        ray_cluster.kill(collector, no_restart=True)
        ray_cluster.kill(counter, no_restart=True)


@pytest.mark.parametrize("outcome", ["SUCCEEDED", "FAILED"])
def test_authoritative_settlement_wins_over_delayed_invocation_events(outcome):
    from django_ray.workflow.progress.protocol import WorkflowProgressEventKind as Kind
    from tests.unit.test_remote import _progress_wire

    collector = _progress_actor()
    assert collector.ingest(
        _progress_wire(
            Kind.STARTED,
            {
                "node_id": "0.0",
                "label": "retrying",
                "execution": {},
            },
        )
    )
    assert collector.ingest(
        _progress_wire(
            Kind.FAILED,
            {
                "node_id": "0.0",
                "label": "retrying",
                "error": "transient",
            },
        )
    )
    assert collector.ingest(
        _progress_wire(
            Kind.NODE_SETTLED,
            {
                "node_id": "0.0",
                "state": outcome,
                "error": None if outcome == "SUCCEEDED" else "final failure",
            },
        )
    )
    before = collector.snapshot()["graph"]["nodes"][0]
    assert before["state"] == outcome
    assert before["error"] == (None if outcome == "SUCCEEDED" else "final failure")
    for kind, payload in (
        (Kind.STARTED, {"node_id": "0.0", "label": "stale", "execution": {}}),
        (Kind.COMPLETED, {"node_id": "0.0", "label": "stale"}),
        (Kind.FAILED, {"node_id": "0.0", "label": "stale", "error": "old error"}),
        (
            Kind.NODE_SETTLED,
            {
                "node_id": "0.0",
                "state": "FAILED" if outcome == "SUCCEEDED" else "SUCCEEDED",
                "error": "stale" if outcome == "SUCCEEDED" else None,
            },
        ),
    ):
        assert collector.ingest(_progress_wire(kind, payload))
        assert collector.snapshot()["graph"]["nodes"][0] == before


def test_settlement_cannot_allocate_unregistered_node_capacity():
    from django_ray.workflow.progress.protocol import WorkflowProgressEventKind as Kind
    from tests.unit.test_remote import _progress_wire

    collector = _progress_actor()
    assert not collector.ingest(
        _progress_wire(
            Kind.NODE_SETTLED,
            {
                "node_id": "unknown",
                "state": "SUCCEEDED",
                "error": None,
            },
        )
    )
    assert collector.snapshot()["graph"]["nodes"] == []


def _settle_outcome(ray_api, actor, ref):
    """Use the production coordinator poll with an explicitly owned marker."""
    from django_ray.workflow.progress.limits import WORKFLOW_PROGRESS_LIMITS_V1
    from django_ray.workflows import _RayExecutor

    executor = _RayExecutor.__new__(_RayExecutor)
    executor.ray = ray_api
    executor.progress_actor = actor
    executor.workflow_run_identity = SimpleNamespace(as_dict=lambda: _WORKFLOW_RUN_IDENTITY)
    executor.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
    executor._pending_leaf_outcomes = {ref: "0.0"}
    executor._poll_leaf_outcomes()
    assert executor._pending_leaf_outcomes == {}
    assert executor.progress_actor is actor


def test_final_failure_marker_replaces_old_error_before_delayed_leaf_events():
    from ray.exceptions import RayTaskError

    from django_ray.workflow.progress.protocol import WorkflowProgressEventKind as Kind
    from tests.unit.test_remote import _progress_wire

    collector = _progress_actor()
    actor = SimpleNamespace(ingest=SimpleNamespace(remote=collector.ingest))
    assert collector.ingest(
        _progress_wire(
            Kind.FAILED,
            {
                "node_id": "0.0",
                "label": "retrying",
                "error": "first invocation",
            },
        )
    )
    final_error = RayTaskError(
        "retry_settlement_target",
        "",
        ValueError("final invocation"),
        proctitle="fixture",
        pid=1,
        ip="127.0.0.1",
    )
    marker_ref = object()

    def get_marker(ref, *, timeout):
        assert ref is marker_ref and timeout == 0
        raise final_error

    _settle_outcome(
        SimpleNamespace(wait=lambda refs, **kw: (refs, []), get=get_marker), actor, marker_ref
    )
    before = collector.snapshot()["graph"]["nodes"][0]
    assert before["error"] == "final invocation"
    assert before["state"] == "FAILED"
    assert collector.ingest(
        _progress_wire(
            Kind.FAILED,
            {
                "node_id": "0.0",
                "label": "delayed",
                "error": "first invocation",
            },
        )
    )
    assert collector.snapshot()["graph"]["nodes"][0] == before


def test_snapshot_requested_before_settlement_cannot_publish_old_failure():
    from django_ray.workflow.progress.limits import WORKFLOW_PROGRESS_LIMITS_V1
    from django_ray.workflows import _RayExecutor

    marker_ref, old_snapshot_ref = object(), object()
    collector = _progress_actor()
    actor = SimpleNamespace(ingest=SimpleNamespace(remote=collector.ingest))
    from django_ray.workflow.progress.protocol import WorkflowProgressEventKind as Kind
    from tests.unit.test_remote import _progress_wire

    assert collector.ingest(
        _progress_wire(
            Kind.FAILED,
            {
                "node_id": "0.0",
                "label": "first",
                "error": "transient",
            },
        )
    )
    fetched = []

    def get(ref, *, timeout=0):
        fetched.append(ref)
        assert ref is marker_ref, "stale snapshot must not be fetched or persisted"
        return _success_marker()

    executor = _RayExecutor.__new__(_RayExecutor)
    executor.ray = SimpleNamespace(wait=lambda refs, **kw: (refs, []), get=get)
    executor.progress_actor = actor
    executor.workflow_run_identity = SimpleNamespace(as_dict=lambda: _WORKFLOW_RUN_IDENTITY)
    executor.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
    executor._pending_leaf_outcomes = {marker_ref: "0.0"}
    executor._pending_progress_snapshot_ref = old_snapshot_ref
    assert executor._flush_progress(bypass_interval=True) is None
    assert fetched == [marker_ref]
    assert executor._pending_progress_snapshot_ref is None
    assert executor._pending_leaf_outcomes == {}
    assert collector.snapshot()["graph"]["nodes"][0]["state"] == "SUCCEEDED"


@pytest.mark.parametrize("failure", ["cancelled", "unavailable", "invalid"])
def test_uncertain_outcome_disables_publication_without_cancelling_application(failure):
    from ray.exceptions import TaskCancelledError

    from django_ray.workflows import _RayExecutor

    marker_ref = object()
    disabled, warnings = [], []

    def get(ref, *, timeout):
        assert ref is marker_ref and timeout == 0
        if failure == "cancelled":
            raise TaskCancelledError(error_message="private cancellation detail")
        if failure == "unavailable":
            raise RuntimeError("private transport detail")
        return {"unexpected": "private metadata"}

    executor = _RayExecutor.__new__(_RayExecutor)
    executor.ray = SimpleNamespace(wait=lambda refs, **kw: (refs, []), get=get)
    executor.progress_actor = SimpleNamespace(
        disable=SimpleNamespace(remote=lambda: disabled.append(True)),
    )
    from django_ray.workflow.progress.limits import WORKFLOW_PROGRESS_LIMITS_V1

    executor.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
    executor.workflow_run_identity = None
    executor._pending_leaf_outcomes = {marker_ref: "0.0"}
    executor._progress_warning = lambda message, **fields: warnings.append(fields)
    executor._poll_leaf_outcomes()
    assert executor.progress_actor is None
    assert executor._pending_leaf_outcomes == {}
    assert disabled == [True]
    assert warnings == [
        {"reason": "leaf_outcome_invalid" if failure == "invalid" else "leaf_outcome_unavailable"}
    ]
    # No cancel method exists on this fake: observational failure must not cancel
    # the application's result reference or retry its callable.


def _success_marker(**detail):
    from django_ray.workflow.progress.protocol import WorkflowProgressEventKind as Kind
    from tests.unit.test_remote import _progress_wire

    return _progress_wire(
        Kind.NODE_SETTLED,
        {
            "node_id": "0.0",
            "state": "SUCCEEDED",
            "error": None,
            **detail,
        },
    )


def test_success_marker_preserves_final_preview_and_progress_before_delayed_events(monkeypatch):
    from django_ray.runtime.context import report_workflow_progress
    from django_ray.workflow.progress.protocol import WorkflowProgressEventKind as Kind
    from tests.unit.test_remote import _progress_wire

    collector = _progress_actor()
    assert collector.ingest(
        _progress_wire(
            Kind.FAILED,
            {
                "node_id": "0.0",
                "label": "old",
                "error": "old invocation",
            },
        )
    )
    delayed = []
    actor = SimpleNamespace(ingest=SimpleNamespace(remote=lambda wire: delayed.append(wire)))

    def callback():
        report_workflow_progress(1, 2, message="final invocation", metrics={"count": 2})
        return 42

    monkeypatch.setattr(
        "django_ray.runtime.import_utils.import_callable",
        lambda path: (lambda value: {"answer": value}) if path.endswith("preview") else callback,
    )
    value, marker = _execute_bound_workflow_step(
        "tests.unit.test_remote.workflow_target",
        False,
        (),
        {},
        {},
        _WORKFLOW_RUN_IDENTITY["task_execution_pk"],
        actor,
        "0.0",
        workflow_run_identity=_WORKFLOW_RUN_IDENTITY,
        return_outcome_marker=True,
        output_preview_path="tests.unit.test_remote.preview",
    )
    assert value == 42
    # Ingest final metadata before every event emitted by the successful worker.
    assert collector.ingest(marker)
    before = collector.snapshot()["graph"]["nodes"][0]
    assert before["state"] == "SUCCEEDED" and before["error"] is None
    assert before["output_preview"]["value"] == {"answer": 42}
    assert before["progress"]["message"] == "final invocation"
    assert before["progress"]["percent"] == 100.0
    assert before["progress"]["metrics"] == {"count": 2}
    for wire in reversed(delayed):
        assert collector.ingest(wire)
    assert collector.snapshot()["graph"]["nodes"][0] == before


def test_marker_encoding_failure_preserves_success_without_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "django_ray.runtime.import_utils.import_callable",
        lambda path: lambda: calls.append(1) or 42,
    )
    monkeypatch.setattr(
        "django_ray.runtime.remote.prepare_workflow_progress_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("private metadata")),
    )
    value, marker = _execute_bound_workflow_step(
        "tests.unit.test_remote.workflow_target",
        False,
        (),
        {},
        {},
        _WORKFLOW_RUN_IDENTITY["task_execution_pk"],
        None,
        "0.0",
        workflow_run_identity=_WORKFLOW_RUN_IDENTITY,
        return_outcome_marker=True,
    )
    assert value == 42 and marker is None and calls == [1]


@pytest.mark.parametrize("defer_parent", [False, True])
def test_flattened_dependency_error_waits_for_originating_leaf(defer_parent):
    from ray.exceptions import RayTaskError

    from django_ray.workflow.progress.limits import WORKFLOW_PROGRESS_LIMITS_V1
    from django_ray.workflow.progress.protocol import WorkflowProgressEventKind as Kind
    from django_ray.workflows import _RayExecutor
    from tests.unit.test_remote import _progress_wire

    collector = _progress_actor()
    actor = SimpleNamespace(ingest=SimpleNamespace(remote=collector.ingest))
    for node_id in ("0.0", "0.1"):
        assert collector.ingest(
            _progress_wire(
                Kind.SUBMITTED,
                {
                    "node_id": node_id,
                    "label": node_id,
                    "ray_task_id": "task-" + node_id,
                },
            )
        )
    parent, child = object(), object()
    failure = RayTaskError(
        "execute_workflow_step_remote",
        "",
        ValueError("origin failure"),
        proctitle="fixture",
        pid=1,
        ip="127.0.0.1",
    )

    def get(ref, **kwargs):
        raise failure

    executor = _RayExecutor.__new__(_RayExecutor)
    executor.progress_actor = actor
    executor.workflow_run_identity = SimpleNamespace(as_dict=lambda: _WORKFLOW_RUN_IDENTITY)
    executor.workflow_progress_limits = WORKFLOW_PROGRESS_LIMITS_V1
    executor._pending_leaf_outcomes = {parent: "0.0", child: "0.1"}
    executor._leaf_outcome_dependencies = {"0.0": (), "0.1": ("0.0",)}
    executor.ray = SimpleNamespace(wait=lambda refs, **kwargs: ([child], [parent]), get=get)
    if defer_parent:
        executor._poll_leaf_outcomes()
        assert executor._pending_leaf_outcomes == {parent: "0.0", child: "0.1"}
    executor.ray.wait = lambda refs, **kwargs: ([child, parent], [])
    executor._poll_leaf_outcomes()
    assert not executor._pending_leaf_outcomes
    assert not executor._leaf_outcome_dependencies
    nodes = {node["node_id"]: node for node in collector.snapshot()["graph"]["nodes"]}
    assert nodes["0.0"]["state"] == "FAILED"
    assert nodes["0.1"]["state"] == "PENDING"
    assert nodes["0.1"]["error"] is None
