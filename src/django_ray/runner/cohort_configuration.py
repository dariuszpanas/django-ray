"""Prepare finite manager configuration outside claim locks.

The worker supplies aliases whose backend classes it already validated. This
adapter never imports a backend or task callable, resolves an endpoint, uploads
files, or reads the database. Jobs uses the manager's default RuntimeEnv as its
trusted control profile; task/backend overrides cannot replace it. Core binds
only its process connection settings. Sync requires no Ray qualification plan.
Digests record configuration, not source authenticity or the uploaded mapping.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ImproperlyConfigured

from django_ray.runner.cohort_qualification import PreparedCohortAlias
from django_ray.runtime.runtime_env import normalize_runtime_env, resolve_runtime_env_profile
from django_ray.target.attestation import RayRunnerFamily
from django_ray.target.cohort_intent import (
    CohortExecutionDeclaration,
    CohortIntentError,
    CohortSelectionPolicy,
    _endpoint,
    cohort_declaration_digest,
)
from django_ray.target.cohort_job_control import (
    COHORT_PROBE_RUNTIME_ENV_MAX_BYTES,
    CohortJobInspectionError,
    cohort_probe_submitted_runtime_env_digest,
)

_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_MAX_ALIASES = 64


class CohortConfigurationError(ValueError):
    def __init__(self) -> None:
        super().__init__("Invalid current-cohort manager configuration")


@dataclass(frozen=True, slots=True)
class PreparedCohortWorkerConfiguration:
    aliases: tuple[PreparedCohortAlias, ...]
    core_configuration_digest: str | None
    control_profile_digest: str
    control_runtime_env_json: str | None = field(repr=False)
    django_settings_module: str | None = field(repr=False)


def _names(values: object, *, maximum: int, allow_sets: bool = False) -> tuple[str, ...]:
    if allow_sets and isinstance(values, set | frozenset):
        values = sorted(values)
    if isinstance(values, str | bytes) or not isinstance(values, Sequence):
        raise CohortConfigurationError
    names = tuple(values)
    if len(names) > maximum or any(
        type(name) is not str
        or not 0 < len(name) <= 128
        or any(not 33 <= ord(char) <= 126 for char in name)
        for name in names
    ):
        raise CohortConfigurationError
    if len(set(names)) != len(names):
        raise CohortConfigurationError
    return names


def _queues(values: object) -> tuple[str, ...]:
    # Match worker queue selection: exact Unicode/space spelling is significant,
    # with ordered deduplication. The model limits persisted names to 100 chars.
    if isinstance(values, set | frozenset):
        if any(type(value) is not str for value in values):
            raise CohortConfigurationError
        values = sorted(values)
    if isinstance(values, str | bytes) or not isinstance(values, Sequence):
        raise CohortConfigurationError
    if len(values) > 64 or any(
        type(value) is not str or not value.strip() or len(value) > 100 or "\x00" in value
        for value in values
    ):
        raise CohortConfigurationError
    return tuple(dict.fromkeys(values))


def cohort_core_connection_digest(
    *, execution_mode: str, address: str | None, control_settings: dict[str, Any]
) -> str:
    """Bind the process-selected connection without an alias routing assertion.

    The worker must supply the options used to create its one actual connection.
    Local mode uses the literal local descriptor, not a backend endpoint. Alias
    edits therefore change admission declarations without forcing a new physical
    observation. Reconnect still advances a separate manager connection epoch.
    """
    try:
        if execution_mode == "local":
            if address is not None:
                raise CohortConfigurationError
            selected_address = "local"
        elif execution_mode == "cluster":
            selected_address = _endpoint(address)
        else:
            raise CohortConfigurationError
        normalized = normalize_runtime_env(control_settings)
        if len(normalized.serialized.encode("utf-8")) > COHORT_PROBE_RUNTIME_ENV_MAX_BYTES:
            raise CohortConfigurationError
        encoded = json.dumps(
            {"mode": execution_mode, "address": selected_address, "control": normalized.spec},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return (
            "sha256:" + hashlib.sha256(b"django-ray/core-connection/v1\x00" + encoded).hexdigest()
        )
    except (CohortIntentError, ImproperlyConfigured, TypeError, ValueError, OverflowError):
        raise CohortConfigurationError from None


def prepare_cohort_worker_configuration(
    *,
    tasks: Mapping[str, Any],
    validated_aliases: Sequence[str],
    selected_queues: Sequence[str],
    manager_settings: dict[str, Any],
    django_settings_module: str | None,
    runner_family: RayRunnerFamily,
    execution_mode: str,
    core_address: str | None = None,
    core_control_settings: dict[str, Any] | None = None,
) -> PreparedCohortWorkerConfiguration:
    """Snapshot current declarations and the trusted manager control profile.

    ``validated_aliases`` comes from the worker's backend-class validation, not
    from queued rows. No task-specific RuntimeEnv or historical intent enters
    this plan. The caller compares a freshly prepared plan outside locks, then
    rechecks its retained configuration epoch inside the claim transaction.
    """
    try:
        aliases = _names(validated_aliases, maximum=_MAX_ALIASES)
        queues = _queues(selected_queues)
        if not queues or type(runner_family) is not RayRunnerFamily:
            raise CohortConfigurationError
        core_digest = None
        control_json = None
        if runner_family is RayRunnerFamily.RAY_CORE:
            core_digest = cohort_core_connection_digest(
                execution_mode=execution_mode,
                address=core_address,
                control_settings={} if core_control_settings is None else core_control_settings,
            )
            profile_digest = core_digest
            django_settings_module = None
        else:
            if (
                execution_mode != "ray"
                or type(django_settings_module) is not str
                or len(django_settings_module) > 255
                or _MODULE.fullmatch(django_settings_module) is None
            ):
                raise CohortConfigurationError
            resolved = resolve_runtime_env_profile(config=manager_settings)
            environment = dict(resolved.spec)
            environment["env_vars"] = {
                **(environment.get("env_vars") or {}),
                "DJANGO_SETTINGS_MODULE": django_settings_module,
            }
            resolved = normalize_runtime_env(environment)
            profile_digest = cohort_probe_submitted_runtime_env_digest(resolved.spec)
            control_json = resolved.serialized
        current = []
        for alias in aliases:
            backend = tasks[alias]
            options = backend.get("OPTIONS", {})
            declared_queues = _queues(backend.get("QUEUES", ["default"]))
            accepted_queues = tuple(queue for queue in queues if queue in declared_queues)
            if not accepted_queues:
                continue
            jobs_only = options.get("RAY_JOB_ONLY", False)
            if type(jobs_only) is not bool:
                raise CohortConfigurationError
            if jobs_only and runner_family is not RayRunnerFamily.RAY_JOB:
                continue
            declaration = CohortExecutionDeclaration(
                alias,
                options.get("RAY_ADDRESS", manager_settings.get("RAY_ADDRESS", "auto")),
                jobs_only,
                manager_settings.get("WORKFLOW_PLAN_TRUST_IDENTITY", {}),
            )
            current.append(
                PreparedCohortAlias(
                    alias,
                    cohort_declaration_digest(declaration),
                    CohortSelectionPolicy.JOBS_ONLY
                    if jobs_only
                    else CohortSelectionPolicy.WORKER_SELECTED,
                    accepted_queues,
                    profile_digest,
                )
            )
        return PreparedCohortWorkerConfiguration(
            tuple(current), core_digest, profile_digest, control_json, django_settings_module
        )
    except (
        CohortIntentError,
        CohortJobInspectionError,
        ImproperlyConfigured,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        OverflowError,
    ):
        raise CohortConfigurationError from None
