"""Shared identity assertions for public and local workflow qualification.

These assertions validate a bounded decoded response. The caller owns HTTP
byte limits, authentication, source identity, execution and cleanup evidence.
"""

from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID


def _mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return value


def validate_workflow_envelope(
    payload: Mapping[str, Any],
    *,
    task_id: str,
    endpoint: str,
    schema: str,
    expected_run_identity: Mapping[str, Any] | None = None,
    expected_publication: Mapping[str, Any] | None = None,
    expected_availability: str = "AVAILABLE",
    expected_complete: bool = True,
    expect_detail_revisions: bool = True,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Validate common bounded-reader identity without retaining response payloads."""

    if (
        payload.get("schema") != schema
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
    ):
        raise ValueError(f"{endpoint} returned an unsupported read envelope")
    if payload.get("task_id") != task_id:
        raise ValueError(f"{endpoint} returned the wrong workflow task")
    if (
        payload.get("availability") != expected_availability
        or payload.get("complete") is not expected_complete
    ):
        raise ValueError(f"{endpoint} returned the wrong workflow detail availability")

    run_identity = _mapping(
        payload.get("run_identity"),
        field_name=f"{endpoint} run_identity",
    )
    if set(run_identity) != {
        "schema_version",
        "run_id",
        "attempt_number",
        "execution_generation",
    }:
        raise ValueError(f"{endpoint} returned an invalid public run identity")
    run_id = run_identity.get("run_id")
    try:
        parsed_run_id = UUID(cast(str, run_id))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{endpoint} returned an invalid workflow run UUID") from error
    if (
        type(run_identity.get("schema_version")) is not int
        or run_identity.get("schema_version") != 1
        or str(parsed_run_id) != run_id
        or type(run_identity.get("attempt_number")) is not int
        or cast(int, run_identity["attempt_number"]) < 1
        or type(run_identity.get("execution_generation")) is not int
        or cast(int, run_identity["execution_generation"]) < 0
    ):
        raise ValueError(f"{endpoint} returned an invalid public run identity")
    if expected_run_identity is not None and run_identity != expected_run_identity:
        raise ValueError(f"{endpoint} returned a different workflow run")

    publication = _mapping(
        payload.get("publication"),
        field_name=f"{endpoint} publication",
    )
    if set(publication) != {
        "summary_revision",
        "topology_version",
        "detail_revision",
    }:
        raise ValueError(f"{endpoint} returned invalid publication revisions")
    summary_revision = publication.get("summary_revision")
    topology_version = publication.get("topology_version")
    detail_revision = publication.get("detail_revision")
    if type(summary_revision) is not int or cast(int, summary_revision) < 1:
        raise ValueError(f"{endpoint} returned invalid publication revisions")
    if expect_detail_revisions:
        if (
            type(topology_version) is not int
            or cast(int, topology_version) < 1
            or type(detail_revision) is not int
            or cast(int, detail_revision) < 1
        ):
            raise ValueError(f"{endpoint} returned invalid publication revisions")
    elif topology_version is not None or detail_revision is not None:
        raise ValueError(f"{endpoint} unexpectedly advertised retained workflow detail")
    if expected_publication is not None and publication != expected_publication:
        raise ValueError(f"{endpoint} returned a different workflow publication")
    return run_identity, publication
