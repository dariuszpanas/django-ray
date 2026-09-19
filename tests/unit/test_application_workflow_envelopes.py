"""Reject identity substitution and false completeness in workflow qualification."""

from copy import deepcopy

import pytest

from qualification.application.workflow_envelopes import validate_workflow_envelope

SCHEMA = "django-ray.workflow-progress-summary"
IDENTITY = {
    "schema_version": 1,
    "run_id": "00000000-0000-0000-0000-000000000219",
    "attempt_number": 2,
    "execution_generation": 3,
}
PUBLICATION = {"summary_revision": 4, "topology_version": 2, "detail_revision": 3}


@pytest.fixture
def envelope():
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "task_id": "expected-task",
        "availability": "AVAILABLE",
        "complete": True,
        "run_identity": deepcopy(IDENTITY),
        "publication": deepcopy(PUBLICATION),
    }


def validate(payload, **kwargs):
    return validate_workflow_envelope(
        payload,
        task_id="expected-task",
        endpoint="workflow summary",
        schema=SCHEMA,
        **kwargs,
    )


def test_accepts_exact_run_and_publication(envelope):
    assert validate(envelope, expected_run_identity=IDENTITY, expected_publication=PUBLICATION) == (
        IDENTITY,
        PUBLICATION,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "other-task"),
        ("schema", "other-schema"),
        ("schema_version", True),
        ("schema_version", 1.0),
        ("schema_version", 2),
        ("availability", "MISSING"),
        ("complete", False),
        ("complete", 1),
        ("run_identity", []),
        ("publication", None),
    ],
)
def test_rejects_wrong_or_incomplete_envelope(envelope, field, value):
    envelope[field] = value
    with pytest.raises(ValueError):
        validate(envelope)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("attempt_number", True),
        ("attempt_number", 0),
        ("execution_generation", False),
        ("execution_generation", -1),
        ("run_id", None),
        ("run_id", "not-a-uuid"),
        ("run_id", "00000000000000000000000000000219"),
        ("extra", "unexpected"),
    ],
)
def test_rejects_malformed_run_identity(envelope, field, value):
    envelope["run_identity"][field] = value
    with pytest.raises(ValueError):
        validate(envelope)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "00000000-0000-0000-0000-000000000220"),
        ("attempt_number", 3),
        ("execution_generation", 4),
    ],
)
def test_rejects_other_valid_attempt_identity(envelope, field, value):
    envelope["run_identity"][field] = value
    with pytest.raises(ValueError, match="different workflow run"):
        validate(envelope, expected_run_identity=IDENTITY)


@pytest.mark.parametrize("field", PUBLICATION)
@pytest.mark.parametrize("value", [None, True, 0, -1, 1.5])
def test_rejects_invalid_publication_revision(envelope, field, value):
    envelope["publication"][field] = value
    with pytest.raises(ValueError):
        validate(envelope)


@pytest.mark.parametrize("field", PUBLICATION)
def test_rejects_other_valid_publication(envelope, field):
    envelope["publication"][field] += 1
    with pytest.raises(ValueError, match="different workflow publication"):
        validate(envelope, expected_publication=PUBLICATION)


def test_terminal_only_has_no_detail_revisions(envelope):
    envelope["availability"] = "OMITTED_BY_POLICY"
    envelope["publication"].update(topology_version=None, detail_revision=None)
    validate(envelope, expected_availability="OMITTED_BY_POLICY", expect_detail_revisions=False)
    envelope["publication"]["detail_revision"] = 1
    with pytest.raises(ValueError, match="unexpectedly advertised"):
        validate(
            envelope,
            expected_availability="OMITTED_BY_POLICY",
            expect_detail_revisions=False,
        )


def test_rejects_extra_publication_fields(envelope):
    envelope["publication"]["extra"] = 1
    with pytest.raises(ValueError, match="invalid publication"):
        validate(envelope)
