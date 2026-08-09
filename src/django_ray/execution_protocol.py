"""Version constants for django-ray's durable task execution contract.

Package versions are diagnostic provenance.  These integer epochs are the
normative compatibility boundary for persisted task execution and worker
capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass

LEGACY_EXECUTION_METADATA_SCHEMA_VERSION = 0
EXECUTION_METADATA_SCHEMA_VERSION = 1
LEGACY_EXECUTION_PROTOCOL_VERSION = 1
EXECUTION_PROTOCOL_VERSION = LEGACY_EXECUTION_PROTOCOL_VERSION

LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION = 0
WORKER_CAPABILITY_SCHEMA_VERSION = 1
MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION = EXECUTION_PROTOCOL_VERSION
MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION = EXECUTION_PROTOCOL_VERSION

PROTOCOL_POLICY_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ExecutionProtocolRange:
    """One explicit inclusive worker capability range."""

    minimum: int
    maximum: int

    def supports(self, protocol_version: int) -> bool:
        """Return whether the durable execution protocol is supported."""
        return self.minimum <= protocol_version <= self.maximum


def explicit_worker_protocol_range(
    *,
    capability_schema_version: int,
    legacy_admission_token_present: bool,
    minimum: int | None,
    maximum: int | None,
) -> ExecutionProtocolRange | None:
    """Decode an explicit worker advertisement, failing closed otherwise."""
    if (
        capability_schema_version != WORKER_CAPABILITY_SCHEMA_VERSION
        or legacy_admission_token_present
        or minimum is None
        or maximum is None
        or minimum < 1
        or maximum < minimum
    ):
        return None
    return ExecutionProtocolRange(minimum=minimum, maximum=maximum)
