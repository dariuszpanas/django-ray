"""Live fault-injection tests against a real Ray cluster.

These tests are intentionally opt-in and skipped unless
DJANGO_RAY_LIVE_CLUSTER_TESTS is enabled.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from io import StringIO
from typing import cast

import pytest

from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.runner.ray_core import RayCoreHandle, RayCoreRunner
from tests.migration_cleanup import preactivation_protocol_schema as preactivation_protocol_schema


def _truthy(value: str | None) -> bool:
    return str(value).lower() in {"1", "true", "yes"}


LIVE_CLUSTER_ENABLED = _truthy(os.environ.get("DJANGO_RAY_LIVE_CLUSTER_TESTS"))
LIVE_RAY_ADDRESS = os.environ.get("DJANGO_RAY_LIVE_RAY_ADDRESS") or os.environ.get(
    "RAY_ADDRESS", "auto"
)
LIVE_MIN_NODES = int(os.environ.get("DJANGO_RAY_LIVE_MIN_NODES", "2"))
LIVE_WORKING_DIR_URI = os.environ.get("DJANGO_RAY_LIVE_WORKING_DIR_URI")

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.live_cluster,
]
if not LIVE_CLUSTER_ENABLED:
    pytestmark.append(
        pytest.mark.skip(
            reason=("live cluster tests disabled; set DJANGO_RAY_LIVE_CLUSTER_TESTS=1 to enable")
        )
    )


def _live_project_runtime_env_spec() -> dict[str, object]:
    """Build the generic-cluster smoke environment from project dependencies."""
    from testproject import settings as testproject_settings

    if LIVE_WORKING_DIR_URI is None:
        raise RuntimeError("The live project RuntimeEnv requires a working directory URI")
    django_ray_config = cast(dict[str, object], testproject_settings.DJANGO_RAY)
    project_profiles = cast(
        dict[str, dict[str, object]],
        django_ray_config["RUNTIME_ENV_PROFILES"],
    )
    project_packages = cast(list[str], project_profiles["project"]["pip"])
    return {
        "working_dir": LIVE_WORKING_DIR_URI,
        "pip": list(project_packages),
        "env_vars": {
            "DATABASE_ENGINE": "django.db.backends.sqlite3",
            "DJANGO_SETTINGS_MODULE": "testproject.settings",
            "PYTHONPATH": "src",
        },
    }


@pytest.fixture()
def live_ray_cluster():
    """Connect to a live Ray cluster and ensure minimum node count."""
    import ray

    if ray.is_initialized():
        raise RuntimeError("Required live Ray fixture found an initialized driver")

    try:
        ray.init(address=LIVE_RAY_ADDRESS)
    except Exception as exc:  # pragma: no cover - environment-dependent
        ray.shutdown()
        raise RuntimeError(f"Required live Ray connection failed at {LIVE_RAY_ADDRESS}") from exc

    try:
        alive_nodes = [node for node in ray.nodes() if node.get("Alive")]
        if len(alive_nodes) < LIVE_MIN_NODES:
            raise RuntimeError(
                "Required live Ray cluster has "
                f"{len(alive_nodes)} alive node(s); requires at least {LIVE_MIN_NODES}"
            )
        yield ray
    finally:
        ray.shutdown()


@pytest.fixture
def historical_protocol(preactivation_protocol_schema, monkeypatch):
    """Only the retained local polling cases use actual protocol-1 admission."""
    from django_ray.execution_protocol import EXECUTION_PROTOCOL_VERSION, ExecutionProtocolRange
    from django_ray.lifecycle import cancel_task
    from django_ray.models import TaskExecutionProtocolPolicy

    assert EXECUTION_PROTOCOL_VERSION == 3
    assert TaskExecutionProtocolPolicy.objects.get().active_write_protocol_version == 1

    def finalize(task, **fences):
        # The current public lifecycle defaults to 3..3. This retained released
        # adapter explicitly selects its historical epoch without widening it.
        return cancel_task(task, supported_protocols=ExecutionProtocolRange(1, 1), **fences)

    monkeypatch.setattr(
        "django_ray.management.commands.django_ray_worker.finalize_cancellation", finalize
    )


@pytest.fixture
def stopped_live_cohort_ledger():
    """Reuse the existing isolated ledger cleanup after this fixture's Ray exit."""
    from tests.integration.test_cohort_claim_storage import isolated_sqlite_ledger_maintenance

    yield from isolated_sqlite_ledger_maintenance.__wrapped__()


@pytest.fixture
def current_cohort_live_cluster(stopped_live_cohort_ledger, live_ray_cluster):
    # Dependencies tear down in reverse order: owned Ray connection first,
    # isolated stopped ledger second. Neither exit invents positive proof.
    yield live_ray_cluster


def _make_historical_live_command(worker_id: str = "live-failure-worker") -> Command:
    """Build an exact historical lease without changing current startup defaults."""
    import socket

    from django_ray import __version__
    from django_ray.runner.leasing import WorkerLeaseIdentity

    cmd = Command()
    cmd.stdout = StringIO()
    cmd.style = cmd.style
    cmd._set_worker_id(worker_id)
    cmd.execution_mode = "local" if LIVE_RAY_ADDRESS == "auto" else "cluster"
    cmd.cluster_address = None if LIVE_RAY_ADDRESS == "auto" else LIVE_RAY_ADDRESS
    cmd.sync_mode = False
    cmd.active_tasks = {}
    cmd.ray_core_runner = RayCoreRunner()
    now = datetime.now(UTC)
    cmd.lease = TaskWorkerLease.objects.create(
        worker_id=worker_id,
        hostname=socket.gethostname(),
        pid=os.getpid(),
        queue_name="default",
        started_at=now,
        last_heartbeat_at=now,
        capability_schema_version=1,
        django_ray_version=__version__,
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=1,
        legacy_admission_token=None,
    )
    cmd.lease_identity = WorkerLeaseIdentity(worker_id, cmd.lease.hostname, cmd.lease.pid, now)
    return cmd


def _submit_live_sleep_task(ray_module, sleep_seconds: int):
    """Submit a long-running task directly to Ray for live fault tests."""

    @ray_module.remote(name=f"django_ray_live_sleep_{time.time_ns()}")
    def _live_sleep(seconds: int) -> str:
        import time as _time

        _time.sleep(seconds)
        return json.dumps(
            {
                "success": True,
                "result": f"slept-{seconds}",
                "error": None,
                "traceback": None,
                "exception_type": None,
            }
        )

    return _live_sleep.remote(sleep_seconds)


class TestLiveFailureInjection:
    """Live cluster fault-injection scenarios."""

    def test_target_attestation_probes_every_package_free_ray_client_node(
        self,
        live_ray_cluster,
    ):
        """The current pure probe observes the exact two-node Ray Client target."""
        import platform
        import sys

        from django_ray.target.attestation import (
            RayRunnerFamily,
            RayRuntimeVersion,
            RayTargetExpectation,
            compare_ray_target_attestation,
            decode_ray_cluster_attestation,
            encode_ray_cluster_attestation,
        )
        from django_ray.target.probe import probe_ray_target

        context = live_ray_cluster.get_runtime_context()
        expectation = RayTargetExpectation(
            target_key="ci-ray-client",
            runner_family=RayRunnerFamily.RAY_CORE,
            cluster_session=context.get_session_name(),
            policy_revision=1,
            runtime=RayRuntimeVersion(
                ray_major=2,
                ray_minor=58,
                ray_patch=0,
                python_implementation=platform.python_implementation().lower(),
                python_major=sys.version_info.major,
                python_minor=sys.version_info.minor,
                python_patch=sys.version_info.micro,
            ),
        )

        attestation = probe_ray_target(
            expectation,
            ttl_seconds=60,
            timeout_seconds=60,
            max_nodes=16,
        )

        assert live_ray_cluster.is_initialized() is True
        assert (
            decode_ray_cluster_attestation(encode_ray_cluster_attestation(attestation))
            == attestation
        )
        assert (
            compare_ray_target_attestation(
                expectation,
                attestation,
                now=attestation.observed_at,
            )
            is None
        )
        observed_node_ids = tuple(node.node_id for node in attestation.nodes)
        alive_node_ids = tuple(
            sorted(node["NodeID"] for node in live_ray_cluster.nodes() if node.get("Alive"))
        )
        assert len(observed_node_ids) >= LIVE_MIN_NODES
        assert observed_node_ids == alive_node_ids
        assert observed_node_ids == tuple(
            item.node_id for item in attestation.boundary.node_state_versions_before
        )
        assert observed_node_ids == tuple(
            item.node_id for item in attestation.boundary.node_state_versions_after
        )

    @pytest.mark.skipif(
        not LIVE_WORKING_DIR_URI,
        reason="DJANGO_RAY_LIVE_WORKING_DIR_URI is required for the submission smoke test",
    )
    def test_ray_core_runner_submits_project_code_to_generic_cluster(
        self, current_cohort_live_cluster, settings
    ):
        """Actual protocol3 qualification/claim/bootstrap produces result5 on generic nodes."""
        from django_ray.conf.settings import get_settings
        from django_ray.lifecycle import succeed_task
        from django_ray.runner.cohort_claims import CohortClaimAlias, claim_cohort_tasks
        from django_ray.runner.cohort_completion import apply_cohort_completion
        from django_ray.runner.cohort_configuration import prepare_cohort_worker_configuration
        from django_ray.runner.cohort_core import CoreCohortManagerAdapter
        from django_ray.runner.cohort_dispatch import (
            mark_cohort_dispatch_started,
            prepare_claimed_cohort_dispatch,
        )
        from django_ray.runner.cohort_qualification import CohortQualificationLifecycle
        from django_ray.runner.ray_core import _compiled_graph_submission_transport
        from django_ray.runtime.cohort_job import CohortProbeJobLease
        from django_ray.target.attestation import RayRunnerFamily
        from django_ray.target.cohort_claim import (
            CohortManagerRuntime,
            CohortPythonVersion,
            CohortRunnerFamily,
        )
        from django_ray.target.cohort_runtime import _local_runtime
        from testproject.tasks import add_numbers

        ray = current_cohort_live_cluster
        settings.TASKS = {
            "default": {
                "BACKEND": "django_ray.backends.RayTaskBackend",
                "QUEUES": ["default"],
                "OPTIONS": {
                    "RAY_ADDRESS": LIVE_RAY_ADDRESS,
                    "RAY_RUNTIME_ENV": _live_project_runtime_env_spec(),
                },
            }
        }
        enqueued = add_numbers.enqueue(2, 3)
        task = RayTaskExecution.objects.get(task_id=enqueued.id)
        assert task.execution_protocol_version == 3
        from django_ray.runtime.runtime_env import runtime_env_for_execution

        # Verify the real producer consumed this backend declaration before
        # qualifying or crossing Ray; an ignored option otherwise stores {}.
        assert runtime_env_for_execution(task).spec == _live_project_runtime_env_spec()
        command = Command()
        command._set_worker_id("live-current-core-bootstrap")
        command._create_lease("default")
        identity = command.lease_identity
        assert identity is not None
        package, runtime = _local_runtime(ray)
        local = LIVE_RAY_ADDRESS == "auto"
        configuration = prepare_cohort_worker_configuration(
            tasks=settings.TASKS,
            validated_aliases=("default",),
            selected_queues=("default",),
            manager_settings=get_settings(),
            django_settings_module="testproject.settings",
            runner_family=RayRunnerFamily.RAY_CORE,
            execution_mode="local" if local else "cluster",
            core_address=None if local else LIVE_RAY_ADDRESS,
        )
        lifecycle = CohortQualificationLifecycle(
            CohortProbeJobLease(
                identity.worker_id, identity.hostname, identity.pid, identity.started_at
            ),
            package,
            runtime,
            RayRunnerFamily.RAY_CORE,
        )
        lifecycle.configure_aliases(configuration.aliases)
        manager = CoreCohortManagerAdapter(lifecycle)
        assert configuration.core_configuration_digest is not None
        deadline = time.monotonic() + 140

        def heartbeat():
            assert time.monotonic() < deadline, "Current Core bootstrap exceeded its deadline"
            assert command._update_lease_heartbeat()

        try:
            publications = []
            for _ in range(2):
                manager.begin(configuration.core_configuration_digest, 1, timeout_seconds=30)
                result = None
                while result is None:
                    heartbeat()
                    result = manager.poll()
                    time.sleep(0.02)
                publications.append(result.shared)
            assert publications[0].desired_state == "draining"
            assert publications[0].activation_policy_id == publications[1].target_policy_id
            assert publications[1].desired_state == "active"
            assert publications[0].attestation_id != publications[1].attestation_id
            qualified = manager.eligible_aliases()
            assert len(qualified) == 1
            heartbeat()
            aliases = tuple(
                CohortClaimAlias(
                    item.alias, item.declaration_digest, item.selection_policy, item.queues
                )
                for item in configuration.aliases
            )
            claimed = claim_cohort_tasks(
                identity,
                aliases=aliases,
                qualifications=qualified,
                runner_family=CohortRunnerFamily.RAY_CORE,
                manager_runtime=CohortManagerRuntime(
                    package,
                    CohortPythonVersion(
                        runtime.python_implementation,
                        runtime.python_major,
                        runtime.python_minor,
                        runtime.python_patch,
                    ),
                    (runtime.ray_major, runtime.ray_minor, runtime.ray_patch),
                ),
                limit=1,
                now=datetime.now(UTC),
            )
            assert len(claimed) == 1 and claimed[0].execution.pk == task.pk
            dispatch = prepare_claimed_cohort_dispatch(
                claimed[0], transport=_compiled_graph_submission_transport(ray)
            )
            heartbeat()
            dispatch = mark_cohort_dispatch_started(dispatch)
            # This test owns the already-connected fixture's default context.
            # The runner must not initialize or adopt a replacement connection.
            runner = RayCoreRunner._from_existing_connection()
            handle = runner.submit_cohort_task(dispatch.execution, prepared=dispatch.prepared)
            while not ray.wait([handle.object_ref], timeout=0.05)[0]:
                heartbeat()
            payload = ray.get(handle.object_ref, timeout=1)
            heartbeat()

            def apply(current, decoded, *, retry_admitted):
                assert decoded.completion.success is True
                assert decoded.completion.result == 5
                assert decoded.completion.executor_django_ray_version == package
                return succeed_task(
                    current,
                    result_data=json.dumps(decoded.completion.result),
                    result_reference=None,
                    expected_claimed_by_worker=identity.worker_id,
                    expected_attempt_number=dispatch.claim.facts.identity.attempt_number,
                    expected_execution_generation=dispatch.claim.facts.identity.execution_generation,
                )

            outcome = apply_cohort_completion(
                dispatch, payload, provenance="owned_direct", apply_completion=apply
            )
            assert outcome.applied and outcome.dispatch.execution.state == TaskState.SUCCEEDED
            assert outcome.dispatch.claim.disposition.value == "RESOLVED"
            assert runner.retire_pending_handle(handle)
            enqueued.refresh()
            assert enqueued.return_value == 5
        finally:
            lifecycle.invalidate()

    def test_disconnect_retries_pending_ray_core_task(self, historical_protocol, live_ray_cluster):
        """Historical protocol1 local poll retains its old disconnect/retry behavior."""
        cmd = _make_historical_live_command()
        task = RayTaskExecution.objects.create(
            task_id="live-fi-disconnect-001",
            callable_path="time.sleep",
            queue_name="default",
            state=TaskState.RUNNING,
            execution_protocol_version=1,
            args_json="[30]",
            kwargs_json="{}",
            attempt_number=1,
            claimed_by_worker=cmd.worker_id,
        )

        object_ref = _submit_live_sleep_task(live_ray_cluster, sleep_seconds=30)
        cmd.ray_core_runner._pending_tasks[task.pk] = RayCoreHandle(
            task_pk=task.pk,
            object_ref=object_ref,
            submitted_at=datetime.now(UTC),
            task_name="live_sleep",
            attempt_number=task.attempt_number,
            execution_generation=task.execution_generation,
        )

        live_ray_cluster.shutdown()
        cmd.poll_ray_core_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.run_after is not None
        assert "Ray connection lost" in (task.error_message or "")
        assert cmd.ray_core_runner._pending_tasks == {}

    def test_cancellation_finalizes_live_pending_task(self, historical_protocol, live_ray_cluster):
        """Historical protocol1 local cancellation retains its terminal transition."""
        from ray.exceptions import TaskCancelledError

        cmd = _make_historical_live_command()
        task = RayTaskExecution.objects.create(
            task_id="live-fi-cancel-001",
            callable_path="time.sleep",
            queue_name="default",
            state=TaskState.CANCELLING,
            execution_protocol_version=1,
            args_json="[30]",
            kwargs_json="{}",
            attempt_number=1,
            started_at=datetime.now(UTC),
            claimed_by_worker=cmd.worker_id,
        )

        object_ref = _submit_live_sleep_task(live_ray_cluster, sleep_seconds=30)
        cmd.ray_core_runner._pending_tasks[task.pk] = RayCoreHandle(
            task_pk=task.pk,
            object_ref=object_ref,
            submitted_at=datetime.now(UTC),
            task_name="live_sleep",
            attempt_number=task.attempt_number,
            execution_generation=task.execution_generation,
        )
        cmd.active_tasks[task.pk] = f"ray_core:{task.pk}"

        cmd.process_cancellations()

        # The old SQL transition only records its cancellation request outcome.
        # Independently require terminal cancellation of this exact native task.
        with pytest.raises(TaskCancelledError):
            live_ray_cluster.get(object_ref, timeout=10)

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.finished_at is not None
        assert task.pk not in cmd.active_tasks
        assert task.pk not in cmd.ray_core_runner._pending_tasks
