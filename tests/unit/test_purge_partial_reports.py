"""Resource-free command checks for registered outcomes before inventory failure."""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_ray.management.commands import django_ray_purge_inputs as purge_command
from django_ray.management.purge_cursor import decode_purge_cursor
from django_ray.models import TaskInputPayload
from django_ray.payload_registry import PayloadBackendInventoryReport

_REPORT_KEYS = {
    "schema_version",
    "mode",
    "cutoff",
    "retention_days",
    "counts",
    "has_more",
    "next_cursor",
    "details",
    "details_omitted",
    "limits",
}
_PRIVATE_DETAIL = "private-provider-location-and-credential"


class _CandidateQuery:
    def __init__(self, references):
        self.references = references

    def filter(self, **_filters):
        return self

    def order_by(self, *_fields):
        return self

    def annotate(self, **_annotations):
        return self

    def values_list(self, *fields):
        assert fields == ("bounded_key", "key_complete")
        return [(reference, True) for reference in self.references]


def _successful_inventory():
    return PayloadBackendInventoryReport(
        backend="filesystem",
        cutoff="2026-11-01T00:00:00+00:00",
        grace_seconds=3600,
        delete_unregistered=False,
        has_more=False,
        counts=dict.fromkeys(
            (
                "scanned",
                "unregistered_eligible",
                "unregistered_recent",
                "registered_active",
                "registered_missing",
                "tombstone",
                "ambiguous",
                "malformed_key",
                "purged",
                "failed",
            ),
            0,
        ),
        details=[],
        details_omitted=0,
    )


def _invoke(
    monkeypatch,
    *,
    outcomes=("purged",),
    inventory_error=None,
    inventory_report=None,
    discover=True,
    output_format="json",
    references=None,
    **options,
):
    if references is None:
        references = [f"synthetic-reference-{index:04}" for index in range(len(outcomes))]
    states = dict.fromkeys(references, "ACTIVE")
    monkeypatch.setattr(TaskInputPayload, "objects", _CandidateQuery(references))

    def process(_command, reference, **_options):
        outcome = outcomes[references.index(reference)]
        if outcome == "purged":
            states[reference] = "PURGED"
        return outcome

    monkeypatch.setattr(purge_command.Command, "_process_reference", process)
    inventory_calls = []

    def inventory(**_options):
        inventory_calls.append(dict(states))
        if inventory_error is not None:
            raise inventory_error
        return inventory_report

    monkeypatch.setattr(purge_command, "discover_and_reap_unregistered_payloads", inventory)
    output = StringIO()
    error = None
    try:
        call_command(
            "django_ray_purge_inputs",
            delete=True,
            discover_unregistered=discover,
            format=output_format,
            stdout=output,
            **options,
        )
    except CommandError as caught:
        error = caught
    return output.getvalue(), error, states, inventory_calls


@pytest.mark.parametrize("output_format", ["json", "text"])
def test_inventory_failure_retains_completed_registered_results_and_cursor(
    monkeypatch, output_format
):
    output, error, states, calls = _invoke(
        monkeypatch,
        outcomes=("purged", "eligible"),
        inventory_error=OSError(_PRIVATE_DETAIL),
        output_format=output_format,
        max_candidates=1,
    )

    assert error is not None and error.returncode == 1
    assert str(error) == "Unregistered payload inventory failed; inspect the partial report."
    assert calls == [states]
    assert states["synthetic-reference-0000"] == "PURGED"
    assert states["synthetic-reference-0001"] == "ACTIVE"
    assert _PRIVATE_DETAIL not in output + str(error)
    assert "inventory_failed" in output
    assert "OSError" not in output + str(error)
    assert "synthetic-reference" not in output
    if output_format == "json":
        report = json.loads(output)
        assert set(report) == _REPORT_KEYS | {"inventory_error"}
        assert report["schema_version"] == 1
        assert report["inventory_error"] == "inventory_failed"
        assert report["counts"] == {
            "processed": 1,
            "eligible": 1,
            "purged": 1,
            "retained": 0,
            "failed": 0,
            "incomplete": 0,
        }
        assert report["has_more"] is True
        cursor = decode_purge_cursor(report["next_cursor"])
        assert cursor.after == "synthetic-reference-0000"
        assert cursor.delete is True
        assert cursor.cutoff.isoformat() == report["cutoff"]
        assert report["details"] == [
            {
                "reference_sha256": purge_command.Command._reference_fingerprint(cursor.after),
                "outcome": "purged",
            }
        ]
    else:
        assert "1 eligible, 1 purged, 0 failed" in output
        assert "Processed 1; retained 0; incomplete 0" in output
        assert "outcome=purged" in output
        assert "Next cursor: " in output
        assert "Unregistered inventory: inventory_failed; outcomes unavailable." in output


@pytest.mark.parametrize("outcome", ["failed", "incomplete"])
def test_inventory_failure_keeps_registered_failure_counts_and_exit_precedence(
    monkeypatch, outcome
):
    output, error, _, _ = _invoke(
        monkeypatch, outcomes=(outcome, "eligible"), inventory_error=OSError(), max_candidates=1
    )

    report = json.loads(output)
    assert error is not None and error.returncode == 1
    assert report["counts"][outcome] == 1
    assert report["counts"]["retained"] == 1
    assert report["counts"]["processed"] == 1
    assert report["has_more"] is True and report["next_cursor"]
    assert report["inventory_error"] == "inventory_failed"
    assert "unregistered_inventory" not in report


@pytest.mark.parametrize("discover", [False, True])
def test_successful_and_unrequested_inventory_keep_existing_report_fields(monkeypatch, discover):
    inventory = _successful_inventory() if discover else None
    output, error, _, calls = _invoke(monkeypatch, discover=discover, inventory_report=inventory)

    report = json.loads(output)
    assert error is None
    assert set(report) == _REPORT_KEYS | ({"unregistered_inventory"} if discover else set())
    assert report["schema_version"] == 1
    assert report["counts"]["purged"] == 1
    assert len(calls) == int(discover)
    if discover:
        assert report["unregistered_inventory"] == inventory.as_dict()


@pytest.mark.parametrize("output_format", ["json", "text"])
def test_partial_inventory_report_includes_error_within_existing_output_budget(
    monkeypatch, output_format
):
    references = [f"{index:04}" + "\U0001f600" * 496 for index in range(1001)]
    output, error, _, _ = _invoke(
        monkeypatch,
        outcomes=("purged",) * 1001,
        inventory_error=OSError(_PRIVATE_DETAIL * 10000),
        output_format=output_format,
        references=references,
        max_candidates=1000,
        max_details=100,
    )

    assert error is not None and error.returncode == 1
    # Reserve the same stderr/newline margin as the existing command contract.
    assert len(output.encode()) + len(str(error).encode()) + 20 <= purge_command.OUTPUT_MAX_BYTES
    assert _PRIVATE_DETAIL not in output + str(error)
    if output_format == "json":
        report = json.loads(output)
        assert report["counts"]["purged"] == 1000
        assert len(report["details"]) <= 100
        assert report["details_omitted"] == 1000 - len(report["details"])
        assert decode_purge_cursor(report["next_cursor"]).after == references[999]


def test_inventory_failure_never_renders_exception_values(monkeypatch):
    class UnrenderableError(Exception):
        def __str__(self):
            pytest.fail("Inventory provider exceptions must not be rendered")

    output, error, _, _ = _invoke(monkeypatch, inventory_error=UnrenderableError())

    assert error is not None and error.returncode == 1
    assert json.loads(output)["inventory_error"] == "inventory_failed"
    assert "UnrenderableError" not in output + str(error)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_inventory_interruption_still_propagates(monkeypatch, interruption):
    with pytest.raises(interruption):
        _invoke(monkeypatch, inventory_error=interruption())
