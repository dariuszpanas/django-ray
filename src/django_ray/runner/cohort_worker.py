"""One worker incarnation's finite current-cohort admission controller.

The command retains application handles and processes authentic completions
before calling ``tick``. This controller owns only connection/qualification
operations and new claims. A failed operation cannot turn an existing task into
LOST, authorize replay, or silently replace its selected connection.
"""

from __future__ import annotations

import platform
import sys
import time
from datetime import UTC, datetime
from typing import Any

from django_ray import __version__
from django_ray.runner.cohort_claims import CohortClaimAlias, claim_cohort_tasks
from django_ray.runner.cohort_configuration import prepare_cohort_worker_configuration
from django_ray.runner.cohort_connection import CoreConnectionLifecycle, CoreConnectionPhase
from django_ray.runner.cohort_core import CoreCohortManagerAdapter
from django_ray.runner.cohort_jobs import JobsCohortManagerAdapter, JobsCohortPhase
from django_ray.runner.cohort_qualification import CohortQualificationLifecycle
from django_ray.runtime.cohort_job import CohortProbeJobLease
from django_ray.target.attestation import RayRunnerFamily
from django_ray.target.cohort_claim import (
    CohortManagerRuntime,
    CohortPythonVersion,
    CohortRunnerFamily,
)
from django_ray.target.cohort_intent import (
    CohortSelectionPolicy,
    cohort_declaration_digest,
    prepare_cohort_declaration,
)


class CohortWorkerController:
    """Parent-owned progress; no waiting for native connection or probe work."""

    def __init__(
        self,
        identity,
        *,
        execution_mode,
        validated_aliases,
        selected_queues,
        tasks,
        manager_settings,
        django_settings_module=None,
        core_address=None,
        core_control_settings=None,
        connect=None,
        monotonic=time.monotonic,
    ):
        self.identity = identity
        self.family = CohortRunnerFamily(
            "sync"
            if execution_mode == "sync"
            else "ray_job"
            if execution_mode == "ray"
            else "ray_core"
        )
        self._monotonic = monotonic
        self._connect = connect
        self._next_probe = 0.0
        self._alias_cursor = 0
        self._stopped = False
        self._retiring = False
        self._cleanup_started = False
        self.reason = None
        self.connection = None
        self.connection_ticket = None
        self.lifecycle = None
        self.adapter = None
        self.configuration = None
        self._configuration_arguments: dict[str, Any] = {
            "tasks": tasks,
            "validated_aliases": validated_aliases,
            "selected_queues": selected_queues,
            "manager_settings": manager_settings,
            "django_settings_module": django_settings_module,
            "execution_mode": execution_mode,
            "core_address": core_address,
            "core_control_settings": core_control_settings,
        }
        python = CohortPythonVersion(
            platform.python_implementation().strip().lower(),
            sys.version_info.major,
            sys.version_info.minor,
            sys.version_info.micro,
        )
        if self.family is CohortRunnerFamily.SYNC:
            self.runtime = CohortManagerRuntime(__version__, python)
            self.aliases = self._sync_aliases(tasks, manager_settings)
            return
        import ray

        from django_ray.target.cohort_runtime import _local_runtime

        package, runtime = _local_runtime(ray)
        if package != __version__:
            raise ValueError("Current-cohort manager package changed")
        self.runtime = CohortManagerRuntime(
            package, python, (runtime.ray_major, runtime.ray_minor, runtime.ray_patch)
        )
        family = RayRunnerFamily(self.family.value)
        self.configuration = prepare_cohort_worker_configuration(
            **self._configuration_arguments, runner_family=family
        )
        self.aliases = tuple(
            CohortClaimAlias(
                item.alias, item.declaration_digest, item.selection_policy, item.queues
            )
            for item in self.configuration.aliases
        )
        self.lifecycle = CohortQualificationLifecycle(
            CohortProbeJobLease(
                identity.worker_id, identity.hostname, identity.pid, identity.started_at
            ),
            package,
            runtime,
            family,
            monotonic=monotonic,
        )
        if self.family is CohortRunnerFamily.RAY_JOB:
            self.adapter = JobsCohortManagerAdapter(
                self.lifecycle, self.configuration, monotonic=monotonic
            )
        else:
            if not callable(connect):
                raise ValueError("Current-cohort Core connection is unavailable")
            self.lifecycle.configure_aliases(self.configuration.aliases)
            self.adapter = CoreCohortManagerAdapter(self.lifecycle)
            self.connection = CoreConnectionLifecycle(monotonic=monotonic)

    def _sync_aliases(self, tasks, manager_settings):
        aliases = self._configuration_arguments["validated_aliases"]
        queues = self._configuration_arguments["selected_queues"]
        if len(aliases) > 64 or not 1 <= len(queues) <= 64:
            raise ValueError("Current-cohort configuration exceeds bounds")
        result = []
        for alias in aliases:
            backend = tasks[alias]
            options = backend.get("OPTIONS", {})
            declaration = prepare_cohort_declaration(
                alias, options=options, current_settings=manager_settings
            )
            selected = tuple(
                queue for queue in queues if queue in backend.get("QUEUES", ["default"])
            )
            if not declaration.ray_job_only and selected:
                result.append(
                    CohortClaimAlias(
                        alias,
                        cohort_declaration_digest(declaration),
                        CohortSelectionPolicy.WORKER_SELECTED,
                        selected,
                    )
                )
        return tuple(result)

    def check_configuration(self, *, tasks, manager_settings):
        """Stop new admission on configuration replacement; retain cleanup ownership."""
        if self._stopped:
            return False
        try:
            if self.family is CohortRunnerFamily.SYNC:
                same = self._sync_aliases(tasks, manager_settings) == self.aliases
            else:
                arguments = dict(
                    self._configuration_arguments, tasks=tasks, manager_settings=manager_settings
                )
                same = (
                    prepare_cohort_worker_configuration(
                        **arguments, runner_family=RayRunnerFamily(self.family.value)
                    )
                    == self.configuration
                )
        except Exception:
            same = False
        if not same:
            self.invalidate("configuration_changed")
        return same

    @property
    def connected(self):
        if self.connection is None or self.connection_ticket is None:
            return False
        return self.connection.poll(self.connection_ticket).phase is CoreConnectionPhase.CONNECTED

    @property
    def stopped(self):
        return self._stopped or self._retiring

    def qualifications(self):
        if self._stopped or self._retiring or self.adapter is None:
            return ()
        try:
            return self.adapter.qualified_aliases()
        except Exception:
            self.reason = "qualification_unavailable"
            return ()

    def tick(self):
        """Advance at most one bounded parent operation and one probe start."""
        if self._retiring:
            self._tick_retirement()
            return
        if self._stopped or self.family is CohortRunnerFamily.SYNC:
            return
        if self.connection is not None:
            if self.connection_ticket is None:
                self.connection_ticket = self.connection.begin(
                    self.configuration.core_configuration_digest, connect=self._connect
                )
                return
            state = self.connection.poll(self.connection_ticket)
            if state.phase is not CoreConnectionPhase.CONNECTED:
                if state.reason is not None:
                    self.reason = "connection_unavailable"
                return
        now = self._monotonic()
        try:
            if self.family is CohortRunnerFamily.RAY_CORE:
                assert isinstance(self.adapter, CoreCohortManagerAdapter)
                if self.lifecycle.outstanding is not None:
                    if self.adapter.poll() is not None:
                        self.reason = None
                if self.lifecycle.outstanding is None and now >= self._next_probe:
                    qualified = self.qualifications()
                    if not qualified:
                        self._next_probe = now + 1.0
                        self.adapter.begin(
                            self.configuration.core_configuration_digest,
                            self.connection_ticket.epoch,
                        )
                return
            assert isinstance(self.adapter, JobsCohortManagerAdapter)
            ticket = self.adapter.outstanding
            if ticket is not None:
                if self.adapter.poll(ticket) is not None:
                    self.reason = None
                if self.adapter.outstanding is None:
                    self._cleanup_started = False
                return
            if now < self._next_probe or not self.aliases:
                return
            qualified = {item.configuration.alias for item in self.qualifications()}
            for offset in range(len(self.aliases)):
                index = (self._alias_cursor + offset) % len(self.aliases)
                alias = self.aliases[index].alias
                if alias not in qualified:
                    self._alias_cursor = (index + 1) % len(self.aliases)
                    self._next_probe = now + 1.0
                    self.adapter.begin(alias)
                    break
        except Exception:
            self.reason = "qualification_unavailable"
            # Jobs cleanup is a separate owned operation. A helper still being
            # reaped or unknown discovery never permits a replacement probe.
            if self.family is CohortRunnerFamily.RAY_JOB:
                self._poll_jobs_cleanup()

    def _poll_jobs_cleanup(self):
        assert isinstance(self.adapter, JobsCohortManagerAdapter)
        ticket = self.adapter.outstanding
        if ticket is None:
            self._cleanup_started = False
            return
        if self.adapter.phase is JobsCohortPhase.BLOCKED and not self._cleanup_started:
            try:
                self.adapter.begin_cleanup(ticket)
                self._cleanup_started = True
            except Exception:
                self.reason = "cleanup_unconfirmed"

    def claim(self, *, limit):
        if self._stopped or self._retiring or limit <= 0:
            return ()
        return claim_cohort_tasks(
            self.identity,
            aliases=self.aliases,
            qualifications=self.qualifications(),
            runner_family=self.family,
            manager_runtime=self.runtime,
            limit=min(limit, 100),
            now=datetime.now(UTC),
        )

    def request_retirement(self):
        """Stop new ownership while letting already-owned proof work finish."""
        self._retiring = True
        self.reason = "worker_retiring"

    @property
    def retirement_ready(self):
        """Owned proof work is gone; connection teardown remains separate."""
        return (
            self._retiring
            and self._stopped
            and (self.lifecycle is None or self.lifecycle.outstanding is None)
            and (self.adapter is None or getattr(self.adapter, "outstanding", None) is None)
        )

    def _tick_retirement(self):
        if self._stopped:
            self.poll_stopped_cleanup()
            return
        if self.adapter is not None:
            if self.family is CohortRunnerFamily.RAY_CORE:
                assert isinstance(self.adapter, CoreCohortManagerAdapter)
                if self.lifecycle.outstanding is not None:
                    try:
                        self.adapter.poll()
                    except Exception:
                        self.invalidate("cleanup_unconfirmed")
                    return
            elif (
                isinstance(self.adapter, JobsCohortManagerAdapter)
                and self.adapter.outstanding is not None
            ):
                try:
                    self.adapter.poll(self.adapter.outstanding)
                except Exception:
                    self._poll_jobs_cleanup()
                return
        self.invalidate("worker_retiring")

    def invalidate(self, reason="stopped"):
        self._stopped = True
        self.reason = reason
        if self.adapter is not None:
            if self.family is CohortRunnerFamily.RAY_JOB:
                self.adapter.invalidate()
            else:
                self.lifecycle.invalidate()

    def poll_stopped_cleanup(self):
        """Reap only retained qualification operations after admission stops."""
        if not self._stopped:
            return
        if self.family is CohortRunnerFamily.RAY_JOB and self.adapter.outstanding is not None:
            assert isinstance(self.adapter, JobsCohortManagerAdapter)
            try:
                self.adapter.poll(self.adapter.outstanding)
            except Exception:
                self._poll_jobs_cleanup()
