"""Current-model contract for durable Ray Job request references."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from django_ray.models import (
    InputPayloadKind,
    RayTaskExecution,
    TaskInputPayload,
)


@pytest.mark.django_db
def test_request_reference_and_payload_kind_model_contract() -> None:
    request_field = RayTaskExecution._meta.get_field("ray_job_request_reference")
    assert request_field.max_length == 500
    assert request_field.null is True
    assert request_field.blank is True
    assert request_field.db_index is True
    assert request_field.remote_field is None

    kind_field = TaskInputPayload._meta.get_field("payload_kind")
    assert kind_field.max_length == 32
    assert kind_field.default == InputPayloadKind.TASK_INPUT
    assert kind_field.db_default == InputPayloadKind.TASK_INPUT
    assert dict(kind_field.choices) == {
        InputPayloadKind.TASK_INPUT: "Task input",
        InputPayloadKind.RAY_JOB_REQUEST: "Ray Job request",
    }

    task_input = TaskInputPayload.objects.create(
        reference="inputfs://sha256/model-default?bytes=32",
        backend="filesystem",
        digest="a" * 64,
        size_bytes=32,
        envelope_version=1,
    )
    assert task_input.payload_kind == InputPayloadKind.TASK_INPUT
    assert str(task_input) == "filesystem Task input aaaaaaaaaaaa (ACTIVE)"

    request_payload = TaskInputPayload.objects.create(
        reference="requestfs://sha256/model-request?bytes=64",
        payload_kind=InputPayloadKind.RAY_JOB_REQUEST,
        backend="filesystem",
        digest="b" * 64,
        size_bytes=64,
        envelope_version=1,
    )
    assert request_payload.payload_kind == InputPayloadKind.RAY_JOB_REQUEST
    assert str(request_payload) == "filesystem Ray Job request bbbbbbbbbbbb (ACTIVE)"

    execution = RayTaskExecution.objects.create(
        task_id="request-reference-model-contract",
        callable_path="testproject.tasks.add_numbers",
        ray_job_request_reference=request_payload.reference,
    )
    assert execution.ray_job_id is None
    assert execution.ray_job_request_reference == request_payload.reference

    with pytest.raises(IntegrityError), transaction.atomic():
        TaskInputPayload.objects.filter(pk=task_input.pk).update(payload_kind="unknown")
