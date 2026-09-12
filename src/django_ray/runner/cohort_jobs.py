"""Private parent Jobs composition; no production worker consumes this adapter.

One immutable configuration belongs to one exact lease incarnation. Configuration
replacement requires invalidation, independently confirmed cleanup and a fresh
manager incarnation. At most 64 alias records retain nonce/reservation ownership,
including failures. A helper's terminal probe Job is not normal-task quiescence.
No network, Ray connection or task callable runs on the polling parent.
"""

from __future__ import annotations

import json
import math
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from django.db import DEFAULT_DB_ALIAS, connections, transaction

from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetPolicyRevision,
    RayTargetProbeJobReceipt,
    RayWorkerTargetCapability,
)
from django_ray.runner.cohort_client_discovery import (
    validate_client_discovery_observation,
    validate_client_discovery_request,
    validate_client_driver_inspection,
)
from django_ray.runner.cohort_configuration import PreparedCohortWorkerConfiguration
from django_ray.runner.cohort_process import CohortProcessSupervisor
from django_ray.runner.cohort_qualification import (
    CohortQualificationLifecycle,
    PublishedCohortQualification,
    SharedCohortQualification,
)
from django_ray.runner.leasing import WorkerLeaseIdentity, get_lease_duration
from django_ray.runtime.cohort_job import (
    CohortProbeJobRequest,
    encode_probe_job_request,
    probe_job_request_digest,
    probe_job_submission_id,
)
from django_ray.runtime.cohort_job_entrypoint import (
    CohortProbeJobLaunch,
    encode_probe_job_launch,
    probe_job_launch_entrypoint,
)
from django_ray.target import capabilities, cohort_publication, coordination
from django_ray.target.attestation import (
    RayRunnerFamily,
    compare_ray_target_attestation,
    decode_ray_cluster_attestation,
    encode_ray_cluster_attestation,
)
from django_ray.target.cohort_contract import _parse_timestamp, _timestamp
from django_ray.target.cohort_intent import _endpoint
from django_ray.target.cohort_job_control import (
    InspectedCohortJobReceipt,
    cohort_probe_submitted_runtime_env_digest,
)
from django_ray.target.cohort_job_receipt import decode_cohort_job_receipt
from django_ray.target.cohort_job_receipt_storage import (
    _validate_reservation,
    reserve_cohort_job_probe,
)
from django_ray.target.cohort_job_retirement import retire_and_reissue_cohort_job_probe
from django_ray.target.cohort_probe_challenges import (
    _locked_probe_lease,
    issue_ray_target_probe_challenge,
    replace_ray_target_probe_challenge,
)


class JobsCohortAdapterReason(StrEnum):
    INVALID = "invalid"
    BUSY = "busy"
    STALE = "stale"
    UNSUPPORTED_ADDRESS = "unsupported_address"
    DEADLINE = "deadline"
    OPERATION_FAILED = "operation_failed"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"
    PUBLICATION_CHANGED = "publication_changed"


class JobsCohortAdapterError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Jobs cohort adapter refused: {reason.value}")


class JobsCohortPhase(StrEnum):
    DISCOVERING = "discovering"
    INSPECTING_DRIVER = "inspecting_driver"
    PREPARING = "preparing"
    SUBMITTING = "submitting"
    WAITING_RECEIPT = "waiting_receipt"
    INSPECTING = "inspecting"
    BLOCKED = "blocked"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class JobsCohortOperation:
    sequence: int
    alias: str = field(repr=False)
    deadline: float = field(repr=False)


@dataclass(slots=True)
class _AliasRecord:
    issued: object = field(default=None, repr=False)
    launch: object = field(default=None, repr=False)
    reservation: object = field(default=None, repr=False)
    challenge_revision: int | None = None
    receipt_digest: str | None = None
    received_at: datetime | None = None
    cleanup_at: datetime | None = None
    submitted: bool = False
    environment: str | None = field(default=None, repr=False)
    endpoint: str | None = field(default=None, repr=False)


@dataclass(slots=True)
class _Operation:
    ticket: JobsCohortOperation
    phase: JobsCohortPhase
    challenge_ttl: int
    helper: object = field(default=None, repr=False)
    lifecycle_ticket: object = field(default=None, repr=False)
    prepared: object = field(default=None, repr=False)
    blocked: JobsCohortAdapterReason | None = None
    cleanup_deadline: float | None = None
    followups: int = 1
    discovery_request: dict | None = field(default=None, repr=False)
    discovery_observation: dict | None = field(default=None, repr=False)
    driver_terminal: bool = False
    prepare_address: str | None = field(default=None, repr=False)


def _reject(reason):
    raise JobsCohortAdapterError(reason) from None


class JobsCohortManagerAdapter:
    """Poll one exact helper operation while the manager keeps heartbeating.

    ``supervisor`` is a trusted parent-owned process controller, not an input
    carrier. Only its accepted success can corroborate an endpoint inspection.
    No DB row or caller-created inspected receipt can seed this adapter's cache.
    """

    def __init__(
        self,
        lifecycle: CohortQualificationLifecycle,
        configuration: PreparedCohortWorkerConfiguration,
        *,
        supervisor=None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        using: str = DEFAULT_DB_ALIAS,
    ):
        try:
            if (
                type(lifecycle) is not CohortQualificationLifecycle
                or lifecycle.runner_family is not RayRunnerFamily.RAY_JOB
                or type(configuration) is not PreparedCohortWorkerConfiguration
                or using != DEFAULT_DB_ALIAS
                or not callable(monotonic)
                or not callable(wall_clock)
                or configuration.core_configuration_digest is not None
                or type(configuration.control_runtime_env_json) is not str
                or len(configuration.control_runtime_env_json.encode("utf-8")) > 64 * 1024
                or type(configuration.django_settings_module) is not str
            ):
                raise ValueError
            addresses = dict(configuration.job_addresses)
            if (
                len(addresses) != len(configuration.job_addresses)
                or set(addresses) != {item.alias for item in configuration.aliases}
                or len(addresses) > 64
            ):
                raise ValueError
            for endpoint in addresses.values():
                _endpoint(endpoint)
            spec = json.loads(configuration.control_runtime_env_json)
            if (
                cohort_probe_submitted_runtime_env_digest(spec)
                != configuration.control_profile_digest
                or spec.get("env_vars", {}).get("DJANGO_SETTINGS_MODULE")
                != configuration.django_settings_module
                or any(
                    item.control_profile_digest != configuration.control_profile_digest
                    for item in configuration.aliases
                )
            ):
                raise ValueError
            lifecycle.configure_aliases(configuration.aliases)
            if lifecycle.outstanding is not None:
                raise ValueError
        except Exception:
            _reject(JobsCohortAdapterReason.INVALID)
        self.lifecycle = lifecycle
        self._configuration = configuration
        self.identity = WorkerLeaseIdentity(**asdict(lifecycle.lease))
        self._addresses = addresses
        self._aliases = {item.alias: item for item in configuration.aliases}
        self._records = {alias: _AliasRecord() for alias in addresses}
        self._supervisor = (
            supervisor if supervisor is not None else CohortProcessSupervisor(monotonic=monotonic)
        )
        self._monotonic, self._wall_clock, self._using = monotonic, wall_clock, using
        self._last_mono = self._last_wall = None
        self._sequence = 0
        self._operation = None
        self._invalidated = False

    def _parent(self, ticket=None):
        _ = self.lifecycle.outstanding
        if any(
            c.in_atomic_block or (c.connection is not None and not c.get_autocommit())
            for c in connections.all()
        ):
            _reject(JobsCohortAdapterReason.INVALID)
        if ticket is not None and (self._operation is None or self._operation.ticket is not ticket):
            _reject(JobsCohortAdapterReason.STALE)

    @property
    def configuration(self):
        return self._configuration

    def _time(self):
        try:
            now, mono = self._wall_clock(), self._monotonic()
            _timestamp(now)
            if (
                type(mono) not in (int, float)
                or not math.isfinite(mono)
                or mono < 0
                or self._last_mono is not None
                and mono < self._last_mono
                or self._last_wall is not None
                and now < self._last_wall
            ):
                raise ValueError
        except Exception:
            _reject(JobsCohortAdapterReason.DEADLINE)
        self._last_wall, self._last_mono = now, float(mono)
        return now, float(mono)

    def _lease(self, now):
        try:
            with transaction.atomic(using=self._using):
                return _locked_probe_lease(self.identity, now, using=self._using)
        except Exception:
            self._invalidated = True
            self.lifecycle.invalidate()
            _reject(JobsCohortAdapterReason.STALE)

    @property
    def outstanding(self):
        self._parent()
        return self._operation.ticket if self._operation else None

    @property
    def phase(self):
        self._parent()
        return self._operation.phase if self._operation else None

    def _block(self, reason):
        operation = self._operation
        if operation is None:
            return
        operation.blocked = operation.blocked or reason
        operation.phase = JobsCohortPhase.BLOCKED
        if operation.lifecycle_ticket is not None and self.lifecycle.outstanding is not None:
            self.lifecycle.require_cleanup(operation.lifecycle_ticket)
        if operation.helper is not None:
            if self._supervisor.outstanding is operation.helper:
                self._supervisor.cancel(operation.helper)
            elif self._supervisor.outstanding is None:
                operation.helper = None

    def _start_helper(self, operation, command, arguments, *, cleanup=False):
        _now, mono = self._time()
        deadline = operation.cleanup_deadline if cleanup else operation.ticket.deadline
        remaining = deadline - mono
        if remaining <= 0:
            _reject(JobsCohortAdapterReason.DEADLINE)
        operation.helper = self._supervisor.start(
            {"command": command, "arguments": arguments}, timeout_seconds=min(600, remaining)
        )

    def begin(self, alias, *, timeout_seconds=300, challenge_ttl_seconds=300):
        self._parent()
        if (
            self._operation is not None
            or self.lifecycle.outstanding is not None
            or self._supervisor.outstanding is not None
        ):
            _reject(JobsCohortAdapterReason.BUSY)
        if self._invalidated:
            _reject(JobsCohortAdapterReason.STALE)
        if (
            type(alias) is not str
            or alias not in self._aliases
            or type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 600
            or type(challenge_ttl_seconds) is not int
            or not 1 <= challenge_ttl_seconds <= 600
        ):
            _reject(JobsCohortAdapterReason.INVALID)
        now, mono = self._time()
        self._lease(now)
        self.lifecycle.withdraw_job_alias(alias)
        self._sequence += 1
        ticket = JobsCohortOperation(self._sequence, alias, mono + timeout_seconds)
        operation = _Operation(ticket, JobsCohortPhase.PREPARING, challenge_ttl_seconds)
        self._operation = operation
        try:
            self._withdraw_unused_capabilities(now)
            if self._addresses[alias].startswith("ray://"):
                request = {
                    "discovery_id": uuid.uuid4().hex,
                    "configuration_digest": self._aliases[alias].declaration_digest,
                    "ray_address": self._addresses[alias],
                    "package_version": self.lifecycle.package_version,
                    "runtime": asdict(self.lifecycle.runtime),
                    "issued_at": _timestamp(now),
                    "expires_at": _timestamp(now + timedelta(seconds=min(timeout_seconds, 300))),
                }
                validate_client_discovery_request(request)
                operation.discovery_request = request
                operation.phase = JobsCohortPhase.DISCOVERING
                self._start_helper(operation, "discover-client", request)
            else:
                self._start_preparation(operation, self._addresses[alias])
        except Exception:
            self._block(JobsCohortAdapterReason.OPERATION_FAILED)
            _reject(JobsCohortAdapterReason.OPERATION_FAILED)
        return ticket

    def _start_preparation(self, operation, address):
        operation.prepare_address = address
        operation.phase = JobsCohortPhase.PREPARING
        self._start_helper(
            operation,
            "prepare",
            {
                "ray_address": address,
                "control_runtime_env_json": self.configuration.control_runtime_env_json,
                "source_control_profile_digest": self.configuration.control_profile_digest,
                "django_settings_module": self.configuration.django_settings_module,
                "expected_package_version": self.lifecycle.package_version,
                "expected_runtime": asdict(self.lifecycle.runtime),
            },
        )

    def _inspect_driver(self, operation, *, cleanup=False):
        operation.phase = JobsCohortPhase.INSPECTING_DRIVER
        self._start_helper(
            operation,
            "inspect-driver",
            {
                "request": operation.discovery_request,
                "observation": operation.discovery_observation,
            },
            cleanup=cleanup,
        )

    def _driver_result(self, operation, record, response):
        value = validate_client_driver_inspection(
            operation.discovery_request, operation.discovery_observation, response
        )
        now, mono = self._time()
        if _parse_timestamp(value["inspected_at"]) > now:
            _reject(JobsCohortAdapterReason.INVALID)
        if operation.cleanup_deadline is not None and mono >= operation.cleanup_deadline:
            _reject(JobsCohortAdapterReason.CLEANUP_UNCONFIRMED)
        if value["terminal"] is not True:
            self._inspect_driver(operation, cleanup=operation.cleanup_deadline is not None)
            return
        operation.driver_terminal = True
        if operation.cleanup_deadline is not None:
            if record.launch is None:
                self._operation = None
            else:
                self._finish_cleanup(operation, record, now)
            return
        self._start_preparation(operation, operation.discovery_observation["jobs_endpoint"])

    def _withdraw_unused_capabilities(self, now):
        lease = self._lease(now)
        keep = {
            item.shared.attestation.expectation.target_key
            for item in self.lifecycle.qualified_aliases(
                now=now,
                live_lease=self.lifecycle.lease,
                lease_expires_at=lease.last_heartbeat_at + get_lease_duration(),
            )
        }
        rows = list(
            RayWorkerTargetCapability.objects.using(self._using)
            .filter(lease_id=self.identity.worker_id)
            .values_list("target_id", "revision")[:65]
        )
        if len(rows) > 64:
            _reject(JobsCohortAdapterReason.INVALID)
        for target, revision in rows:
            if target not in keep:
                capabilities.withdraw_ray_worker_target_capability(
                    self.identity, target, expected_capability_revision=revision, using=self._using
                )

    def _diagnostics(self, record, now):
        """Exact pending/terminal receipt metadata for cleanup, never eligibility."""
        row = RayTargetProbeJobReceipt.objects.using(self._using).get(
            pk=record.launch.request.challenge_id
        )
        _validate_reservation(row, record.launch.request, jobs_endpoint=record.launch.jobs_endpoint)
        if row.reserved_at != record.reservation.reserved_at:
            _reject(JobsCohortAdapterReason.STALE)
        if (row.receipt_digest is None) != (row.received_at is None):
            _reject(JobsCohortAdapterReason.STALE)
        if row.received_at is not None and row.received_at > now:
            _reject(JobsCohortAdapterReason.STALE)
        record.receipt_digest, record.received_at = row.receipt_digest, row.received_at

    def _retire(self, record, operation, now):
        self._diagnostics(record, now)
        if record.cleanup_at is None:
            _reject(JobsCohortAdapterReason.CLEANUP_UNCONFIRMED)
        issued = retire_and_reissue_cohort_job_probe(
            self.identity,
            record.launch,
            expected_challenge_revision=record.challenge_revision,
            nonce=record.issued.nonce,
            expected_reserved_at=record.reservation.reserved_at,
            expected_receipt_digest=record.receipt_digest,
            expected_received_at=record.received_at,
            cleanup_confirmed=True,
            cleanup_confirmed_at=record.cleanup_at,
            now=now,
            ttl_seconds=operation.challenge_ttl,
            using=self._using,
        )
        record.issued, record.launch, record.reservation = issued, None, None
        record.challenge_revision = issued.receipt.revision
        record.receipt_digest = record.received_at = record.cleanup_at = None
        record.submitted = False

    def _reserve_and_submit(self, operation, record, *, policy=None, expectation=None):
        now, _mono = self._time()
        self._lease(now)
        if record.launch is not None:
            self._retire(record, operation, now)
            now, _mono = self._time()
            self._lease(now)
        options = {
            "now": now,
            "expected_target_policy_id": policy.pk if policy else None,
            "ttl_seconds": operation.challenge_ttl,
            "using": self._using,
        }
        if record.issued is None:
            issued = issue_ray_target_probe_challenge(
                self.identity,
                self._aliases[operation.ticket.alias].declaration_digest,
                runner_family=RayRunnerFamily.RAY_JOB,
                **options,
            )
        else:
            issued = replace_ray_target_probe_challenge(
                self.identity,
                record.issued.receipt.challenge_id,
                expected_configuration_digest=record.issued.receipt.configuration_digest,
                configuration_digest=record.issued.receipt.configuration_digest,
                expected_revision=record.issued.receipt.revision,
                expected_nonce=record.issued.nonce,
                **options,
            )
        record.issued = issued
        slot = issued.receipt
        record.challenge_revision = slot.revision
        request = CohortProbeJobRequest(
            slot.challenge_id,
            slot.revision,
            self.lifecycle.lease,
            slot.configuration_digest,
            expectation.target_key if expectation else None,
            RayRunnerFamily.RAY_JOB,
            self.lifecycle.package_version,
            self.lifecycle.runtime,
            expectation.cluster_session if expectation else None,
            policy.pk if policy else None,
            expectation.policy_revision if expectation else 1,
            slot.issued_at,
            slot.expires_at,
        )
        environment = json.loads(record.environment)
        launch = CohortProbeJobLaunch(
            request,
            probe_job_request_digest(request),
            record.endpoint,
            cohort_probe_submitted_runtime_env_digest(environment),
            self.configuration.django_settings_module,
        )
        record.launch = launch
        record.reservation = reserve_cohort_job_probe(
            self.identity,
            request,
            nonce=issued.nonce,
            jobs_endpoint=launch.jobs_endpoint,
            entrypoint=probe_job_launch_entrypoint(launch),
            submitted_runtime_env=environment,
            using=self._using,
        )
        now, mono = self._time()
        operation.lifecycle_ticket = self.lifecycle.begin_job(
            operation.ticket.alias,
            launch,
            source_control_profile_digest=self.configuration.control_profile_digest,
            timeout_seconds=operation.ticket.deadline - mono,
            now=now,
        )
        operation.phase = JobsCohortPhase.SUBMITTING
        record.submitted = True  # Cross the ambiguity boundary before starting the helper.
        self._start_helper(
            operation,
            "submit",
            {
                "launch_json": encode_probe_job_launch(launch),
                "submitted_runtime_env_json": record.environment,
            },
        )

    def _prepared_result(self, operation, record, payload):
        if (
            type(payload) is not dict
            or set(payload)
            != {
                "jobs_endpoint",
                "submitted_runtime_env_json",
                "source_control_profile_digest",
                "submitted_runtime_env_digest",
            }
            or payload["source_control_profile_digest"] != self.configuration.control_profile_digest
        ):
            _reject(JobsCohortAdapterReason.INVALID)
        endpoint = _endpoint(payload["jobs_endpoint"])
        if not endpoint.startswith(("http://", "https://")):
            _reject(JobsCohortAdapterReason.INVALID)
        if operation.prepare_address.startswith(
            ("http://", "https://")
        ) and endpoint != operation.prepare_address.rstrip("/"):
            _reject(JobsCohortAdapterReason.INVALID)
        environment = json.loads(payload["submitted_runtime_env_json"])
        if (
            not secrets.compare_digest(
                cohort_probe_submitted_runtime_env_digest(environment),
                payload["submitted_runtime_env_digest"],
            )
            or environment.get("env_vars", {}).get("DJANGO_SETTINGS_MODULE")
            != self.configuration.django_settings_module
        ):
            _reject(JobsCohortAdapterReason.INVALID)
        record.endpoint, record.environment = endpoint, payload["submitted_runtime_env_json"]
        self._reserve_and_submit(operation, record)

    def _inspect(self, operation, record):
        prepared = cohort_publication.prepare_cohort_job_probe(
            self.identity, record.launch, nonce=record.issued.nonce, using=self._using
        )
        if prepared is None:
            return
        now, _mono = self._time()
        self._diagnostics(record, now)
        operation.prepared = prepared
        snapshot = prepared.snapshot
        self._start_helper(
            operation,
            "inspect",
            {
                "request_json": encode_probe_job_request(snapshot.request),
                "request_digest": snapshot.request_digest,
                "jobs_endpoint": snapshot.jobs_endpoint,
                "entrypoint_digest": snapshot.entrypoint_digest,
                "submitted_runtime_env_digest": snapshot.submitted_runtime_env_digest,
                "receipt_json": snapshot.receipt_json,
                "receipt_digest": snapshot.receipt_digest,
            },
        )
        operation.phase = JobsCohortPhase.INSPECTING

    def _retained_policy(self, proof, now):
        target = (
            RayTarget.objects.using(self._using)
            .filter(runner_family="ray_job", cluster_session=proof.expectation.cluster_session)
            .first()
        )
        if target is None:
            return None, None
        with transaction.atomic(using=self._using):
            _locked_probe_lease(self.identity, now, using=self._using)
            target = coordination._locked_target(
                target_key=target.pk,
                using=self._using,
                vendor=capabilities._database_vendor(using=self._using),
            )
            policy, expectation, state = coordination._latest_policy(target, using=self._using)
            if state not in {"active", "draining"} or expectation.runtime != self.lifecycle.runtime:
                _reject(JobsCohortAdapterReason.PUBLICATION_CHANGED)
            return policy, expectation

    def _publish(self, operation, record, inspected):
        proof = inspected.receipt.attestation
        target = (
            RayTarget.objects.using(self._using).filter(pk=proof.expectation.target_key).first()
        )
        policy = (
            RayTargetPolicyRevision.objects.using(self._using)
            .filter(target=target)
            .order_by("-revision")
            .first()
            if target
            else None
        )
        head = (
            RayTargetAttestationRevision.objects.using(self._using)
            .filter(policy=policy)
            .order_by("-revision")
            .first()
            if policy
            else None
        )
        cap = (
            RayWorkerTargetCapability.objects.using(self._using)
            .filter(lease_id=self.identity.worker_id, target=target)
            .first()
            if target
            else None
        )
        result = cohort_publication.publish_prepared_cohort_job_probe(
            operation.prepared,
            inspected,
            expected_attestation_revision=head.revision if head else 0,
            expected_capability_revision=cap.revision if cap else 0,
            activate_new_target=True,
        )
        # Consume revision comes from our authenticated publisher return only.
        record.challenge_revision = result.job_qualification.consumed_challenge_revision
        row = RayTargetAttestationRevision.objects.using(self._using).get(
            pk=result.attestation_id, policy_id=result.target_policy_id
        )
        retained = decode_ray_cluster_attestation(row.attestation_json)
        if (
            row.attestation_json != encode_ray_cluster_attestation(retained)
            or row.attestation_digest != retained.attestation_digest
            or retained.expectation != proof.expectation
            or retained.membership_digest != proof.membership_digest
            or retained.observed_at < proof.observed_at
        ):
            _reject(JobsCohortAdapterReason.PUBLICATION_CHANGED)
        compare_ray_target_attestation(retained.expectation, retained, now=self._time()[0])
        return PublishedCohortQualification(
            SharedCohortQualification(
                self.lifecycle.lease,
                self.lifecycle.package_version,
                result.target_policy_id,
                result.attestation_id,
                result.capability_id,
                result.capability_revision,
                retained,
                str(result.desired_state),
                result.activation_policy_id,
            ),
            result.challenge_id,
            result.consumed_at,
            result.job_qualification,
        )

    def _followup(self, operation, record, policy, expectation, now):
        if operation.followups <= 0:
            _reject(JobsCohortAdapterReason.PUBLICATION_CHANGED)
        operation.followups -= 1
        if self.lifecycle.outstanding is not None:
            self.lifecycle.require_cleanup(operation.lifecycle_ticket)
            self.lifecycle.confirm_cleanup(operation.lifecycle_ticket, lambda: True)
        operation.lifecycle_ticket = None
        self.lifecycle.withdraw_job_alias(operation.ticket.alias)
        self._withdraw_unused_capabilities(now)
        self._reserve_and_submit(operation, record, policy=policy, expectation=expectation)

    def _inspected_result(self, operation, record, payload):
        if type(payload) is dict and set(payload) == {"pending"} and payload["pending"] is True:
            operation.prepared = None
            operation.phase = JobsCohortPhase.WAITING_RECEIPT
            return None
        if (
            type(payload) is not dict
            or set(payload) != {"pending", "inspected_at"}
            or payload["pending"] is not False
        ):
            _reject(JobsCohortAdapterReason.INVALID)
        inspected_at = _parse_timestamp(payload["inspected_at"])
        now, _mono = self._time()
        if not operation.prepared.began <= inspected_at <= now:
            _reject(JobsCohortAdapterReason.INVALID)
        snapshot = operation.prepared.snapshot
        receipt = decode_cohort_job_receipt(
            snapshot.receipt_json,
            expected_request=snapshot.request,
            expected_request_digest=snapshot.request_digest,
            expected_submission_id=probe_job_submission_id(snapshot.request),
            expected_receipt_digest=snapshot.receipt_digest,
        )
        compare_ray_target_attestation(
            receipt.attestation.expectation, receipt.attestation, now=now
        )
        inspected = InspectedCohortJobReceipt(snapshot, receipt, inspected_at)
        record.cleanup_at = inspected_at
        policy, expectation = self._retained_policy(receipt.attestation, now)
        if expectation is not None and expectation != receipt.attestation.expectation:
            self._followup(operation, record, policy, expectation, now)
            return None
        result = self.lifecycle.publish_job(
            operation.lifecycle_ticket, lambda: self._publish(operation, record, inspected), now=now
        )
        operation.lifecycle_ticket = None
        if result.shared.activation_policy_id is not None:
            policy, expectation = self._retained_policy(receipt.attestation, self._time()[0])
            self._followup(operation, record, policy, expectation, self._time()[0])
            return None
        self._operation = None
        return result

    def poll(self, ticket):
        self._parent(ticket)
        operation = self._operation
        record = self._records[ticket.alias]
        try:
            try:
                now, mono = self._time()
            except JobsCohortAdapterError as error:
                self._block(error.reason)
                # The local supervisor has its own monotonic cleanup fences.
                # A broken parent wall clock cannot prevent exact-child reap,
                # but its discarded response cannot resolve remote uncertainty.
                if (
                    operation.helper is not None
                    and self._supervisor.outstanding is operation.helper
                ):
                    completion = self._supervisor.poll(operation.helper)
                    if completion is not None:
                        if completion.operation_id != operation.helper.operation_id:
                            _reject(JobsCohortAdapterReason.STALE)
                        operation.helper = None
                _reject(operation.blocked)
            if operation.cleanup_deadline is None:
                if self._invalidated or mono >= ticket.deadline:
                    self._block(
                        JobsCohortAdapterReason.STALE
                        if self._invalidated
                        else JobsCohortAdapterReason.DEADLINE
                    )
                if operation.lifecycle_ticket is not None and operation.blocked is None:
                    self.lifecycle.check_operation(operation.lifecycle_ticket, now=now)
            if operation.helper is not None:
                completion = self._supervisor.poll(operation.helper)
                if completion is None:
                    return None
                if completion.operation_id != operation.helper.operation_id:
                    _reject(JobsCohortAdapterReason.STALE)
                operation.helper = None
                if completion.reason is not None:
                    _reject(JobsCohortAdapterReason.OPERATION_FAILED)
                now, mono = self._time()
                if (
                    operation.phase is JobsCohortPhase.INSPECTING_DRIVER
                    and operation.cleanup_deadline is not None
                ):
                    self._driver_result(operation, record, completion.response)
                    return None
                if operation.phase is JobsCohortPhase.STOPPING:
                    if (
                        mono >= operation.cleanup_deadline
                        or type(completion.response) is not dict
                        or set(completion.response) != {"terminal"}
                        or completion.response["terminal"] is not True
                    ):
                        _reject(JobsCohortAdapterReason.CLEANUP_UNCONFIRMED)
                    record.cleanup_at = now
                    self._finish_cleanup(operation, record, now)
                    return None
                if operation.blocked is not None or mono >= ticket.deadline:
                    _reject(operation.blocked or JobsCohortAdapterReason.DEADLINE)
                self._lease(now)
                if operation.phase is JobsCohortPhase.DISCOVERING:
                    observation = validate_client_discovery_observation(
                        operation.discovery_request, completion.response
                    )
                    if _parse_timestamp(observation["observed_at"]) > now:
                        _reject(JobsCohortAdapterReason.INVALID)
                    operation.discovery_observation = observation
                    self._inspect_driver(operation)
                elif operation.phase is JobsCohortPhase.INSPECTING_DRIVER:
                    self._driver_result(operation, record, completion.response)
                elif operation.phase is JobsCohortPhase.PREPARING:
                    self._prepared_result(operation, record, completion.response)
                elif operation.phase is JobsCohortPhase.SUBMITTING:
                    if (
                        type(completion.response) is not dict
                        or set(completion.response) != {"submitted", "submission_id"}
                        or completion.response["submitted"] is not True
                        or completion.response["submission_id"]
                        != probe_job_submission_id(record.launch.request)
                    ):
                        _reject(JobsCohortAdapterReason.INVALID)
                    operation.phase = JobsCohortPhase.WAITING_RECEIPT
                elif operation.phase is JobsCohortPhase.INSPECTING:
                    return self._inspected_result(operation, record, completion.response)
            if operation.blocked is not None:
                _reject(operation.blocked)
            if operation.phase is JobsCohortPhase.WAITING_RECEIPT:
                self._inspect(operation, record)
            return None
        except Exception:
            self._block(JobsCohortAdapterReason.OPERATION_FAILED)
            _reject(operation.blocked)

    def cancel(self, ticket):
        self._parent(ticket)
        self._block(JobsCohortAdapterReason.CLEANUP_UNCONFIRMED)

    def begin_cleanup(self, ticket, *, timeout_seconds=30):
        self._parent(ticket)
        operation = self._operation
        if operation.helper is not None or self._supervisor.outstanding is not None:
            _reject(JobsCohortAdapterReason.BUSY)
        if (
            operation.blocked is None
            or type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 600
        ):
            _reject(JobsCohortAdapterReason.INVALID)
        now, mono = self._time()
        record = self._records[ticket.alias]
        if operation.discovery_request is not None and not operation.driver_terminal:
            if operation.discovery_observation is None:
                _reject(JobsCohortAdapterReason.CLEANUP_UNCONFIRMED)
            operation.cleanup_deadline = mono + timeout_seconds
            self._inspect_driver(operation, cleanup=True)
            return
        if record.launch is None:
            self._operation = None
            return
        if not record.submitted or record.cleanup_at is not None:
            record.cleanup_at = now
            self._finish_cleanup(operation, record, now)
            return
        operation.cleanup_deadline = mono + timeout_seconds
        operation.phase = JobsCohortPhase.STOPPING
        self._start_helper(
            operation, "stop", {"launch_json": encode_probe_job_launch(record.launch)}, cleanup=True
        )

    def _finish_cleanup(self, operation, record, now):
        self._retire(record, operation, now)
        if operation.lifecycle_ticket is not None and self.lifecycle.outstanding is not None:
            self.lifecycle.confirm_cleanup(operation.lifecycle_ticket, lambda: True)
        self._operation = None

    def invalidate(self):
        self._parent()
        self._invalidated = True
        self.lifecycle.invalidate()
        if self._operation is not None:
            self._block(JobsCohortAdapterReason.STALE)

    def qualified_aliases(self):
        self._parent()
        if self._invalidated:
            return ()
        now, _mono = self._time()
        lease = self._lease(now)
        return self.lifecycle.qualified_aliases(
            now=now,
            live_lease=self.lifecycle.lease,
            lease_expires_at=lease.last_heartbeat_at + get_lease_duration(),
        )

    def eligible_aliases(self):
        return tuple(
            item for item in self.qualified_aliases() if item.shared.desired_state == "active"
        )
