"""Real parent database composition with fixed fake isolated helper responses."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import connection

from django_ray.models import (
    RayTarget,
    RayTargetPolicyRevision,
    RayTargetProbeChallenge,
    RayTargetProbeJobReceipt,
    RayWorkerTargetCapability,
)
from django_ray.runner import cohort_jobs as adapter
from django_ray.runner.cohort_qualification import CohortQualificationLifecycle
from django_ray.runtime.cohort_job import (
    CohortProbeJobLease,
    decode_probe_job_request,
    probe_job_submission_id,
)
from django_ray.runtime.cohort_job_entrypoint import decode_probe_job_launch
from django_ray.target import cohort_job_receipt_storage, cohort_job_retirement, cohort_publication
from django_ray.target.attestation import (
    RayRunnerFamily,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
)
from django_ray.target.cohort_contract import _timestamp
from django_ray.target.cohort_job_receipt import CohortJobReceipt
from django_ray.target.cohort_probe import derive_cohort_target_key
from tests.integration.test_cohort_probe_challenges import _lease
from tests.unit.test_cohort_jobs import Clock, FakeSupervisor, configuration
from tests.unit.test_cohort_qualification import RUNTIME, attestation

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)])
def database_backend(request):
    if connection.vendor != request.param:
        pytest.skip(f"Requires {request.param}")


@pytest.fixture
def case(monkeypatch):
    row, identity = _lease()
    clock = Clock()
    clock.now = row.last_heartbeat_at
    lifecycle = CohortQualificationLifecycle(
        CohortProbeJobLease(
            identity.worker_id, identity.hostname, identity.pid, identity.started_at
        ),
        "0.5.0",
        RUNTIME,
        RayRunnerFamily.RAY_JOB,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    helper = FakeSupervisor(clock)
    manager = adapter.JobsCohortManagerAdapter(
        lifecycle,
        configuration(),
        supervisor=helper,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    for module in (cohort_publication, cohort_job_receipt_storage):
        monkeypatch.setattr(module, "_now", clock.wall)
    monkeypatch.setattr(cohort_job_retirement, "_clock", clock.wall)
    monkeypatch.setattr(cohort_publication, "_local_runtime", lambda _ray: ("0.5.0", RUNTIME))
    state = SimpleNamespace(
        manager=manager,
        lifecycle=lifecycle,
        helper=helper,
        clock=clock,
        lease=row,
        session="session_jobs",
        submissions=[],
        receipts=[],
        inspect_pending=False,
        fail_submit=False,
    )

    def execute(payload):
        assert not connection.in_atomic_block
        command, args = payload["command"], payload["arguments"]
        if command == "submit":
            launch = decode_probe_job_launch(args["launch_json"])
            request = launch.request
            assert RayTargetProbeJobReceipt.objects.filter(
                pk=request.challenge_id, request_digest=launch.request_digest
            ).exists()
            state.submissions.append(launch)
            if state.fail_submit:
                raise RuntimeError("simulated lost submit response")
            clock.now += timedelta(milliseconds=100)
            sample = attestation(observed=clock.now)
            expected = RayTargetExpectation(
                request.target_key
                or derive_cohort_target_key(RayRunnerFamily.RAY_JOB, state.session),
                RayRunnerFamily.RAY_JOB,
                state.session,
                request.policy_revision,
                RUNTIME,
            )
            proof = build_ray_cluster_attestation(
                expectation=expected,
                boundary=sample.boundary,
                nodes=(
                    build_ray_node_observation(
                        node_id="1" * 56, cluster_session=state.session, runtime=RUNTIME
                    ),
                ),
                observed_at=clock.now,
                expires_at=clock.now + timedelta(seconds=30),
            )
            receipt = CohortJobReceipt(
                request,
                launch.request_digest,
                probe_job_submission_id(request),
                f"{len(state.submissions):08x}",
                "0.5.0",
                proof,
                clock.now,
            )
            state.receipts.append(receipt)
            cohort_job_receipt_storage.write_cohort_job_receipt(receipt)
            return {"submitted": True, "submission_id": receipt.submission_id}
        if command == "inspect":
            request = decode_probe_job_request(args["request_json"])
            assert request == state.submissions[-1].request
            return (
                {"pending": True}
                if state.inspect_pending
                else {"pending": False, "inspected_at": _timestamp(clock.now)}
            )
        return helper.default(payload)

    helper.handler = execute
    return state


def finish(case, ticket):
    for _ in range(20):
        result = case.manager.poll(ticket)
        if result is not None:
            return result
    raise AssertionError("No bounded result")


def test_new_session_auto_activation_requires_second_probe(case, database_backend):
    ticket = case.manager.begin("alias0")
    result = finish(case, ticket)
    assert len(case.submissions) == 2
    first, second = case.submissions
    assert first.request.target_key is None and first.request.policy_revision == 1
    assert second.request.policy_revision == 2
    assert first.request.challenge_id != second.request.challenge_id
    assert result.shared.desired_state == "active" and result.shared.activation_policy_id is None
    assert len(case.manager.eligible_aliases()) == 1
    assert RayTarget.objects.count() == 1 and RayTargetPolicyRevision.objects.count() == 2
    assert RayTargetProbeChallenge.objects.count() == RayTargetProbeJobReceipt.objects.count() == 1
    assert RayWorkerTargetCapability.objects.count() == 1


def test_client_driver_cleanup_precedes_database_reserved_jobs_qualification(
    case, monkeypatch, database_backend
):
    from django_ray.runner import cohort_client_discovery
    from tests.unit.test_cohort_client_discovery import observation

    monkeypatch.setattr(cohort_client_discovery, "_runtime", lambda *args: None)
    case.manager = adapter.JobsCohortManagerAdapter(
        case.lifecycle,
        configuration(address="ray://ray-head:10001"),
        supervisor=case.helper,
        monotonic=case.clock.monotonic,
        wall_clock=case.clock.wall,
    )
    jobs = case.helper.handler

    def execute(payload):
        command, arguments = payload["command"], payload["arguments"]
        if command == "discover-client":
            assert not RayTargetProbeChallenge.objects.exists()
            return observation(
                arguments,
                observed_at=_timestamp(case.clock.now),
                jobs_endpoint="https://ray-head:8265",
            )
        if command == "inspect-driver":
            assert not RayTargetProbeJobReceipt.objects.exists()
            return {
                "schema_version": 1,
                **arguments,
                "terminal": True,
                "inspected_at": _timestamp(case.clock.now),
            }
        return jobs(payload)

    case.helper.handler = execute
    result = finish(case, case.manager.begin("alias0"))
    assert [call["command"] for call in case.helper.calls] == [
        "discover-client",
        "inspect-driver",
        "prepare",
        "submit",
        "inspect",
        "submit",
        "inspect",
    ]
    assert all(launch.jobs_endpoint == "https://ray-head:8265" for launch in case.submissions)
    assert (
        result.job_qualification.configuration_digest
        == case.manager.configuration.aliases[0].declaration_digest
    )
    assert case.manager.configuration.job_addresses[0][1] == "ray://ray-head:10001"
    assert len(case.manager.eligible_aliases()) == 1


def test_second_alias_reuses_session_without_unpausing_and_preserves_sibling(
    case, database_backend
):
    first = finish(case, case.manager.begin("alias0"))
    second = finish(case, case.manager.begin("alias1"))
    assert (
        first.shared.attestation.expectation.target_key
        == second.shared.attestation.expectation.target_key
    )
    assert len(case.manager.eligible_aliases()) == 2
    assert RayWorkerTargetCapability.objects.count() == 1
    assert RayTargetProbeChallenge.objects.count() == 2
    assert len(case.submissions) == 4


def test_lost_submit_retains_exact_job_until_independent_terminal_cleanup(case, database_backend):
    case.fail_submit = True
    ticket = case.manager.begin("alias0")
    case.manager.poll(ticket)
    with pytest.raises(adapter.JobsCohortAdapterError):
        case.manager.poll(ticket)
    assert len(case.submissions) == 1
    assert case.helper.outstanding is None
    case.manager.begin_cleanup(ticket)
    case.manager.poll(ticket)
    assert case.manager.outstanding is None
    assert len(case.submissions) == 1
    assert not case.manager.eligible_aliases()
    assert not RayTargetProbeJobReceipt.objects.exists()
    assert RayTargetProbeChallenge.objects.get().expected_target_policy_id is None


def test_pending_inspection_cannot_seed_database_only_qualification(case, database_backend):
    case.inspect_pending = True
    ticket = case.manager.begin("alias0")
    for _ in range(5):
        assert case.manager.poll(ticket) is None
    assert not RayTarget.objects.exists()
    assert not case.manager.eligible_aliases()
    assert len(case.submissions) == 1
    assert RayTargetProbeJobReceipt.objects.get().receipt_digest is not None


def test_retirement_advancing_clock_does_not_reuse_old_issue_time(
    case, monkeypatch, database_backend
):
    original = adapter.retire_and_reissue_cohort_job_probe

    def later(*args, **kwargs):
        case.clock.now += timedelta(milliseconds=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "retire_and_reissue_cohort_job_probe", later)
    result = finish(case, case.manager.begin("alias0"))
    assert result.shared.desired_state == "active"
    assert len(case.submissions) == 2


def test_operator_named_draining_session_is_reprobed_without_renaming(case, database_backend):
    from django.db import transaction

    from django_ray.target import coordination

    expected = RayTargetExpectation(
        "operator-drained", RayRunnerFamily.RAY_JOB, case.session, 1, RUNTIME
    )
    with transaction.atomic():
        coordination._register_ray_target_locked(expected, now=case.clock.now)
    result = finish(case, case.manager.begin("alias0"))
    assert result.shared.attestation.expectation == expected
    assert result.shared.desired_state == "draining"
    assert not case.manager.eligible_aliases()
    assert len(case.manager.qualified_aliases()) == 1
    assert list(RayTarget.objects.values_list("target_key", flat=True)) == ["operator-drained"]
    assert len(case.submissions) == 2


def test_same_alias_changed_session_retires_old_slot_without_capability_growth(
    case, database_backend
):
    first = finish(case, case.manager.begin("alias0"))
    old_slot = RayTargetProbeChallenge.objects.get().pk
    case.session = "session_restarted"
    second = finish(case, case.manager.begin("alias0"))
    assert (
        first.shared.attestation.expectation.target_key
        != second.shared.attestation.expectation.target_key
    )
    assert RayTarget.objects.count() == 2
    assert RayTargetProbeChallenge.objects.get().pk > old_slot
    assert RayTargetProbeJobReceipt.objects.count() == 1
    assert RayWorkerTargetCapability.objects.count() == 1


def test_refresh_withdraws_only_its_alias_before_preparation(case, database_backend):
    finish(case, case.manager.begin("alias0"))
    finish(case, case.manager.begin("alias1"))
    case.manager.begin("alias0")
    assert [item.configuration.alias for item in case.manager.eligible_aliases()] == ["alias1"]
    assert RayWorkerTargetCapability.objects.count() == 1


@pytest.mark.parametrize("terminal", [False, 1, "true"])
def test_stop_ack_or_coercion_cannot_retire_uncertain_submission(case, database_backend, terminal):
    case.fail_submit = True
    ticket = case.manager.begin("alias0")
    case.manager.poll(ticket)
    with pytest.raises(adapter.JobsCohortAdapterError):
        case.manager.poll(ticket)
    old = RayTargetProbeChallenge.objects.get().pk
    case.helper.handler = lambda payload: {"terminal": terminal}
    case.manager.begin_cleanup(ticket)
    with pytest.raises(adapter.JobsCohortAdapterError):
        case.manager.poll(ticket)
    assert case.manager.outstanding is ticket
    assert RayTargetProbeChallenge.objects.get().pk == old
    assert RayTargetProbeJobReceipt.objects.count() == 1


def test_lost_publication_response_never_reconstructs_positive_cache(
    case, monkeypatch, database_backend
):
    original = cohort_publication.publish_prepared_cohort_job_probe

    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("lost publisher acknowledgement")

    monkeypatch.setattr(cohort_publication, "publish_prepared_cohort_job_probe", lost)
    ticket = case.manager.begin("alias0")
    case.manager.poll(ticket)
    case.manager.poll(ticket)
    with pytest.raises(adapter.JobsCohortAdapterError):
        case.manager.poll(ticket)
    assert not case.manager.eligible_aliases()
    assert case.manager.outstanding is ticket
    assert RayTargetProbeChallenge.objects.get().consumed_at is not None
    with pytest.raises(RuntimeError):
        case.manager.begin_cleanup(ticket)
    assert case.manager.outstanding is ticket
