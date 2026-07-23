"""Subprocess and policy tests for the worker-loaded taxonomy selector."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from scripts import pytest_taxonomy

ROOT = Path(__file__).parents[2]


def _taxonomy_manifest() -> dict[str, object]:
    allow = {"mode": "allow", "reason": "Miniature selection allows declared skips."}
    group = {
        "id": "all-tests",
        "owner": "Miniature suite",
        "contract": "Owns the miniature suite.",
        "skip_policy": allow,
        "selection": {"paths": ["tests"]},
    }
    return {
        "schema_version": 3,
        "execution_contracts": [
            {
                "id": "hermetic",
                "owner": "Miniature pure tests",
                "contract": "Excludes database fixtures and external owners.",
                "skip_policy": allow,
                "execution": {
                    "mode": "xdist",
                    "workers": 2,
                    "distribution": "worksteal",
                    "max_worker_restart": 0,
                },
                "selection": {
                    "paths": ["tests"],
                    "exclude_markers": [
                        "django_db",
                        "live_cluster",
                        "postgresql",
                        "real_ray",
                    ],
                    "exclude_fixtures": [
                        "admin_client",
                        "admin_user",
                        "db",
                        "django_db_reset_sequences",
                        "django_db_serialized_rollback",
                        "live_server",
                        "transactional_db",
                    ],
                },
            },
            {
                "id": "sqlite",
                "owner": "Miniature SQLite tests",
                "contract": "Owns fixture-only database cases.",
                "skip_policy": allow,
                "selection": {
                    "paths": ["tests"],
                    "include_any_fixtures": ["db"],
                    "exclude_markers": ["real_ray"],
                },
            },
            {
                "id": "local-ray",
                "owner": "Miniature Ray tests",
                "contract": "Owns marked external cases.",
                "skip_policy": allow,
                "selection": {
                    "paths": ["tests"],
                    "include_markers": ["real_ray"],
                },
            },
        ],
        "domains": [dict(group, id="domain")],
        "boundaries": [dict(group, id="boundary")],
        "profiles": [dict(group, id="profile")],
        "ci_lanes": [dict(group, id="ci", variants=1)],
        "overlap_candidates": [
            {
                "id": "sample-overlap",
                "owner": "Miniature overlap",
                "paths": ["tests/test_sample.py"],
                "reason": "Exercises manifest parsing.",
                "review": "Retain the distinct resource cases.",
            }
        ],
    }


def _mini_repository(tmp_path: Path) -> Path:
    files = {
        ".github/test-suite-taxonomy.json": json.dumps(_taxonomy_manifest(), indent=2),
        "pyproject.toml": (
            "[tool.pytest.ini_options]\n"
            'testpaths = ["tests"]\n'
            'markers = ["real_ray: miniature external owner"]\n'
        ),
        "tests/conftest.py": (
            'pytest_plugins = ("scripts.pytest_taxonomy",)\n\n'
            "import pytest\n\n"
            "@pytest.fixture\n"
            "def db():\n"
            "    return object()\n"
        ),
        "tests/test_sample.py": (
            "from pathlib import Path\n\n"
            "import pytest\n\n"
            "def test_plain():\n"
            "    assert True\n\n"
            "def test_fixture_only_sqlite(db):\n"
            '    Path("sqlite-owner-ran").write_text("unexpected", encoding="utf-8")\n'
            "    assert db is not None\n\n"
            "@pytest.mark.real_ray\n"
            "def test_external_owner():\n"
            '    Path("external-owner-ran").write_text("unexpected", encoding="utf-8")\n'
        ),
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("pytest_taxonomy.py", "test_suite_inventory.py", "test_suite_taxonomy.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    return tmp_path


def _run_pytest(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    for variable in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        environment.pop(variable, None)
    environment.pop("DJANGO_RAY_RUN_COMPILED_SESSION_TOPOLOGY_PROBE", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *arguments],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _xdist_arguments() -> tuple[str, ...]:
    return (
        "tests",
        "--taxonomy-lane=hermetic",
        "--taxonomy-execution=xdist",
        "-n",
        "2",
        "--dist",
        "worksteal",
        "--max-worker-restart",
        "0",
        "-q",
    )


def test_selector_hook_runs_after_the_external_resource_guard() -> None:
    assert pytest_taxonomy.pytest_collection_modifyitems.pytest_impl["trylast"] is True


def test_plugin_is_inert_without_an_explicit_lane(tmp_path: Path) -> None:
    root = _mini_repository(tmp_path)

    result = _run_pytest(root, "tests", "--taxonomy-manifest=missing.json", "-q")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout


def test_xdist_workers_apply_fixture_aware_hermetic_selection(tmp_path: Path) -> None:
    root = _mini_repository(tmp_path)

    result = _run_pytest(root, *_xdist_arguments())

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "3 passed" not in result.stdout
    assert not (root / "sqlite-owner-ran").exists()
    assert not (root / "external-owner-ran").exists()


def test_xdist_worker_collection_drift_fails_closed(tmp_path: Path) -> None:
    root = _mini_repository(tmp_path)
    (root / "tests/test_worker_drift.py").write_text(
        "import os\n\n"
        'if os.environ.get("PYTEST_XDIST_WORKER") == "gw0":\n'
        "    def test_only_on_first_worker():\n"
        "        assert True\n",
        encoding="utf-8",
    )

    result = _run_pytest(root, *_xdist_arguments())

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "Different tests were collected" in output or "different selected node IDs" in output


def test_xdist_excluded_worker_collection_drift_fails_closed(tmp_path: Path) -> None:
    root = _mini_repository(tmp_path)
    (root / "tests/test_excluded_worker_drift.py").write_text(
        "import os\n\n"
        "import pytest\n\n"
        'if os.environ.get("PYTEST_XDIST_WORKER") == "gw0":\n'
        "    @pytest.mark.real_ray\n"
        "    def test_excluded_only_on_first_worker():\n"
        "        assert True\n",
        encoding="utf-8",
    )

    result = _run_pytest(root, *_xdist_arguments())

    assert result.returncode != 0
    assert "full pre-selection" in result.stdout + result.stderr


def test_default_serial_remainder_preserves_only_intentional_skips() -> None:
    result = _run_pytest(
        ROOT,
        "tests",
        "--taxonomy-lane=default-serial-remainder",
        "--taxonomy-execution=serial",
        "-q",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = re.search(r"(\d+) skipped", result.stdout)
    assert summary is not None
    assert int(summary.group(1)) > 1
    assert " passed" not in result.stdout


def test_inventory_cli_rejects_passthrough_lane_override_before_execution(
    tmp_path: Path,
) -> None:
    root = _mini_repository(tmp_path)
    timing_output = tmp_path.parent / f"{tmp_path.name}-untrusted-timing.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/test_suite_inventory.py",
            "run",
            "--lane",
            "hermetic",
            "--execution",
            "xdist",
            "--observation",
            "untrusted-override",
            "--variant",
            "two-workers",
            "--timing-output",
            str(timing_output),
            "--external-note",
            "Intentional passthrough override rejection fixture.",
            "--",
            "--taxonomy-lane",
            "local-ray",
        ],
        cwd=root,
        env={
            **os.environ,
            "PYTHONPATH": str(root),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 2
    assert "can change taxonomy selection" in result.stderr
    assert not timing_output.exists()
    assert not (root / "sqlite-owner-ran").exists()
    assert not (root / "external-owner-ran").exists()
