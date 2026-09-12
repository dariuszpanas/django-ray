"""Resource-free failure contracts for the real-Job qualification workload."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml

from qualification.docker import scenario as wheel
from qualification.latency import contract, scenario


def receipt(module):
    cases = []
    task_pk = 0
    for name in contract.CASES:
        count = 3 if name == "capacity-one" else 1
        tasks = []
        for _ in range(count):
            task_pk += 1
            start = task_pk * 100_000_000_000
            elapsed = 12 if name == "recovery-only" else 0.25
            tasks.append(
                {
                    "task_pk": task_pk,
                    "job_id": f"raysubmit_django_ray_rq2_{task_pk:064x}",
                    "attempt": 1,
                    "generation": 1,
                    "module": module,
                    "state": "FAILED" if name == "failure" else "SUCCEEDED",
                    "job_started_ns": start,
                    "released_ns": start + 1_000_000_000,
                    "committed_ns": start + 2_000_000_000,
                    "terminal_ns": start + 2_000_000_000 + int(elapsed * 1e9),
                    "receipt_to_terminal_seconds": elapsed,
                    "release_to_terminal_seconds": elapsed + 1,
                    "database_times": {
                        "created_ns": task_pk * 1_000_000_000,
                        "claimed_ns": task_pk * 1_000_000_000 + 100,
                        "finished_ns": task_pk * 1_000_000_000 + 1000,
                    },
                }
            )
        managers = []
        for index in range(2 if name == "manager-replacement" else 1):
            completed = (
                0
                if name == "recovery-only" or (name == "manager-replacement" and index == 0)
                else count
            )
            managers.append(
                {
                    "counters": {
                        "queries": 100,
                        "query_seconds": 0.1,
                        "fast_polls": 10,
                        "fast_queries": 20,
                        "fast_completed": completed,
                        "reconciliations": 1,
                        "peak_active": 1,
                    },
                    "recovery_only": name == "recovery-only",
                    "module": module,
                    "elapsed_seconds": 20,
                    "shutdown_exit_code": 143,
                }
            )
        requests = [
            {"method": "POST", "path": "/api/jobs/", "status": 200, "at_ns": 100}
            for _ in range(count)
        ]
        if name == "api-outage":
            requests.append({"method": "GET", "path": "/api/version", "status": 503, "at_ns": 200})
        cases.append(
            {
                "name": name,
                "tasks": tasks,
                "managers": managers,
                "initial_reconciliation_ns": 1,
                "api_requests": requests,
                "passed": True,
                "capacity_claim_delays_seconds": [0.9999991] * (count - 1),
            }
        )
    return {
        "schema_version": 1,
        "module": module,
        "cases": cases,
        "failure": None,
        "ray_shutdown": True,
        "managers_stopped": True,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-case",
        "repeated-case",
        "partial-tasks",
        "wrong-module",
        "repeated-job",
        "repeated-pk",
        "old-carrier",
        "retry",
        "wrong-outcome",
        "nan-timing",
        "wrong-timing",
        "slow-receipt",
        "capacity-exceeded",
        "manager-left",
        "ray-left",
        "bad-exit",
        "missing-outage",
        "resubmission",
        "missing-queries",
        "partial-manager",
        "slow-claim",
    ],
)
def test_partial_or_mismatched_proof_cannot_emit_success(mutation):
    value = receipt("/installed/module")
    first = value["cases"][0]
    fast = value["cases"][1]
    task = fast["tasks"][0]
    if mutation == "missing-case":
        value["cases"].pop()
    elif mutation == "repeated-case":
        value["cases"][1] = first
    elif mutation == "partial-tasks":
        fast["tasks"].pop()
    elif mutation == "wrong-module":
        task["module"] = "/editable/module"
    elif mutation == "repeated-job":
        task["job_id"] = first["tasks"][0]["job_id"]
    elif mutation == "repeated-pk":
        task["task_pk"] = first["tasks"][0]["task_pk"]
    elif mutation == "old-carrier":
        task["job_id"] = "raysubmit_legacy"
    elif mutation == "retry":
        task["attempt"] = 2
    elif mutation == "wrong-outcome":
        task["state"] = "LOST"
    elif mutation == "nan-timing":
        task["receipt_to_terminal_seconds"] = float("nan")
    elif mutation == "wrong-timing":
        task["receipt_to_terminal_seconds"] = 0.01
    elif mutation == "slow-receipt":
        task["terminal_ns"] += 10_000_000_000
        task["receipt_to_terminal_seconds"] += 10
        task["release_to_terminal_seconds"] += 10
    elif mutation == "capacity-exceeded":
        fast["managers"][0]["counters"]["peak_active"] = 2
    elif mutation == "manager-left":
        value["managers_stopped"] = False
    elif mutation == "ray-left":
        value["ray_shutdown"] = False
    elif mutation == "bad-exit":
        fast["managers"][0]["shutdown_exit_code"] = 1
    elif mutation == "missing-outage":
        value["cases"][3]["api_requests"].pop()
    elif mutation == "resubmission":
        value["cases"][4]["api_requests"].append(first["api_requests"][0])
    elif mutation == "missing-queries":
        fast["managers"][0]["counters"]["queries"] = 0
    elif mutation == "partial-manager":
        value["cases"][4]["managers"].pop()
    else:
        fast["tasks"][1]["database_times"]["claimed_ns"] += 6_000_000_000
        fast["capacity_claim_delays_seconds"][0] += 6
    with pytest.raises(wheel.QualificationError):
        contract.junit(value, expected_module="/installed/module", failure=None)


def test_complete_proof_emits_only_the_five_fixed_cases():
    document = ElementTree.fromstring(
        contract.junit(
            receipt("/installed"),
            expected_module="/installed",
            failure=None,
        )
    )
    assert document.attrib["failures"] == "0"
    assert [case.attrib["name"] for case in document] == list(contract.CASES)


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "attempt/target"
    module = str((target / "django_ray/__init__.py").resolve())
    candidate = wheel.Candidate("target", "a" * 64, module, "a" * 64, "0.5.0", "x.whl", "b" * 64)
    monkeypatch.setattr(wheel, "_require_non_root", lambda: None)
    monkeypatch.setattr(wheel, "_require_linux_process_groups", lambda: None)
    monkeypatch.setattr(wheel, "_select_wheel", lambda path: path / "x.whl")
    monkeypatch.setattr(wheel, "_install_wheel", lambda *args: None)
    monkeypatch.setattr(wheel, "_inspect_candidate", lambda *args: candidate)
    monkeypatch.setattr(wheel, "_dependency_manifest", lambda candidate: {})
    monkeypatch.setattr(wheel, "_target_manifest", lambda *args: {"python": "3.12"})
    monkeypatch.setattr(wheel, "_package_tree_digest", lambda path: "a" * 64)
    state = {"failure": None, "fixtures": []}

    def run(command, *, cwd, env, timeout):
        fixture = Path(command[-3])
        state["fixtures"].append(fixture)
        (fixture / "db.sqlite3").write_bytes(b"owned database")
        assert command[1:4] == ("-P", "-m", "qualification.latency.probe")
        assert timeout == 300 and cwd == source
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(target)
        if state["failure"] == "timeout":
            raise wheel.BoundedProcessError("command-timeout", b"progress", b"failure")
        value = receipt(module)
        if state["failure"] == "incomplete":
            value["cases"].pop()
        payload = json.dumps(value).encode()
        if state["failure"] == "oversized":
            payload = b" " * (contract.MAX_PROBE_BYTES + 1)
        Path(command[-1]).write_bytes(payload)
        return wheel.BoundedProcessResult(1 if state["failure"] == "exit" else 0, b"progress", b"")

    monkeypatch.setattr(wheel, "_run_bounded_command", run)
    return {"source_root": source, "evidence_root": tmp_path / "evidence", "target": target}, state


@pytest.mark.parametrize(
    "failure", [None, "timeout", "incomplete", "oversized", "exit", "tree-drift"]
)
def test_scenario_reports_failure_and_removes_only_owned_fixtures(invocation, monkeypatch, failure):
    arguments, state = invocation
    state["failure"] = failure
    if failure == "tree-drift":
        monkeypatch.setattr(wheel, "_package_tree_digest", lambda path: "c" * 64)
    assert scenario.execute(**arguments) == (0 if failure is None else 1)
    manifest = json.loads((arguments["evidence_root"] / "execution-manifest.json").read_bytes())
    assert manifest["outcome"] == ("passed" if failure is None else "failed")
    assert manifest["fixture_cleanup"] is True
    assert state["fixtures"] and all(not path.exists() for path in state["fixtures"])
    assert arguments["target"].is_dir()
    junit = ElementTree.fromstring((arguments["evidence_root"] / "junit.xml").read_bytes())
    assert junit.attrib["failures"] == ("0" if failure is None else "1")


def test_existing_evidence_is_never_overwritten(invocation):
    arguments, state = invocation
    arguments["evidence_root"].mkdir()
    existing = arguments["evidence_root"] / "junit.xml"
    existing.write_bytes(b"original evidence")
    with pytest.raises(wheel.QualificationError, match="evidence-root-not-empty"):
        scenario.execute(**arguments)
    assert existing.read_bytes() == b"original evidence" and not state["fixtures"]


def test_fixed_definition_and_optimized_interpreter_refusal(tmp_path):
    root = Path(__file__).resolve().parents[2]
    definition = yaml.safe_load((root / contract.DEFINITION_PATH).read_text())["definition"]
    assert definition["executor"]["payload"]["argv"] == [
        "python",
        "-m",
        "qualification.latency.scenario",
    ]
    assert definition["timeout_seconds"] == 420
    assert definition["cleanup"] == {"policy": "always", "timeout_seconds": 180}
    import django_ray

    module = Path(django_ray.__file__).resolve()
    environment = dict(
        os.environ, PYTHONPATH=os.pathsep.join((str(module.parent.parent), str(root)))
    )
    result = subprocess.run(
        [
            sys.executable,
            "-O",
            "-P",
            "-m",
            "qualification.latency.probe",
            str(tmp_path),
            str(module),
            str(tmp_path / "receipt.json"),
        ],
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0 and b"requires assertions enabled" in result.stderr
    assert not any(tmp_path.iterdir())


def test_fresh_django_fixture_enqueues_and_observes_real_completion_writes(tmp_path):
    """Exercise the current Sync boundary and real receipt writer without Ray.

    This observes the SQL writer only; native Jobs qualification must also prove
    the independently owned transport and manager receipt consumption.
    """
    import django_ray

    root = Path(__file__).resolve().parents[2]
    module = Path(django_ray.__file__).resolve()
    pythonpath = os.pathsep.join((str(module.parent.parent), str(root)))
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "address": "http://127.0.0.1:9",
                "pythonpath": pythonpath,
            }
        )
    )
    (tmp_path / "inputs").mkdir()
    environment = dict(
        os.environ,
        PYTHONPATH=pythonpath,
        DJANGO_SETTINGS_MODULE="qualification.latency.settings",
        DJANGO_RAY_LATENCY_ROOT=str(tmp_path),
    )
    code = """
import json
import django
django.setup()
from django.conf import settings
from django.core.management import call_command
from django.db import connections
from django_ray.conf.settings import get_settings
from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskState
from django_ray.runner.cohort_dispatch import (
    prepare_claimed_cohort_dispatch, mark_cohort_dispatch_started,
)
from django_ray.runtime.cohort_execution import execute_cohort_request
from django_ray.runtime.entrypoint import _persist_task_completion
from django_ray.target.cohort_transport import decode_cohort_execution_result
settings.TASKS["default"]["OPTIONS"]["RAY_JOB_ONLY"] = False
from qualification.latency.tasks import held_result
call_command("migrate", verbosity=0)
command = Command()
command.execution_mode = "sync"
command._validate_execution_mode_configuration(get_settings())
command._create_lease("default")
command._initialize_cohort_execution(("default",))
for fail in (False, True):
    queued = held_result.enqueue(fail=fail)
    row = RayTaskExecution.objects.get(task_id=queued.id)
    assert row.state == TaskState.QUEUED and row.ray_job_id is None
    claimed, = command._cohort_controller.claim(limit=1)
    assert claimed.execution.pk == row.pk
    prepared = prepare_claimed_cohort_dispatch(claimed)
    dispatched = mark_cohort_dispatch_started(prepared)
    identity = dispatched.claim.facts.identity
    assert identity.attempt_number == identity.execution_generation == 1
    (settings.ROOT / f"release-{row.pk}").touch()
    expected = dict(
        expected_identity=identity,
        expected_request_digest=dispatched.prepared.request_digest,
        expected_cohort_contract_digest=dispatched.prepared.contract_digest,
    )
    encoded = execute_cohort_request(dispatched.prepared.request_json, **expected)
    envelope = decode_cohort_execution_result(encoded, **expected)
    assert envelope.refusal is None
    result = json.loads(envelope.completion_json)
    assert result["success"] is (not fail), result
    assert result["execution_protocol_version"] == 3
    _persist_task_completion(row.pk, 1, 1, encoded)
    row.refresh_from_db()
    assert row.completion_data == encoded
    observed = json.loads((settings.ROOT / f"completion-{row.pk}.json").read_text())
    assert observed["committed_ns"] > 0
    started = json.loads((settings.ROOT / f"started-{row.pk}.json").read_text())
    assert started["started_ns"] <= observed["committed_ns"], (started, observed)
connections.close_all()
print("two real completion writes observed")
"""
    result = subprocess.run(
        [sys.executable, "-P", "-c", code],
        env=environment,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"two real completion writes observed" in result.stdout
