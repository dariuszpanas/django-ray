"""Credential separation, docs gating, and bounded parsing for the sample API."""

from __future__ import annotations

import json
import secrets
from functools import wraps
from typing import Any

from django.conf import settings
from django.http import JsonResponse
from ninja import NinjaAPI
from ninja.parser import Parser
from ninja.security import HttpBearer

from testproject.admission import SampleInputError
from testproject.route_policy import POST_ROUTE_CAPABILITIES, is_demo_route
from testproject.workload_limits import MAX_BODY_BYTES, MAX_JSON_DEPTH, validate_json

_SECURITY_SCHEME_DESCRIPTIONS = {
    "ApiTokenAuth": (
        "Operator bearer token (DJANGO_API_TOKEN) for ordinary enqueue, task-status, "
        "execution-monitoring, lifecycle, and workflow routes."
    ),
    "DemoTokenAuth": (
        "Demo workload bearer token (DJANGO_DEMO_TOKEN) for opt-in stress and benchmark "
        "routes when demo workloads are enabled."
    ),
    "MetricsTokenAuth": (
        "Metrics bearer token (DJANGO_METRICS_TOKEN, or operator DJANGO_API_TOKEN) "
        "for Prometheus metrics scraping at /api/metrics."
    ),
}

_OPENAPI_ERROR_SCHEMA = {
    "type": "object",
    "title": "ApiErrorSchema",
    "description": "Unified structured error envelope returned by the sample API.",
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "errors": {
            "anyOf": [
                {"type": "array", "items": {"type": "object"}},
                {"type": "null"},
            ]
        },
    },
    "required": ["code", "message"],
}

_OPENAPI_ERROR_RESPONSES = {
    "UnauthorizedError": {
        "description": "Authentication credentials were missing or invalid.",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ApiErrorSchema"},
            }
        },
    },
    "ValidationError": {
        "description": "Request parameters or JSON body failed validation.",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ApiErrorSchema"},
            }
        },
    },
    "RateLimitedError": {
        "description": "Sample admission request window exceeded; honor Retry-After.",
        "headers": {
            "Retry-After": {
                "description": "Seconds to wait before retrying.",
                "schema": {"type": "string"},
            }
        },
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ApiErrorSchema"},
            }
        },
    },
    "ServiceUnavailableError": {
        "description": "Admission capacity or backing dependency temporarily unavailable.",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ApiErrorSchema"},
            }
        },
    },
}


def demo_enabled() -> bool:
    return (
        getattr(settings, "DEPLOYMENT_MODE", "demo") == "demo"
        and getattr(settings, "DJANGO_DEMO_WORKLOADS_ENABLED", False) is True
        and bool(getattr(settings, "DJANGO_DEMO_TOKEN", None))
    )


def _matches_bearer_token(token: str, expected: object) -> bool:
    """Reject unsupported text before the ASCII constant-time comparison."""
    return (
        isinstance(token, str)
        and isinstance(expected, str)
        and bool(expected)
        and token.isascii()
        and expected.isascii()
        and secrets.compare_digest(token, expected)
    )


def docs_require_auth(*, is_schema_endpoint: bool = False) -> bool:
    """Require authentication on docs outside debug mode and on schema in production."""
    if getattr(settings, "DJANGO_ALLOW_UNAUTHENTICATED_SAMPLE_SURFACES", False):
        return False
    if not getattr(settings, "DJANGO_API_ENABLED", True):
        return True
    if getattr(settings, "DEPLOYMENT_MODE", "demo") == "production":
        return True
    if is_schema_endpoint:
        return False
    return not settings.DEBUG


def require_docs_access(view: Any) -> Any:
    """Gate Swagger UI and OpenAPI JSON in production / non-debug / disabled-API modes."""

    @wraps(view)
    def wrapped(request: Any, *args: Any, **kwargs: Any) -> Any:
        is_schema_endpoint = request.path_info.rstrip("/").endswith("/openapi.json")
        if docs_require_auth(is_schema_endpoint=is_schema_endpoint):
            authorized = False
            if getattr(settings, "DJANGO_API_ENABLED", True):
                authorization = request.META.get("HTTP_AUTHORIZATION", "")
                if isinstance(authorization, str):
                    scheme, separator, token = authorization.partition(" ")
                    if separator and scheme.lower() == "bearer" and token:
                        authorized = ApiTokenAuth().authenticate(request, token) is not None
                if not authorized:
                    user = getattr(request, "user", None)
                    if (
                        user is not None
                        and user.is_authenticated
                        and user.is_active
                        and user.is_staff
                    ):
                        authorized = True
            if not authorized:
                response = JsonResponse(
                    {
                        "code": "UNAUTHORIZED",
                        "message": "Authentication credentials were not provided or are invalid.",
                    },
                    status=401,
                )
                response["WWW-Authenticate"] = "Bearer"
                response["Cache-Control"] = "no-store"
                response["X-Content-Type-Options"] = "nosniff"
                return response
        response = view(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    return wrapped


class ApiTokenAuth(HttpBearer):
    """Operator bearer authentication (DJANGO_API_TOKEN)."""

    def authenticate(self, request: Any, token: str) -> str | None:
        if not getattr(settings, "DJANGO_API_ENABLED", True):
            return None
        if is_demo_route(request.path_info):
            expected = getattr(settings, "DJANGO_DEMO_TOKEN", None) if demo_enabled() else None
            identity = "django-ray-testproject-demo"
        else:
            expected = getattr(settings, "DJANGO_API_TOKEN", None)
            identity = "django-ray-testproject-operator"
        if _matches_bearer_token(token, expected):
            if request.method == "POST":
                _bound_request(request)
            return identity
        return None


class DemoTokenAuth(HttpBearer):
    """Demo workload bearer authentication (DJANGO_DEMO_TOKEN)."""

    def authenticate(self, request: Any, token: str) -> str | None:
        if not getattr(settings, "DJANGO_API_ENABLED", True) or not demo_enabled():
            return None
        expected = getattr(settings, "DJANGO_DEMO_TOKEN", None)
        if _matches_bearer_token(token, expected):
            if request.method == "POST":
                _bound_request(request)
            return "django-ray-testproject-demo"
        return None


class MetricsTokenAuth(HttpBearer):
    """Metrics bearer authentication (DJANGO_METRICS_TOKEN or operator fallback)."""

    def authenticate(self, request: Any, token: str) -> str | None:
        expected = getattr(settings, "DJANGO_METRICS_TOKEN", None)
        if _matches_bearer_token(token, expected):
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
            if (
                request.content_type == "multipart/form-data"
                and not getattr(request, "POST", None)
                and not getattr(request, "FILES", None)
            ):
                return None
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
        except SampleInputError:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise SampleInputError("Sample input exceeds its supported bounds.") from error


def _bound_request(request: Any) -> None:
    """Bound all mutation requests, including routes without a body schema."""
    try:
        if len(request.META.get("QUERY_STRING", "")) > 8192:
            raise ValueError
        validate_json(dict(request.GET.lists()))
        if "queue" in request.GET:
            allowed = sorted(
                {
                    queue
                    for backend in settings.TASKS.values()
                    for queue in backend.get("QUEUES", ())
                }
            )
            if request.GET["queue"] not in allowed:
                valid_queues = ", ".join(allowed)
                raise SampleInputError(
                    f"Unknown queue parameter. Valid configured queues: {valid_queues}.",
                    code="INVALID_QUEUE",
                )
        if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY_BYTES:
            raise ValueError
        raw = request.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError
        request._body = raw
        if raw and request.content_type == "application/json":
            BoundedParser().parse_body(request)
    except SampleInputError:
        raise
    except (ValueError, TypeError) as error:
        raise SampleInputError("Sample input exceeds its supported bounds.") from error


class SampleAPI(NinjaAPI):
    def get_openapi_schema(self, *args: Any, **kwargs: Any) -> Any:
        schema = super().get_openapi_schema(*args, **kwargs)
        demo_active = demo_enabled()
        if not demo_active:
            schema["paths"] = {
                path: value
                for path, value in schema["paths"].items()
                if not is_demo_route(path.rsplit("/api", 1)[-1])
            }

        components = schema.setdefault("components", {})
        schemas = components.setdefault("schemas", {})
        schemas.setdefault("ApiErrorSchema", dict(_OPENAPI_ERROR_SCHEMA))

        responses_components = components.setdefault("responses", {})
        for response_name, definition in _OPENAPI_ERROR_RESPONSES.items():
            responses_components.setdefault(response_name, dict(definition))

        security_schemes = components.setdefault("securitySchemes", {})
        if not demo_active:
            security_schemes.pop("DemoTokenAuth", None)
        for scheme_name, description in _SECURITY_SCHEME_DESCRIPTIONS.items():
            if scheme_name in security_schemes:
                security_schemes[scheme_name]["description"] = description

        for path, path_item in schema["paths"].items():
            normalized_path = path.rsplit("/api", 1)[-1]
            capability = POST_ROUTE_CAPABILITIES.get(normalized_path)
            for method, operation in path_item.items():
                if not isinstance(operation, dict):
                    continue
                op_responses = operation.setdefault("responses", {})
                if operation.get("security"):
                    op_responses.setdefault(
                        "401",
                        {"$ref": "#/components/responses/UnauthorizedError"},
                    )
                if method == "post" or operation.get("parameters"):
                    op_responses.setdefault(
                        "422",
                        {"$ref": "#/components/responses/ValidationError"},
                    )
                if (
                    method == "post"
                    and capability is not None
                    and not normalized_path.endswith("/cancel")
                ):
                    op_responses.setdefault(
                        "429",
                        {"$ref": "#/components/responses/RateLimitedError"},
                    )
                    op_responses.setdefault(
                        "503",
                        {"$ref": "#/components/responses/ServiceUnavailableError"},
                    )

        return schema
