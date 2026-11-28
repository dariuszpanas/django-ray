"""Ray Job Submission API runner implementation."""

from __future__ import annotations

import json
import logging
import math
import threading
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from django_ray.conf.settings import get_settings
from django_ray.execution_codec import (
    ExecutionIdentity,
    ExecutionRequest,
    ExecutionRequestEncodeError,
    ExecutionRequestRejection,
    encode_execution_request,
)
from django_ray.ray_job_protocol import (
    STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
    RayJobRequestBindingError,
    RayJobRequestBindingRejection,
    build_ray_job_request_reference_metadata,
    coordination_sha256,
    fixed_safe_ray_job_metadata,
)
from django_ray.ray_job_request_storage import (
    RayJobRequestStorageError,
    prepare_ray_job_request,
    register_and_attach_ray_job_request,
    release_ray_job_request_reservation,
)
from django_ray.redaction import materialize_exception_message, materialize_exception_text
from django_ray.runner.base import BaseRunner, JobInfo, JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcome, CancellationOutcomeStatus
from django_ray.runner.errors import (
    RayJobRequestPreparationError,
    RayJobRequestPreparationRejection,
    RayJobSubmissionUncertainError,
)
from django_ray.runtime.runtime_env import (
    normalize_runtime_env,
    runtime_env_for_execution,
    snapshot_local_runtime_env,
)
from django_ray.runtime.serialization import serialize_args

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution


_CONTROL_REQUEST_TIMEOUT_SECONDS = 5.0
_SUBMISSION_REQUEST_TIMEOUT_SECONDS = 30.0
_REQUEST_TIMEOUT_ATTRIBUTE = "_django_ray_request_timeout_seconds"
_REQUEST_TIMEOUT_LOCAL = threading.local()
_JOB_CLIENT_POOL: dict[tuple[str, tuple[tuple[str, str], ...]], Any] = {}
_JOB_CLIENT_POOL_LOCK = threading.RLock()
_JOB_CLIENT_POOL_MAX_SIZE = 32
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

logger = logging.getLogger(__name__)


def _warn_if_plaintext_http_runtime_env_vars(
    endpoint: str | None,
    runtime_env_spec: Mapping[str, Any],
) -> None:
    """Warn when RuntimeEnv env_vars are submitted over non-loopback HTTP."""
    env_vars = runtime_env_spec.get("env_vars")
    if not isinstance(env_vars, Mapping) or not env_vars:
        return
    if type(endpoint) is not str or "://" not in endpoint:
        return
    parsed = urlsplit(endpoint)
    if parsed.scheme.lower() != "http":
        return
    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname in _LOOPBACK_HOSTS:
        return
    host_display = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    origin = f"http://{host_display}:{port}" if port is not None else f"http://{host_display}"
    logger.warning(
        "Submitting Ray Job with RuntimeEnv env_vars over plaintext HTTP to non-loopback "
        "endpoint %s. RUNTIME_ENV_STORAGE_MODE='encrypted' protects database storage at rest "
        "only, not Ray Job transport or GCS; use HTTPS/TLS and mount secrets via pod volumes "
        "or an external secret manager.",
        origin,
    )


_STORAGE_PREPARATION_REJECTIONS = {
    "invalid_locator": RayJobRequestPreparationRejection.INVALID_REQUEST,
    "resource_limit": RayJobRequestPreparationRejection.RESOURCE_LIMIT,
    "configuration": RayJobRequestPreparationRejection.CONFIGURATION,
    "storage_unavailable": RayJobRequestPreparationRejection.STORAGE_UNAVAILABLE,
    "integrity_mismatch": RayJobRequestPreparationRejection.INTEGRITY_MISMATCH,
    "invalid_request": RayJobRequestPreparationRejection.INVALID_REQUEST,
    "registry_mismatch": RayJobRequestPreparationRejection.REGISTRY_MISMATCH,
    "binding_mismatch": RayJobRequestPreparationRejection.BINDING_MISMATCH,
}


def _fixed_preparation_rejection(
    error: ExecutionRequestEncodeError | RayJobRequestBindingError | Exception,
) -> RayJobRequestPreparationRejection:
    """Map a fixed lower-layer classification without inspecting exception text."""
    classification = getattr(error, "classification", None)
    value = getattr(classification, "value", None)
    if isinstance(error, ExecutionRequestEncodeError):
        if classification is ExecutionRequestRejection.RESOURCE_LIMIT:
            return RayJobRequestPreparationRejection.RESOURCE_LIMIT
        return RayJobRequestPreparationRejection.INVALID_REQUEST
    if isinstance(error, RayJobRequestBindingError):
        if classification is RayJobRequestBindingRejection.RESOURCE_LIMIT:
            return RayJobRequestPreparationRejection.RESOURCE_LIMIT
        return RayJobRequestPreparationRejection.INVALID_REQUEST
    return _STORAGE_PREPARATION_REJECTIONS.get(
        value,
        RayJobRequestPreparationRejection.INVALID_REQUEST,
    )


def _find_auto_ray_address() -> str:
    """Resolve ``auto`` without allowing ``RAY_ADDRESS`` to replace it."""
    from ray._private import services

    gcs_addresses = services.find_gcs_addresses()
    bootstrap_address = services.find_bootstrap_address(None)

    # Match Ray's ``auto`` discovery order while deliberately omitting its
    # environment-variable precedence. An explicit backend target must remain
    # authoritative even when the task-manager process has global Ray settings.
    if len(gcs_addresses) > 1 and bootstrap_address is not None:
        return bootstrap_address
    if gcs_addresses:
        return next(iter(gcs_addresses))
    if bootstrap_address is not None:
        return bootstrap_address
    raise ConnectionError("Could not find any running Ray instance for the explicit 'auto' target.")


_ALLOWED_RAY_ADDRESS_SCHEMES = frozenset({"http", "https", "ray"})
_ALLOWED_API_SERVER_SCHEMES = frozenset({"http", "https"})
_RUNTIME_TRUSTED_RAY_ADDRESSES: set[str] = set()
_RUNTIME_TRUSTED_RAY_ADDRESSES_LOCK = threading.RLock()


class UntrustedRayAddressError(ValueError):
    """Raised when a Ray cluster address is malformed or outside trusted config."""


def _has_forbidden_uri_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in value)


def _validate_ray_address_uri(ray_address: str) -> str:
    """Validate Ray target URI syntax, scheme, and absence of credentials/queries."""
    if type(ray_address) is not str or not ray_address:
        raise UntrustedRayAddressError("Ray address must be a non-empty string")
    if _has_forbidden_uri_chars(ray_address):
        raise UntrustedRayAddressError(
            "Ray address must not contain whitespace or control characters"
        )
    if ray_address == "auto":
        return ray_address

    if "://" in ray_address:
        parsed = urlsplit(ray_address)
        scheme = parsed.scheme.lower()
        if scheme not in _ALLOWED_RAY_ADDRESS_SCHEMES:
            raise UntrustedRayAddressError("Ray address scheme must be one of http, https, or ray")
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise UntrustedRayAddressError("Ray address must not embed credentials")
        if not parsed.hostname:
            raise UntrustedRayAddressError("Ray address must specify a valid hostname")
        try:
            _ = parsed.port
        except ValueError as error:
            raise UntrustedRayAddressError("Ray address must specify a valid port") from error
        if parsed.query or parsed.fragment or "?" in ray_address or "#" in ray_address:
            raise UntrustedRayAddressError("Ray address must not contain query or fragment parts")
        if "\\" in parsed.path:
            raise UntrustedRayAddressError("Ray address path is invalid")
        if scheme == "ray" and parsed.path not in {"", "/"}:
            raise UntrustedRayAddressError("ray:// address must not contain a path")
        if any(segment == ".." for segment in parsed.path.split("/")):
            raise UntrustedRayAddressError("Ray address path must not contain traversal segments")
        return ray_address

    parsed = urlsplit(f"ray://{ray_address}")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise UntrustedRayAddressError(
            "Ray address must be 'auto', host:port, or an HTTP(S)/ray URI"
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise UntrustedRayAddressError("Ray address must specify a valid port") from error
    if port is None:
        raise UntrustedRayAddressError("Ray host address must include an explicit port")
    return ray_address


def _validate_resolved_api_server_url(url: str) -> str:
    """Validate that a resolved dashboard/Jobs endpoint is a clean HTTP(S) URI."""
    validated = _validate_ray_address_uri(url)
    parsed = urlsplit(validated)
    if parsed.scheme.lower() not in _ALLOWED_API_SERVER_SCHEMES or not parsed.hostname:
        raise UntrustedRayAddressError("Resolved Ray API server address must use http or https")
    return validated


def _canonical_ray_address(ray_address: str) -> str:
    """Return a canonical representation for allowlist membership checks."""
    validated = _validate_ray_address_uri(ray_address)
    if validated == "auto":
        return "auto"
    if "://" not in validated:
        parsed = urlsplit(f"ray://{validated}")
        host = (parsed.hostname or "").lower()
        if ":" in host:
            host = f"[{host}]"
        return f"{host}:{parsed.port}"
    parsed = urlsplit(validated)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    path = parsed.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


def register_trusted_ray_address(ray_address: str) -> str:
    """Register one validated runtime cluster address (e.g., CLI ``--cluster``)."""
    canonical = _canonical_ray_address(ray_address)
    with _RUNTIME_TRUSTED_RAY_ADDRESSES_LOCK:
        _RUNTIME_TRUSTED_RAY_ADDRESSES.add(canonical)
    return canonical


def clear_registered_trusted_ray_addresses() -> None:
    """Clear runtime-registered cluster addresses (primarily for tests)."""
    with _RUNTIME_TRUSTED_RAY_ADDRESSES_LOCK:
        _RUNTIME_TRUSTED_RAY_ADDRESSES.clear()


@contextmanager
def trusted_ray_address_override(ray_address: str) -> Generator[str, None, None]:
    """Temporarily trust one validated runtime Ray cluster address."""
    canonical = _canonical_ray_address(ray_address)
    with _RUNTIME_TRUSTED_RAY_ADDRESSES_LOCK:
        already_present = canonical in _RUNTIME_TRUSTED_RAY_ADDRESSES
        _RUNTIME_TRUSTED_RAY_ADDRESSES.add(canonical)
    try:
        yield canonical
    finally:
        if not already_present:
            with _RUNTIME_TRUSTED_RAY_ADDRESSES_LOCK:
                _RUNTIME_TRUSTED_RAY_ADDRESSES.discard(canonical)


def _iter_allowlist_entries(raw: object, *, setting_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        raise ImproperlyConfigured(
            f"django-ray: {setting_name} must be an iterable of Ray address strings"
        )
    entries: list[str] = []
    for item in raw:
        if type(item) is not str or not item.strip():
            raise ImproperlyConfigured(
                f"django-ray: {setting_name} entries must be non-empty Ray address strings"
            )
        entries.append(item)
    return tuple(entries)


def get_trusted_ray_addresses(*, extra_allowed: Iterable[str] = ()) -> frozenset[str]:
    """Collect canonical trusted Ray addresses from Django settings and runtime overrides."""
    allowed: set[str] = set()

    def _add_candidate(candidate: object) -> None:
        if type(candidate) is str and candidate.strip():
            allowed.add(_canonical_ray_address(candidate))

    config = get_settings()
    _add_candidate(config.get("RAY_ADDRESS"))
    _add_candidate(config.get("RAY_STATE_API_ADDRESS"))
    for entry in _iter_allowlist_entries(
        config.get("ALLOWED_RAY_ADDRESSES"),
        setting_name="ALLOWED_RAY_ADDRESSES",
    ):
        _add_candidate(entry)

    try:
        from django.conf import settings as django_settings
    except ImportError:  # pragma: no cover - Django is always installed
        django_settings = None
    if django_settings is not None and django_settings.configured:
        tasks_cfg = getattr(django_settings, "TASKS", None)
        if isinstance(tasks_cfg, Mapping):
            for backend_cfg in tasks_cfg.values():
                if not isinstance(backend_cfg, Mapping):
                    continue
                options = backend_cfg.get("OPTIONS")
                if not isinstance(options, Mapping):
                    continue
                _add_candidate(options.get("RAY_ADDRESS"))
                _add_candidate(options.get("RAY_STATE_API_ADDRESS"))
                for entry in _iter_allowlist_entries(
                    options.get("ALLOWED_RAY_ADDRESSES"),
                    setting_name="TASKS[*]['OPTIONS']['ALLOWED_RAY_ADDRESSES']",
                ):
                    _add_candidate(entry)

    with _RUNTIME_TRUSTED_RAY_ADDRESSES_LOCK:
        allowed.update(_RUNTIME_TRUSTED_RAY_ADDRESSES)

    for entry in extra_allowed:
        _add_candidate(entry)

    return frozenset(allowed)


def validate_trusted_ray_address(
    ray_address: str,
    *,
    extra_allowed: Iterable[str] = (),
) -> str:
    """Ensure ``ray_address`` is syntactically valid and present in trusted configuration."""
    validated = _validate_ray_address_uri(ray_address)
    canonical = _canonical_ray_address(validated)
    trusted = get_trusted_ray_addresses(extra_allowed=extra_allowed)
    if canonical not in trusted:
        raise UntrustedRayAddressError(
            "Ray address is not in the trusted cluster allowlist; configure it in "
            "DJANGO_RAY['RAY_ADDRESS'], TASKS backend OPTIONS['RAY_ADDRESS'], "
            "DJANGO_RAY['ALLOWED_RAY_ADDRESSES'], or via the worker --cluster option"
        )
    return validated


def _resolve_submission_address(ray_address: str) -> str:
    """Resolve one selected Ray target without consulting ambient Ray addresses."""
    from ray.dashboard.modules.dashboard_sdk import split_address
    from ray.dashboard.utils import (
        ray_address_to_api_server_url,
        ray_client_address_to_api_server_url,
    )

    validated = _validate_ray_address_uri(ray_address)
    if "://" in validated:
        module_name, _ = split_address(validated)
        if module_name == "ray":
            return _validate_resolved_api_server_url(
                ray_client_address_to_api_server_url(validated)
            )
        return _validate_resolved_api_server_url(validated)

    target = _find_auto_ray_address() if validated == "auto" else validated
    return _validate_resolved_api_server_url(ray_address_to_api_server_url(target))


_ORIGINAL_RESOLVE_SUBMISSION_ADDRESS = _resolve_submission_address


def _close_job_client_session(client: Any) -> None:
    session = getattr(client, "_session", None)
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def clear_job_client_cache() -> None:
    """Close and clear all pooled Ray JobSubmissionClient instances."""
    with _JOB_CLIENT_POOL_LOCK:
        clients = list(_JOB_CLIENT_POOL.values())
        _JOB_CLIENT_POOL.clear()
    for client in clients:
        _close_job_client_session(client)


def _evict_job_client(client: Any) -> None:
    """Remove a specific client from the pool after a transport failure."""
    evicted: list[Any] = []
    with _JOB_CLIENT_POOL_LOCK:
        keys = [key for key, value in _JOB_CLIENT_POOL.items() if value is client]
        for key in keys:
            evicted.append(_JOB_CLIENT_POOL.pop(key))
    for item in evicted:
        _close_job_client_session(item)


def _submission_request_timeout_seconds(config: Mapping[str, Any] | None = None) -> float:
    """Return bounded HTTP timeout for Ray Job code uploads and submit_job."""
    if isinstance(config, Mapping):
        raw = config.get("RAY_JOB_SUBMISSION_TIMEOUT_SECONDS")
        if (
            raw is not None
            and not isinstance(raw, bool)
            and isinstance(raw, (int, float))
            and math.isfinite(float(raw))
            and float(raw) > 0
        ):
            return float(raw)
    return _SUBMISSION_REQUEST_TIMEOUT_SECONDS


def _client_auth_header_fingerprint(dashboard_sdk: Any) -> tuple[tuple[str, str], ...]:
    getter = getattr(dashboard_sdk, "get_auth_headers_if_auth_enabled", None)
    if not callable(getter):
        return ()
    try:
        headers = getter(None)
    except Exception:
        return ()
    if not isinstance(headers, Mapping):
        return ()
    return tuple(sorted((str(k).lower(), str(v)) for k, v in headers.items()))


def _address_pinned_job_client(
    ray_address: str,
    *,
    enforce_allowlist: bool = True,
    extra_allowed: Iterable[str] = (),
    use_cache: bool = True,
) -> Any:
    """Build or reuse a JobSubmissionClient whose explicit address cannot be overridden.

    Ray's public ``JobSubmissionClient`` constructor intentionally gives
    ``RAY_API_SERVER_ADDRESS`` and ``RAY_ADDRESS`` precedence over its address
    argument. That conflicts with django-ray's durable backend routing contract,
    so initialize the inherited HTTP submission client with an independently
    resolved endpoint instead.
    """
    import ray
    from ray.dashboard.modules import dashboard_sdk
    from ray.dashboard.modules.dashboard_sdk import SubmissionClient
    from ray.job_submission import JobSubmissionClient

    if enforce_allowlist:
        validate_trusted_ray_address(ray_address, extra_allowed=extra_allowed)
    else:
        _validate_ray_address_uri(ray_address)

    class _BoundedJobSubmissionClient(JobSubmissionClient):
        def _do_request(
            self,
            method: str,
            endpoint: str,
            *,
            data: bytes | None = None,
            json_data: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> Any:
            timeout = getattr(_REQUEST_TIMEOUT_LOCAL, "timeout", None)
            if timeout is None:
                timeout = getattr(self, _REQUEST_TIMEOUT_ATTRIBUTE, None)
            if timeout is not None:
                kwargs.setdefault("timeout", timeout)
            return super()._do_request(
                method,
                endpoint,
                data=data,
                json_data=json_data,
                **kwargs,
            )

    api_server_url = _validate_resolved_api_server_url(_resolve_submission_address(ray_address))
    can_pool = (
        use_cache
        and _resolve_submission_address is _ORIGINAL_RESOLVE_SUBMISSION_ADDRESS
        and getattr(SubmissionClient._do_request, "__module__", "").startswith("ray.")
        and getattr(JobSubmissionClient._check_connection_and_version, "__module__", "").startswith(
            "ray."
        )
    )
    pool_key = (api_server_url, _client_auth_header_fingerprint(dashboard_sdk))
    if can_pool:
        with _JOB_CLIENT_POOL_LOCK:
            cached = _JOB_CLIENT_POOL.get(pool_key)
            if cached is not None:
                return cached

    client = _BoundedJobSubmissionClient.__new__(_BoundedJobSubmissionClient)
    client._client_ray_version = ray.__version__
    SubmissionClient.__init__(client, address=api_server_url)
    with _bounded_control_requests(client):
        client._check_connection_and_version(
            min_version="2.0",
            version_error_message=(
                f"Client Ray version {client._client_ray_version} is not compatible "
                "with the Ray cluster. Please ensure the cluster is running Ray 2.0 "
                "or higher or downgrade the client Ray version."
            ),
        )
    setattr(client, _REQUEST_TIMEOUT_ATTRIBUTE, None)

    if can_pool:
        evicted: Any | None = None
        with _JOB_CLIENT_POOL_LOCK:
            existing = _JOB_CLIENT_POOL.get(pool_key)
            if existing is not None:
                _close_job_client_session(client)
                return existing
            if len(_JOB_CLIENT_POOL) >= _JOB_CLIENT_POOL_MAX_SIZE:
                oldest_key = next(iter(_JOB_CLIENT_POOL))
                evicted = _JOB_CLIENT_POOL.pop(oldest_key)
            _JOB_CLIENT_POOL[pool_key] = client
        if evicted is not None:
            _close_job_client_session(evicted)
    return client


@contextmanager
def _bounded_request_timeout(client: Any, timeout_seconds: float) -> Generator[None, None, None]:
    """Apply a thread-safe per-call request timeout to a JobSubmissionClient."""
    previous_local = getattr(_REQUEST_TIMEOUT_LOCAL, "timeout", None)
    previous_attr = getattr(client, _REQUEST_TIMEOUT_ATTRIBUTE, None)
    _REQUEST_TIMEOUT_LOCAL.timeout = timeout_seconds
    setattr(client, _REQUEST_TIMEOUT_ATTRIBUTE, timeout_seconds)
    try:
        yield
    finally:
        _REQUEST_TIMEOUT_LOCAL.timeout = previous_local
        setattr(client, _REQUEST_TIMEOUT_ATTRIBUTE, previous_attr)


@contextmanager
def _bounded_control_requests(client: Any) -> Generator[None, None, None]:
    """Bound Ray dashboard control calls that may run under a database row lock."""
    with _bounded_request_timeout(client, _CONTROL_REQUEST_TIMEOUT_SECONDS):
        yield


@contextmanager
def _bounded_submission_requests(
    client: Any,
    *,
    timeout_seconds: float | None = None,
) -> Generator[None, None, None]:
    """Bound Ray Job code upload and submit_job HTTP requests."""
    effective = (
        _SUBMISSION_REQUEST_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
    )
    with _bounded_request_timeout(client, effective):
        yield


def _resolve_ray_task_contract(callable_path: str) -> Any | None:
    """Resolve an optional ``@ray_task`` contract through the task allowlist.

    Returns ``None`` when the callable is not decorated with ``@ray_task``,
    cannot be imported in the manager process (for example, when supplied only
    by a remote ``RuntimeEnv``), or is outside the configured module allowlist.
    """
    from django_ray.decorators import get_ray_task_contract
    from django_ray.runtime.import_utils import resolve_task_callable

    try:
        resolved_task = resolve_task_callable(callable_path)
    except (ImportError, AttributeError, TypeError, ValueError):
        return None
    return get_ray_task_contract(resolved_task)


class RayJobRunner(BaseRunner):
    """Runner that uses Ray Job Submission API."""

    runner_family: str = "ray_job"

    def __init__(self, *, cluster_address: str | None = None) -> None:
        """Initialize Ray Job control without requiring submission storage."""
        try:
            if cluster_address is not None:
                if type(cluster_address) is not str or not cluster_address.strip():
                    raise ImproperlyConfigured(
                        "django-ray: cluster_address must be a non-empty string"
                    )
                ray_address = _validate_ray_address_uri(cluster_address)
            else:
                settings = get_settings()
                ray_address = settings.get("RAY_ADDRESS")
                if type(ray_address) is not str or not ray_address.strip():
                    raise ImproperlyConfigured("django-ray: RAY_ADDRESS must be a non-empty string")
                ray_address = _validate_ray_address_uri(ray_address)
        except (ImproperlyConfigured, UntrustedRayAddressError) as error:
            raise RayJobRequestPreparationError(
                RayJobRequestPreparationRejection.CONFIGURATION
            ) from error
        self.ray_address = ray_address
        self._extra_allowed_addresses: tuple[str, ...] = (ray_address,)

    def _get_client(self, ray_address: str | None = None) -> Any:
        """Get a Ray JobSubmissionClient for the requested cluster.

        ``RayTaskExecution.ray_target_address`` snapshots an explicit backend
        alias target. Keep the process setting as a fallback when an alias did
        not select one, validate the target against trusted configuration, and
        do not let Ray's process environment replace either selected address.
        """
        selected = ray_address or self.ray_address
        return _address_pinned_job_client(
            selected,
            enforce_allowlist=True,
            extra_allowed=self._extra_allowed_addresses,
        )

    @staticmethod
    def submission_id(task_execution: RayTaskExecution) -> str:
        """Return the stable Ray submission ID for one execution generation."""
        if task_execution.pk is None:
            raise RayJobRequestPreparationError(RayJobRequestPreparationRejection.INVALID_REQUEST)
        try:
            digest = coordination_sha256(
                ExecutionIdentity(
                    task_execution_pk=int(task_execution.pk),
                    task_id=str(task_execution.task_id),
                    attempt_number=int(task_execution.attempt_number),
                    execution_generation=int(task_execution.execution_generation),
                )
            )
        except (TypeError, ValueError, OverflowError, RayJobRequestBindingError) as error:
            raise RayJobRequestPreparationError(
                RayJobRequestPreparationRejection.INVALID_REQUEST
            ) from error
        return f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{digest}"

    def submission_handle(self, task_execution: RayTaskExecution) -> SubmissionHandle:
        """Build the exact handle that :meth:`submit` will give to Ray."""
        ray_address = (
            getattr(task_execution, "ray_target_address", None)
            or getattr(task_execution, "ray_address", None)
            or self.ray_address
        )
        return SubmissionHandle(
            ray_job_id=self.submission_id(task_execution),
            ray_address=ray_address,
            submitted_at=datetime.now(UTC),
        )

    def _reserve_public_submission(
        self,
        task_execution: RayTaskExecution,
        handle: SubmissionHandle,
        *,
        callable_path: str,
        args_json: str,
        kwargs_json: str,
        input_reference: str | None,
    ) -> bool:
        """Reserve rq2 for the public API under an exact claimed-row CAS."""
        from django.db import transaction

        from django_ray.models import RayTaskExecution, TaskState
        from django_ray.ray_job_request_storage import _has_live_worker_lease

        if not isinstance(task_execution, RayTaskExecution) or task_execution.pk is None:
            raise RayJobRequestPreparationError(RayJobRequestPreparationRejection.BINDING_MISMATCH)
        if handle.ray_job_id != self.submission_id(task_execution):
            raise RayJobRequestPreparationError(RayJobRequestPreparationRejection.BINDING_MISMATCH)
        database = task_execution._state.db or "default"
        expected_worker = task_execution.claimed_by_worker
        expected_runtime_profile = task_execution.runtime_env_profile
        expected_runtime_hash = task_execution.runtime_env_hash
        if type(expected_worker) is not str or not expected_worker:
            raise RayJobRequestPreparationError(RayJobRequestPreparationRejection.BINDING_MISMATCH)

        with transaction.atomic(using=database):
            current = (
                RayTaskExecution.objects.using(database)
                .select_for_update()
                .filter(pk=task_execution.pk)
                .first()
            )
            selected_address = (
                getattr(current, "ray_target_address", None)
                or getattr(current, "ray_address", None)
                or self.ray_address
            )
            exact_row = current is not None and (
                current.state == TaskState.RUNNING
                and current.task_id == task_execution.task_id
                and current.attempt_number == task_execution.attempt_number
                and current.execution_generation == task_execution.execution_generation
                and current.execution_protocol_version == task_execution.execution_protocol_version
                and current.claimed_by_worker == expected_worker
                and current.callable_path == callable_path
                and current.args_json == args_json
                and current.kwargs_json == kwargs_json
                and current.input_reference == input_reference
                and current.runtime_env_profile == expected_runtime_profile
                and current.runtime_env_hash == expected_runtime_hash
                and selected_address == handle.ray_address
            )
            if not exact_row or not _has_live_worker_lease(
                expected_worker,
                using=database,
            ):
                raise RayJobRequestPreparationError(
                    RayJobRequestPreparationRejection.BINDING_MISMATCH
                )
            assert current is not None

            newly_reserved = current.ray_job_id is None
            if newly_reserved:
                if current.ray_address is not None or current.ray_job_request_reference is not None:
                    raise RayJobRequestPreparationError(
                        RayJobRequestPreparationRejection.BINDING_MISMATCH
                    )
                current.ray_job_id = handle.ray_job_id
                current.ray_address = handle.ray_address
                current.save(update_fields=["ray_job_id", "ray_address"], using=database)
            elif (
                current.ray_job_id != handle.ray_job_id or current.ray_address != handle.ray_address
            ):
                raise RayJobRequestPreparationError(
                    RayJobRequestPreparationRejection.BINDING_MISMATCH
                )

            task_execution.__dict__.update(current.__dict__)
            return newly_reserved

    @staticmethod
    def _release_public_submission(
        task_execution: RayTaskExecution,
        handle: SubmissionHandle,
    ) -> None:
        """Release one definitely unsubmitted public reservation exactly."""
        try:
            released = release_ray_job_request_reservation(
                task_execution,
                handle,
                expected_reference=getattr(
                    task_execution,
                    "ray_job_request_reference",
                    None,
                ),
            )
        except RayJobRequestStorageError as error:
            raise RayJobRequestPreparationError(_fixed_preparation_rejection(error)) from error
        if not released:
            raise RayJobRequestPreparationError(RayJobRequestPreparationRejection.BINDING_MISMATCH)

    def submit(
        self,
        task_execution: RayTaskExecution,
        callable_path: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> SubmissionHandle:
        """Submit from a persisted, RUNNING, manager-claimed execution row.

        The public path reserves its rq2 ID and address with an exact row CAS.
        It never submits an unclaimed row or falls back to the rq1 transport.
        An existing exact reservation raises an uncertain-acceptance result for
        reconciliation without issuing or releasing a second remote submission.
        """
        input_reference = getattr(task_execution, "input_reference", None)
        if input_reference:
            args_json = task_execution.args_json
            kwargs_json = task_execution.kwargs_json
        else:
            args_json = serialize_args(list(args))
            kwargs_json = serialize_args(kwargs)
        handle = self.submission_handle(task_execution)
        newly_reserved = self._reserve_public_submission(
            task_execution,
            handle,
            callable_path=callable_path,
            args_json=args_json,
            kwargs_json=kwargs_json,
            input_reference=input_reference,
        )
        if not newly_reserved:
            # Another caller owns the durable tuple but may not yet have crossed
            # the remote submission boundary. Never report definite submission,
            # submit it again, or release the other caller's reservation.
            raise RayJobSubmissionUncertainError(
                handle.ray_job_id,
                "durable submission reservation already exists",
            )
        try:
            return self._submit_serialized_request(
                task_execution=task_execution,
                callable_path=callable_path,
                args_json=args_json,
                kwargs_json=kwargs_json,
                input_reference=input_reference,
            )
        except RayJobSubmissionUncertainError:
            raise
        except Exception:
            if newly_reserved:
                self._release_public_submission(task_execution, handle)
            raise

    def submit_durable(self, task_execution: RayTaskExecution) -> SubmissionHandle:
        """Submit one already-reserved task-manager row using its opaque inputs."""
        return self._submit_serialized_request(
            task_execution=task_execution,
            callable_path=task_execution.callable_path,
            args_json=task_execution.args_json,
            kwargs_json=task_execution.kwargs_json,
            input_reference=getattr(task_execution, "input_reference", None),
        )

    def _submit_serialized_request(
        self,
        *,
        task_execution: RayTaskExecution,
        callable_path: str,
        args_json: str,
        kwargs_json: str,
        input_reference: str | None,
    ) -> SubmissionHandle:
        """Submit one canonical request without hydrating application input."""
        handle = self.submission_handle(task_execution)
        persisted_runtime_env = runtime_env_for_execution(task_execution)
        from django_ray.workflow.plans import runtime_env_plan_identity

        settings = get_settings()
        contract = _resolve_ray_task_contract(callable_path)
        runtime_env = persisted_runtime_env
        if (
            contract is not None
            and contract.runtime_env_profile is not None
            and persisted_runtime_env.profile is None
            and not persisted_runtime_env.spec
        ):
            from django_ray.runtime.runtime_env import resolve_runtime_env_profile

            runtime_env = resolve_runtime_env_profile(
                contract.runtime_env_profile,
                config=settings,
            )
        trust_identity = settings.get("WORKFLOW_PLAN_TRUST_IDENTITY", {})
        source_runtime_env_identity = runtime_env_plan_identity(
            runtime_env,
            trust_identity=trust_identity,
        )
        request_started = False
        try:
            with snapshot_local_runtime_env(runtime_env) as immutable_snapshot:
                snapshot_runtime_env_identity = runtime_env_plan_identity(
                    immutable_snapshot,
                    trust_identity=trust_identity,
                )
                if (
                    snapshot_runtime_env_identity.manifest["digest"]
                    != source_runtime_env_identity.manifest["digest"]
                ):
                    from django_ray.workflow.plans import WorkflowPlanMismatchError

                    raise WorkflowPlanMismatchError(
                        "Outer RuntimeEnv immutable snapshot differs from its effective plan"
                    )
                try:
                    request_identity = ExecutionIdentity(
                        task_execution_pk=int(task_execution.pk),
                        task_id=str(task_execution.task_id),
                        attempt_number=int(task_execution.attempt_number),
                        execution_generation=int(task_execution.execution_generation),
                    )
                    # Keep persisted_runtime_env on ExecutionRequest so
                    # register_and_attach_ray_job_request() matches the exact DB
                    # row binding (runtime_env_profile/runtime_env_hash); any
                    # legacy @ray_task(runtime_env_profile=...) fallback is
                    # carried in snapshot_runtime_env_identity and submitted_runtime_env.
                    execution_request = ExecutionRequest(
                        identity=request_identity,
                        execution_protocol_version=int(task_execution.execution_protocol_version),
                        callable_path=callable_path,
                        transport_version=2 if input_reference else 1,
                        serialized_args=args_json,
                        serialized_kwargs=kwargs_json,
                        input_reference=input_reference,
                        runtime_env_profile=persisted_runtime_env.profile,
                        runtime_env_hash=persisted_runtime_env.digest,
                        runtime_env_plan_identity=(
                            snapshot_runtime_env_identity.as_transport_dict()
                        ),
                        compiled_graph_submission_transport="ray-job",
                    )
                    payload_json = encode_execution_request(execution_request)
                except (TypeError, ValueError, OverflowError) as error:
                    rejection = (
                        _fixed_preparation_rejection(error)
                        if isinstance(error, ExecutionRequestEncodeError)
                        else RayJobRequestPreparationRejection.INVALID_REQUEST
                    )
                    raise RayJobRequestPreparationError(rejection) from error

                try:
                    prepared_request = prepare_ray_job_request(payload_json, settings)
                    metadata = build_ray_job_request_reference_metadata(
                        execution_request,
                        payload_json,
                        prepared_request.reference,
                        prepared_request.encoded_locator,
                    )
                    attached_reference = register_and_attach_ray_job_request(
                        prepared_request,
                        task_execution=task_execution,
                        submission_handle=handle,
                    )
                except (RayJobRequestStorageError, RayJobRequestBindingError) as error:
                    raise RayJobRequestPreparationError(
                        _fixed_preparation_rejection(error)
                    ) from error
                if attached_reference != prepared_request.reference:
                    raise RayJobRequestPreparationError(
                        RayJobRequestPreparationRejection.INTEGRITY_MISMATCH
                    )

                entrypoint = (
                    "python -m django_ray.runtime.entrypoint --request-ref-b64 "
                    f"{prepared_request.encoded_locator}"
                )

                # The canonical request and its durable binding are complete
                # before a Ray client is opened or any RuntimeEnv is uploaded.
                client = self._get_client(handle.ray_address)
                submission_timeout = _submission_request_timeout_seconds(settings)
                submitted_spec = json.loads(immutable_snapshot.serialized)
                with _bounded_submission_requests(client, timeout_seconds=submission_timeout):
                    client._upload_working_dir_if_needed(submitted_spec)
                    client._upload_py_modules_if_needed(submitted_spec)
                from ray.runtime_env import RuntimeEnv

                submitted_runtime_env = normalize_runtime_env(
                    RuntimeEnv(**submitted_spec).to_dict(),
                    profile=runtime_env.profile,
                    source=f"prepared Ray Job RuntimeEnv for task {task_execution.pk}",
                )
                verified_source_identity = runtime_env_plan_identity(
                    runtime_env,
                    trust_identity=trust_identity,
                )
                if (
                    verified_source_identity.manifest["digest"]
                    != source_runtime_env_identity.manifest["digest"]
                ):
                    from django_ray.workflow.plans import WorkflowPlanMismatchError

                    raise WorkflowPlanMismatchError(
                        "Outer RuntimeEnv local content changed while it was being snapshotted"
                    )

                _warn_if_plaintext_http_runtime_env_vars(
                    getattr(client, "_address", None) or handle.ray_address,
                    submitted_runtime_env.spec,
                )
                submit_kwargs: dict[str, Any] = {
                    "entrypoint": entrypoint,
                    "runtime_env": submitted_runtime_env.spec,
                    "submission_id": handle.ray_job_id,
                    "metadata": metadata,
                }
                if contract is not None:
                    if contract.num_cpus is not None:
                        submit_kwargs["entrypoint_num_cpus"] = contract.num_cpus
                    if contract.num_gpus is not None:
                        submit_kwargs["entrypoint_num_gpus"] = contract.num_gpus
                    if contract.memory is not None:
                        submit_kwargs["entrypoint_memory"] = contract.memory
                request_started = True
                with _bounded_submission_requests(client, timeout_seconds=submission_timeout):
                    returned_submission_id = client.submit_job(**submit_kwargs)
        except Exception as exc:
            if request_started:
                raise RayJobSubmissionUncertainError(
                    handle.ray_job_id,
                    f"submission request or post-request cleanup raised "
                    f"{materialize_exception_text(exc)}",
                ) from exc
            raise

        if (
            type(returned_submission_id) is not str
            or len(returned_submission_id) != len(handle.ray_job_id)
            or returned_submission_id != handle.ray_job_id
        ):
            raise RayJobSubmissionUncertainError(
                handle.ray_job_id,
                "submit_job returned an unexpected submission ID",
                observed_submission_id=None,
            )
        return handle

    def get_status(self, handle: SubmissionHandle) -> JobInfo:
        """Get status of a Ray job using a single get_job_info() RPC when available."""
        client: Any | None = None
        try:
            client = self._get_client(handle.ray_address)
            with _bounded_control_requests(client):
                get_info = getattr(client, "get_job_info", None)
                if callable(get_info):
                    info = get_info(handle.ray_job_id)
                    raw_status = getattr(info, "status", None)
                    if raw_status is None:
                        raw_status = client.get_job_status(handle.ray_job_id)
                else:
                    info = None
                    raw_status = client.get_job_status(handle.ray_job_id)

            status_map = {
                "PENDING": JobStatus.PENDING,
                "RUNNING": JobStatus.RUNNING,
                "SUCCEEDED": JobStatus.SUCCEEDED,
                "FAILED": JobStatus.FAILED,
                "STOPPED": JobStatus.STOPPED,
            }
            normalized_status = str(getattr(raw_status, "value", raw_status)).upper()

            return JobInfo(
                job_id=handle.ray_job_id,
                status=status_map.get(normalized_status, JobStatus.UNKNOWN),
                message=getattr(info, "message", None),
                start_time=getattr(info, "start_time", None),
                end_time=getattr(info, "end_time", None),
                metadata=fixed_safe_ray_job_metadata(getattr(info, "metadata", None)),
                driver_exit_code=(
                    getattr(info, "driver_exit_code", None)
                    if type(getattr(info, "driver_exit_code", None)) is int
                    else None
                ),
            )
        except Exception as e:
            if client is not None and isinstance(e, (ConnectionError, TimeoutError, OSError)):
                _evict_job_client(client)
            return JobInfo(
                job_id=handle.ray_job_id,
                status=JobStatus.UNKNOWN,
                message=materialize_exception_message(e),
            )

    def cancel(self, handle: SubmissionHandle) -> bool:
        """Cancel a Ray job."""
        return self.cancel_with_status(handle).status == CancellationOutcomeStatus.REQUESTED

    def prepare_cancellation(self, handle: SubmissionHandle) -> Any:
        """Resolve the address-pinned control client before a caller takes a row lock."""
        return self._get_client(handle.ray_address)

    def cancel_prepared_with_status(
        self,
        handle: SubmissionHandle,
        client: Any,
    ) -> CancellationOutcome:
        """Stop a Ray Job through a previously resolved, address-pinned client."""
        try:
            with _bounded_control_requests(client):
                stopped = client.stop_job(handle.ray_job_id)
            if stopped:
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)
            return CancellationOutcome(
                CancellationOutcomeStatus.NOT_APPLICABLE,
                "Ray Job was not running when the stop request arrived",
            )
        except Exception as exc:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Ray Job stop request raised {materialize_exception_text(exc)}",
            )

    def cancel_with_status(self, handle: SubmissionHandle) -> CancellationOutcome:
        """Request a Ray Job stop while preserving an indeterminate API result."""
        try:
            client = self.prepare_cancellation(handle)
        except Exception as exc:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Ray Job stop request raised {materialize_exception_text(exc)}",
            )
        return self.cancel_prepared_with_status(handle, client)

    def get_logs(self, handle: SubmissionHandle) -> str | None:
        """Get logs from a Ray job."""
        try:
            client = self._get_client(handle.ray_address)
            with _bounded_control_requests(client):
                return client.get_job_logs(handle.ray_job_id)
        except Exception:
            return None
