"""Resource-free tests for the source-owned result-fold qualification."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from qualification.docker import scenario

_PYTEST_SUCCESS_JUNIT = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" tests="1" failures="0" errors="0" skipped="0"
    time="12.345" timestamp="volatile" hostname="volatile-host">
    <testcase classname="tests.unit.test_result_fold"
      name="test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup" time="12.345" />
  </testsuite>
</testsuites>
"""


def test_runbook_selects_the_exact_real_ray_node_and_bounded_profile() -> None:
    root = Path(__file__).resolve().parents[2]
    document = yaml.safe_load(
        (root / "qualification/docker/runbook.yaml").read_text(encoding="utf-8")
    )

    assert document["namespace"] == "django-ray"
    assert document["schema_version"] == 1
    assert document["definition"]["executor"]["payload"]["argv"] == [
        "python",
        "-m",
        "qualification.docker.scenario",
    ]
    assert document["definition"]["requirements"]["capabilities"] == [
        "docker",
        "linux",
        "python",
        "ray",
    ]
    assert document["definition"]["timeout_seconds"] == 420
    assert document["definition"]["cleanup"] == {
        "policy": "always",
        "timeout_seconds": 180,
    }
    assert document["definition"]["evidence"] == {
        "max_artifacts": 3,
        "max_bytes_per_artifact": 1024 * 1024,
        "max_total_bytes": 3 * 1024 * 1024,
        "required_kinds": ["junit", "log", "manifest"],
    }
    assert scenario._TEST_NODE == (  # noqa: SLF001 - fixed source contract
        "tests/unit/test_result_fold.py::"
        "test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup"
    )


def test_kubernetes_runbook_selects_the_external_evidence_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    document = yaml.safe_load(
        (root / "qualification/kubernetes/runbook.yaml").read_text(encoding="utf-8")
    )

    assert document["namespace"] == "django-ray"
    assert document["name"] == "result-fold-real-ray-kubernetes-qualification"
    assert document["schema_version"] == 1
    assert document["definition"]["executor"]["payload"]["argv"] == [
        "python",
        "-m",
        "qualification.docker.scenario",
        "--definition-path",
        "qualification/kubernetes/runbook.yaml",
    ]
    assert document["definition"]["requirements"]["capabilities"] == [
        "external-evidence-v1",
        "kubernetes",
        "linux",
        "python",
        "ray",
    ]
    assert document["definition"]["timeout_seconds"] == 420
    assert document["definition"]["cleanup"] == {
        "policy": "always",
        "timeout_seconds": 180,
    }
    assert document["definition"]["evidence"] == {
        "max_artifacts": 3,
        "max_bytes_per_artifact": 1024 * 1024,
        "max_total_bytes": 3 * 1024 * 1024,
        "required_kinds": ["junit", "log", "manifest"],
    }


def test_definition_path_is_limited_to_tracked_runbooks() -> None:
    parser = scenario._parser()  # noqa: SLF001 - exact command contract

    assert parser.parse_args([]).definition_path == "qualification/docker/runbook.yaml"
    assert (
        parser.parse_args(
            ["--definition-path", "qualification/kubernetes/runbook.yaml"]
        ).definition_path
        == "qualification/kubernetes/runbook.yaml"
    )
    with pytest.raises(SystemExit):
        parser.parse_args(["--definition-path", "../operator-selected.yaml"])
    with pytest.raises(scenario.QualificationError, match="invalid-definition-path"):
        scenario._validated_definition_path("qualification/other.yaml")  # noqa: SLF001


def test_image_keeps_dependencies_separate_and_preserves_absolute_venv_path() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "qualification/docker/Dockerfile").read_text(encoding="utf-8")
    readme = (root / "qualification/docker/README.md").read_text(encoding="utf-8")

    assert "UV_PROJECT_ENVIRONMENT=/opt/qualification/.venv" in dockerfile
    assert "uv sync --frozen --no-install-project" in dockerfile
    assert dockerfile.count("uv sync") == 1
    assert "uv build --wheel --out-dir /opt/django-ray-wheels" in dockerfile
    assert "rm -f /opt/django-ray-wheels/.gitignore" in dockerfile
    assert dockerfile.count("find /opt/django-ray-wheels -mindepth 1 -maxdepth 1") == 2
    assert "-type f -name '*.whl'" in dockerfile
    assert "COPY --from=builder /opt/qualification/.venv /opt/qualification/.venv" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "DJANGO_RAY_QUALIFICATION_HOLD_SECONDS=10" in dockerfile
    assert "git archive <full-commit>" in readme
    assert "core.autocrlf=true" in readme
    assert "not\nfrom an ordinary working-tree directory" in readme
    assert "^(?:[0-9a-f]{40}|[0-9a-f]{64})$" in readme
    assert readme.count("if ($LASTEXITCODE -ne 0)") == 5
    assert "[IO.Path]::GetDirectoryName($Path) -ine $ResolvedTemporaryRoot" in readme
    assert '$DockerContext = "desktop-linux"' in readme
    assert '$SourceTag = "django-ray-qualification:candidate-$QualificationRun"' in readme
    assert "docker --context $DockerContext build" in readme
    assert "--tag $SourceTag --iidfile $IidFile $Context" in readme
    assert "^sha256:[0-9a-f]{64}$" in readme
    assert "if ($InspectedImageId -cne $ImageId)" in readme
    assert "Use exact control-plane --image $ImageId" in readme
    assert "do not synthesize a repository\ndigest from the unique source tag" in readme
    assert "512 MiB writable `/tmp` and 1 GiB `/dev/shm`" in readme


def test_wheel_install_is_exact_offline_and_does_not_forward_index_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / "wheels" / "django_ray-0.5.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel")
    target = tmp_path / "temporary-target"
    target.mkdir()
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return scenario.BoundedProcessResult(returncode=0, stderr=b"", stdout=b"")

    monkeypatch.setenv("PIP_INDEX_URL", "https://credential.invalid/simple")
    monkeypatch.setenv("UV_INDEX_URL", "https://credential.invalid/simple")
    monkeypatch.setattr(scenario, "_run_bounded_command", fake_run)

    scenario._install_wheel(wheel, target, Path("/usr/local/bin/uv"))  # noqa: SLF001

    assert observed["command"] == (
        str(Path("/usr/local/bin/uv")),
        "pip",
        "install",
        "--quiet",
        "--offline",
        "--no-index",
        "--no-deps",
        "--python",
        scenario.sys.executable,
        "--target",
        str(target),
        str(wheel),
    )
    environment = observed["env"]
    assert isinstance(environment, dict)
    assert "PIP_INDEX_URL" not in environment
    assert "UV_INDEX_URL" not in environment
    assert environment["UV_PYTHON_DOWNLOADS"] == "never"
    assert environment["UV_NO_CACHE"] == "1"


def test_bounded_command_stops_on_output_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maximum = 128
    monkeypatch.setattr(scenario, "_MAX_COMMAND_OUTPUT_BYTES", maximum)
    started = time.monotonic()

    with pytest.raises(scenario.BoundedProcessError, match="command-output-limit") as captured:
        scenario._run_bounded_command(  # noqa: SLF001 - qualification process boundary
            (
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 65536); sys.stdout.flush()",
            ),
            cwd=tmp_path,
            env=scenario._subprocess_environment(),  # noqa: SLF001
            timeout=5,
        )

    assert time.monotonic() - started < 3
    assert captured.value.code == "command-output-limit"
    assert len(captured.value.stdout) <= maximum + 1
    assert len(captured.value.stderr) <= maximum + 1


def test_bounded_command_stops_on_timeout(tmp_path: Path) -> None:
    started = time.monotonic()

    with pytest.raises(scenario.BoundedProcessError, match="command-timeout") as captured:
        scenario._run_bounded_command(  # noqa: SLF001 - qualification process boundary
            (sys.executable, "-c", "import time; time.sleep(10)"),
            cwd=tmp_path,
            env=scenario._subprocess_environment(),  # noqa: SLF001
            timeout=0.1,
        )

    assert time.monotonic() - started < 3
    assert captured.value.code == "command-timeout"
    assert captured.value.stdout == b""
    assert captured.value.stderr == b""


def test_bounded_command_returns_exact_streams_and_exit_code(tmp_path: Path) -> None:
    result = scenario._run_bounded_command(  # noqa: SLF001 - qualification process boundary
        (
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'out\\n'); "
            "sys.stderr.buffer.write(b'err\\n'); raise SystemExit(7)",
        ),
        cwd=tmp_path,
        env=scenario._subprocess_environment(),  # noqa: SLF001
        timeout=5,
    )

    assert result.returncode == 7
    assert result.stdout == b"out\n"
    assert result.stderr == b"err\n"


def test_bounded_command_cleans_up_when_a_reader_cannot_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_start = scenario.threading.Thread.start
    starts = 0

    def fail_second_start(thread: scenario.threading.Thread) -> None:
        nonlocal starts
        starts += 1
        if starts == 2:
            raise RuntimeError("reader unavailable")
        original_start(thread)

    monkeypatch.setattr(scenario.threading.Thread, "start", fail_second_start)
    started = time.monotonic()

    with pytest.raises(
        scenario.BoundedProcessError,
        match="command-reader-start-failed",
    ):
        scenario._run_bounded_command(  # noqa: SLF001 - process cleanup boundary
            (sys.executable, "-c", "import time; time.sleep(10)"),
            cwd=tmp_path,
            env=scenario._subprocess_environment(),  # noqa: SLF001
            timeout=5,
        )

    assert time.monotonic() - started < 3


def test_stream_read_failure_is_not_treated_as_clean_eof() -> None:
    class RaisingStream:
        def read(self, maximum: int) -> bytes:
            del maximum
            raise OSError("capture failed")

    signals = scenario._CaptureSignals(  # noqa: SLF001 - capture failure contract
        changed=scenario.threading.Event(),
        closing=scenario.threading.Event(),
        overflow=scenario.threading.Event(),
        read_failed=scenario.threading.Event(),
    )

    scenario._drain_stream(  # type: ignore[arg-type]  # noqa: SLF001
        RaisingStream(),
        bytearray(),
        signals,
        128,
    )

    assert signals.read_failed.is_set()
    assert signals.changed.is_set()


@pytest.mark.skipif(os.name != "posix", reason="process groups are the Linux target boundary")
def test_bounded_command_timeout_terminates_its_process_group(tmp_path: Path) -> None:
    grandchild_pid = tmp_path / "grandchild.pid"
    parent = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "print('ready', flush=True); time.sleep(30)"
    )

    with pytest.raises(scenario.BoundedProcessError, match="command-timeout"):
        scenario._run_bounded_command(  # noqa: SLF001 - Linux process-group boundary
            (sys.executable, "-c", parent, str(grandchild_pid)),
            cwd=tmp_path,
            env=scenario._subprocess_environment(),  # noqa: SLF001
            timeout=1,
        )

    pid = int(grandchild_pid.read_text(encoding="ascii"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        status = Path(f"/proc/{pid}/stat")
        if status.is_file() and status.read_text(encoding="ascii").split()[2] == "Z":
            break
        time.sleep(0.02)
    else:
        pytest.fail("qualification grandchild remained alive after process-group timeout")


def test_pytest_environment_puts_installed_target_before_source(tmp_path: Path) -> None:
    target = tmp_path / "target"
    source = tmp_path / "source"

    environment = scenario._subprocess_environment(  # noqa: SLF001
        install_target=target,
        source_root=source,
    )

    assert environment["PYTHONPATH"].split(os.pathsep) == [str(target), str(source)]
    assert environment["PYTHONSAFEPATH"] == "1"


def test_package_tree_digest_is_ordered_and_ignores_bytecode(tmp_path: Path) -> None:
    source = tmp_path / "source"
    installed = tmp_path / "installed"
    for root in (source, installed):
        (root / "nested").mkdir(parents=True)
        (root / "nested" / "b.py").write_text("B\n", encoding="utf-8")
        (root / "a.py").write_text("A\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "a.pyc").write_bytes(b"source-bytecode")
    (installed / "nested" / "ignored.pyo").write_bytes(b"installed-bytecode")

    assert scenario._package_tree_digest(source) == scenario._package_tree_digest(  # noqa: SLF001
        installed
    )


def test_candidate_inspection_rejects_a_wheel_tree_that_differs_from_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source_package = source / "src" / "django_ray"
    source_package.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        '[project]\nname = "django-ray"\nversion = "0.5.0"\n',
        encoding="utf-8",
    )
    (source_package / "__init__.py").write_text("SOURCE = True\n", encoding="utf-8")
    target = tmp_path / "target"
    installed_package = target / "django_ray"
    installed_package.mkdir(parents=True)
    installed_module = installed_package / "__init__.py"
    installed_module.write_text("SOURCE = False\n", encoding="utf-8")
    wheel = tmp_path / "django_ray-0.5.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    module = ModuleType("django_ray")
    module.__file__ = str(installed_module)
    distribution = type("Distribution", (), {"version": "0.5.0"})()

    monkeypatch.setattr(scenario, "_target_distribution", lambda selected: distribution)
    monkeypatch.setattr(scenario, "_reject_preinstalled_django_ray", lambda: None)
    monkeypatch.setattr(scenario, "_import_candidate", lambda selected: module)

    with pytest.raises(scenario.QualificationError, match="candidate-source-tree-mismatch"):
        scenario._inspect_candidate(wheel, target, source)  # noqa: SLF001


def test_canonical_success_junit_removes_volatile_fields(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_bytes(_PYTEST_SUCCESS_JUNIT)

    scenario._canonicalize_success_junit(junit)  # noqa: SLF001

    assert junit.read_bytes() == scenario._CANONICAL_JUNIT  # noqa: SLF001
    assert b"time=" not in junit.read_bytes()
    assert b"hostname=" not in junit.read_bytes()


@pytest.mark.parametrize(
    "payload",
    [
        b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" tests="1" time="1" timestamp="x" hostname="x">
    <testcase classname="tests.unit.test_result_fold"
      name="test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup" time="1" />
  </testsuite>
</testsuites>
""",
        b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" tests="1" failures="0" errors="0" skipped="0"
    time="1" timestamp="x" hostname="x">
    <testcase classname="tests.unit.test_result_fold"
      name="test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup" time="1">
      <failure message="hidden failure" />
    </testcase>
  </testsuite>
</testsuites>
""",
        b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" tests="1" failures="0" errors="0" skipped="0"
    time="1" timestamp="x" hostname="x">
    <testsuite name="nested" tests="0" failures="0" errors="0" skipped="0" />
    <testcase classname="tests.unit.test_result_fold"
      name="test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup" time="1" />
  </testsuite>
</testsuites>
""",
        _PYTEST_SUCCESS_JUNIT.replace(b"tests.unit.test_result_fold", b"unexpected.class"),
        _PYTEST_SUCCESS_JUNIT.replace(
            b'<testsuites name="pytest tests">',
            b'<testsuites name="pytest tests" tests="1">',
        ),
        _PYTEST_SUCCESS_JUNIT.replace(b'tests="1"', b'tests="+1"'),
        b"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE testsuites [<!ENTITY suite "pytest tests">]>
<testsuites name="&suite;">
  <testsuite name="pytest" tests="1" failures="0" errors="0" skipped="0"
    time="1" timestamp="x" hostname="x">
    <testcase classname="tests.unit.test_result_fold"
      name="test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup" time="1" />
  </testsuite>
</testsuites>
""",
        """<?xml version="1.0" encoding="utf-16"?>
<!DOCTYPE testsuites [<!ENTITY suite "pytest tests">]>
<testsuites name="&suite;">
  <testsuite name="pytest" tests="1" failures="0" errors="0" skipped="0"
    time="1" timestamp="x" hostname="x">
    <testcase classname="tests.unit.test_result_fold"
      name="test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup" time="1" />
  </testsuite>
</testsuites>
""".encode("utf-16"),
    ],
    ids=(
        "missing-counters",
        "hidden-failure",
        "nested-suite",
        "wrong-classname",
        "root-counter",
        "noncanonical-counter",
        "doctype-entity",
        "utf16-doctype-entity",
    ),
)
def test_canonical_success_junit_rejects_unexpected_grammar(
    tmp_path: Path,
    payload: bytes,
) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_bytes(payload)

    with pytest.raises(scenario.QualificationError, match="unexpected-pytest-junit"):
        scenario._canonicalize_success_junit(junit)  # noqa: SLF001


def test_canonical_success_junit_rejects_oversized_file(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_bytes(b"x" * (scenario._MAX_JUNIT_BYTES + 1))  # noqa: SLF001

    with pytest.raises(scenario.QualificationError, match="junit-too-large"):
        scenario._canonicalize_success_junit(junit)  # noqa: SLF001


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO required")
def test_canonical_success_junit_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    mkfifo = getattr(os, "mkfifo", None)
    assert mkfifo is not None
    mkfifo(junit)
    code = """
import sys
from pathlib import Path
from qualification.docker import scenario

try:
    scenario._canonicalize_success_junit(Path(sys.argv[1]))
except scenario.QualificationError as error:
    if error.code != "missing-or-unsafe-junit":
        raise
else:
    raise AssertionError("FIFO was accepted as JUnit")
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(junit)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_manifest_is_canonical_bounded_and_contains_target_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = scenario.Candidate(
        distribution_location="/tmp/django-ray-qualification/target",
        installed_package_tree_sha256="a" * 64,
        module_location="/tmp/django-ray-qualification/target/django_ray/__init__.py",
        source_package_tree_sha256="a" * 64,
        version="0.5.0",
        wheel_filename="django_ray-0.5.0-py3-none-any.whl",
        wheel_sha256="b" * 64,
    )
    versions = {"django": "6.1", "pytest": "9.1.1", "ray": "2.56.0"}

    monkeypatch.setattr(scenario.metadata, "version", versions.__getitem__)
    monkeypatch.setattr(
        scenario,
        "_installed_distributions",
        lambda: [
            {"name": "django", "version": "6.1"},
            {"name": "django-ray", "version": "0.5.0"},
            {"name": "pytest", "version": "9.1.1"},
            {"name": "ray", "version": "2.56.0"},
        ],
    )

    first = scenario._manifest(  # noqa: SLF001
        candidate,
        definition_path="qualification/kubernetes/runbook.yaml",
        exit_code=0,
        outcome="passed",
    )
    second = scenario._manifest(  # noqa: SLF001
        candidate,
        definition_path="qualification/kubernetes/runbook.yaml",
        exit_code=0,
        outcome="passed",
    )
    payload = json.loads(first)

    assert first == second
    assert len(first) < 64 * 1024
    assert first.endswith(b"\n")
    assert payload["candidate"]["wheel_sha256"] == "b" * 64
    assert payload["candidate"]["installed_package_tree_sha256"] == "a" * 64
    assert payload["definition_path"] == "qualification/kubernetes/runbook.yaml"
    assert payload["dependencies"]["django_ray"] == "0.5.0"
    assert len(payload["dependencies"]["installed_distributions"]) == 4
    assert payload["target"] == {
        "dependencies": {
            "django": "6.1",
            "django-ray": "0.5.0",
            "pytest": "9.1.1",
            "ray": "2.56.0",
        },
        "python": scenario.platform.python_version(),
    }
    assert payload["test"]["node_id"] == scenario._TEST_NODE  # noqa: SLF001

    pre_candidate = json.loads(
        scenario._manifest(  # noqa: SLF001
            None,
            definition_path="qualification/docker/runbook.yaml",
            exit_code=None,
            outcome="failed",
        )
    )
    assert "target" not in pre_candidate


@pytest.mark.parametrize("value", ["", "has space", "snowman-☃", "v\n1"])
def test_target_versions_are_bounded_visible_ascii(value: str) -> None:
    with pytest.raises(scenario.QualificationError, match="invalid-distribution-version"):
        scenario._safe_version(value)  # noqa: SLF001


def test_success_emits_only_canonical_summary_and_holds_after_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = tmp_path / "evidence"
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    wheel = wheels / "django_ray-0.5.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    target = tmp_path / "install" / "target"
    candidate = scenario.Candidate(
        distribution_location=str(target),
        installed_package_tree_sha256="a" * 64,
        module_location=str(target / "django_ray/__init__.py"),
        source_package_tree_sha256="a" * 64,
        version="0.5.0",
        wheel_filename=wheel.name,
        wheel_sha256="b" * 64,
    )
    events: list[str] = []

    monkeypatch.setattr(scenario, "_install_wheel", lambda *args: events.append("installed"))
    monkeypatch.setattr(scenario, "_inspect_candidate", lambda *args: candidate)
    monkeypatch.setattr(
        scenario,
        "_dependency_manifest",
        lambda selected: {
            "django": "6.1",
            "django_ray": selected.version,
            "installed_distributions": [],
            "pytest": "9.1.1",
            "python": "3.12.0",
            "ray": "2.56.0",
        },
    )

    def fake_test(source_root: Path, install_target: Path, junit_path: Path) -> int:
        del source_root, install_target
        junit_path.write_bytes(_PYTEST_SUCCESS_JUNIT)
        events.append("tested")
        return 0

    def fake_sleep(seconds: float) -> None:
        assert (evidence / "execution-manifest.json").is_file()
        assert (evidence / "junit.xml").is_file()
        events.append(f"held:{seconds:g}")

    monkeypatch.setattr(scenario, "_run_exact_test", fake_test)
    monkeypatch.setattr(scenario.time, "sleep", fake_sleep)

    result = scenario.execute(
        definition_path="qualification/docker/runbook.yaml",
        evidence_root=evidence,
        wheel_directory=wheels,
        install_target=target,
        source_root=Path(__file__).resolve().parents[2],
        uv_executable=Path("/usr/local/bin/uv"),
        hold_seconds=10,
        require_non_root=False,
    )

    assert result == 0
    assert events == ["installed", "tested", "held:10"]
    captured = capsys.readouterr()
    assert "1 passed in" not in captured.out
    assert captured.err == ""
    assert json.loads(captured.out.splitlines()[1]) == {
        "outcome": "passed",
        "scenario": "result-fold-real-ray",
        "test_node": scenario._TEST_NODE,  # noqa: SLF001
    }
    assert json.loads((evidence / "execution-manifest.json").read_bytes())["outcome"] == ("passed")


def test_pre_candidate_failure_emits_bounded_junit_and_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = tmp_path / "evidence"
    wheels = tmp_path / "wheels"
    wheels.mkdir()

    result = scenario.execute(
        definition_path="qualification/kubernetes/runbook.yaml",
        evidence_root=evidence,
        wheel_directory=wheels,
        install_target=tmp_path / "install" / "target",
        source_root=Path(__file__).resolve().parents[2],
        uv_executable=Path("/usr/local/bin/uv"),
        hold_seconds=0,
        require_non_root=False,
    )

    assert result == 1
    junit = (evidence / "junit.xml").read_text(encoding="ascii")
    manifest = json.loads((evidence / "execution-manifest.json").read_bytes())
    assert 'message="expected-one-wheel"' in junit
    assert manifest["candidate"] is None
    assert manifest["definition_path"] == "qualification/kubernetes/runbook.yaml"
    assert manifest["outcome"] == "failed"
    assert capsys.readouterr().err.endswith("code=expected-one-wheel\n")


@pytest.mark.parametrize("code", ["pytest-timeout", "pytest-output-limit"])
def test_post_candidate_failure_retains_exact_target_provenance(
    code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "evidence"
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    wheel = wheels / "django_ray-0.5.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    target = tmp_path / "install" / "target"
    candidate = scenario.Candidate(
        distribution_location=str(target),
        installed_package_tree_sha256="a" * 64,
        module_location=str(target / "django_ray/__init__.py"),
        source_package_tree_sha256="a" * 64,
        version="0.5.0",
        wheel_filename=wheel.name,
        wheel_sha256="b" * 64,
    )
    dependencies = {
        "django": "6.1",
        "django_ray": candidate.version,
        "installed_distributions": [],
        "pytest": "9.1.1",
        "python": "3.12.0",
        "ray": "2.56.0",
    }

    monkeypatch.setattr(scenario, "_install_wheel", lambda *args: None)
    monkeypatch.setattr(scenario, "_inspect_candidate", lambda *args: candidate)
    monkeypatch.setattr(scenario, "_dependency_manifest", lambda selected: dependencies)

    def fail_test(*args) -> int:
        raise scenario.QualificationError(code)

    monkeypatch.setattr(scenario, "_run_exact_test", fail_test)

    result = scenario.execute(
        definition_path="qualification/docker/runbook.yaml",
        evidence_root=evidence,
        wheel_directory=wheels,
        install_target=target,
        source_root=Path(__file__).resolve().parents[2],
        uv_executable=Path("/usr/local/bin/uv"),
        hold_seconds=0,
        require_non_root=False,
    )

    manifest = json.loads((evidence / "execution-manifest.json").read_bytes())
    assert result == 1
    assert manifest["candidate"] == candidate.as_manifest()
    assert manifest["outcome"] == "failed"
    assert manifest["target"] == {
        "dependencies": {
            "django": "6.1",
            "django-ray": "0.5.0",
            "pytest": "9.1.1",
            "ray": "2.56.0",
        },
        "python": "3.12.0",
    }
    assert f'message="{code}"' in (evidence / "junit.xml").read_text(encoding="ascii")


def test_hold_defaults_to_zero_and_rejects_unbounded_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DJANGO_RAY_QUALIFICATION_HOLD_SECONDS", raising=False)
    assert scenario._hold_seconds() == 0  # noqa: SLF001

    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_HOLD_SECONDS", "10")
    assert scenario._hold_seconds() == 10  # noqa: SLF001

    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_HOLD_SECONDS", "31")
    with pytest.raises(SystemExit):
        scenario.main([])


def test_failure_output_is_capped(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(scenario, "_MAX_COMMAND_OUTPUT_BYTES", 4)

    scenario._emit_failure_output("pytest-stderr", b"abcdefgh")  # noqa: SLF001

    assert capsys.readouterr().err == (
        "qualification=result-fold-real-ray diagnostic=pytest-stderr\nabcd\n...[truncated]...\n"
    )
