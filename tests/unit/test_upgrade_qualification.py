"""The database stage must reject incomplete evidence and preserve its runtime boundary."""

from __future__ import annotations

import copy
import io
import json
import tarfile
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest
import yaml

from qualification.docker.scenario import QualificationError
from qualification.upgrade import contract, prepare, scenario


def receipt(backend="sqlite"):
    values = [
        {"tasks": 8, "fixture_kind": "synthetic-released-models"},
        {"blocked_tasks": 2, "active_leases": 1, "read_only": True},
        {"nonterminal_tasks": 0, "active_leases": 0, "settlement": "synthetic-only"},
        {
            "historical_sha256": "a" * 64,
            "tasks": 8,
            "inert_execution_refusals": 0,
            "input_and_result_artifacts_read": True,
        },
        {
            "historical_sha256": "a" * 64,
            "tasks": 8,
            "inert_execution_refusals": 8,
            "input_and_result_artifacts_read": True,
            "missing_and_corrupt_result_rejected": True,
        },
        {"current_enqueue": True, "candidate_only_rows": 1},
        {
            "historical_sha256": "a" * 64,
            "tasks": 8,
            "inert_execution_refusals": 0,
            "input_and_result_artifacts_read": True,
            "candidate_writes_absent_from_old_backup": True,
        },
    ]
    return {
        "backend": backend,
        "phases": [
            {
                "phase": phase,
                "pid": index + 1,
                "module": "/installed/django_ray/__init__.py",
                "version": contract.CANDIDATE_VERSION
                if phase.startswith("candidate-")
                else contract.BASELINE_VERSION,
                "status": "passed",
                "observations": values[index],
            }
            for index, phase in enumerate(contract.PHASES)
        ],
        "backup_sha256": "b" * 64,
        "artifacts_sha256": "c" * 64,
        "fixture_cleanup": True,
        "server_stopped": True,
        "complete_upgrade_gate": False,
        "missing_acceptance": list(contract.MISSING),
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-phase",
        "reordered",
        "same-process",
        "wrong-version",
        "failed-phase",
        "bad-backup",
        "cleanup",
        "server",
        "overclaim",
        "missing-gaps",
        "changed-history",
        "no-blockers",
        "executable-history",
        "lost-new-write",
        "ignored-missing-artifact",
        "boolean-row-count",
    ],
)
def test_incomplete_upgrade_cannot_pass(mutation):
    value = receipt()
    if mutation == "missing-phase":
        value["phases"].pop()
    elif mutation == "reordered":
        value["phases"].reverse()
    elif mutation == "same-process":
        value["phases"][1]["pid"] = value["phases"][0]["pid"]
    elif mutation == "wrong-version":
        value["phases"][4]["version"] = "0.4.0"
    elif mutation == "failed-phase":
        value["phases"][2]["status"] = "skipped"
    elif mutation == "bad-backup":
        value["backup_sha256"] = "x" * 64
    elif mutation == "cleanup":
        value["fixture_cleanup"] = False
    elif mutation == "server":
        value["server_stopped"] = False
    elif mutation == "overclaim":
        value["complete_upgrade_gate"] = True
    elif mutation == "missing-gaps":
        value["missing_acceptance"].clear()
    elif mutation == "changed-history":
        value["phases"][4]["observations"]["historical_sha256"] = "d" * 64
    elif mutation == "no-blockers":
        value["phases"][1]["observations"]["blocked_tasks"] = 0
    elif mutation == "executable-history":
        value["phases"][4]["observations"]["inert_execution_refusals"] = 0
    elif mutation == "lost-new-write":
        value["phases"][5]["observations"]["candidate_only_rows"] = 0
    elif mutation == "ignored-missing-artifact":
        value["phases"][4]["observations"]["missing_and_corrupt_result_rejected"] = False
    else:
        value["phases"][5]["observations"]["candidate_only_rows"] = True
    with pytest.raises(QualificationError):
        contract.validate_backend(value, backend="sqlite")


def test_complete_stage_emits_fourteen_phases_but_no_complete_upgrade_claim():
    values = [receipt(backend) for backend in contract.BACKENDS]
    before = copy.deepcopy(values)
    suite = ElementTree.fromstring(contract.junit(values, failure=None))
    assert len(suite) == 14 and suite.attrib["failures"] == "0"
    assert values == before and all(value["complete_upgrade_gate"] is False for value in values)
    with pytest.raises(QualificationError, match="missing-upgrade-backend"):
        contract.junit(values[:1], failure=None)
    failed = ElementTree.fromstring(contract.junit([], failure="fixture-cleanup-failed"))
    assert failed.attrib["failures"] == "1"


@pytest.mark.parametrize("kind", ["wrong-commit", "escape", "symlink", "oversized"])
def test_baseline_archive_rejects_wrong_identity_and_unsafe_entries(tmp_path, kind):
    path = tmp_path / "source.tar"
    with tarfile.open(
        path,
        "w",
        format=tarfile.PAX_FORMAT,
        pax_headers={
            "comment": "0" * 40 if kind == "wrong-commit" else contract.BASELINE_COMMIT,
        },
    ) as archive:
        entry = tarfile.TarInfo("../outside" if kind == "escape" else "pyproject.toml")
        entry.size = 0
        if kind == "symlink":
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/outside"
        archive.addfile(entry, io.BytesIO())
    if kind == "oversized":
        with path.open("r+b") as stream:
            stream.truncate(128 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        prepare.verify_archive(path)


def test_restore_cannot_target_original_database(tmp_path, monkeypatch):
    command = Mock()
    monkeypatch.setattr(scenario, "_run", command)
    with pytest.raises(QualificationError, match="restore-destination"):
        scenario._restore(tmp_path, "postgresql", "baseline")
    command.assert_not_called()


def test_postgres_restore_uses_only_owned_socket_and_fixed_database(tmp_path, monkeypatch):
    command = Mock()
    monkeypatch.setattr(scenario, "_run", command)
    scenario._restore(tmp_path, "postgresql", "rollback")
    args = command.call_args.args[0]
    assert args[2] == tmp_path / "socket"
    assert args[6] == "rollback"
    assert "--exit-on-error" in args and args[-1] == tmp_path / "backup"


def test_compose_has_no_runtime_network_and_requires_explicit_source_and_evidence():
    root = Path(__file__).resolve().parents[2]
    document = yaml.safe_load((root / "qualification/upgrade/data.yaml").read_text())
    service = document["services"]["data"]
    assert service["network_mode"] == "none"
    assert service["cpus"] == 1.0 and service["mem_limit"] == service["memswap_limit"] == "1g"
    assert service["pids_limit"] == 128 and service["read_only"] is True
    assert service["user"] == "10001:10001"
    assert "ports" not in service and "depends_on" not in service
    assert service["build"]["additional_contexts"]["tools"] == "service:tools"
    assert "UPGRADE_BASELINE_SOURCE:?" in service["build"]["additional_contexts"]["released-source"]
    assert any("UPGRADE_EVIDENCE_DIR:?" in volume["source"] for volume in service["volumes"])


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), ValueError("invalid")])
def test_archive_failure_retains_failed_manifest_without_rehashing_missing_file(monkeypatch, error):
    output = {}
    monkeypatch.setattr(scenario.wheel, "_ensure_evidence_root", lambda _path: None)
    monkeypatch.setattr(scenario.wheel, "_require_non_root", lambda: None)
    monkeypatch.setattr(scenario.wheel, "_require_linux_process_groups", lambda: None)
    monkeypatch.setattr(scenario, "verify_archive", Mock(side_effect=error))
    digest = Mock(side_effect=AssertionError("failed archive must not be hashed"))
    monkeypatch.setattr(scenario.wheel, "_sha256", digest)
    monkeypatch.setattr(
        scenario.wheel, "_write_manifest", lambda path, value: output.setdefault(path.name, value)
    )

    assert scenario.execute() == 1

    digest.assert_not_called()
    manifest = json.loads(output["execution-manifest.json"])
    assert manifest["outcome"] == "failed" and manifest["failure"] == type(error).__name__
    assert manifest["baseline_archive_sha256"] is None
    assert manifest["candidate_source_files_sha256"] is None
    assert manifest["complete_upgrade_gate"] is False
    assert ElementTree.fromstring(output["junit.xml"]).attrib["failures"] == "1"
