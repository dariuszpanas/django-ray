"""Tests for the manifest-backed pytest suite inventory."""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

import scripts.test_suite_inventory as inventory_module
import scripts.test_suite_taxonomy as taxonomy_module
from scripts.test_suite_inventory import (
    CollectedTest,
    InventoryError,
    Selection,
    _source_digest,
    build_inventory,
    load_manifest,
    render_markdown,
    run_lane,
)

ROOT = Path(__file__).parents[2]
MANIFEST_PATH = ROOT / ".github" / "test-suite-taxonomy.json"


def _item(
    name: str,
    *markers: str,
    path: str = "tests/unit/test_example.py",
    fixtures: tuple[str, ...] = ("request",),
    parameter_keys: tuple[str, ...] = (),
) -> CollectedTest:
    return CollectedTest(
        nodeid=f"{path}::{name}",
        path=path,
        markers=tuple(sorted(markers)),
        fixtures=fixtures,
        parameter_keys=parameter_keys,
    )


def _mini_inventory_repository(tmp_path: Path) -> Path:
    allow_skips = {"mode": "allow", "reason": "Miniature profile exposes skips."}
    forbid_skips = {"mode": "forbid", "reason": "Miniature gate must execute all cases."}
    manifest = {
        "schema_version": 3,
        "execution_contracts": [
            {
                "id": "hermetic",
                "owner": "Miniature pure tests",
                "contract": "Does not request the database fixture.",
                "skip_policy": allow_skips,
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
                "id": "database",
                "owner": "Miniature database tests",
                "contract": "Requests the database fixture.",
                "skip_policy": allow_skips,
                "selection": {"paths": ["tests"], "include_any_fixtures": ["db"]},
            },
        ],
        "domains": [
            {
                "id": "sample-domain",
                "owner": "Miniature domain",
                "contract": "Owns all miniature cases.",
                "skip_policy": allow_skips,
                "selection": {"paths": ["tests"]},
            }
        ],
        "boundaries": [
            {
                "id": "sample-boundary",
                "owner": "Miniature product boundary",
                "contract": "Owns the sample module.",
                "skip_policy": allow_skips,
                "selection": {"paths": ["tests/test_sample.py"]},
            }
        ],
        "profiles": [
            {
                "id": "sample-profile",
                "owner": "Miniature local profile",
                "contract": "Runs the miniature collection.",
                "skip_policy": allow_skips,
                "selection": {"paths": ["tests"]},
            }
        ],
        "ci_lanes": [
            {
                "id": "portable",
                "owner": "Miniature blocking lane",
                "contract": "Runs every miniature case.",
                "skip_policy": forbid_skips,
                "variants": 1,
                "selection": {"paths": ["tests"]},
            }
        ],
        "overlap_candidates": [
            {
                "id": "sample-overlap",
                "owner": "Miniature overlap",
                "paths": ["tests/test_sample.py"],
                "reason": "Exercises overlap reporting.",
                "review": "Retain all cases in the fixture.",
            }
        ],
    }
    files = {
        ".gitignore": "artifacts/\n__pycache__/\n.pytest_cache/\n.coverage*\n*.pyc\n",
        ".github/test-suite-taxonomy.json": json.dumps(manifest, indent=2) + "\n",
        "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "tests/conftest.py": (
            'pytest_plugins = ("scripts.pytest_taxonomy",)\n\n'
            "import pytest\n\n"
            "@pytest.fixture\n"
            "def db():\n"
            "    return object()\n"
        ),
        "tests/test_sample.py": (
            "import pytest\n\n"
            '@pytest.mark.parametrize("value", [1, 2])\n'
            "def test_parameter(value):\n"
            "    assert value > 0\n\n"
            "def test_database(db):\n"
            "    assert db is not None\n"
        ),
        "testproject/static/sample.png": b"\x89PNG\r\n\x1a\n\x00fixture\r\n",
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    script = tmp_path / "scripts" / "test_suite_inventory.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "scripts" / "test_suite_inventory.py", script)
    shutil.copyfile(
        ROOT / "scripts" / "test_suite_taxonomy.py", script.with_name("test_suite_taxonomy.py")
    )
    shutil.copyfile(ROOT / "scripts" / "pytest_taxonomy.py", script.with_name("pytest_taxonomy.py"))
    tracked_paths = sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file()
    )
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "--", *tracked_paths], cwd=tmp_path, check=True)
    return tmp_path


def _inventory_cli(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    environment.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    for key in list(environment):
        if key.startswith(("COV_CORE_", "COVERAGE_")):
            environment.pop(key)
    return subprocess.run(
        [sys.executable, "scripts/test_suite_inventory.py", *arguments],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_checked_in_manifest_partitions_representative_execution_contracts() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    items = [
        _item("test_hermetic"),
        _item("test_sqlite", "django_db"),
        _item("test_sqlite_fixture", fixtures=("db", "request")),
        _item("test_ray", "django_db", "real_ray"),
        _item(
            "test_compiled_graph_opt_in",
            "compiled_graph_opt_in",
            "real_ray",
        ),
        _item("test_postgresql", "django_db", "postgresql"),
        _item(
            "test_live",
            "live_cluster",
            path="tests/integration/test_live_failure_injection.py",
        ),
    ]

    assignments = {
        item.nodeid: [
            contract.id
            for contract in manifest.execution_contracts
            if contract.selection.matches(item)
        ]
        for item in items
    }

    assert assignments == {
        items[0].nodeid: ["hermetic"],
        items[1].nodeid: ["sqlite-django"],
        items[2].nodeid: ["sqlite-django"],
        items[3].nodeid: ["local-ray"],
        items[4].nodeid: ["compiled-graph-opt-in"],
        items[5].nodeid: ["postgresql"],
        items[6].nodeid: ["live-cluster"],
    }
    assert manifest.group("local-ray").skip_policy.mode == "forbid"
    assert manifest.group("compiled-graph-opt-in").skip_policy.mode == "allow"
    assert manifest.group("hermetic").execution.as_mapping() == {
        "mode": "xdist",
        "workers": 2,
        "distribution": "worksteal",
        "max_worker_restart": 0,
    }
    assert all(
        contract.execution.mode == "serial"
        for contract in manifest.execution_contracts
        if contract.id != "hermetic"
    )
    assert manifest.group("supported-python").selection.pytest_arguments() == [
        "tests",
        "-m",
        "not live_cluster",
    ]


def test_inventory_reexports_the_extracted_taxonomy_contract() -> None:
    assert inventory_module.CollectedTest is taxonomy_module.CollectedTest
    assert inventory_module.InventoryError is taxonomy_module.InventoryError
    assert inventory_module.Selection is taxonomy_module.Selection
    assert inventory_module.load_manifest is taxonomy_module.load_manifest


def test_cli_select_and_collect_use_fixture_aware_taxonomy_and_atomic_outputs(
    tmp_path: Path,
) -> None:
    root = _mini_inventory_repository(tmp_path)

    selected = _inventory_cli(root, "select", "--lane", "hermetic", "--format", "json")

    assert selected.returncode == 0, selected.stderr
    selected_payload = json.loads(selected.stdout)
    assert selected_payload["lane"] == "hermetic"
    assert selected_payload["pytest_arguments"] is None
    assert "db" in selected_payload["selection"]["exclude_fixtures"]
    assert selected_payload["django_settings_modules"] == ["unset"]
    assert selected_payload["skip_policy"]["mode"] == "allow"
    assert selected_payload["owner"] == "Miniature pure tests"
    assert selected_payload["manifest_runner"][-2:] == ["--lane", "hermetic"]

    collected = _inventory_cli(
        root,
        "collect",
        "--json-output",
        "artifacts/inventory.json",
        "--markdown-output",
        "artifacts/inventory.md",
    )

    assert collected.returncode == 0, collected.stderr
    report = json.loads((root / "artifacts/inventory.json").read_text(encoding="utf-8"))
    groups = {group["id"]: group for group in report["groups"]}
    assert report["totals"]["collected"] == 3
    assert groups["hermetic"]["selected_count"] == 2
    assert groups["database"]["selected_count"] == 1
    assert "db" in {fixture["name"] for fixture in report["fixtures"]}
    assert "value" not in {fixture["name"] for fixture in report["fixtures"]}
    assert report["parameterized_families"][0]["case_count"] == 2
    assert not list((root / "artifacts").glob(".*.pending"))


def test_cli_run_records_completed_outcomes_and_merges_valid_timing(tmp_path: Path) -> None:
    root = _mini_inventory_repository(tmp_path)

    run = _inventory_cli(
        root,
        "run",
        "--lane",
        "portable",
        "--observation",
        "mini-local",
        "--variant",
        "locked",
        "--timing-output",
        "artifacts/timing.json",
        "--external-note",
        "Miniature environment was already prepared; external intervals excluded.",
        "--",
        "-q",
    )

    assert run.returncode == 0, run.stderr
    timing = json.loads((root / "artifacts/timing.json").read_text(encoding="utf-8"))
    assert timing["integrity"] == {"errors": [], "valid": True}
    assert timing["schema_version"] == 3
    assert timing["execution"] == {
        "mode": "serial",
        "workers": 0,
        "distribution": "no",
        "max_worker_restart": 0,
    }
    assert timing["collection"]["mode"] == "serial"
    assert timing["collection"]["valid"] is True
    assert timing["source_after_digest"] == timing["source"]["digest"]
    assert timing["pytest"]["selected_count"] == 3
    assert timing["pytest"]["completed_count"] == 3
    assert timing["pytest"]["logfinished_count"] == 3
    assert timing["pytest"]["outcomes"] == {
        "failed": 0,
        "passed": 3,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
    }
    for field in (
        "collection_seconds",
        "execution_wall_seconds",
        "post_test_reporting_seconds",
        "terminal_reporting_seconds",
    ):
        assert timing["pytest"][field] >= 0

    wrong_settings = copy.deepcopy(timing)
    wrong_settings["environment"]["django_settings_module"] = "other.settings"
    (root / "artifacts/wrong-settings.json").write_text(
        json.dumps(wrong_settings), encoding="utf-8"
    )
    rejected_settings = _inventory_cli(
        root,
        "collect",
        "--timing",
        "artifacts/wrong-settings.json",
        "--json-output",
        "artifacts/wrong-settings-report.json",
        "--markdown-output",
        "artifacts/wrong-settings-report.md",
    )
    assert rejected_settings.returncode == 2
    assert "Django settings identity" in rejected_settings.stderr

    positional = _inventory_cli(
        root,
        "run",
        "--lane",
        "portable",
        "--observation",
        "invalid-positional-selector",
        "--variant",
        "locked",
        "--timing-output",
        "artifacts/positional.json",
        "--external-note",
        "Intentional positional-selector rejection fixture.",
        "--",
        "tests/test_sample.py::test_database",
        "-q",
    )

    assert positional.returncode == pytest.ExitCode.USAGE_ERROR
    assert "additional positional selectors are not supported" in positional.stderr

    repeated_timing = copy.deepcopy(timing)
    repeated_timing["sample_id"] = str(uuid.uuid4())
    repeated_timing["external"]["note"] = (
        "Repeated miniature sample on the same prepared environment."
    )
    (root / "artifacts/timing-repeat.json").write_text(
        json.dumps(repeated_timing), encoding="utf-8"
    )
    assert repeated_timing["sample_id"] != timing["sample_id"]

    merged = _inventory_cli(
        root,
        "collect",
        "--timing",
        "artifacts/timing.json",
        "--timing",
        "artifacts/timing-repeat.json",
        "--json-output",
        "artifacts/merged.json",
        "--markdown-output",
        "artifacts/merged.md",
    )

    assert merged.returncode == 0, merged.stderr
    merged_report = json.loads((root / "artifacts/merged.json").read_text(encoding="utf-8"))
    assert [record["observation"] for record in merged_report["timings"]] == [
        "mini-local",
        "mini-local",
    ]
    assert "mini-local" in (root / "artifacts/merged.md").read_text(encoding="utf-8")
    assert not list((root / "artifacts").glob(".*.pending"))


def test_direct_cli_xdist_run_consumes_worker_collection_report(tmp_path: Path) -> None:
    root = _mini_inventory_repository(tmp_path)

    run = _inventory_cli(
        root,
        "run",
        "--lane",
        "hermetic",
        "--execution",
        "xdist",
        "--observation",
        "mini-xdist",
        "--variant",
        "two-workers",
        "--timing-output",
        "artifacts/xdist-timing.json",
        "--external-note",
        "Miniature worker-loaded selector regression.",
        "--",
        "-q",
    )

    assert run.returncode == 0, run.stderr
    timing = json.loads((root / "artifacts/xdist-timing.json").read_text(encoding="utf-8"))
    assert timing["execution"] == {
        "mode": "xdist",
        "workers": 2,
        "distribution": "worksteal",
        "max_worker_restart": 0,
    }
    assert timing["collection"]["valid"] is True
    assert timing["collection"]["selected_count"] == 2
    assert timing["collection"]["collected_count"] == 3
    assert len(timing["collection"]["worker_collections"]) == 2
    assert all(
        worker["collected_count"] == 3 for worker in timing["collection"]["worker_collections"]
    )
    assert timing["pytest"]["selected_count"] == 2
    assert timing["pytest"]["completed_count"] == 2
    assert timing["pytest"]["outcomes"]["passed"] == 2
    assert "outcome_digest" not in timing["pytest"]

    tampered_records = []
    wrong_execution_type = copy.deepcopy(timing)
    wrong_execution_type["execution"]["workers"] = 2.0
    tampered_records.append((wrong_execution_type, "exact execution policy"))
    wrong_collection_digest = copy.deepcopy(timing)
    wrong_collection_digest["collection"]["collected_nodeid_digest"] = "0" * 64
    tampered_records.append((wrong_collection_digest, "does not match current collection"))
    wrong_worker_digest = copy.deepcopy(timing)
    wrong_worker_digest["collection"]["worker_collections"][0]["collected_contract_digest"] = (
        "0" * 64
    )
    tampered_records.append((wrong_worker_digest, "xdist worker"))
    for index, (tampered, expected_error) in enumerate(tampered_records):
        evidence_path = root / f"artifacts/tampered-{index}.json"
        evidence_path.write_text(json.dumps(tampered), encoding="utf-8")
        rejected = _inventory_cli(
            root,
            "collect",
            "--timing",
            evidence_path.relative_to(root).as_posix(),
            "--json-output",
            f"artifacts/tampered-{index}-report.json",
            "--markdown-output",
            f"artifacts/tampered-{index}-report.md",
        )

        assert rejected.returncode == 2
        assert expected_error in rejected.stderr


def test_selection_and_output_guards_fail_closed_without_running_pytest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _mini_inventory_repository(tmp_path)

    for pytest_arguments in (
        ["-k", "test_parameter"],
        ["--sw-skip"],
        ["--sw-reset"],
        ["--taxonomy-lane", "local-ray"],
        ["--taxonomy-lane=local-ray"],
        ["--taxonomy-execution", "serial"],
        ["--taxonomy-execution=serial"],
        ["--taxonomy-manifest", "other.json"],
        ["--taxonomy-manifest=other.json"],
    ):
        with pytest.raises(InventoryError, match="change taxonomy selection"):
            inventory_module._validate_pytest_passthrough(pytest_arguments)
    for pytest_arguments in (
        ["-n", "2"],
        ["-nauto"],
        ["-nlogical"],
        ["-d", "--tx", "popen"],
    ):
        with pytest.raises(InventoryError, match="execution policy"):
            inventory_module._validate_pytest_passthrough(pytest_arguments)
    with pytest.raises(InventoryError, match="must be ignored"):
        inventory_module._validate_output_path(root, Path("timing.json"), "timing output")
    with pytest.raises(InventoryError, match="must be ignored"):
        inventory_module._validate_output_path(
            root,
            Path("docs/investigations/test-suite-baseline-2026-07-22.json"),
            "timing output",
        )
    with pytest.raises(InventoryError, match="must use .json"):
        inventory_module._validate_output_path(
            root,
            Path("docs/investigations/test-suite-baseline-2026-07-22.md"),
            "JSON output",
            allow_generated_baseline=True,
            generated_baseline_suffix=".json",
        )
    with pytest.raises(InventoryError, match="must not overwrite timing inputs"):
        inventory_module._validate_collect_path_aliases(
            root,
            Path("artifacts/timing.json"),
            Path("artifacts/report.md"),
            [Path("artifacts/timing.json")],
        )

    for variable, value in (
        ("PYTEST_ADDOPTS", "-k narrowed"),
        ("PYTEST_PLUGINS", "untracked_plugin"),
        ("DJANGO_SETTINGS_MODULE", "other.settings"),
        ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1"),
    ):
        monkeypatch.setenv(variable, value)
        with pytest.raises(InventoryError, match=variable):
            inventory_module._reject_pytest_environment()
        monkeypatch.delenv(variable)

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.postgres_settings")
    with pytest.raises(InventoryError, match="DJANGO_SETTINGS_MODULE"):
        inventory_module._reject_pytest_environment()
    inventory_module._reject_pytest_environment(("tests.postgres_settings",))
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE")

    with pytest.raises(InventoryError, match="outcome distribution"):
        inventory_module._validate_timing_detail_records(
            {
                "test_outcomes": [
                    {"nodeid": "tests/unit/test_example.py::test_a", "outcome": "skipped"},
                    {"nodeid": "tests/unit/test_example.py::test_b", "outcome": "xfailed"},
                ],
                "skipped_tests": [
                    {"nodeid": "tests/unit/test_example.py::test_a", "outcome": "skipped"},
                    {"nodeid": "tests/unit/test_example.py::test_b", "outcome": "skipped"},
                ],
            },
            {"passed": 0, "failed": 0, "skipped": 1, "xfailed": 1, "xpassed": 0},
            [_item("test_a"), _item("test_b")],
        )

    for invalid in (math.nan, math.inf, -1.0):
        with pytest.raises(InventoryError, match="finite nonnegative"):
            inventory_module._require_nonnegative_number(invalid, "fixture")
    invalid_json = tmp_path / "invalid.json"
    with pytest.raises(InventoryError, match="portable JSON"):
        inventory_module._write_json(invalid_json, {"seconds": math.nan})
    assert not invalid_json.exists()


def test_runtime_paths_are_exact_ignored_siblings(tmp_path: Path) -> None:
    root = _mini_inventory_repository(tmp_path)
    timing = Path("artifacts/benchmark/hermetic.json")

    coverage, ray_tmp = inventory_module._validated_runtime_paths(
        root,
        timing,
        Path("artifacts/benchmark/.coverage"),
        Path("artifacts/benchmark/ray-tmp"),
    )

    assert coverage == (root / "artifacts/benchmark/.coverage").resolve()
    assert ray_tmp == (root / "artifacts/benchmark/ray-tmp").resolve()
    if os.name != "nt":
        output_directory = root / "artifacts" / "benchmark"
        output_directory.mkdir(parents=True)
        short_alias = tmp_path / "ray-alias"
        short_alias.symlink_to(output_directory, target_is_directory=True)

        _, aliased_ray_tmp = inventory_module._validated_runtime_paths(
            root,
            timing,
            Path("artifacts/benchmark/.coverage"),
            short_alias / "ray-tmp",
        )

        assert aliased_ray_tmp == short_alias / "ray-tmp"
        assert aliased_ray_tmp.resolve() == (output_directory / "ray-tmp").resolve()

        wrong_output = root / "artifacts" / "other"
        wrong_output.mkdir()
        wrong_alias = tmp_path / "wrong-ray-alias"
        wrong_alias.symlink_to(wrong_output, target_is_directory=True)
        with pytest.raises(InventoryError, match="sibling ray-tmp"):
            inventory_module._validated_runtime_paths(
                root,
                timing,
                Path("artifacts/benchmark/.coverage"),
                wrong_alias / "ray-tmp",
            )
    with pytest.raises(InventoryError, match="sibling .coverage"):
        inventory_module._validated_runtime_paths(
            root,
            timing,
            Path("artifacts/other/.coverage"),
            Path("artifacts/benchmark/ray-tmp"),
        )
    with pytest.raises(InventoryError, match="provided together"):
        inventory_module._validated_runtime_paths(
            root,
            timing,
            Path("artifacts/benchmark/.coverage"),
            None,
        )


def test_cli_run_enforces_group_skip_policy(tmp_path: Path) -> None:
    root = _mini_inventory_repository(tmp_path)
    conftest = root / "tests/conftest.py"
    conftest.write_text(
        conftest.read_text(encoding="utf-8")
        + "\n@pytest.fixture\ndef teardown_skip():\n"
        + "    yield\n"
        + '    pytest.skip("resource unavailable during teardown")\n',
        encoding="utf-8",
    )
    sample = root / "tests/test_sample.py"
    sample.write_text(
        sample.read_text(encoding="utf-8")
        + "\ndef test_resource(teardown_skip):\n"
        + "    assert teardown_skip is None\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "--", "tests/conftest.py", "tests/test_sample.py"], cwd=root, check=True
    )

    run = _inventory_cli(
        root,
        "run",
        "--lane",
        "portable",
        "--observation",
        "forbidden-skip",
        "--variant",
        "locked",
        "--timing-output",
        "artifacts/skip.json",
        "--external-note",
        "Intentional forbidden-skip regression fixture.",
        "--",
        "-q",
    )

    assert run.returncode == 2
    timing = json.loads((root / "artifacts/skip.json").read_text(encoding="utf-8"))
    assert timing["pytest"]["exit_code"] == 0
    assert timing["pytest"]["outcomes"]["skipped"] == 1
    assert timing["skipped_tests"][0]["nodeid"].endswith("::test_resource")
    assert "skip policy forbids" in " ".join(timing["integrity"]["errors"])

    merged = _inventory_cli(
        root,
        "collect",
        "--timing",
        "artifacts/skip.json",
        "--json-output",
        "artifacts/skip-merged.json",
        "--markdown-output",
        "artifacts/skip-merged.md",
    )

    assert merged.returncode == 2
    assert "execution-integrity" in merged.stderr


def test_run_lane_rejects_source_changes_even_when_pytest_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    sources = iter(
        [
            {"algorithm": "sha256", "digest": "a" * 64, "file_count": 1, "roots": []},
            {"algorithm": "sha256", "digest": "b" * 64, "file_count": 1, "roots": []},
        ]
    )
    monkeypatch.setattr(inventory_module, "_source_digest", lambda *_args: next(sources))
    coverage_file = ROOT / "artifacts" / "runtime-paths" / ".coverage"
    ray_tmp_dir = ROOT / "artifacts" / "runtime-paths" / "ray-tmp"
    monkeypatch.setenv("COVERAGE_FILE", "previous-coverage")
    monkeypatch.setenv("RAY_TMPDIR", "previous-ray")

    def _pytest_main(*_args: object, **_kwargs: object) -> int:
        assert os.environ["COVERAGE_FILE"] == str(coverage_file)
        assert os.environ["RAY_TMPDIR"] == str(ray_tmp_dir)
        return 0

    monkeypatch.setattr(inventory_module.pytest, "main", _pytest_main)

    exit_code, timing = run_lane(
        ROOT,
        MANIFEST_PATH,
        manifest,
        "hermetic",
        [],
        observation="mutated-source",
        variant="locked",
        runner_queue_seconds=None,
        environment_setup_seconds=None,
        external_note="Intentional source mutation fixture.",
        coverage_file=coverage_file,
        ray_tmp_dir=ray_tmp_dir,
    )

    assert exit_code == 2
    assert timing["source"]["digest"] == "a" * 64
    assert timing["source_after_digest"] == "b" * 64
    assert "source changed" in " ".join(timing["integrity"]["errors"])
    assert "promised timing phases" in " ".join(timing["integrity"]["errors"])
    assert os.environ["COVERAGE_FILE"] == "previous-coverage"
    assert os.environ["RAY_TMPDIR"] == "previous-ray"


def test_source_digest_covers_binary_inputs_but_excludes_generated_baseline(
    tmp_path: Path,
) -> None:
    root = _mini_inventory_repository(tmp_path)
    manifest_path = root / ".github/test-suite-taxonomy.json"
    original = _source_digest(root, manifest_path)

    (root / "testproject/static/sample.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00changed\r\n")
    changed = _source_digest(root, manifest_path)

    assert changed["digest"] != original["digest"]

    baseline = root / "docs/investigations/test-suite-baseline-2026-07-22.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text('{"generated": true}\n', encoding="utf-8")

    assert _source_digest(root, manifest_path)["digest"] == changed["digest"]

    (root / "testproject/static/sample.png").unlink()
    deleted = _source_digest(root, manifest_path)

    assert deleted["file_count"] == changed["file_count"]
    assert deleted["digest"] != changed["digest"]


def test_python312_ci_generates_validated_source_fenced_timing_artifact() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    by_name = {step["name"]: step for step in steps if "name" in step}

    timed = by_name["Run tests with suite timing"]
    assert timed["if"] == "matrix.python-version == '3.12'"
    assert "scripts/test_suite_inventory.py run" in timed["run"]
    assert "--lane supported-python" in timed["run"]
    assert "--observation github-actions-ubuntu-py312" in timed["run"]
    assert "--variant locked-dependencies" in timed["run"]
    assert "--runner-queue-seconds" not in timed["run"]
    assert "--environment-setup-seconds" not in timed["run"]

    validated = by_name["Validate suite timing evidence"]
    assert "scripts/test_suite_inventory.py collect" in validated["run"]
    assert "--timing artifacts/test-suite-inventory/github-actions-py312.json" in validated["run"]

    uploaded = by_name["Upload suite timing evidence"]
    assert uploaded["if"] == "always() && matrix.python-version == '3.12'"
    assert uploaded["with"]["if-no-files-found"] == "warn"
    assert uploaded["with"]["path"] == "artifacts/test-suite-inventory/"


def test_inventory_reports_counts_parameterization_and_ci_duplication() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    items = [
        _item("test_plain"),
        _item(
            "test_matrix[first]",
            "parametrize",
            parameter_keys=("scenario",),
        ),
        _item(
            "test_matrix[second]",
            "parametrize",
            parameter_keys=("scenario",),
        ),
        _item(
            "test_api",
            "django_db",
            path="tests/integration/test_api.py",
            fixtures=("client", "db", "request"),
        ),
        _item(
            "test_live",
            "live_cluster",
            path="tests/integration/test_live_failure_injection.py",
        ),
    ]

    report = build_inventory(ROOT, MANIFEST_PATH, manifest, items)

    assert report["totals"] == {
        "collected": 5,
        "files": 3,
        "parameterized_cases": 2,
        "parameterized_families": 1,
        "estimated_blocking_ci_selected_case_slots": 22,
    }
    groups = {group["id"]: group for group in report["groups"]}
    assert groups["hermetic"]["selected_count"] == 3
    assert groups["sqlite-django"]["selected_count"] == 1
    assert groups["live-cluster"]["selected_count"] == 1
    assert groups["bundled-testproject"]["selected_count"] == 1
    assert groups["dependency-compatibility"]["estimated_ci_selected_case_slots"] == 8
    assert report["parameterized_families"] == [
        {
            "nodeid": "tests/unit/test_example.py::test_matrix",
            "case_count": 2,
            "parameter_keys": ["scenario"],
        }
    ]
    assert report["fixtures"][0] == {"name": "request", "selected_count": 5}

    report["timings"] = [
        {
            "lane": "supported-python",
            "measured_at_utc": "2026-07-22T00:00:00+00:00",
            "observation": "representative-observation",
            "variant": "locked",
            "environment": {
                "python": "3.12.0",
                "platform": "test-platform",
                "packages": {
                    "coverage": "7.0",
                    "django": "6.0",
                    "pytest": "9.0",
                    "pytest-cov": "7.0",
                    "pytest-django": "4.0",
                    "pytest-xdist": "not-installed",
                    "ray": "2.0",
                },
            },
            "external": {
                "runner_queue_seconds": 2.0,
                "environment_setup_seconds": 7.0,
                "note": "representative fixture",
            },
            "pytest": {
                "outcomes": {"passed": 4, "skipped": 1},
                "collection_seconds": 1.0,
                "execution_wall_seconds": 4.0,
                "setup_phase_seconds": 0.5,
                "call_phase_seconds": 3.0,
                "teardown_phase_seconds": 0.25,
                "post_test_reporting_seconds": 0.5,
                "terminal_reporting_seconds": 0.25,
            },
            "slowest_files": [{"path": "tests/unit/test_example.py", "total_seconds": 2.0}],
            "slowest_tests": [{"nodeid": items[0].nodeid, "total_seconds": 1.0}],
        }
    ]
    markdown = render_markdown(report)
    assert "Estimated blocking CI selected case slots" in markdown
    assert "`supported-python`" in markdown
    assert "Most-used fixtures" in markdown
    assert (
        "`supported-python` / `representative-observation` / `locked` slow paths at "
        "`2026-07-22T00:00:00+00:00`"
    ) in markdown
    assert "representative fixture" in markdown
    assert "These are review candidates, not pre-approved deletions." in markdown


def test_inventory_rejects_unowned_or_multiply_owned_execution_contracts() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    unsupported = _item("test_mixed", "postgresql", "real_ray")

    with pytest.raises(InventoryError, match="partition every collected item exactly once"):
        build_inventory(ROOT, MANIFEST_PATH, manifest, [unsupported])


def test_selection_requires_paths_and_disjoint_marker_rules() -> None:
    with pytest.raises(InventoryError, match="paths must be a non-empty list"):
        Selection.from_mapping({}, "selection")

    with pytest.raises(InventoryError, match="includes and excludes the same marker"):
        Selection.from_mapping(
            {
                "paths": ["tests"],
                "include_markers": ["real_ray"],
                "exclude_markers": ["real_ray"],
            },
            "selection",
        )

    with pytest.raises(InventoryError, match="includes and excludes the same fixture"):
        Selection.from_mapping(
            {
                "paths": ["tests"],
                "include_any_fixtures": ["db"],
                "exclude_fixtures": ["db"],
            },
            "selection",
        )

    with pytest.raises(InventoryError, match="stay inside the repository"):
        Selection.from_mapping({"paths": ["tests/.."]}, "selection")

    selection = Selection.from_mapping({"paths": ["tests//unit/./"]}, "selection")

    assert selection.paths == ("tests/unit",)

    fixture_aware = Selection.from_mapping(
        {"paths": ["tests"], "exclude_fixtures": ["db"]}, "selection"
    )
    with pytest.raises(InventoryError, match="manifest-backed run command"):
        fixture_aware.pytest_arguments()


def test_manifest_rejects_duplicate_ids(tmp_path: Path) -> None:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["boundaries"][0]["id"] = document["execution_contracts"][0]["id"]
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(InventoryError, match="ids must be unique"):
        load_manifest(path)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("workers", 3),
        ("workers", 2.0),
        ("workers", False),
        ("distribution", "load"),
        ("max_worker_restart", 1),
        ("max_worker_restart", False),
    ),
)
def test_manifest_rejects_unbounded_hermetic_xdist_policy(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["execution_contracts"][0]["execution"][field] = value
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(InventoryError, match="fixed xdist topology"):
        load_manifest(path)


@pytest.mark.parametrize(
    ("field", "value"),
    (("exclude_markers", "real_ray"), ("exclude_fixtures", "db")),
)
def test_manifest_rejects_xdist_selection_without_resource_exclusion(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["execution_contracts"][0]["selection"][field].remove(value)
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(InventoryError, match="must exclude every external or database owner"):
        load_manifest(path)


def test_manifest_rejects_xdist_for_nonhermetic_contract(tmp_path: Path) -> None:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["execution_contracts"][1]["execution"] = document["execution_contracts"][0][
        "execution"
    ]
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(InventoryError, match="only the hermetic"):
        load_manifest(path)


@pytest.mark.parametrize("schema_version", (True, 3.0))
def test_manifest_and_timing_loaders_reject_coerced_schema_versions(
    tmp_path: Path,
    schema_version: object,
) -> None:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["schema_version"] = schema_version
    manifest_path = tmp_path / "taxonomy.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    timing_path = tmp_path / "timing.json"
    timing_path.write_text(
        json.dumps({"schema_version": schema_version}),
        encoding="utf-8",
    )

    with pytest.raises(InventoryError, match="manifest schema"):
        load_manifest(manifest_path)
    with pytest.raises(InventoryError, match="unsupported timing evidence"):
        inventory_module._load_timing(timing_path)


def test_inventory_refuses_timing_from_another_source_digest() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    timing = {
        "schema_version": 3,
        "sample_id": "00000000-0000-4000-8000-000000000001",
        "measured_at_utc": "2026-07-22T00:00:00+00:00",
        "source": {"digest": "0" * 64},
        "source_after_digest": "0" * 64,
    }

    with pytest.raises(InventoryError, match="does not match"):
        build_inventory(ROOT, MANIFEST_PATH, manifest, [_item("test_plain")], [timing])


def test_inventory_refuses_failed_timing_evidence() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    item = _item("test_plain")
    source = _source_digest(ROOT, MANIFEST_PATH)
    timing = {
        "schema_version": 3,
        "sample_id": "00000000-0000-4000-8000-000000000002",
        "measured_at_utc": "2026-07-22T00:00:00+00:00",
        "source": source,
        "source_after_digest": source["digest"],
        "lane": "hermetic",
        "observation": "failed-test",
        "variant": "locked",
        "selection": manifest.group("hermetic").selection.expression(),
        "pytest_arguments": [],
        "skip_policy": {
            "mode": manifest.group("hermetic").skip_policy.mode,
            "reason": manifest.group("hermetic").skip_policy.reason,
        },
        "integrity": {"valid": True, "errors": []},
        "pytest": {"exit_code": 1, "selected_count": 1},
    }

    with pytest.raises(InventoryError, match="successful pytest run"):
        build_inventory(ROOT, MANIFEST_PATH, manifest, [item], [timing])
