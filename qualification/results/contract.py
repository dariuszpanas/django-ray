"""Exact cold-process cases and bounded result-read evidence."""

from __future__ import annotations

from xml.etree import ElementTree

from qualification.docker.scenario import QualificationError

CASES = ("unimported", "module_attribute", "removed_task")
WORKLOAD = "historical-result-reads"
DEFINITION_PATH = "qualification/results/reads.yaml"
MAX_PROBE_BYTES = 16 * 1024
_ASSERTIONS = (
    "stored-identity",
    "sync-and-async-reads",
    "sync-and-async-refresh",
    "no-import-or-attribute-effects",
    "read-only-execution",
    "copy-pickle-refusal",
    "no-read-triggered-submissions",
    "current-task-enqueue",
    "database-connections-closed",
)


def assertions_for(kind: str) -> list[str]:
    if kind not in CASES:
        raise QualificationError("unknown-result-read-case")
    extra = ("matching-task-identity",) if kind == "removed_task" else ()
    return sorted((*_ASSERTIONS, *extra))


def validate_probe(value: object, *, kind: str, expected_module: str) -> dict:
    """No missing case, identity, assertion or refusal can become success."""
    keys = {
        "schema_version",
        "kind",
        "status",
        "module_location",
        "assertions",
        "refused_operations",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise QualificationError("invalid-result-read-receipt")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["kind"] != kind
        or value["status"] != "passed"
        or value["module_location"] != expected_module
        or value["assertions"] != assertions_for(kind)
        or type(value["refused_operations"]) is not int
        or value["refused_operations"] != 28
    ):
        raise QualificationError("result-read-proof-mismatch")
    return value


def junit(receipts: list[dict], *, expected_module: str, failure: str | None) -> bytes:
    """Require the complete fixed case set before emitting successful JUnit."""
    suite = ElementTree.Element(
        "testsuite",
        name=WORKLOAD,
        tests="1" if failure else str(len(CASES)),
        failures="1" if failure else "0",
        errors="0",
        skipped="0",
    )
    if failure is not None:
        case = ElementTree.SubElement(suite, "testcase", classname=WORKLOAD, name="contract")
        ElementTree.SubElement(case, "failure", message=failure)
    else:
        if len(receipts) != len(CASES):
            raise QualificationError("missing-result-read-case")
        for kind, receipt in zip(CASES, receipts, strict=True):
            validate_probe(receipt, kind=kind, expected_module=expected_module)
            ElementTree.SubElement(suite, "testcase", classname=WORKLOAD, name=kind)
    return ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n"
