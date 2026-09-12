"""Bounded inspection of an original application submission, never new admission.

Expectations originate in the manager's retained dispatch or supported-writer
completion recovery. They are not self-authenticating receipts. A physical
STOPPED submission proves only its outer driver is terminal; it says nothing
about invocation or descendant cleanup. Other terminal statuses require the
independently bound durable completion before the ledger can resolve them.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from threading import Event, Thread

from django_ray.execution_codec import ExecutionIdentity
from django_ray.ray_job_protocol import (
    _RQ2_METADATA_KEYS,
    STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
    RayJobRequestReferenceExpectation,
    _duplicate_safe_object,
    coordination_sha256,
    load_ray_job_request_expectation,
    validate_ray_job_request_reference_expectation,
)
from django_ray.ray_job_request_storage import (
    decode_ray_job_request_locator,
    encode_ray_job_request_locator,
    ray_job_request_reference_content_identity,
)
from django_ray.runtime.cohort_entrypoint import _fetch_execution_job
from django_ray.target.cohort_contract import (
    CohortExecutionContract,
    _digest,
    cohort_execution_contract_digest,
)
from django_ray.target.cohort_job_http import _arguments, _Budget, _read_body
from django_ray.target.cohort_job_receipt import is_canonical_native_ray_job_id
from django_ray.target.cohort_transport import validate_prepared_cohort_execution
from django_ray.target.probe import RAY_TARGET_PROBE_RAY_VERSION

_COMMAND = "python -m django_ray.runtime.cohort_entrypoint --request-ref-b64 "
_COHORT_METADATA = {
    "cohort_request_digest",
    "cohort_contract_digest",
    "cohort_jobs_endpoint",
    "cohort_submitted_runtime_env_digest",
}


class CohortJobExecutionControlError(RuntimeError):
    def __init__(self):
        super().__init__("Cohort Job control inspection unavailable")


@dataclass(frozen=True, slots=True)
class CohortJobExecutionExpectation:
    identity: ExecutionIdentity
    jobs_endpoint: str = field(repr=False)
    submission_id: str
    request_digest: str
    contract_digest: str
    request_reference: str = field(repr=False)
    raw_request_sha256: str
    request_size_bytes: int


@dataclass(frozen=True, slots=True)
class CohortJobExecutionInspection:
    expectation: CohortJobExecutionExpectation
    status: str
    outer_driver_terminal: bool
    native_job_id: str | None


def _outside_transaction():
    from django.db import connections

    if any(
        connection.in_atomic_block
        or (connection.connection is not None and not connection.get_autocommit())
        for connection in connections.all()
    ):
        raise CohortJobExecutionControlError


def _validate(expected):
    if type(expected) is not CohortJobExecutionExpectation:
        raise CohortJobExecutionControlError
    if type(expected.identity) is not ExecutionIdentity:
        raise CohortJobExecutionControlError
    submission_id = STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX + coordination_sha256(
        expected.identity
    )
    endpoint = _arguments(expected.jobs_endpoint, "django-ray-cohort-probe-" + "0" * 64, 5)[0]
    if (
        expected.submission_id != submission_id
        or endpoint != expected.jobs_endpoint
        or type(expected.raw_request_sha256) is not str
        or type(expected.request_size_bytes) is not int
        or ray_job_request_reference_content_identity(expected.request_reference)
        != (expected.raw_request_sha256, expected.request_size_bytes)
    ):
        raise CohortJobExecutionControlError
    _digest(expected.request_digest)
    _digest(expected.contract_digest)


def build_cohort_job_execution_expectation(dispatch):
    """Capture old submission bindings without retrieving input or RuntimeEnv.

    Missing durable reference is insufficient for physical cancellation proof.
    Recovery validation may read protected claim/attestation rows outside locks;
    the subsequent inspector performs no database query.
    """
    _outside_transaction()
    try:
        from django_ray.runner.cohort_dispatch import PreparedCohortDispatch
        from django_ray.runner.cohort_recovery import (
            RecoveredCohortJobCompletion,
            validate_recovered_cohort_job_completion,
        )
        from django_ray.target.cohort_claim import (
            CohortClaimDisposition,
            CohortRunnerFamily,
            cohort_claim_facts_digest,
        )

        if type(dispatch) is RecoveredCohortJobCompletion:
            validate_recovered_cohort_job_completion(dispatch)
            record = dispatch.claim
            endpoint = dispatch.handle.ray_address
            submission_id = dispatch.handle.ray_job_id
            request_digest, contract_digest = dispatch.request_digest, dispatch.contract_digest
            reference = dispatch.request_reference
            raw_sha, size = ray_job_request_reference_content_identity(reference)
        elif type(dispatch) is PreparedCohortDispatch:
            record, task = dispatch.claim, dispatch.execution
            request, contract = validate_prepared_cohort_execution(dispatch.prepared, task=task)
            qualification = record.facts.job_qualification
            if (
                type(contract) is not CohortExecutionContract
                or request.compiled_graph_submission_transport != "ray-job"
                or record.facts.binding.runner_family is not CohortRunnerFamily.RAY_JOB
                or qualification is None
                or contract.identity != record.facts.identity
                or contract.cohort_evidence_id != record.claim_id
                or contract.cohort_evidence_digest != record.facts_digest
                or contract.claimed_at != record.facts.claimed_at
                or contract.target_binding_id != record.facts.binding_id
                or contract.expected_django_ray_version != record.facts.binding.package_version
                or record.facts_digest != cohort_claim_facts_digest(record.facts)
                or record.disposition
                not in {CohortClaimDisposition.OPEN, CohortClaimDisposition.HELD}
                or record.dispatched_at is None
                or task.claimed_by_worker != record.owner.worker_id
                or task.state not in {"RUNNING", "CANCELLING"}
                or task.ray_address != qualification.jobs_endpoint
                or record.prepared_request_digest != dispatch.prepared.request_digest
                or cohort_execution_contract_digest(contract) != dispatch.prepared.contract_digest
            ):
                raise CohortJobExecutionControlError
            endpoint, submission_id = task.ray_address, task.ray_job_id
            reference = task.ray_job_request_reference
            request_digest, contract_digest = (
                dispatch.prepared.request_digest,
                dispatch.prepared.contract_digest,
            )
            payload = dispatch.prepared.request_json.encode("utf-8")
            raw_sha, size = hashlib.sha256(payload).hexdigest(), len(payload)
        else:
            raise CohortJobExecutionControlError
        expected = CohortJobExecutionExpectation(
            record.facts.identity,
            endpoint,
            submission_id,
            request_digest,
            contract_digest,
            reference,
            raw_sha,
            size,
        )
        _validate(expected)
        return expected
    except Exception:
        raise CohortJobExecutionControlError from None


def inspect_cohort_job_execution(expected):
    """Read one exact physical Jobs record under the fixed five-second budget.

    This does not stop or resubmit anything. The owner may issue one existing
    address-pinned stop request separately, retaining an ambiguous response.
    """
    _outside_transaction()
    try:
        _validate(expected)
        import ray
        from ray.dashboard.modules.job.pydantic_models import JobDetails, JobStatus, JobType

        if ray.__version__ != RAY_TARGET_PROBE_RAY_VERSION:
            raise CohortJobExecutionControlError
        details = _fetch_execution_job(expected.jobs_endpoint, expected.submission_id)
        if (
            type(details) is not JobDetails
            or details.type is not JobType.SUBMISSION
            or details.submission_id != expected.submission_id
            or type(details.entrypoint) is not str
            or not details.entrypoint.startswith(_COMMAND)
            or type(details.metadata) is not dict
            or set(details.metadata) != set(_RQ2_METADATA_KEYS) | _COHORT_METADATA
            or details.metadata["cohort_request_digest"] != expected.request_digest
            or details.metadata["cohort_contract_digest"] != expected.contract_digest
            or details.metadata["cohort_jobs_endpoint"] != expected.jobs_endpoint
            or type(details.runtime_env) is not dict
        ):
            raise CohortJobExecutionControlError
        encoded_locator = details.entrypoint[len(_COMMAND) :]
        locator = decode_ray_job_request_locator(encoded_locator)
        if (
            encode_ray_job_request_locator(locator) != encoded_locator
            or locator.reference != expected.request_reference
            or (locator.digest, locator.size_bytes)
            != (expected.raw_request_sha256, expected.request_size_bytes)
        ):
            raise CohortJobExecutionControlError
        metadata = load_ray_job_request_expectation(json.dumps({"metadata": details.metadata}))
        if type(metadata) is not RayJobRequestReferenceExpectation:
            raise CohortJobExecutionControlError
        validate_ray_job_request_reference_expectation(
            metadata,
            expected_identity=expected.identity,
            expected_execution_protocol_version=3,
            expected_request_sha256=expected.raw_request_sha256,
            expected_request_size_bytes=expected.request_size_bytes,
            expected_submission_id=expected.submission_id,
            request_reference=expected.request_reference,
            request_locator=encoded_locator,
        )
        serialized_env = json.dumps(
            details.runtime_env,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if "sha256:" + hashlib.sha256(serialized_env.encode("utf-8")).hexdigest() != _digest(
            details.metadata["cohort_submitted_runtime_env_digest"]
        ):
            raise CohortJobExecutionControlError
        if details.job_id is None:
            if details.driver_info is not None:
                raise CohortJobExecutionControlError
        elif (
            not is_canonical_native_ray_job_id(details.job_id)
            or details.driver_info is None
            or details.driver_info.id != details.job_id
        ):
            raise CohortJobExecutionControlError
        return CohortJobExecutionInspection(
            expected, details.status.value, details.status is JobStatus.STOPPED, details.job_id
        )
    except Exception:
        raise CohortJobExecutionControlError from None


def _stop_execution_job(expected):
    """One exact Ray 2.58 stop POST, with no SDK discovery or request retry."""
    import requests
    from ray.dashboard.modules.dashboard_sdk import SubmissionClient

    _validate(expected)
    budget = _Budget(5)
    client = SubmissionClient(address=expected.jobs_endpoint)
    if client._address != expected.jobs_endpoint:
        raise CohortJobExecutionControlError
    headers = dict(client._headers, **{"Accept-Encoding": "identity"})
    with requests.Session() as session:
        session.trust_env = False
        session.proxies.clear()
        remaining = budget.remaining()
        response = session.post(
            expected.jobs_endpoint + "/api/jobs/" + expected.submission_id + "/stop",
            headers=headers,
            cookies=client._cookies,
            verify=client._verify,
            proxies={},
            stream=True,
            allow_redirects=False,
            timeout=(remaining, remaining),
        )
        try:
            if type(response.status_code) is not int or response.status_code != 200:
                raise CohortJobExecutionControlError
            payload = json.loads(
                _read_body(response, budget), object_pairs_hook=_duplicate_safe_object
            )
        finally:
            response.close()
    if (
        type(payload) is not dict
        or set(payload) != {"stopped"}
        or type(payload["stopped"]) is not bool
    ):
        raise CohortJobExecutionControlError
    budget.remaining()
    return payload["stopped"]


@dataclass(frozen=True, slots=True)
class CohortJobControlTicket:
    expectation: CohortJobExecutionExpectation
    operation: str


@dataclass(frozen=True, slots=True)
class CohortJobControlResult:
    ticket: CohortJobControlTicket
    inspection: CohortJobExecutionInspection | None
    stop_requested: bool | None
    uncertainty: str | None


@dataclass(slots=True)
class _ControlOperation:
    ticket: CohortJobControlTicket
    began: float
    deadline: float
    last_parent_time: float
    completed: Event = field(default_factory=Event)
    thread: Thread | None = None
    result: CohortJobControlResult | None = None
    uncertainty: str | None = None


def _monotonic():
    try:
        value = time.monotonic()
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError
        return value
    except Exception:
        raise CohortJobExecutionControlError from None


class CohortJobExecutionController:
    """One owned callback; neither a timeout nor local retirement kills it.

    The parent must reserve durable INDETERMINATE cancellation before begin_stop
    and retain that marker across restart. This local map prevents replay only
    during this controller's lifetime. Callbacks perform no database queries.
    One retained slot is reserved for terminal inspection so acknowledged or
    ambiguous stops cannot prevent their own resolution. A capacity of one
    therefore permits inspection only.
    """

    def __init__(self, *, max_pending=100):
        if type(max_pending) is not int or not 1 <= max_pending <= 100:
            raise CohortJobExecutionControlError
        self._max_pending = max_pending
        self._operations: dict[int, _ControlOperation] = {}
        self._stops: dict[ExecutionIdentity, CohortJobControlTicket] = {}
        self._inspections: dict[ExecutionIdentity, CohortJobControlTicket] = {}
        self._active: _ControlOperation | None = None

    @property
    def busy(self):
        """True until the owned thread exits, regardless of deadline/clock."""
        operation = self._active
        if operation is None:
            return False
        if operation.thread is not None and operation.thread.is_alive():
            return True
        self._active = None
        return False

    def _owned(self, ticket):
        operation = self._operations.get(id(ticket))
        if operation is None or operation.ticket is not ticket:
            raise CohortJobExecutionControlError
        return operation

    def begin_inspection(self, expected, *, timeout_seconds=6.0):
        return self._begin(expected, "inspect", timeout_seconds)

    def begin_stop(self, expected, *, timeout_seconds=12.0):
        return self._begin(expected, "stop", timeout_seconds)

    def _begin(self, expected, kind, timeout):
        _outside_transaction()
        try:
            _validate(expected)
            if (
                type(timeout) not in {int, float}
                or not math.isfinite(timeout)
                or not 0 < timeout <= 12
            ):
                raise CohortJobExecutionControlError
            previous = self._stops.get(expected.identity)
            if previous is not None:
                if previous.expectation != expected:
                    raise CohortJobExecutionControlError
                if kind == "stop":
                    return previous
            if kind == "inspect" and expected.identity in self._inspections:
                previous = self._inspections[expected.identity]
                if previous.expectation != expected:
                    raise CohortJobExecutionControlError
                return previous
            if self.busy:
                active = self._active
                if active.ticket.operation == kind and active.ticket.expectation == expected:
                    return active.ticket
                return None
            if len(self._operations) >= self._max_pending:
                return None
            if kind == "stop" and len(self._stops) >= self._max_pending - 1:
                return None
            now = _monotonic()
        except Exception:
            raise CohortJobExecutionControlError from None
        ticket = CohortJobControlTicket(expected, kind)
        operation = _ControlOperation(ticket, now, now + timeout, now)
        self._operations[id(ticket)] = operation
        if kind == "stop":
            self._stops[expected.identity] = ticket
        else:
            self._inspections[expected.identity] = ticket
        self._active = operation

        def run():
            previous_time = operation.began

            def fresh():
                nonlocal previous_time
                current = _monotonic()
                if (
                    operation.uncertainty is not None
                    or current < previous_time
                    or current >= operation.deadline
                ):
                    raise CohortJobExecutionControlError
                previous_time = current

            try:
                fresh()
                inspection = inspect_cohort_job_execution(expected)
                fresh()
                if (
                    type(inspection) is not CohortJobExecutionInspection
                    or inspection.expectation is not expected
                ):
                    raise CohortJobExecutionControlError
                requested = None
                if kind == "stop" and inspection.status in {"PENDING", "RUNNING"}:
                    requested = _stop_execution_job(expected)
                    fresh()
                    if requested is not True:
                        raise CohortJobExecutionControlError
                operation.result = CohortJobControlResult(ticket, inspection, requested, None)
            except BaseException:
                operation.result = CohortJobControlResult(ticket, None, None, "control_unconfirmed")
            finally:
                operation.completed.set()

        try:
            operation.thread = Thread(target=run, daemon=True, name="django-ray-cohort-job-control")
            operation.thread.start()
        except Exception:
            operation.thread = None
            operation.uncertainty = "callback_start_failed"
            operation.completed.set()
        return ticket

    def poll(self, ticket):
        """Sample only local state; late results are discarded permanently."""
        operation = self._owned(ticket)
        active = self.busy
        try:
            now = _monotonic()
            if now < operation.last_parent_time:
                raise CohortJobExecutionControlError
            operation.last_parent_time = now
            if now >= operation.deadline:
                operation.uncertainty = operation.uncertainty or "deadline"
        except Exception:
            operation.uncertainty = operation.uncertainty or "clock_unavailable"
        if operation.uncertainty is not None:
            return CohortJobControlResult(ticket, None, None, operation.uncertainty)
        if not operation.completed.is_set() or active and self._active is operation:
            return None
        return operation.result

    def retire_inspection(self, ticket):
        """Discard a finished read; the next read needs its own owned ticket."""
        operation = self._owned(ticket)
        if ticket.operation != "inspect":
            raise CohortJobExecutionControlError
        if operation.thread is not None and operation.thread.is_alive():
            return False
        self._operations.pop(id(ticket))
        self._inspections.pop(ticket.expectation.identity)
        return True

    def retire_execution(self, identity):
        """Called only after fenced ledger retirement, never to retry a stop."""
        if type(identity) is not ExecutionIdentity:
            raise CohortJobExecutionControlError
        matches = [
            operation
            for operation in self._operations.values()
            if operation.ticket.expectation.identity == identity
        ]
        if any(
            operation.thread is not None and operation.thread.is_alive() for operation in matches
        ):
            return False
        for operation in matches:
            self._operations.pop(id(operation.ticket))
        self._stops.pop(identity, None)
        self._inspections.pop(identity, None)
        return True
