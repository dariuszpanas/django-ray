"""Private manager-owned qualification lifecycle, without database or Ray I/O.

Only the owning thread may change state or invoke a publication callback. The
Core daemon performs observation and all its LOCAL cleanup in one owned call;
it must not detach cleanup threads. Its deadline limits acceptance, not native
call duration. Failed/expired calls retain the single slot until both the call
has exited and the manager independently confirms cleanup. Thread exit alone
does not establish remote quiescence or permission to reconnect.

Publisher callbacks are trusted manager code which authenticate the current
lease/challenge and perform atomic publication. Constructing the result types,
reading consumed database rows, or observing a Job exit code is not that proof.
Cached candidates still require the authoritative claim transaction's checks.
"""

from __future__ import annotations

import math
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from queue import Empty, Queue
from typing import Never

from django_ray.runtime.cohort_job import CohortProbeJobLease, probe_job_submission_id
from django_ray.runtime.cohort_job_entrypoint import (
    CohortProbeJobLaunch,
    decode_probe_job_launch,
    encode_probe_job_launch,
    probe_job_launch_entrypoint,
)
from django_ray.target.attestation import (
    RayClusterAttestation,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    compare_ray_target_attestation,
    encode_ray_target_expectation,
)
from django_ray.target.cohort_claim import (
    CohortJobQualificationProvenance,
    validate_cohort_job_qualification,
)
from django_ray.target.cohort_contract import _digest, _package_version, _timestamp
from django_ray.target.cohort_intent import CohortSelectionPolicy
from django_ray.target.cohort_job_control import cohort_probe_entrypoint_digest
from django_ray.target.cohort_probe import derive_cohort_target_key

_MAX_COUNTER = (1 << 63) - 1
_MAX_ALIASES = 64


class CohortQualificationReason(StrEnum):
    INVALID = "invalid"
    WRONG_OWNER = "wrong_owner"
    BUSY = "busy"
    STALE = "stale"
    DEADLINE = "deadline"
    CLOCK_REGRESSION = "clock_regression"
    OBSERVATION_FAILED = "observation_failed"
    PUBLICATION_FAILED = "publication_failed"
    PUBLICATION_MISMATCH = "publication_mismatch"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"


class CohortQualificationError(RuntimeError):
    def __init__(self, reason: CohortQualificationReason):
        self.reason = reason
        super().__init__(f"Cohort qualification refused: {reason.value}")


def _reject(reason: CohortQualificationReason) -> Never:
    raise CohortQualificationError(reason) from None


def _positive(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_COUNTER:
        _reject(CohortQualificationReason.INVALID)
    return value


def _text(value: object, maximum: int = 256) -> str:
    if (
        type(value) is not str
        or not 0 < len(value) <= maximum
        or any(not 33 <= ord(char) <= 126 for char in value)
    ):
        _reject(CohortQualificationReason.INVALID)
    return value


def _lease(value: CohortProbeJobLease):
    if type(value) is not CohortProbeJobLease:
        _reject(CohortQualificationReason.INVALID)
    _text(value.worker_id)
    _text(value.hostname)
    if _positive(value.pid) >= 1 << 31:
        _reject(CohortQualificationReason.INVALID)
    _timestamp(value.started_at)


def _queue(value: object) -> str:
    """Preserve existing queue spelling within the model's character bound."""
    if type(value) is not str or not value.strip() or len(value) > 100 or "\x00" in value:
        _reject(CohortQualificationReason.INVALID)
    return value


@dataclass(frozen=True, slots=True)
class PreparedCohortAlias:
    alias: str
    declaration_digest: str
    selection_policy: CohortSelectionPolicy
    queues: tuple[str, ...]
    control_profile_digest: str


@dataclass(frozen=True, slots=True)
class CoreObservationInput:
    configuration_digest: str
    connection_epoch: int
    challenge_id: int
    challenge_revision: int
    issued_at: datetime
    expires_at: datetime
    target_key: str | None = None
    cluster_session: str | None = None
    policy_revision: int = 1


@dataclass(frozen=True, slots=True)
class SharedCohortQualification:
    lease: CohortProbeJobLease
    package_version: str
    target_policy_id: int
    attestation_id: int
    capability_id: int
    capability_revision: int
    attestation: RayClusterAttestation
    desired_state: str
    activation_policy_id: int | None = None


@dataclass(frozen=True, slots=True)
class PublishedCohortQualification:
    shared: SharedCohortQualification
    challenge_id: int
    consumed_at: datetime
    job_qualification: CohortJobQualificationProvenance | None = None


@dataclass(frozen=True, slots=True)
class QualificationOperation:
    """Opaque ownership ticket: an equal reconstructed value is not accepted."""

    sequence: int
    deadline: float
    alias: str | None


@dataclass(frozen=True, slots=True)
class OwnedCoreObservation:
    operation: QualificationOperation
    attestation: RayClusterAttestation


@dataclass(frozen=True, slots=True)
class EligibleCohortAlias:
    configuration: PreparedCohortAlias
    shared: SharedCohortQualification
    job_qualification: CohortJobQualificationProvenance | None = None
    launch: CohortProbeJobLaunch | None = field(default=None, repr=False)


@dataclass(slots=True)
class _Operation:
    ticket: QualificationOperation
    configuration_epoch: int
    issued_at: datetime
    expires_at: datetime
    core: CoreObservationInput | None = None
    launch: CohortProbeJobLaunch | None = field(default=None, repr=False)
    source_control_profile_digest: str | None = None
    thread: threading.Thread | None = None
    mailbox: Queue = field(default_factory=lambda: Queue(maxsize=1))
    observation: OwnedCoreObservation | None = None
    blocked: CohortQualificationReason | None = None
    delivered: bool = False
    publishing: bool = False


class CohortQualificationLifecycle:
    """One control-plane owner, one outstanding operation, finite local caches.

    ``observe`` must perform ONLY the bounded full-cluster observation with
    owned synchronous local cleanup. Callbacks must not reconnect, publish from
    a background thread, or delegate work that escapes the retained slot.
    ``live_lease``/expiry supplied at selection are a trusted manager snapshot;
    this class never claims that an in-memory identity proves database liveness.
    """

    def __init__(
        self,
        lease: CohortProbeJobLease,
        package_version: str,
        runtime: RayRuntimeVersion,
        runner_family: RayRunnerFamily,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        try:
            if type(lease) is not CohortProbeJobLease or type(runtime) is not RayRuntimeVersion:
                raise ValueError
            if (
                type(runner_family) is not RayRunnerFamily
                or not callable(monotonic)
                or not callable(wall_clock)
            ):
                raise ValueError
            _lease(lease)
            _package_version(package_version)
            encode_ray_target_expectation(
                RayTargetExpectation("validation", runner_family, "session_validation", 1, runtime)
            )
        except Exception:
            _reject(CohortQualificationReason.INVALID)
        self.lease = lease
        self.package_version = package_version
        self.runtime = runtime
        self.runner_family = runner_family
        self._owner = threading.get_ident()
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._last_monotonic: float | None = None
        self._last_wall: datetime | None = None
        self._sequence = 0
        self._configuration_epoch = 0
        self._aliases: dict[str, tuple[int, PreparedCohortAlias]] = {}
        self._connection: tuple[str, int] | None = None
        self._operation: _Operation | None = None
        self._core: SharedCohortQualification | None = None
        self._shared: dict[str, SharedCohortQualification] = {}
        self._jobs: dict[
            str, tuple[int, CohortProbeJobLaunch, CohortJobQualificationProvenance]
        ] = {}
        self._stopped = False

    def _owner_only(self):
        if threading.get_ident() != self._owner:
            _reject(CohortQualificationReason.WRONG_OWNER)

    def _clock(self) -> float:
        self._owner_only()
        try:
            current = self._monotonic()
        except Exception:
            self.invalidate()
            _reject(CohortQualificationReason.INVALID)
        if type(current) not in (int, float) or not math.isfinite(current) or current < 0:
            self.invalidate()
            _reject(CohortQualificationReason.INVALID)
        if self._last_monotonic is not None and current < self._last_monotonic:
            self.invalidate()
            _reject(CohortQualificationReason.CLOCK_REGRESSION)
        self._last_monotonic = float(current)
        return float(current)

    def _wall(self, now: datetime):
        try:
            _timestamp(now)
        except Exception:
            _reject(CohortQualificationReason.INVALID)
        if now < self.lease.started_at or (self._last_wall is not None and now < self._last_wall):
            self.invalidate()
            _reject(CohortQualificationReason.CLOCK_REGRESSION)
        self._last_wall = now

    @property
    def outstanding(self) -> QualificationOperation | None:
        self._owner_only()
        return self._operation.ticket if self._operation is not None else None

    @property
    def blocked_reason(self) -> CohortQualificationReason | None:
        self._owner_only()
        return self._operation.blocked if self._operation is not None else None

    @property
    def connection_busy(self) -> bool:
        self._owner_only()
        return self.runner_family is RayRunnerFamily.RAY_CORE and self._operation is not None

    def invalidate(self):
        """Lease loss/shutdown: discard eligibility but retain cleanup ownership."""
        self._owner_only()
        self._stopped = True
        self._core = None
        self._jobs.clear()
        self._shared.clear()
        if self._operation is not None:
            self._operation.blocked = CohortQualificationReason.STALE

    def _block(self, operation: _Operation, reason: CohortQualificationReason):
        if operation.blocked is None:
            operation.blocked = reason
        if operation.core is not None:
            self._core = None
        elif operation.ticket.alias is not None:
            self._jobs.pop(operation.ticket.alias, None)
            self._prune_shared()

    def configure_aliases(self, aliases: Sequence[PreparedCohortAlias]):
        self._owner_only()
        if self._stopped:
            _reject(CohortQualificationReason.STALE)
        try:
            if type(aliases) not in (list, tuple) or len(aliases) > _MAX_ALIASES:
                raise ValueError
            prepared = {}
            for item in aliases:
                if type(item) is not PreparedCohortAlias:
                    raise ValueError
                _text(item.alias)
                _digest(item.declaration_digest)
                _digest(item.control_profile_digest)
                if type(item.selection_policy) is not CohortSelectionPolicy:
                    raise ValueError
                if type(item.queues) is not tuple or not 1 <= len(item.queues) <= 64:
                    raise ValueError
                if len(set(item.queues)) != len(item.queues):
                    raise ValueError
                for queue in item.queues:
                    _queue(queue)
                if item.alias in prepared:
                    raise ValueError
                prepared[item.alias] = item
        except Exception:
            _reject(CohortQualificationReason.INVALID)
        if prepared == {name: value for name, (_epoch, value) in self._aliases.items()}:
            return
        self._configuration_epoch = _positive(self._configuration_epoch + 1)
        changed = {}
        for name, value in prepared.items():
            previous = self._aliases.get(name)
            changed[name] = (
                previous
                if previous is not None and previous[1] == value
                else (self._configuration_epoch, value)
            )
        self._aliases = changed
        self._jobs = {
            name: entry
            for name, entry in self._jobs.items()
            if name in changed and changed[name][0] == entry[0]
        }
        self._prune_shared()

    def configure_core_connection(self, configuration_digest: str, connection_epoch: int):
        """Call only for the actual selected process connection, never per alias."""
        self._owner_only()
        try:
            _digest(configuration_digest)
            _positive(connection_epoch)
        except Exception:
            _reject(CohortQualificationReason.INVALID)
        if self.runner_family is not RayRunnerFamily.RAY_CORE or self._stopped:
            _reject(CohortQualificationReason.STALE)
        current = (configuration_digest, connection_epoch)
        if current == self._connection:
            return
        if self._operation is not None:
            _reject(CohortQualificationReason.BUSY)
        if self._connection is not None and connection_epoch <= self._connection[1]:
            _reject(CohortQualificationReason.STALE)
        self._connection = current
        self._core = None

    def withdraw_job_alias(self, alias: str):
        """Withdraw cached authority before preparation can rotate its DB slot.

        The parent retains the old reservation and exact cleanup ownership.
        This removes no database capability and confirms no remote cleanup.
        An already-running operation must enter its separate cleanup path.
        """
        self._owner_only()
        _text(alias)
        if self.runner_family is not RayRunnerFamily.RAY_JOB or alias not in self._aliases:
            _reject(CohortQualificationReason.INVALID)
        if self._operation is not None and self._operation.ticket.alias == alias:
            _reject(CohortQualificationReason.BUSY)
        self._jobs.pop(alias, None)
        self._prune_shared()

    def _begin(self, *, alias, epoch, issued_at, expires_at, timeout_seconds, now):
        current = self._clock()
        self._wall(now)
        if self._stopped:
            _reject(CohortQualificationReason.STALE)
        if self._operation is not None:
            _reject(CohortQualificationReason.BUSY)
        try:
            _timestamp(issued_at)
            _timestamp(expires_at)
            if not self.lease.started_at <= issued_at <= now < expires_at:
                raise ValueError
            if not timedelta(0) < expires_at - issued_at <= timedelta(seconds=600):
                raise ValueError
            if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds):
                raise ValueError
            if not 0 < timeout_seconds <= 600:
                raise ValueError
        except Exception:
            _reject(CohortQualificationReason.INVALID)
        self._sequence = _positive(self._sequence + 1)
        deadline = current + min(float(timeout_seconds), (expires_at - now).total_seconds())
        ticket = QualificationOperation(self._sequence, deadline, alias)
        operation = _Operation(ticket, epoch, issued_at, expires_at)
        self._operation = operation
        return operation

    def begin_core(
        self,
        prepared: CoreObservationInput,
        *,
        observe: Callable[[], RayClusterAttestation],
        timeout_seconds: float,
        now: datetime,
    ) -> QualificationOperation:
        self._owner_only()
        try:
            if type(prepared) is not CoreObservationInput or not callable(observe):
                raise ValueError
            if self.runner_family is not RayRunnerFamily.RAY_CORE:
                raise ValueError
            _digest(prepared.configuration_digest)
            _positive(prepared.connection_epoch)
            if self._connection != (prepared.configuration_digest, prepared.connection_epoch):
                raise ValueError
            _positive(prepared.challenge_id)
            _positive(prepared.challenge_revision)
            _positive(prepared.policy_revision)
            if prepared.cluster_session is None:
                if prepared.target_key is not None or prepared.policy_revision != 1:
                    raise ValueError
            elif prepared.target_key is None:
                raise ValueError
            encode_ray_target_expectation(
                RayTargetExpectation(
                    prepared.target_key or "validation",
                    self.runner_family,
                    prepared.cluster_session or "session_validation",
                    prepared.policy_revision,
                    self.runtime,
                )
            )
        except Exception:
            _reject(CohortQualificationReason.INVALID)
        operation = self._begin(
            alias=None,
            epoch=prepared.connection_epoch,
            issued_at=prepared.issued_at,
            expires_at=prepared.expires_at,
            timeout_seconds=timeout_seconds,
            now=now,
        )
        operation.core = prepared

        def observe_owned():
            try:
                value = observe()
                operation.mailbox.put_nowait((True, value))
            except BaseException:
                # Never publish, retain/log exception payloads, or touch Django here.
                operation.mailbox.put_nowait((False, None))

        thread = threading.Thread(target=observe_owned, daemon=True, name="cohort-observation")
        operation.thread = thread
        try:
            thread.start()
        except Exception:
            operation.thread = None
            self._block(operation, CohortQualificationReason.OBSERVATION_FAILED)
            _reject(CohortQualificationReason.OBSERVATION_FAILED)
        return operation.ticket

    def begin_job(
        self,
        alias: str,
        launch: CohortProbeJobLaunch,
        *,
        source_control_profile_digest: str,
        timeout_seconds: float,
        now: datetime,
    ) -> QualificationOperation:
        """Bind the configured source profile independently of uploaded mapping.

        The owned preparation adapter supplies this digest from the exact
        configured profile it normalizes/uploads into ``launch``. This is
        trusted-caller provenance, not independent authentication or a claim
        that the pre-upload and submitted RuntimeEnv digests are identical.
        """
        self._owner_only()
        try:
            _text(alias)
            _digest(source_control_profile_digest)
            epoch, prepared = self._aliases[alias]
            if not secrets.compare_digest(
                source_control_profile_digest, prepared.control_profile_digest
            ):
                raise ValueError
            launch = decode_probe_job_launch(encode_probe_job_launch(launch))
            request = launch.request
            if (
                self.runner_family is not RayRunnerFamily.RAY_JOB
                or request.configuration_digest != prepared.declaration_digest
                or request.lease != self.lease
                or request.expected_package_version != self.package_version
                or request.expected_runtime != self.runtime
            ):
                raise ValueError
        except Exception:
            _reject(CohortQualificationReason.INVALID)
        operation = self._begin(
            alias=alias,
            epoch=epoch,
            issued_at=request.issued_at,
            expires_at=request.expires_at,
            timeout_seconds=timeout_seconds,
            now=now,
        )
        operation.launch = launch
        operation.source_control_profile_digest = source_control_profile_digest
        # Reserving this refresh rotates the one database slot and removes the
        # old immutable receipt. Other aliases retain their independent proof.
        self._jobs.pop(alias, None)
        self._prune_shared()
        return operation.ticket

    def _current(self, ticket: QualificationOperation, now: datetime) -> _Operation:
        current = self._clock()
        self._wall(now)
        operation = self._operation
        if operation is None or operation.ticket is not ticket:
            _reject(CohortQualificationReason.STALE)
        if self._stopped:
            self._block(operation, CohortQualificationReason.STALE)
        elif current >= ticket.deadline or now >= operation.expires_at:
            self._block(operation, CohortQualificationReason.DEADLINE)
        elif ticket.alias is not None and (
            ticket.alias not in self._aliases
            or self._aliases[ticket.alias][0] != operation.configuration_epoch
            or operation.source_control_profile_digest is None
            or not secrets.compare_digest(
                operation.source_control_profile_digest,
                self._aliases[ticket.alias][1].control_profile_digest,
            )
        ):
            self._block(operation, CohortQualificationReason.STALE)
        elif operation.core is not None and self._connection != (
            operation.core.configuration_digest,
            operation.core.connection_epoch,
        ):
            self._block(operation, CohortQualificationReason.STALE)
        if operation.blocked is not None:
            _reject(operation.blocked)
        return operation

    def poll_core(self, *, now: datetime) -> OwnedCoreObservation | None:
        self._owner_only()
        if self._operation is None:
            return None
        operation = self._current(self._operation.ticket, now)
        if operation.core is None:
            _reject(CohortQualificationReason.INVALID)
        if operation.thread is not None and operation.thread.is_alive():
            return None
        if operation.delivered:
            return None
        try:
            success, attestation = operation.mailbox.get_nowait()
        except Empty:
            success, attestation = False, None
        if not success or type(attestation) is not RayClusterAttestation:
            self._block(operation, CohortQualificationReason.OBSERVATION_FAILED)
            _reject(operation.blocked)
        try:
            core = operation.core
            expected = RayTargetExpectation(
                core.target_key
                or derive_cohort_target_key(
                    self.runner_family, attestation.expectation.cluster_session
                ),
                self.runner_family,
                core.cluster_session or attestation.expectation.cluster_session,
                core.policy_revision,
                self.runtime,
            )
            compare_ray_target_attestation(expected, attestation, now=now)
            if attestation.observed_at < operation.issued_at:
                raise ValueError
        except Exception:
            self._block(operation, CohortQualificationReason.OBSERVATION_FAILED)
            _reject(operation.blocked)
        self._current(operation.ticket, now)
        operation.observation = OwnedCoreObservation(operation.ticket, attestation)
        operation.delivered = True
        return operation.observation

    def _validate_publication(self, operation, publication, now):
        if type(publication) is not PublishedCohortQualification:
            raise ValueError
        shared = publication.shared
        if type(shared) is not SharedCohortQualification:
            raise ValueError
        _package_version(shared.package_version)
        _lease(shared.lease)
        for counter in (
            shared.target_policy_id,
            shared.attestation_id,
            shared.capability_id,
            shared.capability_revision,
            publication.challenge_id,
        ):
            _positive(counter)
        if shared.activation_policy_id is not None:
            _positive(shared.activation_policy_id)
        if (
            type(shared.lease) is not CohortProbeJobLease
            or shared.lease != self.lease
            or shared.package_version != self.package_version
            or type(shared.desired_state) is not str
            or shared.desired_state not in {"active", "draining", "retired"}
        ):
            raise ValueError
        attestation = shared.attestation
        if type(attestation) is not RayClusterAttestation:
            raise ValueError
        expected = attestation.expectation
        if expected.runner_family is not self.runner_family or expected.runtime != self.runtime:
            raise ValueError
        compare_ray_target_attestation(expected, attestation, now=now)
        _timestamp(publication.consumed_at)
        if not operation.issued_at <= attestation.observed_at <= publication.consumed_at <= now:
            raise ValueError
        if operation.core is not None:
            if (
                operation.observation is None
                or attestation != operation.observation.attestation
                or publication.challenge_id != operation.core.challenge_id
                or publication.job_qualification is not None
            ):
                raise ValueError
            core = operation.core
            if expected.policy_revision != core.policy_revision:
                raise ValueError
            if core.cluster_session is not None and (
                expected.target_key != core.target_key
                or expected.cluster_session != core.cluster_session
                or expected.policy_revision != core.policy_revision
            ):
                raise ValueError
        else:
            launch = operation.launch
            assert launch is not None
            request = launch.request
            q = publication.job_qualification
            if type(q) is not CohortJobQualificationProvenance:
                raise ValueError
            validate_cohort_job_qualification(q)
            if (
                q.configuration_digest != request.configuration_digest
                or q.jobs_endpoint != launch.jobs_endpoint
                or q.challenge_id != request.challenge_id
                or publication.challenge_id != q.challenge_id
                or q.request_revision != request.challenge_revision
                or q.challenge_issued_at != request.issued_at
                or q.challenge_expires_at != request.expires_at
                or q.request_digest != launch.request_digest
                or q.submission_id != probe_job_submission_id(request)
                or q.entrypoint_digest
                != cohort_probe_entrypoint_digest(probe_job_launch_entrypoint(launch))
                or q.submitted_control_runtime_env_digest != launch.submitted_runtime_env_digest
                or q.endpoint_expectation_digest != attestation.expectation_digest
                or q.endpoint_membership_digest != attestation.membership_digest
                or q.consumed_at != publication.consumed_at
                or q.endpoint_observed_at > attestation.observed_at
                or not q.consumed_at <= now < min(q.endpoint_expires_at, q.challenge_expires_at)
            ):
                raise ValueError
            if expected.policy_revision != request.policy_revision:
                raise ValueError
            if request.expected_cluster_session is not None and (
                expected.cluster_session != request.expected_cluster_session
                or expected.target_key != request.target_key
                or expected.policy_revision != request.policy_revision
                or shared.target_policy_id != request.expected_target_policy_id
            ):
                raise ValueError

    def _publication_clock(self, operation: _Operation) -> datetime:
        try:
            after = self._wall_clock()
            self._current(operation.ticket, after)
            return after
        except Exception:
            # The callback may already have committed. Any failure after that
            # point must prevent replay, including malformed clock results.
            self._block(operation, CohortQualificationReason.PUBLICATION_FAILED)
            _reject(operation.blocked)

    def _publish(self, operation, publisher, *, now):
        if operation.publishing or not callable(publisher):
            _reject(CohortQualificationReason.INVALID)
        operation.publishing = True
        try:
            publication = publisher()
        except Exception:
            self._block(operation, CohortQualificationReason.PUBLICATION_FAILED)
            _reject(operation.blocked)
        finally:
            operation.publishing = False
        after = self._publication_clock(operation)
        if publication is None and operation.launch is not None:
            return None
        try:
            self._validate_publication(operation, publication, after)
        except Exception:
            self._block(operation, CohortQualificationReason.PUBLICATION_MISMATCH)
            _reject(operation.blocked)
        self._publication_clock(operation)
        shared = publication.shared
        if operation.core is not None:
            self._core = shared
        else:
            assert operation.ticket.alias is not None and operation.launch is not None
            self._shared[shared.attestation.expectation.target_key] = shared
            self._jobs[operation.ticket.alias] = (
                operation.configuration_epoch,
                operation.launch,
                publication.job_qualification,
            )
            self._prune_shared()
        self._operation = None
        return publication

    def publish_core(self, observation, publisher, *, now: datetime):
        self._owner_only()
        if type(observation) is not OwnedCoreObservation:
            _reject(CohortQualificationReason.INVALID)
        operation = self._current(observation.operation, now)
        if operation.core is None or operation.observation is not observation:
            _reject(CohortQualificationReason.STALE)
        return self._publish(operation, lambda: publisher(observation.attestation), now=now)

    def discard_core_observation(self, observation, *, now: datetime):
        """Retire only this slot's successfully completed, accepted observation.

        This grants no eligibility and cannot release failed or timed-out work.
        The owned collector must have returned its full canonical proof, after
        completing all children, before ``poll_core`` can issue this identity.
        """
        self._owner_only()
        if type(observation) is not OwnedCoreObservation:
            _reject(CohortQualificationReason.INVALID)
        operation = self._current(observation.operation, now)
        if operation.core is None or operation.observation is not observation:
            _reject(CohortQualificationReason.STALE)
        if operation.publishing or (operation.thread is not None and operation.thread.is_alive()):
            _reject(CohortQualificationReason.BUSY)
        try:
            compare_ray_target_attestation(
                observation.attestation.expectation, observation.attestation, now=now
            )
        except Exception:
            self._block(operation, CohortQualificationReason.OBSERVATION_FAILED)
            _reject(operation.blocked)
        self._current(operation.ticket, now)
        self._core = None
        self._operation = None

    def publish_job(self, ticket, publisher, *, now: datetime):
        operation = self._current(ticket, now)
        if operation.launch is None:
            _reject(CohortQualificationReason.INVALID)
        return self._publish(operation, publisher, now=now)

    def check_operation(self, ticket: QualificationOperation, *, now: datetime):
        """Poll the parent acceptance deadline without waiting for any operation."""
        self._current(ticket, now)

    def require_cleanup(self, ticket: QualificationOperation):
        """Retain an ambiguous submission/failed operation for exact cleanup."""
        self._owner_only()
        if self._operation is None or self._operation.ticket is not ticket:
            _reject(CohortQualificationReason.STALE)
        self._block(self._operation, CohortQualificationReason.CLEANUP_UNCONFIRMED)

    def confirm_cleanup(self, ticket: QualificationOperation, confirm: Callable[[], bool]):
        """Trusted manager confirms remote cleanup; local thread exit is also required."""
        self._owner_only()
        operation = self._operation
        if operation is None or operation.ticket is not ticket or operation.blocked is None:
            _reject(CohortQualificationReason.STALE)
        if operation.publishing:
            _reject(CohortQualificationReason.BUSY)
        if operation.thread is not None and operation.thread.is_alive():
            _reject(CohortQualificationReason.BUSY)
        if not callable(confirm):
            _reject(CohortQualificationReason.INVALID)
        operation.publishing = True
        try:
            confirmed = confirm()
        except Exception:
            confirmed = False
        finally:
            operation.publishing = False
        if confirmed is not True:
            _reject(CohortQualificationReason.CLEANUP_UNCONFIRMED)
        if self._operation is not operation:
            _reject(CohortQualificationReason.STALE)
        self._operation = None

    def _prune_shared(self):
        retained = {entry[2].endpoint_expectation_digest for entry in self._jobs.values()}
        self._shared = {
            key: value
            for key, value in self._shared.items()
            if value.attestation.expectation_digest in retained
        }

    def eligible_aliases(
        self,
        *,
        now: datetime,
        live_lease: CohortProbeJobLease,
        lease_expires_at: datetime,
    ) -> tuple[EligibleCohortAlias, ...]:
        """ACTIVE candidates for first claims, still subject to locked checks."""
        return tuple(
            item
            for item in self.qualified_aliases(
                now=now, live_lease=live_lease, lease_expires_at=lease_expires_at
            )
            if item.shared.desired_state == "active"
        )

    def qualified_aliases(
        self,
        *,
        now: datetime,
        live_lease: CohortProbeJobLease,
        lease_expires_at: datetime,
    ) -> tuple[EligibleCohortAlias, ...]:
        """Fresh owned observations for task-specific continuation filtering.

        DRAINING observations never authorize a first claim. A continuation
        query must prove resolved prior claim history and the original target
        before LIMIT, then use the existing authoritative claim transaction.
        Neither a binding alone nor this cached view establishes that history.
        """
        self._clock()
        self._wall(now)
        if self._operation is not None:
            try:
                self._current(self._operation.ticket, now)
            except CohortQualificationError as error:
                if error.reason not in {
                    CohortQualificationReason.DEADLINE,
                    CohortQualificationReason.STALE,
                    CohortQualificationReason.OBSERVATION_FAILED,
                    CohortQualificationReason.PUBLICATION_FAILED,
                    CohortQualificationReason.PUBLICATION_MISMATCH,
                    CohortQualificationReason.CLEANUP_UNCONFIRMED,
                }:
                    raise
        try:
            _timestamp(lease_expires_at)
            _lease(live_lease)
        except Exception:
            _reject(CohortQualificationReason.INVALID)
        if (
            self._stopped
            or type(live_lease) is not CohortProbeJobLease
            or live_lease != self.lease
            or now >= lease_expires_at
        ):
            return ()
        candidates = []
        for name, (epoch, configuration) in self._aliases.items():
            q, launch = None, None
            if self.runner_family is RayRunnerFamily.RAY_CORE:
                if configuration.selection_policy is not CohortSelectionPolicy.WORKER_SELECTED:
                    continue
                if self._operation is not None and self._operation.blocked is not None:
                    continue
                shared = self._core
            else:
                entry = self._jobs.get(name)
                if entry is None or entry[0] != epoch:
                    continue
                _epoch, launch, q = entry
                shared = next(
                    (
                        value
                        for value in self._shared.values()
                        if (
                            value.attestation.expectation_digest == q.endpoint_expectation_digest
                            and value.attestation.membership_digest == q.endpoint_membership_digest
                        )
                    ),
                    None,
                )
                if now >= min(q.endpoint_expires_at, q.challenge_expires_at):
                    continue
            if (
                shared is None
                or not shared.attestation.observed_at <= now < shared.attestation.expires_at
            ):
                continue
            if (
                shared.desired_state not in {"active", "draining"}
                or shared.activation_policy_id is not None
            ):
                continue
            candidates.append(EligibleCohortAlias(configuration, shared, q, launch))
        return tuple(candidates)
