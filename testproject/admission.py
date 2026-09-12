"""Sample-only admission; Django remains the durable enqueue boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, DatabaseError, connection, router, transaction
from django.db.models import F
from django.utils import timezone

from django_ray.models import RayTaskExecution, TaskInputPayload, TaskState
from testproject.models import SampleAdmissionBudget
from testproject.workload_limits import integer, validate_call

REQUEST_WINDOW_SECONDS = 60
MAX_REQUESTS_PER_WINDOW = 30
MAX_OUTSTANDING_EXECUTIONS = 32


class SampleInputError(ValueError):
    """A rejected input never reaches durable enqueue."""


class SampleAdmissionError(Exception):
    def __init__(self, *, rate_limited: bool = False) -> None:
        self.status = 429 if rate_limited else 503
        self.code = "ADMISSION_LIMITED" if rate_limited else "ADMISSION_UNAVAILABLE"
        self.retry_after = "60" if rate_limited else "5"
        super().__init__(self.code)


def enqueue_sample(task: Any, *args: Any, **kwargs: Any) -> Any:
    """Serialize admission and enqueue in one transaction on SQLite/PostgreSQL.

    The first statement is a write on the seeded singleton. Unlike a read then
    SELECT FOR UPDATE, this also serializes SQLite connections before counting.
    Lock contention fails closed with a retryable response. This is a sample
    request/outstanding-work ceiling, not a scheduler or a library-wide quota.
    """
    try:
        validate_call(task.func, args, kwargs)
        queues = settings.TASKS[task.backend].get("QUEUES", ())
        if task.queue_name not in queues:
            raise ValueError("Unsupported sample queue")
    except (TypeError, ValueError, KeyError) as error:
        raise SampleInputError("Sample input exceeds its supported bounds") from error
    return admit_sample(task.enqueue, *args, **kwargs)


def admit_sample(operation: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Admit enqueue or exact-ID retry without changing lifecycle fencing."""
    if connection.vendor not in {"sqlite", "postgresql"}:
        raise SampleAdmissionError
    if any(
        route(model) != DEFAULT_DB_ALIAS
        for model in (SampleAdmissionBudget, RayTaskExecution, TaskInputPayload)
        for route in (router.db_for_read, router.db_for_write)
    ):
        raise SampleAdmissionError
    request_limit = getattr(settings, "SAMPLE_MAX_REQUESTS_PER_WINDOW", MAX_REQUESTS_PER_WINDOW)
    outstanding_limit = getattr(
        settings, "SAMPLE_MAX_OUTSTANDING_EXECUTIONS", MAX_OUTSTANDING_EXECUTIONS
    )
    try:
        integer(request_limit, 1, MAX_REQUESTS_PER_WINDOW)
        integer(outstanding_limit, 1, MAX_OUTSTANDING_EXECUTIONS)
    except ValueError as error:
        raise SampleAdmissionError from error
    try:
        with transaction.atomic(using=DEFAULT_DB_ALIAS):
            if (
                SampleAdmissionBudget.objects.using(DEFAULT_DB_ALIAS)
                .filter(pk=1)
                .update(request_count=F("request_count"))
                != 1
            ):
                raise SampleAdmissionError
            budget = SampleAdmissionBudget.objects.using(DEFAULT_DB_ALIAS).get(pk=1)
            now = timezone.now()
            if budget.window_started_at > now or budget.window_started_at <= now - timedelta(
                seconds=REQUEST_WINDOW_SECONDS
            ):
                budget.window_started_at = now
                budget.request_count = 0
            if budget.request_count >= request_limit:
                raise SampleAdmissionError(rate_limited=True)
            outstanding = (
                RayTaskExecution.objects.using(DEFAULT_DB_ALIAS)
                .filter(
                    state__in=(TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING),
                )
                .values_list("pk", flat=True)[:outstanding_limit]
            )
            if len(outstanding) >= outstanding_limit:
                raise SampleAdmissionError
            budget.request_count += 1
            budget.save(
                using=DEFAULT_DB_ALIAS, update_fields=("request_count", "window_started_at")
            )
            return operation(*args, **kwargs)
    except DatabaseError as error:
        raise SampleAdmissionError from error
