"""Resource-free regression contracts for the finite Linux stage catalogue."""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from pathlib import Path

import pytest
from coverage import CoverageData

from scripts import linux_test_plan as plan
from scripts.test_suite_taxonomy import CollectedTest, InventoryError, load_manifest


@pytest.fixture
def manifest():
    return load_manifest(plan.TAXONOMY)


@pytest.fixture
def items():
    return [
        CollectedTest("tests/unit/test_pure.py::test_case", "tests/unit/test_pure.py", (), ()),
        CollectedTest("tests/test_db.py::test_case", "tests/test_db.py", (), ("db",)),
        CollectedTest("tests/test_ray.py::test_case", "tests/test_ray.py", ("real_ray",), ()),
        CollectedTest(
            "tests/test_compiled.py::test_case",
            "tests/test_compiled.py",
            ("real_ray", "compiled_graph_opt_in"),
            (),
        ),
        CollectedTest("tests/test_pg.py::test_case", "tests/test_pg.py", ("postgresql",), ()),
        CollectedTest("tests/test_live.py::test_case", "tests/test_live.py", ("live_cluster",), ()),
    ]


def test_partition_matches_reference_exactly_and_excludes_only_external_live_cluster(
    manifest, items
):
    result = plan.partition_evidence(items, manifest)
    assert result["disjoint"] is True
    assert result["complete"] is True
    assert result["selected_count"] == 5
    assert result["selections"]["sqlite-django"]["count"] == 1
    assert set(result["selections"]) == set(plan.PARTITION_LANES)


def test_partition_rejects_unowned_marker_combination(manifest, items):
    items.append(
        CollectedTest(
            "tests/test_gap.py::test_gap", "tests/test_gap.py", ("real_ray", "postgresql"), ()
        )
    )
    with pytest.raises(InventoryError, match="does not equal"):
        plan.partition_evidence(items, manifest)


def test_sample_database_variants_keep_their_required_lane_ownership(manifest, items):
    path = "tests/integration/test_sample_admission.py"
    sqlite = CollectedTest(f"{path}::test_budget[sqlite]", path, ("django_db",), ())
    postgres = CollectedTest(
        f"{path}::test_budget[postgresql]", path, ("django_db", "postgresql"), ()
    )
    sample = manifest.group("testproject-contract")
    assert sample.selection.matches(sqlite)
    assert not sample.selection.matches(postgres)
    assert sample.skip_policy.mode == "forbid"
    assert manifest.group("postgresql").selection.matches(postgres)
    assert manifest.group("postgresql").skip_policy.mode == "forbid"
    partition = plan.partition_evidence([*items, sqlite, postgres], manifest)
    assert partition["complete"] is True
    assert partition["selections"]["sqlite-django"]["count"] == 2
    assert partition["selections"]["postgresql"]["count"] == 2


def test_partition_rejects_duplicate_ownership_and_duplicate_collection(manifest, items):
    hermetic = manifest.group("hermetic")
    sqlite = manifest.group("sqlite-django")
    duplicate = replace(sqlite, selection=hermetic.selection)
    changed = replace(
        manifest,
        execution_contracts=tuple(
            duplicate if group.id == sqlite.id else group for group in manifest.execution_contracts
        ),
    )
    with pytest.raises(InventoryError, match="duplicate owner"):
        plan.partition_evidence(items, changed)
    with pytest.raises(InventoryError, match="duplicate normalized"):
        plan.partition_evidence([*items, items[0]], manifest)


def test_catalogue_has_finite_serial_contracts_and_explicit_remaining_evidence(manifest):
    catalogue = plan.catalogue(manifest)
    assert catalogue["complete_hosted_ci"] is False
    assert any(row["id"] == "live-cluster" for row in catalogue["separate_required_evidence"])
    assert all(
        row["execution"] == "serial" and 0 < row["timeout_seconds"] <= 1200
        for row in catalogue["stages"]
    )
    assert all(
        row["cpu_millis"] > 0
        and row["memory_mib"] > 0
        and row["pids"] > 0
        and row["scratch_mib"] > 0
        for row in catalogue["stages"]
    )
    for row in catalogue["stages"]:
        if row["id"] in {"local-ray", "postgresql", "testproject"}:
            assert row["skip_policy"]["mode"] == "forbid"
        if row["id"] in {"local-ray", "compiled-graph-opt-in"}:
            assert (row["cpu_millis"], row["memory_mib"], row["scratch_mib"]) == (3000, 6144, 7168)
            assert row["pids"] == 1024
    makefile = (plan.ROOT / "Makefile").read_text()
    for variable, floor in (("GLOBAL", 95), ("WORKER", 90), ("RAY_JOB", 90), ("TESTPROJECT", 80)):
        assert f"COVERAGE_{variable}_MIN ?= {floor}" in makefile


def test_windows_rejects_stage_before_creating_output_or_launching_process(monkeypatch, tmp_path):
    monkeypatch.setattr(plan.sys, "platform", "win32")
    output = tmp_path / "stage"
    with pytest.raises(SystemExit, match="requires Linux"):
        plan.run_stage(plan.stage_by_id("hermetic"), output, None, None)
    assert not output.exists()


def test_environment_refuses_external_ray_and_missing_browser_runtime(monkeypatch):
    monkeypatch.setattr(plan.inventory, "_reject_pytest_environment", lambda: None)
    monkeypatch.setenv("RAY_ADDRESS", "ray://external.invalid:10001")
    with pytest.raises(InventoryError, match="ambient external Ray"):
        plan._environment(plan.stage_by_id("local-ray"))
    monkeypatch.delenv("RAY_ADDRESS")
    monkeypatch.setattr(plan.shutil, "which", lambda name: None if name == "node" else name)
    with pytest.raises(InventoryError, match="node"):
        plan._environment(plan.stage_by_id("collection"))


def test_postgresql_requires_explicit_disposable_database(monkeypatch):
    monkeypatch.setattr(plan.inventory, "_reject_pytest_environment", lambda: None)
    monkeypatch.setattr(plan.shutil, "which", lambda name: name)
    for name in ("RAY_ADDRESS", "RAY_NAMESPACE", "DJANGO_RAY_LIVE_CLUSTER", "DATABASE_HOST"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(InventoryError, match="DATABASE_HOST"):
        plan._environment(plan.stage_by_id("postgresql"))
    for name in (
        "DATABASE_HOST",
        "DATABASE_NAME",
        "DATABASE_USER",
        "DATABASE_PASSWORD",
        "DATABASE_TEST_NAME",
    ):
        monkeypatch.setenv(name, "same")
    with pytest.raises(InventoryError, match="distinct disposable"):
        plan._environment(plan.stage_by_id("postgresql"))


def test_artifact_export_is_content_bound_and_caps_expansion(tmp_path, monkeypatch):
    raw = b"coverage-data" * 100
    (tmp_path / "coverage.data").write_bytes(raw)
    records = plan.export_artifacts(tmp_path)
    receipt = {"artifacts": records}
    assert plan._verified_artifact(tmp_path, receipt, "coverage.data.gz") == raw
    (tmp_path / "coverage.data.gz").write_bytes(gzip.compress(b"tampered"))
    with pytest.raises(InventoryError, match="artifact digest"):
        plan._verified_artifact(tmp_path, receipt, "coverage.data.gz")
    (tmp_path / "large.gz").write_bytes(gzip.compress(b"x" * 100))
    monkeypatch.setattr(plan, "MAX_RAW_ARTIFACT_BYTES", 10)
    with pytest.raises(InventoryError, match="expanded artifact"):
        plan._uncompressed(tmp_path / "large.gz")


def test_aggregate_refuses_missing_or_stale_stage_before_coverage(
    monkeypatch, tmp_path, manifest, items
):
    source = {"digest": "current"}
    monkeypatch.setattr(plan, "source_identity", lambda *_: source)
    monkeypatch.setattr(plan, "load_manifest", lambda *_: manifest)
    monkeypatch.setattr(plan.inventory, "collect_tests", lambda *_: items)
    monkeypatch.setattr(plan, "runtime_identity", dict)
    with pytest.raises(FileNotFoundError):
        plan.validate_receipts(tmp_path, tmp_path, None)
    directory = tmp_path / "collection"
    directory.mkdir()
    receipt = {
        "schema_version": 1,
        "stage": plan.stage_record(plan.STAGES[0]),
        "source": {"digest": "old"},
        "source_after": source,
        "outcome": "passed",
        "process": {"outcome": "passed"},
        "environment": {},
    }
    (directory / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(InventoryError, match="failed or stale: collection"):
        plan.validate_receipts(tmp_path, tmp_path, None)


def _coverage_shards(tmp_path: Path, *, covered: int, sample_covered: int = 100):
    source = tmp_path / "src/django_ray"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("")
    paths = [
        "src/django_ray/main.py",
        "src/django_ray/management/commands/django_ray_worker.py",
        "src/django_ray/runner/ray_job.py",
    ]
    coverage = CoverageData(basename=str(tmp_path / "shard.data"))
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"value_{number} = {number}" for number in range(100)) + "\n")
        coverage.add_lines({"/other-checkout/" + relative: list(range(1, covered + 1))})
    coverage.write()
    sample = CoverageData(basename=str(tmp_path / "sample.data"))
    for relative in plan.SAMPLE_COVERAGE_PATHS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"value_{number} = {number}" for number in range(100)) + "\n")
        sample.add_lines({"/other-checkout/" + relative: list(range(1, sample_covered + 1))})
    sample.write()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nsource=["src"]\n[tool.coverage.report]\nprecision=2\nfail_under=95\n'
    )
    return [
        (
            plan.stage_by_id("hermetic"),
            {"source_root": "/other-checkout"},
            (tmp_path / "shard.data").read_bytes(),
        ),
        (
            plan.stage_by_id("testproject"),
            {"source_root": "/other-checkout"},
            (tmp_path / "sample.data").read_bytes(),
        ),
    ]


@pytest.mark.parametrize("covered", [94, 95, 100])
def test_aggregation_maps_source_paths_and_enforces_actual_coverage_floor(
    monkeypatch, tmp_path, covered
):
    shards = _coverage_shards(tmp_path, covered=covered)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        plan, "validate_receipts", lambda *_: ({"partition": {"complete": True}}, shards)
    )
    output = tmp_path / "output"
    output.mkdir()
    if covered < 95:
        with pytest.raises(InventoryError, match="source coverage is below"):
            plan.aggregate_evidence(tmp_path, tmp_path, output, None)
        assert not (output / "summary.json").exists()
    else:
        plan.aggregate_evidence(tmp_path, tmp_path, output, None)
        summary = json.loads((output / "summary.json").read_text())
        assert summary["coverage"]["source"]["observed_percent"] == covered
        assert summary["outcome"] == "passed"
        assert summary["complete_hosted_ci"] is False


def test_source_coverage_fragments_defer_floors_only_to_mandatory_aggregate():
    assert "--cov-fail-under=0" in plan.pytest_arguments(plan.stage_by_id("hermetic"))
    assert "--cov-fail-under=80" in plan.pytest_arguments(plan.stage_by_id("testproject"))
    assert "--maxfail=1" in plan.pytest_arguments(plan.stage_by_id("local-ray"))
    assert set(plan.PARTITION_LANES) == {
        "hermetic",
        "sqlite-django",
        "local-ray",
        "compiled-graph-opt-in",
        "postgresql",
    }


@pytest.mark.parametrize("process_outcome", ["timed-out", "cleanup-error", "passed"])
def test_stage_retains_failed_process_or_source_change_receipt(
    monkeypatch, tmp_path, process_outcome
):
    monkeypatch.setattr(plan, "require_linux", lambda: None)
    monkeypatch.setattr(plan.inventory, "_validate_output_path", lambda *_: None)
    monkeypatch.setattr(plan, "runtime_identity", dict)
    observations = iter([{"digest": "before"}, {"digest": "after"}])
    monkeypatch.setattr(plan, "source_identity", lambda *_: next(observations))
    monkeypatch.setattr(plan, "run_coverage_phase", lambda *_: {"outcome": process_outcome})
    output = tmp_path / "new-stage"
    assert plan.run_stage(plan.stage_by_id("static"), output, None, None) == 1
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["process"]["outcome"] == process_outcome
    assert receipt["outcome"] == "invalid-evidence"
    assert receipt["errors"] == ["source changed during the stage"]


def test_stage_refuses_reusing_an_existing_attempt_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(plan, "require_linux", lambda: None)
    with pytest.raises(InventoryError, match="must not already exist"):
        plan.run_stage(plan.stage_by_id("hermetic"), tmp_path, None, None)


def _complete_receipts(monkeypatch, tmp_path, manifest, items):
    source = {"digest": "same"}
    monkeypatch.setattr(plan, "source_identity", lambda *_: source)
    monkeypatch.setattr(plan, "load_manifest", lambda *_: manifest)
    monkeypatch.setattr(plan.inventory, "collect_tests", lambda *_: items)
    environment = {
        "python": "3.12.0",
        "platform": "linux",
        "platform_description": "Linux-test",
        "packages": {"coverage": "7.0"},
    }
    monkeypatch.setattr(plan, "runtime_identity", lambda: environment)
    validated = []
    monkeypatch.setattr(
        plan.inventory,
        "_validate_timing_record",
        lambda timing, *_: validated.append(timing["lane"]),
    )
    for stage in plan.STAGES:
        output = tmp_path / stage.id
        output.mkdir()
        if stage.id == "collection":
            plan._write(
                output / "collection.json",
                {
                    "source": source,
                    "items": [plan.asdict(item) for item in items],
                    "partition": plan.partition_evidence(items, manifest),
                },
            )
        if stage.lane:
            plan._write(
                output / "timing.json",
                {
                    "lane": stage.lane,
                    "pytest_arguments": plan.pytest_arguments(stage),
                    "pytest": {"outcomes": {"passed": 1}},
                    "environment": {
                        "python": environment["python"],
                        "platform": environment["platform_description"],
                        "packages": environment["packages"],
                    },
                },
            )
            (output / "coverage.data").write_bytes(b"coverage bytes are validated on combination")
        plan._write(
            output / "receipt.json",
            {
                "schema_version": 1,
                "stage": plan.stage_record(stage),
                "source": source,
                "source_after": source,
                "source_root": "/work",
                "environment": environment,
                "assertion_commands": plan.commands(stage),
                "process": {
                    "name": stage.id,
                    "selection": stage.lane or stage.id,
                    "coverage_mode": stage.coverage or "none",
                    "timeout_seconds": stage.timeout_seconds,
                    "outcome": "passed",
                    "exit_code": 0,
                    "timed_out": False,
                    "launch_error": None,
                    "termination_error": None,
                    "cleanup_error": None,
                    "capture_error": None,
                    "timing_error": None,
                    "post_exit_descendants_terminated": True,
                },
                "outcome": "passed",
                "errors": [],
                "artifacts": plan.export_artifacts(output),
            },
        )
    return validated


def test_aggregate_checks_every_stage_and_delegates_exact_timing_verification(
    monkeypatch, tmp_path, manifest, items
):
    validated = _complete_receipts(monkeypatch, tmp_path, manifest, items)
    summary, coverage = plan.validate_receipts(tmp_path, tmp_path, None)
    assert validated == [stage.lane for stage in plan.STAGES if stage.lane]
    assert len(coverage) == 6
    assert summary["partition"]["complete"] is True
    receipt_path = tmp_path / "static/receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["process"]["exit_code"] = 1
    plan._write(receipt_path, receipt)
    with pytest.raises(InventoryError, match="failed or stale: static"):
        plan.validate_receipts(tmp_path, tmp_path, None)


def test_aggregate_rejects_waived_sample_floor_even_with_matching_artifact_digest(
    monkeypatch, tmp_path, manifest, items
):
    _complete_receipts(monkeypatch, tmp_path, manifest, items)
    output = tmp_path / "testproject"
    timing = json.loads((output / "timing.json").read_text())
    timing["pytest_arguments"] = ["--cov=src", "--cov-fail-under=0"]
    plan._write(output / "timing.json", timing)
    receipt = json.loads((output / "receipt.json").read_text())
    receipt["artifacts"] = plan.export_artifacts(output)
    plan._write(output / "receipt.json", receipt)
    with pytest.raises(InventoryError, match="required selection or coverage command: testproject"):
        plan.validate_receipts(tmp_path, tmp_path, None)


@pytest.mark.parametrize("sample_covered", [79, 80, 100])
def test_aggregate_rechecks_sample_floor_from_actual_data(monkeypatch, tmp_path, sample_covered):
    shards = _coverage_shards(tmp_path, covered=100, sample_covered=sample_covered)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plan, "validate_receipts", lambda *_: ({}, shards))
    output = tmp_path / "output"
    output.mkdir()
    if sample_covered < 80:
        with pytest.raises(InventoryError, match="testproject coverage is below"):
            plan.aggregate_evidence(tmp_path, tmp_path, output, None)
    else:
        plan.aggregate_evidence(tmp_path, tmp_path, output, None)
        summary = json.loads((output / "summary.json").read_text())
        assert summary["coverage"]["testproject"]["observed_percent"] == sample_covered


@pytest.mark.parametrize("change", ["missing", "invalid", "incomplete"])
def test_aggregate_rejects_unusable_sample_data(monkeypatch, tmp_path, change):
    shards = _coverage_shards(tmp_path, covered=100)
    if change == "missing":
        shards.pop()
    else:
        stage, receipt, _raw = shards[-1]
        raw = b"not a coverage database"
        if change == "incomplete":
            sample = CoverageData(basename=str(tmp_path / "incomplete.data"))
            sample.add_lines({"/other-checkout/testproject/urls.py": [1]})
            sample.write()
            raw = (tmp_path / "incomplete.data").read_bytes()
        shards[-1] = (stage, receipt, raw)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plan, "validate_receipts", lambda *_: ({}, shards))
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(InventoryError, match="sample coverage|coverage data is invalid"):
        plan.aggregate_evidence(tmp_path, tmp_path, output, None)


@pytest.mark.parametrize("field", ["python", "platform", "packages"])
def test_aggregate_rejects_nested_runtime_drift_with_matching_artifact_hash(
    monkeypatch, tmp_path, manifest, items, field
):
    _complete_receipts(monkeypatch, tmp_path, manifest, items)
    output = tmp_path / "hermetic"
    timing = json.loads((output / "timing.json").read_text())
    timing["environment"][field] = "changed"
    plan._write(output / "timing.json", timing)
    receipt = json.loads((output / "receipt.json").read_text())
    receipt["artifacts"] = plan.export_artifacts(output)
    plan._write(output / "receipt.json", receipt)
    with pytest.raises(InventoryError, match="timing environment differs"):
        plan.validate_receipts(tmp_path, tmp_path, None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("timed_out", True),
        ("cleanup_error", "failed"),
        ("termination_error", "failed"),
        ("launch_error", "failed"),
        ("capture_error", "failed"),
        ("timing_error", "failed"),
        ("timeout_seconds", 3600),
        ("selection", "other"),
    ],
)
def test_aggregate_rejects_contradictory_passed_process(
    monkeypatch, tmp_path, manifest, items, field, value
):
    _complete_receipts(monkeypatch, tmp_path, manifest, items)
    path = tmp_path / "static/receipt.json"
    receipt = json.loads(path.read_text())
    receipt["process"][field] = value
    plan._write(path, receipt)
    with pytest.raises(InventoryError, match="process contract"):
        plan.validate_receipts(tmp_path, tmp_path, None)


def test_aggregate_rejects_missing_static_assertions(monkeypatch, tmp_path, manifest, items):
    _complete_receipts(monkeypatch, tmp_path, manifest, items)
    path = tmp_path / "static/receipt.json"
    receipt = json.loads(path.read_text())
    receipt["assertion_commands"] = []
    plan._write(path, receipt)
    with pytest.raises(InventoryError, match="failed or stale: static"):
        plan.validate_receipts(tmp_path, tmp_path, None)
