"""Private parent-owned adapter for one existing Core connection.

No worker calls this adapter yet. The parent authenticates its lease, owns the
challenge nonce and publishes; the daemon only observes the prepared connection.
The connection descriptor and monotone epoch remain separate lifecycle inputs.
Their lease-bound digest rotates the database challenge across reconnects without
changing physical target identity or the alias admission declarations.

A failed, expired or uncertain operation quarantines the connection. Thread exit
does not prove remote cleanup. Only the caller's independent cleanup evidence can
release that slot; this module never reconnects, shuts Ray down or invents it.
The owner must serialize every connection/context change against the lifecycle
slot and advance the epoch. Out-of-band ray.init/shutdown calls are unsupported.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from django.db import DEFAULT_DB_ALIAS, connections, transaction

from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
    RayTargetProbeChallenge,
    RayWorkerTargetCapability,
)
from django_ray.runner.cohort_qualification import (
    CohortQualificationLifecycle,
    CoreObservationInput,
    PublishedCohortQualification,
    QualificationOperation,
    SharedCohortQualification,
)
from django_ray.runner.leasing import WorkerLeaseIdentity, get_lease_duration
from django_ray.target import capabilities, cohort_publication, coordination
from django_ray.target.attestation import (
    RayClusterAttestation,
    RayRunnerFamily,
    compare_ray_target_attestation,
    decode_ray_cluster_attestation,
    encode_ray_cluster_attestation,
)
from django_ray.target.cohort_contract import _digest, _positive, _timestamp
from django_ray.target.cohort_probe_challenges import (
    IssuedProbeChallenge,
    _locked_probe_lease,
    issue_ray_target_probe_challenge,
    replace_ray_target_probe_challenge,
)
from django_ray.target.cohort_runtime import _local_runtime


class CoreCohortAdapterReason(StrEnum):
    INVALID = "invalid"
    UNSUPPORTED_CONNECTION = "unsupported_connection"
    TARGET_UNAVAILABLE = "target_unavailable"
    PUBLICATION_CHANGED = "publication_changed"
    BUSY = "busy"


class CoreCohortAdapterError(RuntimeError):
    def __init__(self, reason: CoreCohortAdapterReason):
        self.reason = reason
        super().__init__(f"Core cohort adapter refused: {reason.value}")


def _reject(reason: CoreCohortAdapterReason):
    raise CoreCohortAdapterError(reason) from None


def _supported_connection(package_version, runtime):
    """Ray 2.58 new threads inherit only the default Client context.

    ``RayAPIStub.get_context`` initializes a new thread with the default context.
    Reject thread-local multi-client selection and other connected contexts before
    creating an observation thread. These reads do not initialize/connect Ray.
    """
    try:
        import ray
        import ray.util.client as client

        connected = client.ray.is_connected()
        count = client.num_connected_contexts()
        if (
            ray.__version__ != "2.58.0"
            or ray.is_initialized() is not True
            or client.ray.is_default() is not True
            or type(connected) is not bool
            or type(count) is not int
            or count != int(connected)
            or _local_runtime(ray) != (package_version, runtime)
        ):
            raise ValueError
        return ray
    except Exception:
        _reject(CoreCohortAdapterReason.UNSUPPORTED_CONNECTION)


def _challenge_digest(identity, descriptor, epoch):
    _digest(descriptor)
    _positive(epoch)
    encoded = json.dumps(
        [
            identity.worker_id,
            identity.hostname,
            identity.pid,
            _timestamp(identity.started_at),
            descriptor,
            epoch,
        ],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return (
        "sha256:" + hashlib.sha256(b"django-ray/core-probe-connection/v1\0" + encoded).hexdigest()
    )


class CoreCohortManagerAdapter:
    """Compose the prepared probe with one owner's bounded lifecycle ticket.

    Construct a new adapter/lifecycle for every lease incarnation. An existing
    database slot without this adapter's retained nonce cannot seed or resume
    positive state. The first epoch discovers its session only inside the owned
    observation. A successful discovery can schedule one fresh policy-specific
    probe without publishing or relabeling the discovery proof. Call ``begin``
    again after automatic activation to qualify the new active policy.
    """

    def __init__(
        self,
        lifecycle: CohortQualificationLifecycle,
        *,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        using: str = DEFAULT_DB_ALIAS,
    ):
        if (
            type(lifecycle) is not CohortQualificationLifecycle
            or lifecycle.runner_family is not RayRunnerFamily.RAY_CORE
            or using != DEFAULT_DB_ALIAS
            or not callable(wall_clock)
        ):
            _reject(CoreCohortAdapterReason.INVALID)
        self.lifecycle = lifecycle
        lease = lifecycle.lease
        self.identity = WorkerLeaseIdentity(
            lease.worker_id, lease.hostname, lease.pid, lease.started_at
        )
        self._wall_clock = wall_clock
        self._using = using
        self._issued: IssuedProbeChallenge | None = None
        self._prepared: cohort_publication.CoreCohortProbePreparation | None = None
        self._ticket: QualificationOperation | None = None
        self._connection: tuple[str, int] | None = None
        self._session_hint: str | None = None
        self._invalidated = False
        self._followups_remaining = 0
        self._timeout_seconds = 30.0
        self._challenge_ttl_seconds = 300

    def _parent(self):
        # Also enforces the lifecycle's actual creating thread.
        _ = self.lifecycle.outstanding
        if any(connection.in_atomic_block for connection in connections.all()):
            _reject(CoreCohortAdapterReason.INVALID)

    def _now(self):
        value = self._wall_clock()
        _timestamp(value)
        return value

    def _lease(self, now):
        try:
            with transaction.atomic(using=self._using):
                return _locked_probe_lease(self.identity, now, using=self._using)
        except Exception:
            self.lifecycle.invalidate()
            raise

    def _retained_policy(self, session, now):
        target = (
            RayTarget.objects.using(self._using)
            .filter(runner_family=RayRunnerFamily.RAY_CORE.value, cluster_session=session)
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
            if state not in {RayTargetDesiredState.ACTIVE, RayTargetDesiredState.DRAINING} or (
                expectation.runtime != self.lifecycle.runtime
                or expectation.cluster_session != session
                or expectation.runner_family is not RayRunnerFamily.RAY_CORE
            ):
                _reject(CoreCohortAdapterReason.TARGET_UNAVAILABLE)
            return policy, expectation

    def begin(
        self,
        configuration_digest: str,
        connection_epoch: int,
        *,
        timeout_seconds: float = 30.0,
        challenge_ttl_seconds: int = 300,
    ) -> QualificationOperation:
        self._parent()
        if self.lifecycle.outstanding is not None:
            _reject(CoreCohortAdapterReason.BUSY)
        self.lifecycle.configure_core_connection(configuration_digest, connection_epoch)
        selected = (configuration_digest, connection_epoch)
        if self._connection is not None and self._connection != selected:
            # A new owned connection cannot retain the previous connection's
            # single Core admission record. This is not remote cleanup evidence.
            capabilities.withdraw_all_ray_worker_target_capabilities(
                self.identity, using=self._using
            )
        now = self._now()
        self._lease(now)
        try:
            _supported_connection(self.lifecycle.package_version, self.lifecycle.runtime)
        except Exception:
            self._invalidated = True
            raise
        if self._connection != selected:
            self._session_hint = None
        self._connection = selected
        self._invalidated = False
        self._followups_remaining = 1
        self._timeout_seconds = timeout_seconds
        self._challenge_ttl_seconds = challenge_ttl_seconds
        return self._begin_observation()

    def _begin_observation(self):
        # Only an exact previously accepted owned observation may seed this
        # lookup. Ray runtime-context/session reads can perform RPCs and must
        # never run on the heartbeat-owning parent, even for a lookup hint.
        assert self._connection is not None
        configuration_digest, connection_epoch = self._connection
        policy, expectation = (
            self._retained_policy(self._session_hint, self._now())
            if self._session_hint is not None
            else (None, None)
        )
        digest = _challenge_digest(self.identity, configuration_digest, connection_epoch)
        options = {
            "now": self._now(),
            "expected_target_policy_id": policy.pk if policy else None,
            "ttl_seconds": self._challenge_ttl_seconds,
            "using": self._using,
        }
        if self._issued is None:
            issued = issue_ray_target_probe_challenge(
                self.identity, digest, runner_family=RayRunnerFamily.RAY_CORE, **options
            )
        else:
            # A consumed row is read only to rotate our independently held nonce;
            # it cannot reconstruct a positive qualification or observation.
            row = RayTargetProbeChallenge.objects.using(self._using).get(
                pk=self._issued.receipt.challenge_id, lease_id=self.identity.worker_id
            )
            issued = replace_ray_target_probe_challenge(
                self.identity,
                row.pk,
                expected_configuration_digest=row.configuration_digest,
                configuration_digest=digest,
                expected_revision=row.revision,
                expected_nonce=self._issued.nonce,
                **options,
            )
        self._issued = issued
        plan = cohort_publication.CoreCohortProbePlan(
            issued.receipt,
            expectation.target_key if expectation else None,
            self.lifecycle.package_version,
            self.lifecycle.runtime,
            expectation.cluster_session if expectation else None,
            expectation.policy_revision if expectation else 1,
        )
        prepared = cohort_publication.prepare_core_cohort_probe(
            self.identity, plan, nonce=issued.nonce, using=self._using
        )
        self._prepared = prepared
        self._ticket = self.lifecycle.begin_core(
            CoreObservationInput(
                configuration_digest,
                connection_epoch,
                issued.receipt.challenge_id,
                issued.receipt.revision,
                issued.receipt.issued_at,
                issued.receipt.expires_at,
                plan.target_key,
                plan.expected_cluster_session,
                plan.policy_revision,
            ),
            observe=lambda: cohort_publication.observe_prepared_core_cohort_probe(
                prepared, owned_cleanup=True
            ),
            timeout_seconds=self._timeout_seconds,
            now=self._now(),
        )
        return self._ticket

    def _revisions(self, proof):
        with transaction.atomic(using=self._using):
            _locked_probe_lease(self.identity, self._now(), using=self._using)
            target = (
                RayTarget.objects.using(self._using).filter(pk=proof.expectation.target_key).first()
            )
            if target is None:
                return 0, 0
            target = coordination._locked_target(
                target_key=target.pk,
                using=self._using,
                vendor=capabilities._database_vendor(using=self._using),
            )
            policy, expectation, _state = coordination._latest_policy(target, using=self._using)
            if expectation != proof.expectation:
                _reject(CoreCohortAdapterReason.TARGET_UNAVAILABLE)
            attestation = (
                RayTargetAttestationRevision.objects.using(self._using)
                .filter(policy=policy)
                .order_by("-revision")
                .first()
            )
            cap = (
                RayWorkerTargetCapability.objects.using(self._using)
                .filter(lease_id=self.identity.worker_id, target=target)
                .first()
            )
            return attestation.revision if attestation else 0, cap.revision if cap else 0

    def _convert(self, result, proof):
        # Read only exact immutable publication identities, never latest evidence
        # as a substitute for the result this parent actually authenticated.
        policy = RayTargetPolicyRevision.objects.using(self._using).get(
            pk=result.target_policy_id, target_id=result.target_key
        )
        row = RayTargetAttestationRevision.objects.using(self._using).get(
            pk=result.attestation_id, policy=policy
        )
        attestation = decode_ray_cluster_attestation(row.attestation_json)
        if (
            attestation != proof
            or row.attestation_json != encode_ray_cluster_attestation(proof)
            or row.attestation_digest != proof.attestation_digest
            or row.expectation_digest != proof.expectation_digest
            or row.membership_digest != proof.membership_digest
            or row.observed_at != proof.observed_at
            or row.expires_at != proof.expires_at
            or policy.expectation_digest != proof.expectation_digest
            or policy.desired_state != result.desired_state
            or result.job_qualification is not None
        ):
            _reject(CoreCohortAdapterReason.PUBLICATION_CHANGED)
        compare_ray_target_attestation(proof.expectation, proof, now=self._now())
        return PublishedCohortQualification(
            SharedCohortQualification(
                self.lifecycle.lease,
                self.lifecycle.package_version,
                result.target_policy_id,
                result.attestation_id,
                result.capability_id,
                result.capability_revision,
                proof,
                str(result.desired_state),
                result.activation_policy_id,
            ),
            result.challenge_id,
            result.consumed_at,
        )

    def poll(self) -> PublishedCohortQualification | None:
        self._parent()
        observed = self.lifecycle.poll_core(now=self._now())
        if observed is None:
            return None
        if observed.operation is not self._ticket or self._prepared is None:
            _reject(CoreCohortAdapterReason.INVALID)
        prepared = self._prepared

        # Poll has independently accepted this exact completed full observation.
        # Its session is a lookup hint, never authority to rename an attestation.
        session = observed.attestation.expectation.cluster_session
        if self._session_hint is not None and session != self._session_hint:
            self._invalidated = True
            self.lifecycle.require_cleanup(observed.operation)
            _reject(CoreCohortAdapterReason.UNSUPPORTED_CONNECTION)
        self._session_hint = session
        try:
            _policy, expectation = self._retained_policy(session, self._now())
        except Exception:
            self.lifecycle.require_cleanup(observed.operation)
            raise
        if expectation is not None and expectation != observed.attestation.expectation:
            # Existing operator names/current policy win. Discard only a fully
            # successful owned observation, then rotate a fresh bound challenge.
            # At most one automatic followup prevents policy churn from creating
            # an unbounded stream of probes within one caller-initiated attempt.
            self.lifecycle.discard_core_observation(observed, now=self._now())
            self._prepared = self._ticket = None
            if not self._followups_remaining:
                _reject(CoreCohortAdapterReason.TARGET_UNAVAILABLE)
            self._followups_remaining -= 1
            _supported_connection(self.lifecycle.package_version, self.lifecycle.runtime)
            self._begin_observation()
            return None

        def publish(proof: RayClusterAttestation):
            _supported_connection(self.lifecycle.package_version, self.lifecycle.runtime)
            attestation_revision, capability_revision = self._revisions(proof)
            result = cohort_publication.publish_prepared_core_cohort_probe(
                prepared,
                proof,
                expected_attestation_revision=attestation_revision,
                expected_capability_revision=capability_revision,
                activate_new_target=True,
            )
            return self._convert(result, proof)

        result = self.lifecycle.publish_core(observed, publish, now=self._now())
        self._prepared = self._ticket = None
        return result

    def confirm_cleanup(self, ticket: QualificationOperation, confirm: Callable[[], bool]):
        """Release quarantine only with exact caller-owned remote cleanup proof."""
        self._parent()
        self.lifecycle.confirm_cleanup(ticket, confirm)
        self._prepared = self._ticket = None

    def eligible_aliases(self):
        """Return candidates only; the claim transaction must recheck authority."""
        self._parent()
        if self._invalidated:
            return ()
        now = self._now()
        lease = self._lease(now)
        return self.lifecycle.eligible_aliases(
            now=now,
            live_lease=self.lifecycle.lease,
            lease_expires_at=lease.last_heartbeat_at + get_lease_duration(),
        )
