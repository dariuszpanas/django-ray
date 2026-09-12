"""Fabricated values exercise the parser only; these tests are not native evidence."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from qualification.docker.scenario import QualificationError
from qualification.upgrade import runtime_contract as c


def stamp(seconds):
    return (datetime(2026, 9, 12, tzinfo=UTC) + timedelta(seconds=seconds)).isoformat()


def owner(name, started):
    return {
        "worker_id": name,
        "hostname": "fixture-host",
        "pid": started + 100,
        "started_at": stamp(started),
        "pod_uid": name + "-pod",
    }


def task(pk, *, generation=1):
    return {"task_pk": pk, "task_id": f"task-{pk}", "attempt": 1, "generation": generation}


def terminal(name, seconds):
    return {
        "submission_id": name,
        "native_job_id": "01000000" if name == "old" else "02000000",
        "request_sha256": "a" * 64,
        "completion_sha256": "b" * 64,
        "completion_observed_at": stamp(seconds),
        "inspection_began_at": stamp(seconds + 2),
        "terminal_observed_at": stamp(seconds + 3),
        "status": "SUCCEEDED",
    }


def example_run():
    """Small, deliberately fake build identities for the resource-free fixture."""
    return c.RuntimeUpgradeRun(
        "fixture-run",
        "fixture-namespace",
        "namespace-uid",
        "primary",
        "scratch",
        "artifacts",
        c.RuntimeBuildIdentity(
            "0.4.0",
            "2.56.0",
            "3.12.12",
            c.BASELINE_COMMIT,
            "1" * 40,
            "1" * 64,
            "2" * 64,
            "3" * 64,
            "4" * 64,
        ),
        c.RuntimeBuildIdentity(
            "0.5.0",
            "2.58.0",
            "3.12.12",
            "2" * 40,
            "3" * 40,
            "5" * 64,
            "6" * 64,
            "7" * 64,
            "8" * 64,
        ),
    )


def example_observations():
    old, replacement = owner("old", 1), owner("old-replacement", 20)
    core, jobs, jobs_replacement = (
        owner("core", 80),
        owner("jobs", 90),
        owner("jobs-replacement", 92),
    )
    remote, current_remote = terminal("old", 23), terminal("current", 93)
    history, artifacts = "c" * 64, "d" * 64
    values = [
        {
            "ray_session": "old-session",
            "manager": old,
            "queued_task": task(1, generation=0),
            "running_task": task(2),
            "submission_id": remote["submission_id"],
            "native_job_id": remote["native_job_id"],
            "request_sha256": remote["request_sha256"],
            "blocked_backup_sha256": "e" * 64,
            "blocked_artifacts_sha256": "f" * 64,
            "clone_original_rows_sha256": "0" * 64,
            "public_enqueue": True,
            "running_effect_observed": True,
        },
        {
            "backup_sha256": "e" * 64,
            "artifacts_sha256": "f" * 64,
            "original_rows_before_sha256": "0" * 64,
            "original_rows_after_sha256": "0" * 64,
            "activation_refused": True,
            "activation_recorded": False,
            "policy_protocol": 1,
            "legacy_token_present": True,
        },
        {
            "original_manager": old,
            "replacement_manager": replacement,
            "task": task(2),
            "remote": remote,
            "original_manager_reaped": True,
            "stale_ownership_observed": True,
            "submitted_again": False,
            "effect_count": 1,
        },
        {
            "manager": replacement,
            "historical_rows_sha256": history,
            "artifacts_sha256": artifacts,
            "new_admission_stopped": True,
            "queued": 0,
            "running": 0,
            "cancelling": 0,
            "unresolved": 0,
            "remote_inventory_complete": True,
            "owned_callbacks_finished": True,
            "remote_jobs": [remote, terminal("queued", 33)],
        },
        {
            "manager": replacement,
            "manager_reaped": True,
            "producer_reaped": True,
            "purger_reaped": True,
            "active_leases": 0,
            "stopped_writers_sha256": "9" * 64,
            "historical_rows_sha256": history,
        },
        {
            "backup_sha256": "a" * 64,
            "backup_created_at": stamp(51),
            "restored_at": stamp(54),
            "artifacts_sha256": artifacts,
            "historical_rows_sha256": history,
            "restore_database": "scratch",
            "independent_restore": True,
            "input_and_result_bytes_verified": True,
        },
        {"ray_session": "old-session", "ray_pods_reaped": True},
        {
            "ray_session": "new-session",
            "old_ray_session": "old-session",
            "protocol": 3,
            "migration_leaf": "0035_activate_current_cohort",
            "legacy_admission_open": False,
            "historical_rows_sha256": history,
            "stopped_writers_sha256": "9" * 64,
        },
        {
            "ray_session": "new-session",
            "manager": core,
            "task": task(3),
            "claim_sha256": "a" * 64,
            "binding_sha256": "b" * 64,
            "intent_sha256": "c" * 64,
            "request_sha256": "d" * 64,
            "completion_sha256": "e" * 64,
            "application_succeeded": True,
            "effect_count": 1,
            "transport": "direct-ray-core",
        },
        {
            "ray_session": "new-session",
            "original_manager": jobs,
            "before_loss": {
                "task": task(4),
                "claim_sha256": "f" * 64,
                "submission_id": current_remote["submission_id"],
                "native_job_id": current_remote["native_job_id"],
                "request_sha256": current_remote["request_sha256"],
            },
            "replacement_manager": jobs_replacement,
            "task": task(4),
            "claim_sha256": "f" * 64,
            "binding_sha256": "a" * 64,
            "intent_sha256": "b" * 64,
            "remote": current_remote,
            "original_manager_reaped": True,
            "stale_ownership_observed": True,
            "submitted_again": False,
            "effect_count": 1,
            "application_succeeded": True,
            "cleanup_id": 1,
            "cleanup_revision": 2,
            "cleanup_created_at": stamp(94),
            "cleanup_closed_at": stamp(97),
            "cleanup_state": "CLOSED",
        },
        {
            "historical_rows_sha256": history,
            "artifacts_sha256": artifacts,
            "removed_callable_not_imported": True,
            "rendered_workflow_verified": True,
            "encrypted_runtime_env_recovered": True,
            "missing_corrupt_artifacts_refused": True,
        },
        {
            "backup_sha256": "a" * 64,
            "historical_rows_sha256": history,
            "restore_database": "scratch",
            "candidate_write_task_ids": ["task-3", "task-4", "task-5"],
            "absent_task_ids": ["task-3", "task-4", "task-5"],
            "code_only_write_refused": True,
            "reverse_migration_refused": True,
            "old_wheel_restored": True,
            "write_loss": True,
        },
        {
            "core_manager": core,
            "jobs_manager": jobs_replacement,
            "core_retirement_sha256": "c" * 64,
            "jobs_retirement_sha256": "d" * 64,
            "worker_authored_cleanup_verified": True,
            "core_manager_reaped": True,
            "jobs_manager_reaped": True,
            "active_leases": 0,
            "owned_tasks": 0,
            "unresolved_claims": 0,
            "capabilities": 0,
            "open_cleanup": 0,
            "owned_callbacks": 0,
        },
        {
            "namespace_uid": "namespace-uid",
            "namespace_deleted": True,
            "databases_removed": True,
            "artifacts_removed": True,
            "ray_pods_reaped": True,
            "server_stopped": True,
        },
    ]
    for index, value in enumerate(values):
        value.update(observation_kind="native", live_ray_generations=0 if index in (6, 13) else 1)
    return values


def example_receipt(*, values=None, run=None):
    run = example_run() if run is None else run
    values = example_observations() if values is None else values
    phases = []
    for index, (phase, observations) in enumerate(zip(c.PHASES, values, strict=True)):
        build = "baseline" if index in (0, 2, 3, 4, 5, 11) else "candidate"
        phases.append(
            c.build_phase(
                run,
                phase,
                observer=c.RuntimeObserver(
                    f"observer-{index}", index + 1, stamp(index * 10), build
                ),
                began_at=stamp(index * 10),
                finished_at=stamp(index * 10 + 9),
                observations=observations,
                previous=phases[-1] if phases else None,
            )
        )
    return c.RuntimeUpgradeReceipt(run, tuple(phases))


def test_canonical_roundtrip_keeps_runtime_scope_below_complete_release_gate():
    receipt = example_receipt()
    encoded = c.encode_runtime_receipt(receipt)
    assert c.decode_runtime_receipt(encoded) == receipt
    assert json.loads(encoded)["complete_upgrade_gate"] is False
    assert len(receipt.phases) == 14
    assert receipt.phases[0].predecessor_digest is None
    for previous, phase in zip(receipt.phases, receipt.phases[1:], strict=False):
        assert phase.predecessor_digest == c.runtime_phase_digest(previous)
    # Historical data do not pretend the active clone's owner/heartbeat stayed fixed.
    assert (
        receipt.phases[1].observations["original_rows_after_sha256"]
        != receipt.phases[3].observations["historical_rows_sha256"]
    )


@pytest.mark.parametrize(
    "changed_identity",
    [{}, {"pod_uid": "another-pod"}, {"pid": 314}, {"started_at": stamp(1)}],
    ids=["same-process", "different-pod", "different-pid", "different-start"],
)
def test_rehashed_phases_require_distinct_observer_process_identities(changed_identity):
    receipt = example_receipt()
    reused = replace(receipt.phases[0].observer, **changed_identity)
    phases = []
    for index, phase in enumerate(receipt.phases):
        phases.append(
            replace(
                phase,
                # Both phases use the baseline build; all chronology remains valid.
                observer=reused if index == 2 else phase.observer,
                predecessor_digest=c.runtime_phase_digest(phases[-1]) if phases else None,
            )
        )
    changed = replace(receipt, phases=tuple(phases))
    if not changed_identity:
        with pytest.raises(QualificationError, match="^invalid-native-upgrade-receipt$"):
            c.encode_runtime_receipt(changed)
    else:
        assert c.decode_runtime_receipt(c.encode_runtime_receipt(changed)) == changed


@pytest.mark.parametrize(
    "field,bad",
    [
        ("baseline", {"package_version": "0.4.1"}),
        ("baseline", {"ray_version": "2.58.0"}),
        ("baseline", {"source_commit": "f" * 40}),
        ("candidate", {"package_version": "0.4.0"}),
        ("candidate", {"ray_version": "2.59.0"}),
        ("candidate", {"python_version": "3.12"}),
        ("candidate", {"wheel_sha256": "F" * 64}),
        ("candidate", {"source_tree": "a" * 64}),
        ("candidate", {"archive_sha256": "a" * 63}),
        ("candidate", {"image_sha256": True}),
    ],
)
def test_released_and_candidate_builds_are_exact(field, bad):
    run = example_run()
    run = replace(run, **{field: replace(getattr(run, field), **bad)})
    with pytest.raises(QualificationError):
        c.runtime_run_digest(run)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_live_ray_generations": 2},
        {"max_live_ray_generations": True},
        {"manager_concurrency": 2},
        {"scratch_database_identity": "primary"},
        {"namespace_uid": ""},
        {"run_id": "x\x00y"},
        {"artifact_store_identity": "x" * 257},
    ],
)
def test_resource_scope_is_finite_and_restore_is_independent(changes):
    with pytest.raises(QualificationError):
        c.runtime_run_digest(replace(example_run(), **changes))


@pytest.mark.parametrize(
    "index,field,bad",
    [
        (0, "public_enqueue", 1),
        (0, "observation_kind", "synthetic-released-models"),
        (0, "live_ray_generations", 2),
        (1, "activation_refused", False),
        (1, "activation_recorded", True),
        (1, "policy_protocol", True),
        (2, "submitted_again", True),
        (2, "effect_count", 2),
        (3, "remote_jobs", []),
        (3, "remote_inventory_complete", False),
        (3, "owned_callbacks_finished", False),
        (3, "unresolved", 1),
        (3, "queued", False),
        (4, "producer_reaped", False),
        (4, "purger_reaped", False),
        (4, "manager_reaped", False),
        (5, "independent_restore", False),
        (5, "input_and_result_bytes_verified", False),
        (6, "ray_pods_reaped", False),
        (6, "live_ray_generations", 1),
        (7, "protocol", 1),
        (7, "legacy_admission_open", True),
        (8, "application_succeeded", False),
        (8, "transport", "configured-address"),
        (9, "cleanup_state", "OPEN"),
        (9, "cleanup_revision", 1),
        (9, "cleanup_id", True),
        (10, "removed_callable_not_imported", False),
        (10, "rendered_workflow_verified", False),
        (10, "encrypted_runtime_env_recovered", False),
        (10, "missing_corrupt_artifacts_refused", False),
        (11, "write_loss", False),
        (11, "code_only_write_refused", False),
        (11, "reverse_migration_refused", False),
        (11, "old_wheel_restored", False),
        (12, "worker_authored_cleanup_verified", False),
        (12, "open_cleanup", 1),
        (13, "namespace_deleted", False),
        (13, "server_stopped", False),
    ],
)
def test_required_phase_observations_cannot_be_missing_or_substituted(index, field, bad):
    values = example_observations()
    values[index][field] = bad
    with pytest.raises(QualificationError):
        c.encode_runtime_receipt(example_receipt(values=values))


@pytest.mark.parametrize(
    "index,field,bad",
    [
        (1, "backup_sha256", "1" * 64),
        (1, "original_rows_after_sha256", "1" * 64),
        (2, "original_manager", owner("other", 1)),
        (2, "task", task(7)),
        (2, "replacement_manager", owner("old", 1)),
        (3, "manager", owner("other", 20)),
        (4, "historical_rows_sha256", "1" * 64),
        (5, "artifacts_sha256", "1" * 64),
        (5, "restore_database", "primary"),
        (5, "backup_created_at", stamp(48)),
        (6, "ray_session", "another-old-session"),
        (7, "ray_session", "old-session"),
        (7, "stopped_writers_sha256", "1" * 64),
        (8, "ray_session", "other"),
        (8, "task", task(2)),
        (9, "task", task(3)),
        (9, "replacement_manager", owner("jobs", 90)),
        (9, "cleanup_created_at", stamp(96)),
        (9, "cleanup_closed_at", stamp(95)),
        (10, "historical_rows_sha256", "1" * 64),
        (11, "backup_sha256", "e" * 64),
        (11, "absent_task_ids", ["task-3", "task-4"]),
        (12, "jobs_manager", owner("other", 92)),
        (13, "namespace_uid", "other"),
    ],
)
def test_rehashed_chain_still_rejects_contradictory_native_identity_history_and_time(
    index, field, bad
):
    values = example_observations()
    values[index][field] = bad
    with pytest.raises(QualificationError):
        c.encode_runtime_receipt(example_receipt(values=values))


def test_status_and_sql_zeroes_cannot_replace_owned_remote_cleanup():
    values = example_observations()
    values[3]["remote_jobs"] = [terminal("unrelated", 33)]
    with pytest.raises(QualificationError):
        c.encode_runtime_receipt(example_receipt(values=values))
    values = example_observations()
    values[9]["remote"]["inspection_began_at"] = stamp(92)
    with pytest.raises(QualificationError):
        c.encode_runtime_receipt(example_receipt(values=values))


@pytest.mark.parametrize(
    "field,bad",
    [
        ("task", task(99)),
        ("claim_sha256", "e" * 64),
        ("submission_id", "another-job"),
        ("native_job_id", "03000000"),
        ("request_sha256", "e" * 64),
    ],
)
def test_current_recovery_retains_original_claim_and_physical_job(field, bad):
    values = example_observations()
    values[9]["before_loss"][field] = bad
    with pytest.raises(QualificationError):
        c.encode_runtime_receipt(example_receipt(values=values))


@pytest.mark.parametrize(
    "index,field,seconds",
    [
        (2, "terminal_observed_at", 30),
        (9, "completion_observed_at", 99),
        (9, "inspection_began_at", 92),
        (9, "terminal_observed_at", 100),
    ],
)
def test_remote_event_chronology_is_bounded_by_observer_phase(index, field, seconds):
    values = example_observations()
    values[index]["remote"][field] = stamp(seconds)
    with pytest.raises(QualificationError):
        c.encode_runtime_receipt(example_receipt(values=values))


def test_new_managers_cannot_precede_cold_replacement_or_reuse_old_task_primary_key():
    for bad in (owner("core", 60), owner("core", 100)):
        values = example_observations()
        values[8]["manager"] = values[12]["core_manager"] = bad
        with pytest.raises(QualificationError):
            c.encode_runtime_receipt(example_receipt(values=values))
    values = example_observations()
    values[8]["task"]["task_pk"] = 2
    with pytest.raises(QualificationError):
        c.encode_runtime_receipt(example_receipt(values=values))


@pytest.mark.parametrize(
    "field",
    [
        "source_commit",
        "source_tree",
        "archive_sha256",
        "wheel_sha256",
        "image_sha256",
        "lock_sha256",
    ],
)
def test_different_runtime_builds_cannot_relabel_identical_artifacts(field):
    run = example_run()
    changed = replace(run.candidate, **{field: getattr(run.baseline, field)})
    with pytest.raises(QualificationError):
        c.runtime_run_digest(replace(run, candidate=changed))


@pytest.mark.parametrize(
    "mutation", ["missing", "order", "source", "predecessor", "time", "observer", "overclaim"]
)
def test_phase_chain_cannot_be_replayed_into_other_run_or_order(mutation):
    receipt = example_receipt()
    phases = list(receipt.phases)
    if mutation == "missing":
        phases.pop(3)
    elif mutation == "order":
        phases[3], phases[4] = phases[4], phases[3]
    elif mutation == "source":
        receipt = replace(receipt, run=replace(receipt.run, namespace_uid="different"))
    elif mutation == "predecessor":
        phases[2] = replace(phases[2], predecessor_digest="0" * 64)
    elif mutation == "time":
        phases[2] = replace(
            phases[2],
            began_at=stamp(18),
            observer=replace(phases[2].observer, started_at=stamp(18)),
        )
    elif mutation == "observer":
        phases[5] = replace(phases[5], observer=replace(phases[5].observer, build="candidate"))
    elif mutation == "overclaim":
        receipt = replace(receipt, complete_upgrade_gate=True)
    with pytest.raises(QualificationError):
        c.encode_runtime_receipt(replace(receipt, phases=tuple(phases)))


@pytest.mark.parametrize(
    "edit",
    [
        lambda raw: raw.update(extra=True),
        lambda raw: raw.update(schema=True),
        lambda raw: raw["run"].update(extra=True),
        lambda raw: raw["run"].pop("manager_concurrency"),
        lambda raw: raw["phases"][0]["observer"].update(pid="1"),
        lambda raw: raw["phases"][0]["observations"].update(extra=True),
        lambda raw: raw["phases"][0]["observations"].pop("running_effect_observed"),
        lambda raw: raw["phases"][0]["observations"]["manager"].update(extra=True),
        lambda raw: raw["phases"][0]["observations"]["running_task"].update(attempt=1.0),
        lambda raw: raw["phases"][0].update(began_at="2026-09-12T00:00:00"),
    ],
)
def test_decoder_refuses_unknown_missing_and_coerced_fields(edit):
    raw = json.loads(c.encode_runtime_receipt(example_receipt()))
    edit(raw)
    with pytest.raises(QualificationError):
        c.decode_runtime_receipt(json.dumps(raw, sort_keys=True, separators=(",", ":")))


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "null",
        "[]",
        '{"schema":1,"schema":1}',
        "{" * 2000,
        "x" * (c.MAX_RECEIPT_BYTES + 1),
        '{"x":NaN}',
        '{"x":' + "1" * 5000 + "}",
    ],
    ids=["empty", "null", "array", "duplicate", "depth", "bytes", "nan", "integer"],
)
def test_malformed_and_oversized_input_fails_with_fixed_reason(payload):
    with pytest.raises(QualificationError, match="^invalid-native-upgrade-receipt$"):
        c.decode_runtime_receipt(payload)


def test_canonical_decode_rejects_alternate_spelling_and_duplicate_nested_keys():
    encoded = c.encode_runtime_receipt(example_receipt())
    for changed in (
        encoded + "\n",
        encoded.replace('"pid":1,', '"pid":1,"pid":1,', 1),
        encoded.replace('"effect_count":1', '"effect_count":true', 1),
    ):
        with pytest.raises(QualificationError):
            c.decode_runtime_receipt(changed)


def test_builder_copies_observations_and_requires_exact_predecessor():
    receipt = example_receipt()
    first = receipt.phases[0]
    copied = copy.deepcopy(first.observations)
    built = c.build_phase(
        receipt.run,
        first.phase,
        observer=first.observer,
        began_at=first.began_at,
        finished_at=first.finished_at,
        observations=copied,
    )
    copied["manager"]["pid"] = 999
    assert built.observations == first.observations
    for previous in (None, receipt.phases[2]):
        second = receipt.phases[1]
        with pytest.raises(QualificationError):
            c.build_phase(
                receipt.run,
                second.phase,
                observer=second.observer,
                began_at=second.began_at,
                finished_at=second.finished_at,
                observations=second.observations,
                previous=previous,
            )
    assert asdict(receipt.run)["max_live_ray_generations"] == 1


@pytest.mark.parametrize("bad", [None, "invalid", "2026-99-99T00:00:00+00:00", "x" * 33])
def test_invalid_observer_clock_is_not_coerced(bad):
    receipt = example_receipt()
    phase = receipt.phases[0]
    with pytest.raises(QualificationError):
        c.runtime_phase_digest(replace(phase, began_at=bad))


def test_live_rows_are_not_the_historical_immutable_snapshot():
    values = example_observations()
    # A real new incarnation differs, while preserved terminal rows remain fixed.
    values[2]["replacement_manager"] = values[3]["manager"] = values[4]["manager"] = owner(
        "fresh", 21
    )
    value = c.validate_runtime_receipt(example_receipt(values=values))
    assert (
        value.phases[4].observations["historical_rows_sha256"]
        == values[3]["historical_rows_sha256"]
    )
    values[2]["replacement_manager"] = values[3]["manager"] = values[4]["manager"] = owner(
        "fresh", 1
    )
    with pytest.raises(QualificationError):
        c.validate_runtime_receipt(example_receipt(values=values))


def test_old_remote_identity_cannot_change_even_with_recomputed_phase_hashes():
    values = example_observations()
    values[2]["remote"]["native_job_id"] = "ffffffff"
    with pytest.raises(QualificationError):
        c.validate_runtime_receipt(example_receipt(values=values))


@pytest.mark.parametrize(
    "bad",
    [
        [0] * 33,
        {str(index): True for index in range(49)},
        {"x": "x" * 1025},
        {"x": "a\x00b"},
        {"x": float("inf")},
    ],
)
def test_builder_bounds_json_before_processing_observations(bad):
    run = example_run()
    with pytest.raises(QualificationError):
        c.build_phase(
            run,
            c.PHASES[0],
            observer=c.RuntimeObserver("pod", 1, stamp(0), "baseline"),
            began_at=stamp(0),
            finished_at=stamp(1),
            observations=bad,
        )


def test_parser_rejects_deeply_nested_unknown_content_without_leaking_parser_error():
    value = None
    for _ in range(14):
        value = [value]
    with pytest.raises(QualificationError):
        c.decode_runtime_receipt(json.dumps(value))


def test_entrypoints_reject_wrong_dataclass_types_and_invalid_references():
    receipt = example_receipt()
    for value in (None, {}, asdict(receipt.run)):
        with pytest.raises(QualificationError):
            c.runtime_run_digest(value)
    with pytest.raises(QualificationError):
        c.runtime_run_digest(replace(receipt.run, baseline=asdict(receipt.run.baseline)))
    for phase in (None, replace(receipt.phases[0], run_digest="bad")):
        with pytest.raises(QualificationError):
            c.runtime_phase_digest(phase)
    second = receipt.phases[1]
    with pytest.raises(QualificationError):
        c.build_phase(
            receipt.run,
            second.phase,
            observer=second.observer,
            began_at=second.began_at,
            finished_at=second.finished_at,
            observations=second.observations,
            previous={},
        )
