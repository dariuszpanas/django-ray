"""Exact hosted interpreter selection without Docker or Ray execution."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qualification.application import image_runtime as runtime

IMAGE = "rayproject/ray@sha256:" + "a" * 64


@pytest.mark.parametrize("patch", [0, 12, 14])
def test_stock_interpreter_process_is_fixed_bounded_and_network_disabled(monkeypatch, patch):
    def run(argv, **kwargs):
        assert argv[:3] == ["docker", "run", "--rm"]
        assert {
            "--network=none",
            "--read-only",
            "--cpus=0.25",
            "--memory=128m",
            "--pids-limit=32",
            "--cap-drop=ALL",
        } <= set(argv)
        assert argv[-3:] == [IMAGE, "-c", runtime.PROBE]
        assert "ray" not in runtime.PROBE and "django" not in runtime.PROBE
        assert kwargs == {"check": False, "capture_output": True, "timeout": 120}
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"implementation": "CPython", "version": [3, 12, patch]}).encode(),
        )

    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert runtime.image_python(IMAGE) == f"3.12.{patch}"


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"implementation": "PyPy", "version": [3, 12, 1]},
        {"implementation": "CPython", "version": [3, 11, 1]},
        {"implementation": "CPython", "version": [3, 12, True]},
        {"implementation": "CPython", "version": [3, 12, 1], "extra": 1},
    ],
)
def test_image_interpreter_requires_exact_supported_tuple(monkeypatch, value):
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=json.dumps(value).encode()),
    )
    with pytest.raises(ValueError):
        runtime.image_python(IMAGE)


def test_image_python_refuses_mutable_image_before_any_process(monkeypatch):
    monkeypatch.setattr(runtime.subprocess, "run", lambda *_a, **_k: pytest.fail("process"))
    with pytest.raises(ValueError):
        runtime.image_python("rayproject/ray:latest")


def test_profile_pin_requires_all_four_identical_stock_nodes(tmp_path):
    profile = tmp_path / "core.yaml"
    profile.write_text(f"  image: {IMAGE}\n" * 4)
    assert runtime.stock_image(profile) == IMAGE
    profile.write_text(f"  image: {IMAGE}\n" * 3 + "  image: rayproject/ray:latest\n")
    with pytest.raises(ValueError):
        runtime.stock_image(profile)


def test_matching_application_interpreter_is_checked_without_echoing_failure(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "image_python", lambda *_a: "3.12.14")
    assert runtime.main(["--image", IMAGE, "--expected", "3.12.14"]) == 0
    assert runtime.main(["--image", IMAGE, "--expected", "3.12.13"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "3.12.14",
        "Image interpreter qualification failed",
    ]


def test_hosted_build_uses_committed_profile_and_verifies_final_image():
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/application-qualification.yml"
    ).read_text()
    assert (
        '--profile "$RUNNER_TEMP/application-source/qualification/application/core.yaml"'
        in workflow
    )
    assert '--build-arg PYTHON_VERSION="$python_patch"' in workflow
    assert '--image "$image" --expected "$python_patch"' in workflow
    assert workflow.index("python_patch=$(python") < workflow.index("kind create cluster")
