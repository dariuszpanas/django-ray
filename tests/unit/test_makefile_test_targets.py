from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
MAKEFILE = ROOT / "Makefile"


def _target_body(makefile: str, target: str) -> str:
    body = makefile.split(f"{target}:\n", maxsplit=1)[1]
    return body.split("\n# ", maxsplit=1)[0]


def test_xdist_target_parallelizes_only_ordinary_local_tests() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    target = _target_body(makefile, "test-xdist")
    phony_targets = next(
        line for line in makefile.splitlines() if line.startswith(".PHONY:")
    ).split()

    assert "TEST_XDIST_WORKERS ?= 4" in makefile
    assert "test-xdist" in phony_targets
    assert target.strip("\n").splitlines() == [
        "\tpytest -n $(TEST_XDIST_WORKERS) --max-worker-restart=0 \\",
        '\t\t-m "not real_ray and not live_cluster and not postgresql"',
    ]
    assert "--dist" not in target


def test_ci_target_remains_serial_and_independent_of_local_xdist() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    target = _target_body(makefile, "ci")

    assert "test-xdist" not in target
    assert "pytest -n" not in target
    assert "--execution xdist" not in target
