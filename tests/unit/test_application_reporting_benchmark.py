"""Bounded hosted reporting benchmark delivery without external execution."""

import copy
import json

import pytest

from qualification.application import run_chainsaw
from qualification.application import run_reporting_benchmark as runner


def receipt():
    return {
        "schema_version": 1,
        "layer": runner.LAYER,
        "status": "passed",
        "complete_application_gate": False,
        "report": {
            "schema_version": 4,
            "benchmark": "django-ray-live-workflow-reporting-policies",
            "configuration": {"repetitions": 3, "terminal_publisher": "package_default"},
            "environment": {"database_vendor": "postgresql"},
            "samples": [
                {"policy": policy}
                for _ in range(3)
                for policy in ("full", "terminal_only", "disabled")
            ],
            "cleanup": {
                "requested": True,
                "status": "completed",
                "execution_rows_deleted": 9,
                "retained_for_admin_inspection": False,
            },
        },
    }


@pytest.mark.parametrize(
    "fault", ["missing", "cleanup", "policy", "database", "publisher", "schema"]
)
def test_benchmark_receipt_rejects_incomplete_evidence(fault):
    value = receipt()
    if fault == "missing":
        value["report"]["samples"].pop()
    elif fault == "cleanup":
        value["report"]["cleanup"]["status"] = "pending"
    elif fault == "policy":
        value["report"]["samples"][0]["policy"] = "disabled"
    elif fault == "database":
        value["report"]["environment"]["database_vendor"] = "sqlite"
    elif fault == "publisher":
        value["report"]["configuration"]["terminal_publisher"] = "pilot"
    else:
        value["schema_version"] = True
    with pytest.raises(ValueError):
        runner.validate_receipt(value)


def test_large_reporting_receipt_has_separate_bound():
    value = receipt()
    value["report"]["extra_measurement"] = "x" * 20000
    raw = json.dumps(value).encode()
    assert run_chainsaw.parse_receipts(raw, ("reporting",)) == {"reporting": raw}
    with pytest.raises(ValueError):
        run_chainsaw.parse_receipts(raw, ("before-workflows",))
    value["report"]["extra_measurement"] = "x" * runner.MAX_RECEIPT_BYTES
    with pytest.raises(ValueError):
        run_chainsaw.parse_receipts(json.dumps(value).encode(), ("reporting",))


@pytest.mark.parametrize("failure", [None, "command", "wrapped", "cleanup", "oversize"])
def test_reporting_entrypoint_retains_full_report_or_fixed_failure(
    tmp_path, monkeypatch, capsys, failure
):
    import django
    from django.core import management

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    monkeypatch.setenv("DJANGO_RAY_RUN_WORKFLOW_REPORTING_BENCHMARK", "0")
    monkeypatch.setattr(django, "setup", lambda: None)
    output = tmp_path / "benchmark.json"

    def command(name, **options):
        assert name == "django_ray_benchmark_workflow_reporting"
        assert options["repetitions"] == 3
        assert options["cleanup"] is True
        assert options["timeout_seconds"] == 45
        assert options["output_json"] == output
        if failure == "command":
            raise RuntimeError("private failure must not escape")
        if failure == "wrapped":
            from testproject.management.commands import django_ray_benchmark_workflow_reporting

            try:
                django_ray_benchmark_workflow_reporting._validate_complete_report([], repetitions=3)
            except Exception as error:
                raise RuntimeError("private failure must not escape") from error
        report = copy.deepcopy(receipt()["report"])
        if failure == "cleanup":
            report["cleanup"]["status"] = "pending"
        if failure == "oversize":
            report["large"] = "x" * runner.MAX_RECEIPT_BYTES
        output.write_text(json.dumps(report))

    monkeypatch.setattr(management, "call_command", command)
    assert runner.main(["--output", str(output)]) == (1 if failure else 0)
    raw = capsys.readouterr().out
    assert "private failure" not in raw
    value = json.loads(raw)
    assert value["status"] == ("failed" if failure else "passed")
    if failure:
        assert "report" not in value
        if failure == "wrapped":
            assert type(value["benchmark_failure_line"]) is int
            assert "sample matrix" not in raw
    else:
        runner.validate_receipt(value)
        assert value["report"] == receipt()["report"]


def test_reporting_entrypoint_refuses_non_disposable_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.settings")
    assert runner.main(["--output", str(tmp_path / "report.json")]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_reporting_job_is_serial_bounded_and_follows_cold_acceptance():
    from pathlib import Path

    import yaml

    spec = yaml.safe_load(Path("qualification/application/core.yaml").read_text())["spec"]
    assert spec["steps"][-2]["name"] == "assert-cold-generation"
    step = spec["steps"][-1]
    assert step["name"] == "measure-reporting-policies"
    job = step["try"][0]["create"]["resource"]
    assert job["metadata"]["name"] == "reporting-benchmark"
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["parallelism"] == job["spec"]["completions"] == 1
    assert job["spec"]["activeDeadlineSeconds"] == 600
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "($values.applicationImage)"
    assert container["resources"]["limits"]["memory"] == "512Mi"
    assert container["command"] == [
        "/bin/sh",
        "-ec",
        "python -m qualification.application.run_reporting_benchmark "
        "--output /receipts/reporting-benchmark-raw.json",
    ]
    assert step["try"][1]["assert"]["resource"]["status"] == {"succeeded": 1}
    assert run_chainsaw.RECEIPTS["reporting-benchmark"] == ("assertions", ("reporting",))
