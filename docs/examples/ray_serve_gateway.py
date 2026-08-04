"""Application-owned Django gateway for a private Ray Serve data plane.

Copy this module into a Django application and replace the permission and response
schema with application policy. It deliberately has no django-ray, Ray, or FastAPI
dependency.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import urllib.error
import urllib.request
from typing import IO, Any
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 16 * 1024
MAX_TEXT_CHARACTERS = 4_000
MAX_RESPONSE_BYTES = 32 * 1024
MAX_LOG_IDENTIFIER_CHARACTERS = 100
MODEL_REVISION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,99}")


class ModelGatewayError(Exception):
    """A fixed public failure that contains no upstream diagnostics."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the private service credential cannot follow one."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


model_service_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    NoRedirectHandler,
)


def _bounded_log_identifier(value: object) -> str:
    """Render an application identifier without allowing unbounded log records."""

    return str(value)[:MAX_LOG_IDENTIFIER_CHARACTERS]


def _rejected_response(
    request: HttpRequest,
    request_id: str,
    code: str,
    status: int,
) -> JsonResponse:
    """Record a bounded caller rejection and return its fixed public shape."""

    audit_data: dict[str, object] = {
        "request_id": request_id,
        "failure_code": code,
        "status_code": status,
    }
    user = getattr(request, "user", None)
    user_id = getattr(user, "pk", None)
    if getattr(user, "is_authenticated", False) and user_id is not None:
        audit_data["user_id"] = _bounded_log_identifier(user_id)
    logger.warning("model gateway request rejected", extra=audit_data)
    return JsonResponse({"error": code, "request_id": request_id}, status=status)


def _bounded_model_response(payload: dict[str, Any], request_id: str) -> dict[str, str]:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ModelGatewayError("model_request_too_large", 400)

    request = urllib.request.Request(
        settings.MODEL_SERVE_URL,
        data=encoded,
        headers={
            "Authorization": f"Bearer {settings.MODEL_SERVE_TOKEN}",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        },
        method="POST",
    )
    try:
        with model_service_opener.open(
            request, timeout=settings.MODEL_SERVE_TIMEOUT_SECONDS
        ) as response:
            status = response.status
            content_type = response.headers.get_content_type()
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        upstream_status = error.code
        error.close()
        if upstream_status == 408:
            raise ModelGatewayError("model_timeout", 504) from None
        if upstream_status == 429:
            raise ModelGatewayError("model_overloaded", 503) from None
        if upstream_status == 503:
            raise ModelGatewayError("model_unavailable", 503) from None
        if upstream_status in {400, 413, 415, 422}:
            raise ModelGatewayError("model_contract_rejected", 502) from None
        if 300 <= upstream_status < 500:
            raise ModelGatewayError("model_protocol_error", 502) from None
        raise ModelGatewayError("model_error", 502) from None
    except TimeoutError:
        raise ModelGatewayError("model_timeout", 504) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise ModelGatewayError("model_timeout", 504) from None
        raise ModelGatewayError("model_unavailable", 503) from None
    except http.client.HTTPException:
        raise ModelGatewayError("model_protocol_error", 502) from None
    except OSError:
        raise ModelGatewayError("model_unavailable", 503) from None

    if status != 200 or content_type != "application/json":
        raise ModelGatewayError("model_protocol_error", 502)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ModelGatewayError("model_response_too_large", 502)
    try:
        result = json.loads(body)
    except (ValueError, RecursionError):
        raise ModelGatewayError("model_response_invalid", 502) from None
    if not isinstance(result, dict) or set(result) != {
        "model_revision",
        "prediction",
    }:
        raise ModelGatewayError("model_response_invalid", 502)
    if not all(isinstance(result[key], str) for key in result):
        raise ModelGatewayError("model_response_invalid", 502)
    if MODEL_REVISION_PATTERN.fullmatch(result["model_revision"]) is None:
        raise ModelGatewayError("model_response_invalid", 502)
    prediction = result["prediction"]
    if not 1 <= len(prediction) <= 1_000 or any(
        ord(character) < 32 or 127 <= ord(character) <= 159 for character in prediction
    ):
        raise ModelGatewayError("model_response_invalid", 502)
    return result


@never_cache
def csrf_failure(request: HttpRequest, reason: str = "") -> JsonResponse:
    """Return a fixed API failure without exposing Django's raw CSRF reason."""

    request_id = str(uuid4())
    return _rejected_response(request, request_id, "csrf_failed", 403)


@csrf_protect
def _classify_post(request: HttpRequest, request_id: str) -> JsonResponse:
    """Validate and proxy a POST after the method dispatcher admits it."""

    if not request.user.is_authenticated:
        return _rejected_response(request, request_id, "authentication_required", 403)
    if not request.user.has_perm("myapp.use_model"):
        return _rejected_response(request, request_id, "permission_denied", 403)
    if request.content_type != "application/json":
        return _rejected_response(request, request_id, "content_type_not_supported", 415)
    try:
        body = request.body
    except RequestDataTooBig:
        body = None
    if body is None or len(body) > MAX_REQUEST_BYTES:
        return _rejected_response(request, request_id, "request_too_large", 413)
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        payload = None
    if not isinstance(payload, dict) or set(payload) != {"text"}:
        return _rejected_response(request, request_id, "invalid_request", 400)
    text = payload["text"]
    if not isinstance(text, str):
        return _rejected_response(request, request_id, "invalid_request", 400)
    if not 1 <= len(text) <= MAX_TEXT_CHARACTERS:
        return _rejected_response(request, request_id, "invalid_text", 400)

    try:
        result = _bounded_model_response({"text": text}, request_id)
    except ModelGatewayError as error:
        logger.warning(
            "model inference failed",
            extra={
                "request_id": request_id,
                "user_id": _bounded_log_identifier(request.user.pk),
                "failure_code": error.code,
            },
        )
        return JsonResponse(
            {"error": error.code, "request_id": request_id},
            status=error.status,
        )

    logger.info(
        "model inference completed",
        extra={
            "request_id": request_id,
            "user_id": _bounded_log_identifier(request.user.pk),
            "model_revision": result["model_revision"],
        },
    )
    return JsonResponse({**result, "request_id": request_id})


@transaction.non_atomic_requests
@never_cache
@csrf_exempt
def classify(request: HttpRequest) -> JsonResponse:
    """Dispatch bounded method failures and CSRF-protect accepted POSTs."""

    request_id = str(uuid4())
    if request.method != "POST":
        response = _rejected_response(request, request_id, "method_not_allowed", 405)
        response.headers["Allow"] = "POST"
        return response
    return _classify_post(request, request_id)
