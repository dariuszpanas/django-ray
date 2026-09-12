"""Credential separation and bounded parsing for the executable sample API."""

from __future__ import annotations

import json
import secrets
from typing import Any

from django.conf import settings
from ninja import NinjaAPI
from ninja.parser import Parser
from ninja.security import HttpBearer

from testproject.admission import SampleInputError
from testproject.route_policy import is_demo_route
from testproject.workload_limits import MAX_BODY_BYTES, MAX_JSON_DEPTH, validate_json


def demo_enabled() -> bool:
    return (
        getattr(settings, "DEPLOYMENT_MODE", "demo") == "demo"
        and getattr(settings, "DJANGO_DEMO_WORKLOADS_ENABLED", False) is True
        and bool(getattr(settings, "DJANGO_DEMO_TOKEN", None))
    )


class ApiTokenAuth(HttpBearer):
    def authenticate(self, request: Any, token: str) -> str | None:
        if not getattr(settings, "DJANGO_API_ENABLED", True):
            return None
        if is_demo_route(request.path_info):
            expected = getattr(settings, "DJANGO_DEMO_TOKEN", None) if demo_enabled() else None
            identity = "django-ray-testproject-demo"
        else:
            expected = getattr(settings, "DJANGO_API_TOKEN", None)
            identity = "django-ray-testproject-operator"
        if expected and secrets.compare_digest(token, expected):
            if request.method == "POST":
                _bound_request(request)
            return identity
        return None


class MetricsTokenAuth(HttpBearer):
    def authenticate(self, request: Any, token: str) -> str | None:
        expected = getattr(settings, "DJANGO_METRICS_TOKEN", None)
        if expected and secrets.compare_digest(token, expected):
            return "django-ray-testproject-metrics"
        return ApiTokenAuth().authenticate(request, token)


class BoundedParser(Parser):
    def parse_body(self, request: Any) -> Any:
        try:
            if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY_BYTES:
                raise ValueError
            raw = getattr(request, "_body", None)
            if raw is None:
                raw = request.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise ValueError
            # Reject deep structures before the JSON decoder recurses. Ignore
            # braces in strings, including escaped quotes and backslashes.
            depth = 0
            quoted = escaped = False
            for char in raw.decode("utf-8"):
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char in "[{":
                    depth += 1
                    if depth > MAX_JSON_DEPTH:
                        raise ValueError
                elif char in "]}":
                    depth -= 1
            value = json.loads(raw)
            validate_json(value)
            return value
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise SampleInputError("Sample input exceeds its supported bounds") from error


def _bound_request(request: Any) -> None:
    """Bound all mutation requests, including routes without a body schema."""
    try:
        if len(request.META.get("QUERY_STRING", "")) > 8192:
            raise ValueError
        validate_json(dict(request.GET.lists()))
        if "queue" in request.GET:
            allowed = {
                queue for backend in settings.TASKS.values() for queue in backend.get("QUEUES", ())
            }
            if request.GET["queue"] not in allowed:
                raise ValueError
        if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY_BYTES:
            raise ValueError
        raw = request.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError
        request._body = raw
        if raw and request.content_type == "application/json":
            BoundedParser().parse_body(request)
    except (ValueError, TypeError) as error:
        raise SampleInputError("Sample input exceeds its supported bounds") from error


class SampleAPI(NinjaAPI):
    def get_openapi_schema(self, *args: Any, **kwargs: Any) -> Any:
        schema = super().get_openapi_schema(*args, **kwargs)
        if not demo_enabled():
            schema["paths"] = {
                path: value
                for path, value in schema["paths"].items()
                if not is_demo_route(path.rsplit("/api", 1)[-1])
            }
        return schema
