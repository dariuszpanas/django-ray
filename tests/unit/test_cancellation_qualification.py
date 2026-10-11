"""Resource-free contracts for real cancellation witnesses and failure handling."""

import contextlib
import json
import os
import runpy
import signal
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from qualification.cancellation import contract, scenario
from qualification.cancellation import process_witness as witness
from qualification.cancellation.probe import stop_manager


def passing():
    receipt = contract.empty_receipt()
    receipt["status"] = "passed"
    receipt["facts"] = dict.fromkeys(contract.FACTS, True)
    return receipt


@pytest.mark.parametrize("damage", ["missing", "false", "integer", "extra", "failed", "cleanup"])
def test_partial_or_ambiguous_receipts_fail_closed(damage):
    receipt = passing()
    contract.validate_receipt(receipt)
    if damage == "missing":
        receipt["facts"].pop("original_driver_exited")
    elif damage == "false":
        receipt["facts"]["original_driver_exited"] = False
    elif damage == "integer":
        receipt["facts"]["original_driver_exited"] = 1
    elif damage == "extra":
        receipt["facts"]["universal_effects_stopped"] = True
    elif damage == "failed":
        receipt["failure_phase"] = "cancel_observe"
    else:
        receipt["cleanup_failed"] = True
    with pytest.raises(ValueError):
        contract.validate_receipt(receipt)


def test_cleanup_preserves_first_failure_and_finite_diagnostics():
    receipt = contract.empty_receipt()
    contract.record_failure(receipt, "cancel_observe")
    contract.record_failure(receipt, "cleanup", cleanup=True)
    assert receipt["failure_phase"] == "cancel_observe"
    assert receipt["cleanup_failed"] is True
    receipt = contract.empty_receipt()
    contract.record_failure(receipt, "arbitrary-sensitive-text")
    assert receipt["failure_phase"] == "bootstrap"
    assert "arbitrary-sensitive-text" not in json.dumps(receipt)


def proc_record(root, pid, *, start=20, uid=10001, state="S"):
    directory = root / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stat").write_text(
        f"{pid} (python with spaces) "
        + " ".join([state, *("0" for _ in range(18)), str(start)])
        + "\n"
    )
    (directory / "status").write_text("Name:\tpython\nUid:\t" + "\t".join([str(uid)] * 4) + "\n")


@pytest.fixture
def owned_proc(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc_record(proc, 10, start=10)
    proc_record(proc, 20)
    monkeypatch.setattr(witness.os, "readlink", lambda _: "pid:[123]")
    path = tmp_path / "started.json"
    value = {
        "case": "cancel",
        "nonce": "a" * 32,
        "execution": [1, "task", 1, 1],
        "process": asdict(witness.ProcessIdentity(20, 20, 10001, "pid:[123]")),
    }
    path.write_text(json.dumps(value))
    return proc, path, value


def bind(owned):
    proc, path, _ = owned
    return witness.bind_driver(
        path, nonce="a" * 32, case="cancel", execution=[1, "task", 1, 1], proc=proc, observer_pid=10
    )


def test_actual_process_identity_is_required_before_exit_observation(owned_proc):
    proc, _, _ = owned_proc
    identity = bind(owned_proc)
    assert not witness.driver_exited(identity, proc=proc)
    proc_record(proc, 20, state="T")
    assert not witness.driver_exited(identity, proc=proc)
    proc_record(proc, 20, state="Z")
    assert witness.driver_exited(identity, proc=proc)
    proc_record(proc, 20, start=21)
    assert witness.driver_exited(identity, proc=proc)


@pytest.mark.parametrize(
    "damage", ["nonce", "execution", "bool_counter", "pid", "start", "uid", "namespace", "zombie"]
)
def test_forged_or_foreign_start_marker_is_rejected(owned_proc, damage):
    proc, path, value = owned_proc
    if damage == "nonce":
        value["nonce"] = "b" * 32
    elif damage == "execution":
        value["execution"][3] = 2
    elif damage == "bool_counter":
        value["execution"][0] = True
    elif damage == "pid":
        value["process"]["pid"] = True
    elif damage == "start":
        proc_record(proc, 20, start=21)
    elif damage == "uid":
        proc_record(proc, 20, uid=10002)
        value["process"]["uid"] = 10002
    elif damage == "namespace":
        value["process"]["pid_namespace"] = "pid:[456]"
    else:
        proc_record(proc, 20, state="Z")
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        bind(owned_proc)


@pytest.mark.parametrize("error", [PermissionError, ValueError, OSError])
def test_failed_process_read_is_not_evidence_of_exit(monkeypatch, error):
    monkeypatch.setattr(witness, "read_process", Mock(side_effect=error))
    with pytest.raises(error):
        witness.driver_exited(witness.ProcessIdentity(20, 20, 10001, "pid:[123]"))


def test_missing_original_process_is_exit_evidence(monkeypatch):
    monkeypatch.setattr(witness, "read_process", Mock(side_effect=FileNotFoundError))
    assert witness.driver_exited(witness.ProcessIdentity(20, 20, 10001, "pid:[123]"))


def test_start_marker_is_published_complete_and_never_overwritten(tmp_path, monkeypatch):
    identity = witness.ProcessIdentity(20, 20, 10001, "pid:[123]")
    monkeypatch.setattr(witness, "read_process", lambda _: (identity, "R"))
    path = tmp_path / "started.json"
    link = witness.os.link
    completed = []

    def publish(source, destination):
        completed.append(json.loads(source.read_text()))
        link(source, destination)

    monkeypatch.setattr(witness.os, "link", publish)
    arguments = {"nonce": "a" * 32, "case": "cancel", "execution": [1, "task", 1, 1]}
    witness.write_start(path, **arguments)
    assert json.loads(path.read_text()) == completed[0]
    assert not path.with_suffix(".pending").exists()
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        witness.write_start(path, **{**arguments, "nonce": "b" * 32})
    assert path.read_bytes() == before
    assert not path.with_suffix(".pending").exists()


def test_real_settings_overlay_passes_production_validation(tmp_path, monkeypatch, settings):
    from django_ray.conf.settings import get_settings, validate_ray_job_request_storage_settings

    monkeypatch.setenv("DJANGO_RAY_CANCELLATION_CONFIG", json.dumps({"root": str(tmp_path)}))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "installed"))
    root = Path(__file__).resolve().parents[2]
    overlay = runpy.run_path(str(root / "qualification/cancellation/settings.py"))
    settings.DJANGO_RAY = overlay["DJANGO_RAY"]
    config = get_settings()
    validate_ray_job_request_storage_settings(config)
    assert config["RUNNER"] == "ray_job"
    assert config["WORKER_LEASE_SECONDS"] == 60
    assert config["WORKER_HEARTBEAT_SECONDS"] == 2
    assert overlay["TASKS"]["cancellation"]["OPTIONS"]["RAY_JOB_ONLY"] is True


def test_real_task_import_uses_the_only_owned_backend_without_resources(tmp_path):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from django.conf import settings; "
            # The decorator's real TASKS mapping is unchanged. Avoid requiring
            # an optional PostgreSQL driver just to load model metadata.
            "settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', "
            "'NAME': ':memory:'}}; "
            "import django; django.setup(); "
            "from django.db import connections; "
            "from qualification.cancellation.tasks import controlled; "
            "assert set(settings.TASKS) == {'cancellation'}; "
            "assert controlled.backend == 'cancellation'; "
            "assert controlled.queue_name == 'cancellation'; "
            "assert all(c.connection is None for c in connections.all()); "
            "import sys; assert 'ray' not in sys.modules",
        ],
        cwd=root,
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "qualification.cancellation.settings",
            "DJANGO_RAY_CANCELLATION_CONFIG": json.dumps({"root": str(tmp_path)}),
            "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root))),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("data", [b"x" * (witness.MAX_BYTES + 1), b"invalid process stat"])
def test_oversized_or_malformed_process_data_cannot_pass(owned_proc, data):
    proc, _, _ = owned_proc
    (proc / "20/stat").write_bytes(data)
    with pytest.raises(ValueError):
        bind(owned_proc)


def test_manager_cleanup_is_bounded_and_forced_exit_cannot_pass():
    process = Mock()
    process.poll.return_value = None
    process.wait.return_value = 128 + signal.SIGTERM
    stop_manager(process)
    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5)
    process.kill.assert_not_called()
    process.wait.side_effect = [subprocess.TimeoutExpired("owned", 5), -9]
    process.wait.reset_mock()
    with pytest.raises(ValueError):
        stop_manager(process)
    assert [call.kwargs["timeout"] for call in process.wait.call_args_list] == [5, 2]
    process.kill.assert_called_once_with()


@pytest.mark.parametrize("untrusted_receipt", [False, True])
def test_probe_failure_survives_postgres_cleanup_failure(
    tmp_path, monkeypatch, capsys, untrusted_receipt
):
    candidate = SimpleNamespace(
        installed_package_tree_sha256="fixed", as_manifest=lambda: {"verified": True}
    )
    for name in ("_require_non_root", "_require_linux_process_groups", "_install_wheel"):
        monkeypatch.setattr(scenario.wheel, name, lambda *args: None)
    monkeypatch.setattr(scenario.wheel, "_select_wheel", lambda _: tmp_path / "candidate.whl")
    monkeypatch.setattr(scenario.wheel, "_inspect_candidate", lambda *args: candidate)
    monkeypatch.setattr(scenario.wheel, "_package_tree_digest", lambda _: "fixed")

    @contextlib.contextmanager
    def database(_root):
        yield
        raise RuntimeError("sensitive cleanup diagnostic")

    def failed_probe(command, **kwargs):
        assert kwargs["timeout"] == 180
        receipt = contract.empty_receipt()
        if untrusted_receipt:
            receipt["sensitive identity"] = "sensitive diagnostic"
        Path(command[-1]).write_text(json.dumps(receipt))
        return SimpleNamespace(returncode=1, stdout=b"sensitive output", stderr=b"sensitive error")

    monkeypatch.setattr(scenario, "_postgres", database)
    monkeypatch.setattr(scenario.wheel, "_run_bounded_command", failed_probe)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    assert (
        scenario.execute(
            source_root=tmp_path, evidence_root=evidence, target=tmp_path / "install/target"
        )
        == 1
    )
    manifest = json.loads((evidence / "execution-manifest.json").read_text())
    assert manifest["failure"] == "probe_failed"
    assert manifest["cleanup_failed"] and manifest["fixture_removed"]
    assert not manifest["server_stopped"]
    assert "sensitive" not in capsys.readouterr().out
    assert "sensitive" not in json.dumps(manifest)
    assert manifest["observations"] == (None if untrusted_receipt else contract.empty_receipt())


def test_hosted_fixture_has_explicit_owned_resource_and_deadline_contracts():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.load(
        (root / ".github/workflows/cancellation-qualification.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"workflow_call", "workflow_dispatch"}
    assert set(workflow["permissions"].values()) == {"read"}
    job = workflow["jobs"]["cancellation"]
    assert "environment" not in job
    steps = job["steps"]
    assert steps[1]["run"] == "python scripts/wait_for_ci_checkpoint.py"
    execution = next(
        step
        for step in steps
        if step.get("name") == "Observe no-cancel completion and actual cancelled driver exit"
    )
    assert "--kill-after=20s 280s" in execution["run"]
    cleanup = next(step for step in steps if step.get("name") == "Remove owned resources")
    assert cleanup["if"] == "always()"
    service = yaml.safe_load((root / "qualification/cancellation/jobs.yaml").read_text())[
        "services"
    ]["cancellation"]
    assert service["network_mode"] == "none" and service["read_only"] is True
    assert service["cpus"] == 2 and service["mem_limit"] == service["memswap_limit"] == "8g"
    assert service["pids_limit"] == 1024 and service["cap_drop"] == ["ALL"]
    assert service["build"]["dockerfile"] == "qualification/transactions/Dockerfile"
    assert service["volumes"][0]["read_only"] is True
    assert (
        contract.WORK_SECONDS == 150
        and contract.CHILD_SECONDS == 180
        and contract.OUTER_SECONDS == 300
    )
