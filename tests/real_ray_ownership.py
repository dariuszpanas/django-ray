"""Compatibility exports for the historical real-Ray ownership boundary.

The coordinator owns the implementation. Keeping these exact imports preserves
the legacy module path for older tests and linked worktrees while preventing the
locked byte, metadata schema, and path-hardening logic from drifting apart.
"""

from __future__ import annotations

from scripts.local_resource_coordinator import (
    DEFAULT_REAL_RAY_LOCK_PATH,
    LOCK_BYTE_OFFSET,
    MAX_OWNER_METADATA_BYTES,
    OWNER_METADATA_OFFSET,
    RealRayOwnershipLock,
    RealRayOwnershipPathError,
    RealRayOwnershipUnavailableError,
    build_owner_metadata,
)
from scripts.local_resource_coordinator import (
    os as os,
)

__all__ = [
    "DEFAULT_REAL_RAY_LOCK_PATH",
    "LOCK_BYTE_OFFSET",
    "MAX_OWNER_METADATA_BYTES",
    "OWNER_METADATA_OFFSET",
    "RealRayOwnershipLock",
    "RealRayOwnershipPathError",
    "RealRayOwnershipUnavailableError",
    "build_owner_metadata",
    "os",
]
