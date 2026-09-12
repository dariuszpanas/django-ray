"""Finite receipt format for the native coordinated Beta upgrade rehearsal.

This parser checks structure and consistency of trusted, source-owned observer
output. Neither a digest nor a caller-supplied boolean authenticates an event.
The orchestrator must obtain the observations from the actual owned resources;
SQL zeroes, stop acknowledgments and synthetic fixture settlement are insufficient.
Passing this runtime recipe does not assert the complete release upgrade gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Never, cast

from qualification.docker.scenario import QualificationError
from qualification.upgrade.contract import BASELINE_COMMIT, BASELINE_VERSION, CANDIDATE_VERSION

MAX_RECEIPT_BYTES = 256 * 1024
MAX_ITEMS = 32
_DOMAIN = b"django-ray-native-upgrade-v1\x00"


class RuntimePhase(StrEnum):
    OLD_ACTIVE = "old-active"
    BLOCKED_CLONE = "blocked-clone-refusal"
    OLD_RECOVERED = "old-recovered"
    OLD_DRAINED = "old-drained"
    OLD_WRITERS_STOPPED = "old-writers-stopped"
    BACKUP_RESTORED = "backup-restored-old"
    OLD_RAY_STOPPED = "old-ray-stopped"
    ACTIVATED = "activated"
    CURRENT_CORE = "current-core"
    CURRENT_JOBS = "current-jobs-recovered"
    HISTORY_PRESERVED = "history-preserved"
    ROLLBACK_BOUNDARY = "rollback-boundary"
    CURRENT_RETIRED = "current-retired"
    FIXTURE_CLEANED = "fixture-cleaned"


PHASES = tuple(RuntimePhase)


@dataclass(frozen=True)
class RuntimeBuildIdentity:
    package_version: str
    ray_version: str
    python_version: str
    source_commit: str
    source_tree: str
    archive_sha256: str
    wheel_sha256: str
    image_sha256: str
    lock_sha256: str


@dataclass(frozen=True)
class RuntimeUpgradeRun:
    run_id: str
    namespace: str
    namespace_uid: str
    database_identity: str
    scratch_database_identity: str
    artifact_store_identity: str
    baseline: RuntimeBuildIdentity
    candidate: RuntimeBuildIdentity
    max_live_ray_generations: int = 1
    manager_concurrency: int = 1


@dataclass(frozen=True)
class RuntimeObserver:
    """The actual fresh observer process, not the manager it is inspecting."""

    pod_uid: str
    pid: int
    started_at: str
    build: str


@dataclass(frozen=True)
class RuntimePhaseReceipt:
    phase: RuntimePhase
    run_digest: str
    predecessor_digest: str | None
    observer: RuntimeObserver
    began_at: str
    finished_at: str
    observations: dict[str, Any]


@dataclass(frozen=True)
class RuntimeUpgradeReceipt:
    run: RuntimeUpgradeRun
    phases: tuple[RuntimePhaseReceipt, ...]
    complete_upgrade_gate: bool = False


def _reject() -> Never:
    raise QualificationError("invalid-native-upgrade-receipt")


def _text(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 256
        and all(32 <= ord(char) <= 126 for char in value)
        and bool(value.strip())
    )


def _hex(value: object, size: int = 64) -> bool:
    return type(value) is str and re.fullmatch(rf"[0-9a-f]{{{size}}}", value) is not None


def _integer(value: object, minimum: int = 1, maximum: int = 2**63 - 1) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _time(value: object) -> datetime:
    if type(value) is not str or len(value) > 32:
        _reject()
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        _reject()
    if parsed.tzinfo != UTC or parsed.isoformat() != value:
        _reject()
    return parsed


def _shape(value: object, fields: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        _reject()
    return cast(dict[str, Any], value)


def _tree(value: object, depth: int = 0) -> None:
    if depth > 12:
        _reject()
    if value is None or type(value) in (bool, int, str):
        if type(value) is str and (len(value) > 1024 or "\x00" in value):
            _reject()
        return
    if type(value) is list or type(value) is tuple:
        if len(value) > MAX_ITEMS:
            _reject()
        for item in value:
            _tree(item, depth + 1)
    elif type(value) is dict:
        if len(value) > 48 or any(type(key) is not str for key in value):
            _reject()
        for item in value.values():
            _tree(item, depth + 1)
    else:
        _reject()


def _canonical(value: object) -> str:
    _tree(value)
    result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(result.encode("ascii")) > MAX_RECEIPT_BYTES:
        _reject()
    return result


def _digest(kind: bytes, value: object) -> str:
    return hashlib.sha256(_DOMAIN + kind + b"\x00" + _canonical(value).encode("ascii")).hexdigest()


def _validate_run(run: RuntimeUpgradeRun) -> None:
    if type(run) is not RuntimeUpgradeRun:
        _reject()
    for name in (
        "run_id",
        "namespace",
        "namespace_uid",
        "database_identity",
        "scratch_database_identity",
        "artifact_store_identity",
    ):
        if not _text(getattr(run, name)):
            _reject()
    if (
        run.database_identity == run.scratch_database_identity
        or type(run.max_live_ray_generations) is not int
        or run.max_live_ray_generations != 1
        or type(run.manager_concurrency) is not int
        or run.manager_concurrency != 1
    ):
        _reject()
    for build, package, ray in (
        (run.baseline, BASELINE_VERSION, "2.56.0"),
        (run.candidate, CANDIDATE_VERSION, "2.58.0"),
    ):
        if type(build) is not RuntimeBuildIdentity:
            _reject()
        if (
            type(build.package_version) is not str
            or build.package_version != package
            or type(build.ray_version) is not str
            or build.ray_version != ray
            or type(build.python_version) is not str
            or re.fullmatch(r"3\.(?:12|13|14)\.(?:0|[1-9][0-9]{0,2})", build.python_version) is None
            or not _hex(build.source_commit, 40)
            or not _hex(build.source_tree, 40)
            or any(
                not _hex(getattr(build, field))
                for field in (
                    "archive_sha256",
                    "wheel_sha256",
                    "image_sha256",
                    "lock_sha256",
                )
            )
        ):
            _reject()
    if run.baseline.source_commit != BASELINE_COMMIT or any(
        getattr(run.baseline, field) == getattr(run.candidate, field)
        for field in (
            "source_commit",
            "source_tree",
            "archive_sha256",
            "wheel_sha256",
            "image_sha256",
            "lock_sha256",
        )
    ):
        _reject()


def runtime_run_digest(run: RuntimeUpgradeRun) -> str:
    _validate_run(run)
    return _digest(b"run", asdict(run))


def _owner(value: object) -> bool:
    value = _shape(value, {"worker_id", "hostname", "pid", "started_at", "pod_uid"})
    _time(value["started_at"])
    return all(_text(value[name]) for name in ("worker_id", "hostname", "pod_uid")) and _integer(
        value["pid"]
    )


def _task(value: object) -> bool:
    value = _shape(value, {"task_pk", "task_id", "attempt", "generation"})
    return (
        _integer(value["task_pk"])
        and _text(value["task_id"])
        and _integer(value["attempt"])
        and _integer(value["generation"], 0)
    )


def _strings(value: object) -> bool:
    return (
        type(value) is list
        and 0 < len(value) <= MAX_ITEMS
        and all(_text(item) for item in value)
        and len(set(value)) == len(value)
    )


def _terminal(value: object) -> bool:
    value = _shape(
        value,
        {
            "submission_id",
            "native_job_id",
            "request_sha256",
            "completion_sha256",
            "completion_observed_at",
            "inspection_began_at",
            "terminal_observed_at",
            "status",
        },
    )
    return (
        _text(value["submission_id"])
        and _hex(value["native_job_id"], 8)
        and _hex(value["request_sha256"])
        and _hex(value["completion_sha256"])
        and type(value["status"]) is str
        and value["status"] in {"SUCCEEDED", "FAILED", "STOPPED"}
        and _time(value["completion_observed_at"])
        <= _time(value["inspection_began_at"])
        <= _time(value["terminal_observed_at"])
    )


def _terminals(value: Any) -> bool:
    return (
        type(value) is list
        and 0 < len(value) <= MAX_ITEMS
        and all(_terminal(item) for item in value)
        and len({item["submission_id"] for item in value}) == len(value)
    )


def _inflight(value: object) -> bool:
    value = _shape(
        value, {"task", "claim_sha256", "submission_id", "native_job_id", "request_sha256"}
    )
    return (
        _task(value["task"])
        and _hex(value["claim_sha256"])
        and _text(value["submission_id"])
        and _hex(value["native_job_id"], 8)
        and _hex(value["request_sha256"])
    )


# Validators are finite and phase-specific. Counts corroborate the separate
# native terminal observations and writer/creator-thread reaping assertions.
def _yes(value):
    return value is True


def _no(value):
    return value is False


def _zero(value):
    return type(value) is int and value == 0


def _one(value):
    return type(value) is int and value == 1


_FIELDS = {
    RuntimePhase.OLD_ACTIVE: {
        "ray_session": _text,
        "manager": _owner,
        "queued_task": _task,
        "running_task": _task,
        "submission_id": _text,
        "native_job_id": lambda v: _hex(v, 8),
        "request_sha256": _hex,
        "blocked_backup_sha256": _hex,
        "blocked_artifacts_sha256": _hex,
        "clone_original_rows_sha256": _hex,
        "public_enqueue": _yes,
        "running_effect_observed": _yes,
    },
    RuntimePhase.BLOCKED_CLONE: {
        "backup_sha256": _hex,
        "artifacts_sha256": _hex,
        "original_rows_before_sha256": _hex,
        "original_rows_after_sha256": _hex,
        "activation_refused": _yes,
        "activation_recorded": _no,
        "policy_protocol": _one,
        "legacy_token_present": _yes,
    },
    RuntimePhase.OLD_RECOVERED: {
        "original_manager": _owner,
        "replacement_manager": _owner,
        "task": _task,
        "remote": _terminal,
        "original_manager_reaped": _yes,
        "stale_ownership_observed": _yes,
        "submitted_again": _no,
        "effect_count": _one,
    },
    RuntimePhase.OLD_DRAINED: {
        "manager": _owner,
        "historical_rows_sha256": _hex,
        "artifacts_sha256": _hex,
        "new_admission_stopped": _yes,
        "queued": _zero,
        "running": _zero,
        "cancelling": _zero,
        "unresolved": _zero,
        "remote_inventory_complete": _yes,
        "owned_callbacks_finished": _yes,
        "remote_jobs": _terminals,
    },
    RuntimePhase.OLD_WRITERS_STOPPED: {
        "manager": _owner,
        "manager_reaped": _yes,
        "producer_reaped": _yes,
        "purger_reaped": _yes,
        "active_leases": _zero,
        "stopped_writers_sha256": _hex,
        "historical_rows_sha256": _hex,
    },
    RuntimePhase.BACKUP_RESTORED: {
        "backup_sha256": _hex,
        "backup_created_at": lambda v: bool(_time(v)),
        "restored_at": lambda v: bool(_time(v)),
        "artifacts_sha256": _hex,
        "historical_rows_sha256": _hex,
        "restore_database": _text,
        "independent_restore": _yes,
        "input_and_result_bytes_verified": _yes,
    },
    RuntimePhase.OLD_RAY_STOPPED: {"ray_session": _text, "ray_pods_reaped": _yes},
    RuntimePhase.ACTIVATED: {
        "ray_session": _text,
        "old_ray_session": _text,
        "protocol": lambda v: type(v) is int and v == 3,
        "migration_leaf": lambda v: type(v) is str and v == "0035_activate_current_cohort",
        "legacy_admission_open": _no,
        "historical_rows_sha256": _hex,
        "stopped_writers_sha256": _hex,
    },
    RuntimePhase.CURRENT_CORE: {
        "ray_session": _text,
        "manager": _owner,
        "task": _task,
        "claim_sha256": _hex,
        "binding_sha256": _hex,
        "intent_sha256": _hex,
        "request_sha256": _hex,
        "completion_sha256": _hex,
        "application_succeeded": _yes,
        "effect_count": _one,
        "transport": lambda v: type(v) is str and v in {"direct-ray-core", "ray-client"},
    },
    RuntimePhase.CURRENT_JOBS: {
        "ray_session": _text,
        "before_loss": _inflight,
        "original_manager": _owner,
        "replacement_manager": _owner,
        "task": _task,
        "claim_sha256": _hex,
        "binding_sha256": _hex,
        "intent_sha256": _hex,
        "remote": _terminal,
        "original_manager_reaped": _yes,
        "stale_ownership_observed": _yes,
        "submitted_again": _no,
        "effect_count": _one,
        "application_succeeded": _yes,
        "cleanup_id": _integer,
        "cleanup_revision": lambda v: _integer(v, 2),
        "cleanup_created_at": lambda v: bool(_time(v)),
        "cleanup_closed_at": lambda v: bool(_time(v)),
        "cleanup_state": lambda v: type(v) is str and v == "CLOSED",
    },
    RuntimePhase.HISTORY_PRESERVED: {
        "historical_rows_sha256": _hex,
        "artifacts_sha256": _hex,
        "removed_callable_not_imported": _yes,
        "rendered_workflow_verified": _yes,
        "encrypted_runtime_env_recovered": _yes,
        "missing_corrupt_artifacts_refused": _yes,
    },
    RuntimePhase.ROLLBACK_BOUNDARY: {
        "backup_sha256": _hex,
        "historical_rows_sha256": _hex,
        "restore_database": _text,
        "candidate_write_task_ids": _strings,
        "absent_task_ids": _strings,
        "code_only_write_refused": _yes,
        "reverse_migration_refused": _yes,
        "old_wheel_restored": _yes,
        "write_loss": _yes,
    },
    RuntimePhase.CURRENT_RETIRED: {
        "core_manager": _owner,
        "jobs_manager": _owner,
        "core_retirement_sha256": _hex,
        "jobs_retirement_sha256": _hex,
        "worker_authored_cleanup_verified": _yes,
        "core_manager_reaped": _yes,
        "jobs_manager_reaped": _yes,
        "active_leases": _zero,
        "owned_tasks": _zero,
        "unresolved_claims": _zero,
        "capabilities": _zero,
        "open_cleanup": _zero,
        "owned_callbacks": _zero,
    },
    RuntimePhase.FIXTURE_CLEANED: {
        "namespace_uid": _text,
        "namespace_deleted": _yes,
        "databases_removed": _yes,
        "artifacts_removed": _yes,
        "ray_pods_reaped": _yes,
        "server_stopped": _yes,
    },
}


def _phase_value(value: RuntimePhaseReceipt) -> dict:
    result = asdict(value)
    result["phase"] = value.phase.value
    return result


def _validate_phase(value: RuntimePhaseReceipt) -> None:
    if type(value) is not RuntimePhaseReceipt or type(value.phase) is not RuntimePhase:
        _reject()
    if not _hex(value.run_digest) or (
        value.predecessor_digest is not None and not _hex(value.predecessor_digest)
    ):
        _reject()
    observer = value.observer
    if (
        type(observer) is not RuntimeObserver
        or not _text(observer.pod_uid)
        or not _integer(observer.pid)
        or type(observer.build) is not str
        or observer.build not in {"baseline", "candidate"}
        or not _time(observer.started_at) <= _time(value.began_at) <= _time(value.finished_at)
    ):
        _reject()
    baseline = value.phase in {
        RuntimePhase.OLD_ACTIVE,
        RuntimePhase.OLD_RECOVERED,
        RuntimePhase.OLD_DRAINED,
        RuntimePhase.OLD_WRITERS_STOPPED,
        RuntimePhase.BACKUP_RESTORED,
        RuntimePhase.ROLLBACK_BOUNDARY,
    }
    if observer.build != ("baseline" if baseline else "candidate"):
        _reject()
    fields = _FIELDS[value.phase]
    observations = value.observations
    _shape(observations, set(fields) | {"observation_kind", "live_ray_generations"})
    expected_live = (
        0 if value.phase in {RuntimePhase.OLD_RAY_STOPPED, RuntimePhase.FIXTURE_CLEANED} else 1
    )
    if (
        type(observations["observation_kind"]) is not str
        or observations["observation_kind"] != "native"
        or type(observations["live_ray_generations"]) is not int
        or observations["live_ray_generations"] != expected_live
        or any(not validator(observations[field]) for field, validator in fields.items())
    ):
        _reject()
    _canonical(_phase_value(value))


def runtime_phase_digest(value: RuntimePhaseReceipt) -> str:
    _validate_phase(value)
    return _digest(b"phase", _phase_value(value))


def build_phase(
    run: RuntimeUpgradeRun,
    phase: RuntimePhase,
    *,
    observer: RuntimeObserver,
    began_at: str,
    finished_at: str,
    observations: dict[str, Any],
    previous: RuntimePhaseReceipt | None = None,
) -> RuntimePhaseReceipt:
    """Bind observer output; this does not obtain or authenticate any evidence."""
    digest = runtime_run_digest(run)
    index = PHASES.index(phase) if type(phase) is RuntimePhase else -1
    if index < 0 or (index == 0) != (previous is None):
        _reject()
    if previous is not None and type(previous) is not RuntimePhaseReceipt:
        _reject()
    if previous is not None and (
        previous.phase is not PHASES[index - 1]
        or previous.run_digest != digest
        or _time(previous.finished_at) > _time(began_at)
    ):
        _reject()
    value = RuntimePhaseReceipt(
        phase,
        digest,
        None if previous is None else runtime_phase_digest(previous),
        observer,
        began_at,
        finished_at,
        json.loads(_canonical(observations)),
    )
    _validate_phase(value)
    return value


def validate_runtime_receipt(value: RuntimeUpgradeReceipt) -> RuntimeUpgradeReceipt:
    """Require the entire ordered recipe and corroborating identity/chronology links."""
    if (
        type(value) is not RuntimeUpgradeReceipt
        or value.complete_upgrade_gate is not False
        or type(value.phases) is not tuple
        or len(value.phases) != len(PHASES)
    ):
        _reject()
    run_digest = runtime_run_digest(value.run)
    predecessor = None
    previous_time = None
    observers: set[tuple[str, int, str]] = set()
    for expected, phase in zip(PHASES, value.phases, strict=True):
        _validate_phase(phase)
        observer_identity = (phase.observer.pod_uid, phase.observer.pid, phase.observer.started_at)
        if (
            phase.phase is not expected
            or phase.run_digest != run_digest
            or phase.predecessor_digest != predecessor
            or observer_identity in observers
            or previous_time is not None
            and _time(phase.began_at) < previous_time
        ):
            _reject()
        observers.add(observer_identity)
        predecessor = runtime_phase_digest(phase)
        previous_time = _time(phase.finished_at)
    facts = {phase.phase: phase.observations for phase in value.phases}
    (
        a,
        blocked,
        recovered,
        drained,
        stopped,
        backup,
        ray_stop,
        activated,
        core,
        jobs,
        history,
        rollback,
        retired,
        cleaned,
    ) = (facts[phase] for phase in PHASES)
    comparisons = (
        (blocked["backup_sha256"], a["blocked_backup_sha256"]),
        (blocked["artifacts_sha256"], a["blocked_artifacts_sha256"]),
        (blocked["original_rows_before_sha256"], a["clone_original_rows_sha256"]),
        (blocked["original_rows_after_sha256"], a["clone_original_rows_sha256"]),
        (recovered["original_manager"], a["manager"]),
        (recovered["task"], a["running_task"]),
        (drained["manager"], recovered["replacement_manager"]),
        (stopped["manager"], drained["manager"]),
        (ray_stop["ray_session"], a["ray_session"]),
        (activated["old_ray_session"], a["ray_session"]),
        (activated["stopped_writers_sha256"], stopped["stopped_writers_sha256"]),
        (core["ray_session"], activated["ray_session"]),
        (jobs["ray_session"], activated["ray_session"]),
        (rollback["backup_sha256"], backup["backup_sha256"]),
        (backup["restore_database"], value.run.scratch_database_identity),
        (rollback["restore_database"], value.run.scratch_database_identity),
        (retired["core_manager"], core["manager"]),
        (retired["jobs_manager"], jobs["replacement_manager"]),
        (cleaned["namespace_uid"], value.run.namespace_uid),
    )
    if any(left != right for left, right in comparisons):
        _reject()
    for item in (stopped, backup, activated, history, rollback):
        if item["historical_rows_sha256"] != drained["historical_rows_sha256"]:
            _reject()
    if any(item["artifacts_sha256"] != drained["artifacts_sha256"] for item in (backup, history)):
        _reject()
    for field in ("submission_id", "native_job_id", "request_sha256"):
        if recovered["remote"][field] != a[field]:
            _reject()
        if jobs["remote"][field] != jobs["before_loss"][field]:
            _reject()
    if (
        jobs["task"] != jobs["before_loss"]["task"]
        or jobs["claim_sha256"] != jobs["before_loss"]["claim_sha256"]
    ):
        _reject()
    task_ids = [
        item["task_id"]
        for item in (a["queued_task"], a["running_task"], core["task"], jobs["task"])
    ]
    task_pks = [
        item["task_pk"]
        for item in (a["queued_task"], a["running_task"], core["task"], jobs["task"])
    ]
    if len(set(task_ids)) != 4 or len(set(task_pks)) != 4:
        _reject()
    if (
        a["queued_task"]["generation"] != 0
        or a["running_task"]["generation"] < 1
        or recovered["remote"] not in drained["remote_jobs"]
        or activated["ray_session"] == a["ray_session"]
        or core["task"]["generation"] < 1
        or jobs["task"]["generation"] < 1
        or set(rollback["candidate_write_task_ids"]) != set(rollback["absent_task_ids"])
        or not {core["task"]["task_id"], jobs["task"]["task_id"]}
        <= set(rollback["absent_task_ids"])
    ):
        _reject()
    for item in (recovered, jobs):
        old, new = item["original_manager"], item["replacement_manager"]
        if (
            old["pod_uid"] == new["pod_uid"]
            or all(
                old[field] == new[field] for field in ("worker_id", "hostname", "pid", "started_at")
            )
            or _time(new["started_at"]) <= _time(old["started_at"])
        ):
            _reject()
    for manager in (core["manager"], jobs["original_manager"], jobs["replacement_manager"]):
        if _time(manager["started_at"]) <= _time(value.phases[6].finished_at):
            _reject()
    if not _time(value.phases[4].finished_at) <= _time(backup["backup_created_at"]) <= _time(
        backup["restored_at"]
    ) <= _time(value.phases[5].finished_at) or not _time(
        jobs["remote"]["completion_observed_at"]
    ) <= _time(jobs["cleanup_created_at"]) <= _time(jobs["remote"]["inspection_began_at"]) <= _time(
        jobs["remote"]["terminal_observed_at"]
    ) <= _time(jobs["cleanup_closed_at"]) <= _time(value.phases[9].finished_at):
        _reject()
    # Retained observations cannot come from the future relative to their phase.
    for phase in value.phases:
        for observation in phase.observations.values():
            if type(observation) is dict and "started_at" in observation:
                if _time(observation["started_at"]) > _time(phase.finished_at):
                    _reject()
        remotes = phase.observations.get("remote_jobs", [])
        if "remote" in phase.observations:
            remotes = [phase.observations["remote"]]
        if any(_time(item["terminal_observed_at"]) > _time(phase.finished_at) for item in remotes):
            _reject()
    return value


def encode_runtime_receipt(value: RuntimeUpgradeReceipt) -> str:
    validate_runtime_receipt(value)
    return _canonical(
        {
            "schema": 1,
            "run": asdict(value.run),
            "phases": [_phase_value(phase) for phase in value.phases],
            "complete_upgrade_gate": False,
        }
    )


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _reject()
        result[key] = value
    return result


def decode_runtime_receipt(serialized: str) -> RuntimeUpgradeReceipt:
    """Reject unknown keys, coercion, noncanonical JSON and synthetic evidence."""
    if type(serialized) is not str or len(serialized) > MAX_RECEIPT_BYTES:
        _reject()
    try:
        raw = json.loads(serialized, object_pairs_hook=_object)
        _tree(raw)
        _shape(raw, {"schema", "run", "phases", "complete_upgrade_gate"})
        if type(raw["schema"]) is not int or raw["schema"] != 1 or type(raw["phases"]) is not list:
            _reject()
        run = dict(raw["run"])
        run["baseline"] = RuntimeBuildIdentity(**run["baseline"])
        run["candidate"] = RuntimeBuildIdentity(**run["candidate"])
        phases = []
        for item in raw["phases"]:
            item = dict(item)
            item["observer"] = RuntimeObserver(**item["observer"])
            item["phase"] = RuntimePhase(item["phase"])
            phases.append(RuntimePhaseReceipt(**item))
        value = RuntimeUpgradeReceipt(
            RuntimeUpgradeRun(**run), tuple(phases), raw["complete_upgrade_gate"]
        )
        if encode_runtime_receipt(value) != serialized:
            _reject()
        return value
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError, AttributeError):
        _reject()
