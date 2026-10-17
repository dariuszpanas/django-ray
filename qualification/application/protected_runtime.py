"""Bounded private comparisons for the fixed RuntimeEnv qualification rows."""

from __future__ import annotations

import json
from datetime import UTC, datetime

MAX_BODY = 64 * 1024
MAX_BODIES = 128
MAX_TOTAL = 4 * 1024 * 1024
MARKERS = (
    "django-ray-runtime-env-encryption-canary-v1-7c4e2a91",
    "qualification-unavailable",
)


class Capture:
    """Keep only bounded existing response bodies, never headers or diagnostics."""

    def __init__(self, request):
        self.request = request
        self.bodies: list[bytes] = []
        self.total = 0
        self.started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def __call__(self, *args, **kwargs):
        status, body = self.request(*args, **kwargs)
        if not isinstance(body, bytes) or len(body) > MAX_BODY:
            raise ValueError("Protected HTTP observation exceeds its body bound")
        self.total += len(body)
        if self.total > MAX_TOTAL or len(self.bodies) >= MAX_BODIES:
            raise ValueError("Protected HTTP observation exceeds its aggregate bound")
        self.bodies.append(body)
        return status, body

    def finish(self, row_ids: list[int]) -> dict:
        if not self.bodies:
            raise ValueError("Protected HTTP observation has no responses")
        values = protected_values(row_ids)
        assert_clear(self.bodies, values)
        self.bodies.clear()
        return {"api_clear": True, "row_ids": row_ids, "started_at": self.started_at}


def validate(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"api_clear", "row_ids", "started_at"}:
        raise ValueError("Protected RuntimeEnv evidence has an invalid shape")
    ids = value["row_ids"]
    if (
        value["api_clear"] is not True
        or not isinstance(ids, list)
        or len(ids) != 3
        or any(type(pk) is not int or pk <= 0 for pk in ids)
        or len(set(ids)) != 3
    ):
        raise ValueError("Protected RuntimeEnv evidence has invalid owned rows")
    stamp = value["started_at"]
    if not isinstance(stamp, str) or len(stamp) > 40 or not stamp.endswith("Z"):
        raise ValueError("Protected RuntimeEnv evidence has no bounded timestamp")
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo != UTC:
        raise ValueError("Protected RuntimeEnv evidence has no UTC timestamp")


def protected_values(row_ids: list[int]) -> list[str]:
    from django.db.models import BooleanField, Case, F, Func, IntegerField, TextField, Value, When

    from django_ray.models import RayTaskExecution

    class ByteLength(Func):
        function = "OCTET_LENGTH"
        output_field = IntegerField()

        def as_sqlite(self, compiler, connection, **extra_context):
            return self.as_sql(
                compiler,
                connection,
                template="LENGTH(CAST(%(expressions)s AS BLOB))",
                **extra_context,
            )

    validate_row_ids(row_ids)
    rows = list(
        RayTaskExecution.objects.filter(pk__in=row_ids)
        .annotate(
            protected_size=ByteLength("runtime_env_json"),
            protected_body=Case(
                When(protected_size__lte=MAX_BODY, then=F("runtime_env_json")),
                default=Value(None),
                output_field=TextField(),
            ),
            fixture_bound=Case(
                When(
                    runtime_env_profile="thin",
                    callable_path="testproject.apps.cluster_tasks.tasks.runtime_env_probe",
                    then=Value(True),
                ),
                default=Value(False),
                output_field=BooleanField(),
            ),
            succeeded=Case(
                When(state="SUCCEEDED", then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            ),
            failed=Case(
                When(state="FAILED", then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            ),
        )
        .values_list("id", "protected_body", "fixture_bound", "succeeded", "failed")
    )
    return protected_values_from_rows(row_ids, rows)


def validate_row_ids(row_ids: list[int]) -> None:
    if (
        not isinstance(row_ids, list)
        or len(row_ids) != 3
        or any(type(pk) is not int or pk <= 0 for pk in row_ids)
        or len(set(row_ids)) != 3
    ):
        raise ValueError("Protected RuntimeEnv observation requires exactly three owned rows")


def protected_values_from_rows(row_ids: list[int], rows: list[tuple]) -> list[str]:
    """Validate an already bounded projection without loading Django or its settings."""
    validate_row_ids(row_ids)
    if len(rows) != 3 or any(len(row) != 5 for row in rows):
        raise ValueError("Protected RuntimeEnv observation lost an owned row")
    by_id = {row[0]: row for row in rows}
    if set(by_id) != set(row_ids):
        raise ValueError("Protected RuntimeEnv observation lost an owned row")
    values = list(MARKERS)
    for index, pk in enumerate(row_ids):
        _, serialized, bound, succeeded, failed = by_id[pk]
        if not bound or not (succeeded if index == 0 else failed):
            raise ValueError("Protected RuntimeEnv row is outside the fixed fixture")
        if not isinstance(serialized, str) or len(serialized.encode()) > MAX_BODY:
            raise ValueError("Protected RuntimeEnv envelope is missing or oversized")
        envelope = json.loads(serialized)
        if not isinstance(envelope, dict):
            raise ValueError("Protected RuntimeEnv envelope has no required component")
        for name in ("nonce", "ciphertext"):
            if not isinstance(envelope.get(name), str) or not envelope[name]:
                raise ValueError("Protected RuntimeEnv envelope has no required component")
        values += [serialized, envelope["nonce"], envelope["ciphertext"]]
    return values


def assert_clear(surfaces: list[bytes], values: list[str]) -> None:
    if not values or any(not value for value in values):
        raise ValueError("Protected surface observation has no comparison values")
    if any(value.encode() in surface for value in values for surface in surfaces):
        raise ValueError("Protected surface observation failed")


def refused_task_ids(row_ids: list[int]) -> list[str]:
    from django.db.models import BooleanField, Case, F, Value, When
    from django.db.models.functions import Substr

    from django_ray.models import RayTaskExecution

    validate_row_ids(row_ids)
    # Raw equality also rejects SQLite NUL suffixes hidden by SUBSTR.
    rows = list(
        RayTaskExecution.objects.filter(pk__in=row_ids[1:], state="FAILED")
        .annotate(
            bounded_task=Substr("task_id", 1, 37),
            complete_task=Case(
                When(task_id=F("bounded_task"), then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            ),
        )
        .values_list("pk", "bounded_task", "complete_task")
    )
    return refused_task_ids_from_rows(row_ids, rows)


def refused_task_ids_from_rows(row_ids: list[int], rows: list[tuple]) -> list[str]:
    """Keep task identity checks shared by the ORM and private database observers."""
    from uuid import UUID

    validate_row_ids(row_ids)
    if len(rows) != 2 or any(len(row) != 3 for row in rows):
        raise ValueError("Protected status observation lost a refused task")
    if any(not complete for _, _, complete in rows):
        raise ValueError("Protected status observation has an incomplete task identity")
    found = {pk: value for pk, value, _ in rows}
    if len(row_ids) != 3 or set(found) != set(row_ids[1:]):
        raise ValueError("Protected status observation lost a refused task")
    tasks = [found[pk] for pk in row_ids[1:]]
    if any(
        not isinstance(value, str)
        or len(value) != 36
        or str(UUID(value)) != value
        or UUID(value).version != 4
        for value in tasks
    ):
        raise ValueError("Protected status observation has an invalid task identity")
    return tasks
