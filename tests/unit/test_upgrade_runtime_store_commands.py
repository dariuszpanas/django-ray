"""Fixed store command wiring; native clients remain outside these checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from qualification.upgrade import runtime_manifest as renderer
from qualification.upgrade import runtime_steps as steps
from tests.unit.test_upgrade_runtime_manifest import config

RUN = "a" * 64
ARTIFACT = "b" * 64
DUMP = "c" * 64
COMPLETE = steps.RuntimeStoreArguments(
    RUN, "final", ARTIFACT, DUMP, "7450000000000000001", 16384, 16385
)


def arguments(action):
    return steps.RuntimeStoreArguments(
        **{name: getattr(COMPLETE, name) for name in steps.STORE_ACTIONS[action]}
    )


@pytest.fixture
def environment(tmp_path, monkeypatch):
    import django

    import django_ray

    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_ARTIFACT_ROOT", str(root))
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_BUILD", "baseline")
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_DATABASE", "primary")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "qualification.upgrade.runtime_settings")
    # This current-process wiring test does not qualify the released package.
    monkeypatch.setattr(django_ray, "__version__", "0.4.0")
    monkeypatch.setattr(
        django, "setup", lambda: pytest.fail("store commands must not set up Django")
    )
    return root


def test_real_artifact_commands_bind_copy_and_restore_without_database_setup(environment, capsys):
    assert steps.main(["prepare"]) == 0
    capsys.readouterr()
    assert steps.main(steps.store_cli_args("bind-store", arguments("bind-store"))) == 0
    capsys.readouterr()
    (environment / "inputs" / "payload.bin").write_bytes(b"original fixture input")
    assert steps.main(steps.store_cli_args("backup-artifacts", arguments("backup-artifacts"))) == 0
    backup = json.loads(capsys.readouterr().out)["observations"]
    (environment / "inputs" / "payload.bin").write_bytes(b"newer primary input")
    restore = replace(
        arguments("restore-artifacts"),
        point="rollback",
        artifacts_sha256=backup["artifacts_sha256"],
    )
    assert steps.main(steps.store_cli_args("restore-artifacts", restore)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "restore-artifacts"
    assert result["complete_upgrade_gate"] is False
    assert result["observations"]["backup_point"] == "final"
    assert (
        environment / ".upgrade-restores" / "rollback" / "inputs" / "payload.bin"
    ).read_bytes() == b"original fixture input"
    assert (environment / "inputs" / "payload.bin").read_bytes() == b"newer primary input"


@pytest.mark.parametrize("action", ["backup-database", "restore-database", "create-scratch"])
def test_database_commands_forward_only_fixed_observed_identifiers(
    environment, monkeypatch, capsys, action
):
    from qualification.upgrade import runtime_database, runtime_restore, runtime_scratch

    observed = []

    def helper(point, **kwargs):
        observed.append((point, kwargs))
        return {"complete_upgrade_gate": False}

    module = {
        "backup-database": runtime_database,
        "restore-database": runtime_restore,
        "create-scratch": runtime_scratch,
    }[action]
    monkeypatch.setattr(module, action.replace("-", "_"), helper)
    assert steps.main(steps.store_cli_args(action, arguments(action))) == 0
    assert json.loads(capsys.readouterr().out)["complete_upgrade_gate"] is False
    expected = {
        "run_digest": RUN,
        "expected_artifacts_sha256": ARTIFACT,
        "expected_system_identifier": COMPLETE.system_identifier,
        "expected_primary_database_oid": 16384,
    }
    if action == "restore-database":
        expected.update(expected_dump_sha256=DUMP, expected_scratch_database_oid=16385)
    if action == "create-scratch":
        del expected["expected_artifacts_sha256"]
    assert observed == [("final", expected)]


@pytest.mark.parametrize("action", list(steps.STORE_ACTIONS))
def test_rendered_store_observer_keeps_root_mount_and_exact_generated_argv(action):
    store = arguments(action)
    manifest = renderer.render_runtime_manifest(
        "observer",
        config(),
        build="baseline",
        database="primary",
        action=action,
        job_name="owned-store-observer",
        store_arguments=store,
    )
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["args"] == steps.store_cli_args(action, store)
    assert container["command"] == ["python", "-m", "qualification.upgrade.runtime_steps"]
    assert [mount for mount in container["volumeMounts"] if mount["name"] == "artifacts"] == [
        {"name": "artifacts", "mountPath": "/artifacts"}
    ]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["DJANGO_RAY_UPGRADE_DATABASE"] == "primary"
    assert env["DJANGO_SETTINGS_MODULE"] == "qualification.upgrade.runtime_settings"


@pytest.mark.parametrize(
    "change",
    [
        {"build": "candidate"},
        {"database": "scratch"},
        {"restore_point": "final"},
        {"case": "old-success"},
        {"store_arguments": None},
        {"store_arguments": {"run_digest": RUN}},
        {"store_arguments": replace(COMPLETE, scratch_database_oid=16384)},
    ],
)
def test_invalid_store_render_refuses_before_loading_template(monkeypatch, change):
    monkeypatch.setattr(
        renderer, "_templates", lambda: pytest.fail("loaded template before refusal")
    )
    options = {
        "build": "baseline",
        "database": "primary",
        "action": "restore-database",
        "job_name": "owned-observer",
        "store_arguments": COMPLETE,
    }
    options.update(change)
    with pytest.raises(renderer.RuntimeManifestError):
        renderer.render_runtime_manifest("observer", config(), **options)


@pytest.mark.parametrize(
    "action,store",
    [
        ("bind-store", COMPLETE),
        ("backup-artifacts", replace(arguments("backup-artifacts"), point="rollback")),
        ("restore-artifacts", replace(arguments("restore-artifacts"), point="../final")),
        ("restore-database", replace(COMPLETE, run_digest="invalid")),
        ("restore-database", replace(COMPLETE, system_identifier="01")),
        ("restore-database", replace(COMPLETE, system_identifier=str(2**64))),
        ("restore-database", replace(COMPLETE, primary_database_oid=True)),
        ("restore-database", replace(COMPLETE, scratch_database_oid=0)),
        ("restore-database", replace(COMPLETE, scratch_database_oid=2**32)),
        ("restore-database", replace(COMPLETE, dump_sha256=None)),
    ],
)
def test_store_argument_codec_refuses_incomplete_extra_or_ambiguous_values(action, store):
    with pytest.raises(steps.StepError):
        steps.store_cli_args(action, store)


@pytest.mark.parametrize(
    "change",
    [
        {"DJANGO_RAY_UPGRADE_BUILD": "candidate"},
        {"DJANGO_RAY_UPGRADE_DATABASE": "scratch"},
        {"DJANGO_SETTINGS_MODULE": "qualification.upgrade.runtime_history_settings"},
    ],
)
def test_wrong_store_epoch_refuses_before_filesystem_mutation(
    environment, monkeypatch, capsys, change
):
    for name, value in change.items():
        monkeypatch.setenv(name, value)
    assert steps.main(steps.store_cli_args("bind-store", arguments("bind-store"))) == 1
    assert json.loads(capsys.readouterr().out) == {
        "step_failed": True,
        "complete_upgrade_gate": False,
    }
    assert list(environment.iterdir()) == []


def test_ordinary_action_rejects_store_flags_before_preparation(environment, capsys):
    assert steps.main(["prepare", "--run-digest", RUN]) == 1
    assert json.loads(capsys.readouterr().out)["step_failed"] is True
    assert list(environment.iterdir()) == []


def test_store_commands_in_actual_released_interpreter(tmp_path):
    interpreter = os.environ.get("DJANGO_RAY_UPGRADE_BASELINE_PYTHON")
    if not interpreter:
        pytest.skip("requires an explicitly installed isolated django-ray0.4/Ray2.56 interpreter")
    assert Path(interpreter).is_absolute() and Path(interpreter).is_file()
    overlay = tmp_path / "qualifier"
    package = overlay / "qualification" / "upgrade"
    package.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "qualification" / "upgrade"
    for name in ("runtime_steps.py", "runtime_artifacts.py"):
        shutil.copyfile(source / name, package / name)
    root = tmp_path / "artifacts"
    root.mkdir()
    probe = r"""
import contextlib, importlib.metadata, io, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import django_ray
assert django_ray.__version__ == importlib.metadata.version('django-ray') == '0.4.0'
assert importlib.metadata.version('ray') == '2.56.0'
assert Path(django_ray.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
import django, ray
def forbidden(*args, **kwargs):
    raise AssertionError('store artifact command started Django or Ray')
django.setup = ray.init = ray.shutdown = forbidden
from qualification.upgrade import runtime_steps as steps
def call(argv):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert steps.main(argv) == 0
    return json.loads(output.getvalue())
call(['prepare'])
call(['bind-store', '--run-digest', 'a' * 64])
root = Path(os.environ['DJANGO_RAY_UPGRADE_ARTIFACT_ROOT'])
(root / 'inputs' / 'old.bin').write_bytes(b'released input')
backup = call(['backup-artifacts', '--point', 'final', '--run-digest', 'a' * 64])
(root / 'inputs' / 'old.bin').write_bytes(b'new primary input')
restored = call(['restore-artifacts', '--point', 'rollback', '--run-digest', 'a' * 64,
                '--artifacts-sha256', backup['observations']['artifacts_sha256']])
assert (root / '.upgrade-restores' / 'rollback' / 'inputs' / 'old.bin').read_bytes() == b'released input'
assert (root / 'inputs' / 'old.bin').read_bytes() == b'new primary input'
assert restored['complete_upgrade_gate'] is False
assert not ray.is_initialized()
print('released-artifact-command-path-passed')
"""
    completed = subprocess.run(
        [interpreter, "-I", "-c", probe, str(overlay)],
        cwd=tmp_path,
        env=os.environ
        | {
            "DJANGO_RAY_UPGRADE_ARTIFACT_ROOT": str(root),
            "DJANGO_RAY_UPGRADE_BUILD": "baseline",
            "DJANGO_RAY_UPGRADE_DATABASE": "primary",
            "DJANGO_SETTINGS_MODULE": "qualification.upgrade.runtime_settings",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "released-artifact-command-path-passed"
