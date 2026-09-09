"""The literal fanout selection and its bounded, fail-closed result contract."""

from __future__ import annotations

import math
from xml.etree import ElementTree

from qualification.docker.scenario import QualificationError

DEFINITION_PATH = "qualification/runtime/fanout.yaml"
WORKLOAD = "fanout-real-ray"
TEST_PATH = "tests/unit/test_distributed.py"
PYTEST_ARGUMENTS = (TEST_PATH, "-m", "real_ray")
REQUIRED_SELECTORS = (
    f"{TEST_PATH}::TestDistributedWithRay::test_parallel_map_with_ray",
    f"{TEST_PATH}::TestDistributedWithRay::test_parallel_map_repeated_calls_reuse_cached_remote",
    f"{TEST_PATH}::TestDistributedWithRay::test_strict_parallel_map_round_trip_installs_exact_context",
    f"{TEST_PATH}::TestDistributedWithRay::test_strict_rejection_survives_ray_without_invoking_callable[protocol-protocol_mismatch]",
    f"{TEST_PATH}::TestDistributedWithRay::test_strict_rejection_survives_ray_without_invoking_callable[callable-callable_mismatch]",
    f"{TEST_PATH}::TestDistributedWithRay::test_get_ray_resources_with_ray",
    f"{TEST_PATH}::test_nested_rejection_reaches_outer_enriched_completion",
)
MAX_TESTS = 64
MAX_RECEIPT_BYTES = 128 * 1024
PHASES = ("setup", "call", "teardown")


def validate_selection(nodes: object) -> list[str]:
    """Require unique in-scope identities and every baseline assertion family."""
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_TESTS:
        raise QualificationError("invalid-runtime-selection")
    if any(
        not isinstance(node, str)
        or not node.startswith(TEST_PATH + "::")
        or not 1 <= len(node.encode("utf-8")) <= 1024
        or any(ord(char) < 32 or ord(char) == 127 for char in node)
        for node in nodes
    ):
        raise QualificationError("invalid-runtime-test-identity")
    if nodes != sorted(set(nodes)):
        raise QualificationError("duplicate-or-unsorted-runtime-selection")
    for selector in REQUIRED_SELECTORS:
        if selector not in nodes:
            raise QualificationError("missing-required-runtime-assertion")
    return nodes


def validate_receipt(value: object) -> dict:
    """Success requires exact selection, all three phases and clean Ray teardown."""
    keys = {"schema_version", "selected", "reports", "exit_code", "ray_shutdown", "errors"}
    if not isinstance(value, dict) or set(value) != keys:
        raise QualificationError("invalid-runtime-receipt")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise QualificationError("invalid-runtime-receipt-version")
    if type(value["exit_code"]) is not int or value["exit_code"] != 0:
        raise QualificationError("runtime-tests-failed")
    if value["ray_shutdown"] is not True or value["errors"] != []:
        raise QualificationError("runtime-teardown-or-reporting-failed")
    nodes = validate_selection(value["selected"])
    reports = value["reports"]
    if not isinstance(reports, dict) or set(reports) != set(nodes):
        raise QualificationError("runtime-execution-identity-mismatch")
    for phases in reports.values():
        if not isinstance(phases, dict) or set(phases) != set(PHASES):
            raise QualificationError("missing-runtime-phase")
        for report in phases.values():
            if not isinstance(report, dict) or set(report) != {"outcome", "seconds"}:
                raise QualificationError("invalid-runtime-phase")
            seconds = report["seconds"]
            if report["outcome"] != "passed":
                raise QualificationError("required-runtime-phase-did-not-pass")
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
                raise QualificationError("invalid-runtime-duration")
    return value


def success_junit(receipt: dict) -> bytes:
    """Project verified pytest observations into deterministic JUnit identities."""
    receipt = validate_receipt(receipt)
    suite = ElementTree.Element(
        "testsuite",
        name=WORKLOAD,
        tests=str(len(receipt["selected"])),
        failures="0",
        errors="0",
        skipped="0",
    )
    for node in receipt["selected"]:
        path, *owners, name = node.split("::")
        classname = ".".join((path.removesuffix(".py").replace("/", "."), *owners))
        ElementTree.SubElement(suite, "testcase", classname=classname, name=name)
    return ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n"
