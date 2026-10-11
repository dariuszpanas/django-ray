"""Protect the limits of the no-Ray Python 3.15 compatibility evidence."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from scripts import check_python315_compatibility as compatibility


def test_requirements_keep_every_runtime_dependency_except_ray() -> None:
    requirements = compatibility.compatibility_requirements()
    project = compatibility._project()
    expected = [
        dependency
        for dependency in project["project"]["dependencies"]
        if compatibility._requirement_name(dependency) != "ray"
    ]
    assert requirements[: len(expected)] == expected
    assert {compatibility._requirement_name(value) for value in requirements[len(expected) :]} == {
        "pytest",
        "pytest-django",
    }


def test_preloaded_ray_stub_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ray", ModuleType("ray"))
    with pytest.raises(RuntimeError, match="Ray or a Ray stub was already imported"):
        compatibility._require_no_ray()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import pytest\n@pytest.mark.skip(reason='unsupported')\ndef test_case(): pass\n",
            "skip/xfail is forbidden",
        ),
        (
            "import pytest\n@pytest.mark.xfail(reason='unsupported')\ndef test_case(): assert False\n",
            "skip/xfail is forbidden",
        ),
        (
            "import pytest\n@pytest.mark.xfail(reason='unsupported')\ndef test_case(): pass\n",
            "skip/xfail is forbidden",
        ),
        (
            "import pytest\npytest.skip('unsupported', allow_module_level=True)\n",
            "Compatibility collection must not skip",
        ),
        (
            "def test_case():\n    try:\n        import ray\n    except AssertionError:\n        pass\n",
            "Ray imports were attempted",
        ),
        (
            "def test_case(db): pass\n",
            "Compatibility selection must be resource-free",
        ),
    ],
)
def test_incomplete_or_runtime_dependent_evidence_cannot_pass(
    tmp_path: Path, source: str, expected: str
) -> None:
    selected = tmp_path / "test_selected.py"
    selected.write_text(source, encoding="utf-8")
    passing = tmp_path / "test_passing.py"
    passing.write_text("def test_passing(): pass\n", encoding="utf-8")
    code = """
import sys
from scripts import check_python315_compatibility as gate
gate.SMOKE_TESTS = tuple(sys.argv[1:])
# This subprocess tests the reporting/import guard in the ordinary developer
# environment; installed-wheel/interpreter absence checks have separate guards.
gate._require_no_ray = lambda: None
blocker = gate._ForbidRay()
sys.meta_path.insert(0, blocker)
raise SystemExit(gate._run_tests(smoke=True, blocker=blocker))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(compatibility.ROOT)
    result = subprocess.run(
        [sys.executable, "-c", code, str(selected), str(passing)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert expected in result.stdout + result.stderr
