"""A database rehearsal cannot claim the complete deployed upgrade contract."""

from __future__ import annotations

import json
from xml.etree import ElementTree

from qualification.docker.scenario import QualificationError

BASELINE_COMMIT = "95ee5dfe95b1c1bed95ff28c4fcb5fcdc491e485"
BASELINE_VERSION = "0.4.0"
CANDIDATE_VERSION = "0.5.0"
BACKENDS = ("sqlite", "postgresql")
PHASES = (
    "baseline-seed",
    "baseline-blocked",
    "candidate-blocked-activation",
    "baseline-settle-fixture",
    "restored-baseline-read",
    "candidate-migrate-read",
    "candidate-new-write",
    "backup-rollback-read",
)
MISSING = (
    "real-old-manager-drain-and-uncertain-work-reconciliation",
    "unsupported-old-execution-refusal-and-carrier-retirement",
    "current-ray-core-and-jobs-smoke-and-manager-crash-recovery",
    "rendered-workflow-history-and-encrypted-runtimeenv-recovery",
    "migration-specific-code-rollback-after-candidate-writes",
)
MAX_RECEIPT_BYTES = 128 * 1024


def validate_backend(value: object, *, backend: str) -> dict:
    """Reject absent phases, false restoration, changed history and overstated scope."""
    if not isinstance(value, dict) or set(value) != {
        "backend",
        "phases",
        "backup_sha256",
        "blocked_backup_sha256",
        "artifacts_sha256",
        "fixture_cleanup",
        "server_stopped",
        "complete_upgrade_gate",
        "missing_acceptance",
    }:
        raise QualificationError("invalid-upgrade-receipt")
    phases = value["phases"]
    if (
        value["backend"] != backend
        or value["complete_upgrade_gate"] is not False
        or value["missing_acceptance"] != list(MISSING)
        or value["fixture_cleanup"] is not True
        or value["server_stopped"] is not True
        or not isinstance(phases, list)
        or [phase.get("phase") for phase in phases if isinstance(phase, dict)] != list(PHASES)
    ):
        raise QualificationError("incomplete-upgrade-receipt")
    for field in ("backup_sha256", "blocked_backup_sha256", "artifacts_sha256"):
        digest = value[field]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise QualificationError("invalid-upgrade-digest")
    for phase in phases:
        if set(phase) != {"phase", "status", "pid", "module", "version", "observations"}:
            raise QualificationError("invalid-upgrade-phase")
        if phase.get("status") != "passed" or type(phase.get("pid")) is not int:
            raise QualificationError("failed-upgrade-phase")
        if phase["pid"] <= 0 or not isinstance(phase.get("module"), str):
            raise QualificationError("invalid-upgrade-process")
        expected_version = (
            CANDIDATE_VERSION if phase["phase"].startswith("candidate-") else BASELINE_VERSION
        )
        if phase["version"] != expected_version:
            raise QualificationError("wrong-upgrade-version")
    if len({phase["pid"] for phase in phases}) != len(PHASES):
        raise QualificationError("upgrade-phases-must-use-fresh-processes")
    fixed = {
        "baseline-seed": {"tasks": 8, "fixture_kind": "synthetic-released-models"},
        "baseline-blocked": {"blocked_tasks": 2, "active_leases": 1, "read_only": True},
        "candidate-blocked-activation": {
            "blocked_tasks": 2,
            "active_leases": 1,
            "activation_refused": True,
            "activation_recorded": False,
            "active_write_protocol_version": 1,
            "legacy_token_present": True,
            "original_fields_unchanged": True,
        },
        "baseline-settle-fixture": {
            "nonterminal_tasks": 0,
            "active_leases": 0,
            "settlement": "synthetic-only",
        },
        "candidate-new-write": {
            "current_enqueue": True,
            "candidate_only_rows": 1,
            "execution_protocol_version": 3,
            "persisted_intent_matches": True,
        },
    }
    historical_digests = []
    for phase in phases:
        observations = phase["observations"]
        if phase["phase"] in fixed:
            if json.dumps(observations, sort_keys=True) != json.dumps(
                fixed[phase["phase"]], sort_keys=True
            ):
                raise QualificationError("upgrade-phase-observation-mismatch")
        else:
            expected = {
                "historical_sha256": observations.get("historical_sha256")
                if isinstance(observations, dict)
                else None,
                "tasks": 8,
                "input_and_result_artifacts_read": True,
                "inert_execution_refusals": 8 if phase["phase"] == "candidate-migrate-read" else 0,
            }
            if phase["phase"] == "candidate-migrate-read":
                expected["missing_and_corrupt_result_rejected"] = True
            if phase["phase"] == "backup-rollback-read":
                expected["candidate_writes_absent_from_old_backup"] = True
            digest = expected["historical_sha256"]
            if (
                json.dumps(observations, sort_keys=True) != json.dumps(expected, sort_keys=True)
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise QualificationError("upgrade-history-observation-mismatch")
            historical_digests.append(digest)
    if len(set(historical_digests)) != 1:
        raise QualificationError("upgrade-historical-data-changed")
    return value


def junit(receipts: list[dict], *, failure: str | None) -> bytes:
    """Report the fixed database stage without manufacturing runtime passes."""
    if failure is None:
        if len(receipts) != len(BACKENDS):
            raise QualificationError("missing-upgrade-backend")
        for backend, value in zip(BACKENDS, receipts, strict=True):
            validate_backend(value, backend=backend)
    suite = ElementTree.Element(
        "testsuite",
        name="coordinated-beta-data-upgrade",
        tests="1" if failure else str(len(BACKENDS) * len(PHASES)),
        failures="1" if failure else "0",
        errors="0",
        skipped="0",
    )
    if failure:
        case = ElementTree.SubElement(suite, "testcase", name="contract")
        ElementTree.SubElement(case, "failure", message=failure)
    else:
        for backend in BACKENDS:
            for phase in PHASES:
                ElementTree.SubElement(suite, "testcase", classname=backend, name=phase)
    return ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n"
