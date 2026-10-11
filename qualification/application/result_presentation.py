"""Observe fixed ordinary result fixtures through the disposable HTTP service."""

from __future__ import annotations

import html
import json
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from html.parser import HTMLParser
from uuid import uuid4

from qualification.application.http_context import HttpObserver
from qualification.application.run_api import ApplicationHttp
from qualification.application.workflow_admin import FIXTURE_ORIGIN
from qualification.application.workflow_session import qualification_admin_session

OBSERVATION_SECONDS = 30
HTML_MAX_BYTES = 1024 * 1024
API_MAX_BYTES = 64 * 1024
CANARY = "djangoRayOrdinaryResultCanary"
SCOPES = ("current", "archived")
EVIDENCE = {
    "schema_version": 1,
    "current_admin": True,
    "archived_admin": True,
    "current_api": True,
    "anonymous_denied": True,
    "html_escaped_and_inert": True,
    "sensitive_values_redacted": True,
    "raw_fields_preserved": True,
    "owned_rows_removed": True,
    "owned_session_removed": True,
}


def validate_evidence(value: object) -> None:
    """Require every fixed observation; reject truthy substitutes or extra data."""
    if (
        not isinstance(value, dict)
        or value.keys() != EVIDENCE.keys()
        or any(
            type(value[key]) is not type(expected) or value[key] != expected
            for key, expected in EVIDENCE.items()
        )
    ):
        raise ValueError("Ordinary result evidence is incomplete")


def fixture_payload(scope: str) -> dict[str, str]:
    if scope not in SCOPES:
        raise ValueError("Unknown ordinary result fixture")
    return {
        "markup": (
            f'<script data-result="{scope}">window.{CANARY}=1</script>'
            f'<img data-result="{scope}" src="data:," onerror="window.{CANARY}=1">'
        ),
        "password": f"ordinary-result-{scope}-secret-canary",
    }


class _InertHTML(HTMLParser):
    """Inspect actual server HTML, without claiming browser execution evidence."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.executable_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if any(
            CANARY in name or CANARY in (value or "") or name == "data-result"
            for name, value in attrs
        ):
            raise ValueError("Ordinary result introduced an HTML attribute")
        if tag in {"script", "style"}:
            self.executable_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.executable_depth = max(0, self.executable_depth - 1)

    def handle_data(self, data: str) -> None:
        if self.executable_depth and CANARY in data:
            raise ValueError("Ordinary result introduced executable HTML")
        self.text.append(data)


def validate_html(body: bytes, scope: str) -> None:
    payload = fixture_payload(scope)
    if len(body) > HTML_MAX_BYTES:
        raise ValueError("Ordinary result HTML exceeded its bound")
    source = body.decode("utf-8")
    parser = _InertHTML()
    parser.feed(source)
    parser.close()
    rendered = "".join(parser.text)
    # JSON retains quote escapes inside its string. Django then HTML-escapes
    # that ordinary string; require the exact fixture rather than any marker.
    expected = json.dumps({"markup": payload["markup"], "<redacted>": "[REDACTED]"})
    if (
        expected not in rendered
        or html.escape(expected) not in source
        or any(fixture_payload(item)["password"] in source for item in SCOPES)
    ):
        raise ValueError("Ordinary result HTML did not preserve its inert projection")


def validate_api(body: bytes, *, execution_pk: int, task_id: str) -> None:
    if len(body) > API_MAX_BYTES:
        raise ValueError("Ordinary result API exceeded its bound")
    value = json.loads(body)
    expected = {"markup": fixture_payload("current")["markup"], "<redacted>": "[REDACTED]"}
    if (
        not isinstance(value, dict)
        or type(value.get("id")) is not int
        or value["id"] != execution_pk
        or value.get("task_id") != task_id
        or value.get("state") != "SUCCEEDED"
        or value.get("result_data_omission_reason") is not None
        or type(value.get("result_data")) is not str
        or json.loads(value["result_data"]) != expected
        or any(fixture_payload(item)["password"].encode() in body for item in SCOPES)
    ):
        raise ValueError("Ordinary result API did not preserve its redacted projection")


@contextmanager
def _owned_results() -> Generator[tuple[int, int, str], None, None]:
    from django.db import transaction

    from django_ray.models import RayTaskExecution, TaskAttempt

    execution_pk = None
    task_id = str(uuid4())
    before = None
    attempt_pk = None
    try:
        with transaction.atomic():
            execution = RayTaskExecution.objects.create(
                task_id=task_id,
                callable_path="qualification.application.result_presentation.fixture",
                queue_name="qualification-result-presentation",
                state="SUCCEEDED",
                attempt_number=2,
                result_data=json.dumps(fixture_payload("current")),
            )
            execution_pk = execution.pk
            attempt = TaskAttempt.objects.create(
                execution=execution,
                attempt_number=1,
                state="SUCCEEDED",
                result_data=json.dumps(fixture_payload("archived")),
            )
        attempt_pk = attempt.pk
        before = _raw_fields(execution_pk, attempt_pk, task_id)
        yield execution_pk, attempt_pk, task_id
    finally:
        if execution_pk is not None:
            try:
                if before is not None and _raw_fields(execution_pk, attempt_pk, task_id) != before:
                    raise ValueError("Ordinary result observation changed stored fields")
            finally:
                # The opaque task identity also protects rollback paths where
                # a database may reuse an uncommitted numeric primary key.
                owned = RayTaskExecution.objects.filter(pk=execution_pk, task_id=task_id)
                owned.delete()
                if (
                    owned.exists()
                    or TaskAttempt.objects.filter(execution__task_id=task_id).exists()
                ):
                    raise ValueError("Ordinary result fixture cleanup was not observed")


def _raw_fields(execution_pk: int, attempt_pk: int | None, task_id: str) -> tuple:
    from django_ray.models import RayTaskExecution, TaskAttempt

    # Only two newly owned rows; compare every column without exporting values.
    return (
        RayTaskExecution.objects.values().get(pk=execution_pk, task_id=task_id),
        TaskAttempt.objects.values().get(
            pk=attempt_pk, execution_id=execution_pk, execution__task_id=task_id
        ),
    )


def observe_result_presentation(*, token: str) -> dict[str, object]:
    """Use a finite observation budget inside the existing outer process limit.

    ORM work and cleanup are not individually interrupted by this budget. The
    generation supervisor retains the existing 600-second hard process budget.
    """
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification":
        raise ValueError("Ordinary result observation requires disposable settings")
    deadline = time.monotonic() + OBSERVATION_SECONDS

    def read(path: str, *, headers: dict[str, str], status: int, is_html: bool) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Ordinary result observation budget expired")
        request = ApplicationHttp(
            FIXTURE_ORIGIN,
            request_timeout=min(5, remaining),
            observer=HttpObserver.CORE,
            observer_deadline=deadline,
        )
        actual, body = request(
            path,
            method="GET",
            headers=headers,
            response_limit=HTML_MAX_BYTES if is_html else API_MAX_BYTES,
            required_media_types=frozenset({"text/html" if is_html else "application/json"}),
            required_response_headers=(
                {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
                if status == 200
                else {"Location": f"/admin/login/?next={path}"}
                if is_html
                else None
            ),
        )
        if actual != status or time.monotonic() > deadline:
            raise ValueError("Ordinary result HTTP status or budget did not match")
        if status != 200 and (
            CANARY.encode() in body
            or any(fixture_payload(item)["password"].encode() in body for item in SCOPES)
        ):
            raise ValueError("Anonymous response exposed ordinary result fixture data")
        return body

    with _owned_results() as (execution_pk, attempt_pk, task_id):
        paths = (
            (f"/admin/django_ray/raytaskexecution/{execution_pk}/change/", "current"),
            (f"/admin/django_ray/taskattempt/{attempt_pk}/change/", "archived"),
        )
        api_path = f"/api/executions/{execution_pk}"
        for path, _scope in paths:
            read(path, headers={}, status=302, is_html=True)
        read(api_path, headers={}, status=401, is_html=False)
        with qualification_admin_session() as cookie:
            for path, scope in paths:
                validate_html(
                    read(path, headers={"Cookie": cookie}, status=200, is_html=True), scope
                )
            validate_api(
                read(
                    api_path,
                    headers={"Authorization": f"Bearer {token}"},
                    status=200,
                    is_html=False,
                ),
                execution_pk=execution_pk,
                task_id=task_id,
            )
    if time.monotonic() > deadline:
        raise ValueError("Ordinary result observation budget expired during cleanup")
    evidence: dict[str, object] = dict(EVIDENCE)
    validate_evidence(evidence)
    return evidence
