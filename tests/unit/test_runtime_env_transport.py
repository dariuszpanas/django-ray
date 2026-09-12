from __future__ import annotations

import hashlib
import json
import pickle
from copy import deepcopy

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_ray.runtime_env_transport import (
    MAX_RUNTIME_ENV_IDENTITY_BYTES,
    RuntimeEnvTrustConfigurationError,
    WorkflowPlanValidationError,
    _normalize_json,
    validate_runtime_env_transport,
)
from django_ray.workflow import plans


def digest(domain, value):
    wire = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(domain + wire.encode()).hexdigest()


def resign(value):
    value = deepcopy(value)
    value.pop("transport_digest", None)
    value["transport_digest"] = digest(b"django-ray.runtime-env-plan-transport-v1\0", value)
    return value


def manifest(*, trust=None):
    return resign(
        {
            "plan_format": "django-ray.runtime-env-plan",
            "plan_format_version": 1,
            "profile": None,
            "digest": "sha256:" + "a" * 64,
            "reusable": False,
            "unresolved_paths": ["spec.pip.0"],
            "total_unresolved_paths": 1,
            "unresolved_paths_truncated": False,
            "retry_safe": False,
            "retry_unsafe_paths": ["spec.pip.0"],
            "total_retry_unsafe_paths": 1,
            "retry_unsafe_paths_truncated": False,
            "trust_digest": digest(b"django-ray.workflow-plan-trust-v1\0", trust or {}),
        }
    )


def test_public_plan_shape_and_shared_error_identity_are_preserved():
    value = manifest()
    normalized = validate_runtime_env_transport(value)
    public = plans.runtime_env_plan_identity_from_transport(value)
    assert public.as_transport_dict() == normalized == value
    assert public.reusable is False and public.retry_safe is False
    assert public.unresolved_paths == public.retry_unsafe_paths == ("spec.pip.0",)
    assert plans._normalize_json is _normalize_json
    assert plans.WorkflowPlanValidationError is WorkflowPlanValidationError
    assert WorkflowPlanValidationError.__module__ == "django_ray.workflow.plans"
    assert type(pickle.loads(pickle.dumps(WorkflowPlanValidationError("fixed")))) is (
        plans.WorkflowPlanValidationError
    )
    normalized["unresolved_paths"].clear()
    assert value["unresolved_paths"] == ["spec.pip.0"]
    assert public.unresolved_paths == ("spec.pip.0",)


def test_current_numeric_and_unicode_normalization_is_shared_not_duplicated():
    assert _normalize_json({"e\u0301": 1.0}, path="$", depth=0) == {"é": 1}
    with pytest.raises(WorkflowPlanValidationError, match="duplicate keys"):
        _normalize_json({"é": 1, "e\u0301": 2}, path="$", depth=0)
    value = manifest()
    # Existing plan transport canonicalizes integral floats before checking its digest.
    value["plan_format_version"] = 1.0
    assert validate_runtime_env_transport(value)["plan_format_version"] == 1


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("extra", {}, "unsupported schema"),
        ("plan_format", "other", "unsupported format version"),
        ("profile", "bad profile", "invalid profile"),
        ("digest", "sha256:" + "A" * 64, "invalid digest"),
        ("unresolved_paths", ["bad/path"], "invalid unresolved paths"),
        ("unresolved_paths", ["spec.z", "spec.a"], "invalid unresolved paths"),
        ("unresolved_paths", ["spec.a", "spec.a"], "invalid unresolved paths"),
        ("total_unresolved_paths", True, "eligibility metadata"),
        ("total_unresolved_paths", 0, "eligibility metadata"),
        ("unresolved_paths_truncated", 1, "eligibility metadata"),
        ("reusable", True, "eligibility metadata"),
        ("retry_unsafe_paths", ["bad/path"], "invalid retry-unsafe paths"),
        ("total_retry_unsafe_paths", True, "retry-safety metadata"),
        ("retry_unsafe_paths_truncated", True, "retry-safety metadata"),
        ("retry_safe", True, "retry-safety metadata"),
    ],
)
def test_transport_retains_schema_diagnostic_and_eligibility_validation(field, value, message):
    changed = resign(manifest() | {field: value})
    for validate in (
        validate_runtime_env_transport,
        plans.runtime_env_plan_identity_from_transport,
    ):
        with pytest.raises(WorkflowPlanValidationError, match=message):
            validate(changed)


def test_truncated_retry_diagnostics_need_not_be_subset_of_retained_unresolved_paths():
    value = resign(
        manifest()
        | {
            "unresolved_paths": [f"spec.modules.{index:02d}" for index in range(16)],
            "total_unresolved_paths": 17,
            "unresolved_paths_truncated": True,
            "retry_unsafe_paths": ["spec.uv.0"],
        }
    )
    assert validate_runtime_env_transport(value) == value


def test_live_trust_and_forwarded_structure_have_distinct_meanings():
    trust = {"trust_domain": "remote"}
    value = manifest(trust=trust)
    assert validate_runtime_env_transport(value, trust_identity=trust) == value
    with pytest.raises(WorkflowPlanValidationError, match="worker trust identity"):
        validate_runtime_env_transport(value)
    assert validate_runtime_env_transport(value, require_trust_match=False) == value
    forged = value | {"trust_digest": "sha256:" + "b" * 64}
    with pytest.raises(WorkflowPlanValidationError, match="checksum"):
        validate_runtime_env_transport(forged, require_trust_match=False)
    with pytest.raises(WorkflowPlanValidationError, match="must be a boolean"):
        validate_runtime_env_transport(value, require_trust_match=1)


@pytest.mark.parametrize("trust", [{"unexpected": "value"}, {"trust_domain": 1}, "bad"])
def test_public_trust_configuration_errors_remain_improperly_configured(trust):
    value = manifest()
    with pytest.raises(RuntimeEnvTrustConfigurationError) as pure:
        validate_runtime_env_transport(value, trust_identity=trust)
    with pytest.raises(ImproperlyConfigured) as public:
        plans.runtime_env_plan_identity_from_transport(value, trust_identity=trust)
    assert str(pure.value) == str(public.value)
    with pytest.raises(ImproperlyConfigured) as normalization:
        plans._normalize_trust_identity(trust)
    assert str(normalization.value) == str(public.value)


@pytest.mark.parametrize(
    "value,message",
    [
        ({str(index): 0 for index in range(257)}, "mapping entries"),
        ({"extra": list(range(1025))}, "sequence entries"),
        ({"extra": "x" * 2049}, "maximum length"),
        ({"extra": float("nan")}, "non-finite"),
    ],
)
def test_shared_normalization_resource_limits_remain_enforced(value, message):
    with pytest.raises(WorkflowPlanValidationError, match=message):
        validate_runtime_env_transport(value)


def test_depth_and_complete_transport_byte_bounds_remain_enforced():
    nested = 0
    for _ in range(18):
        nested = {"nested": nested}
    with pytest.raises(WorkflowPlanValidationError, match="nesting depth"):
        validate_runtime_env_transport(nested)
    paths = [f"spec.{index:02d}." + "x" * 504 for index in range(16)]
    value = resign(
        manifest()
        | {
            "unresolved_paths": paths,
            "total_unresolved_paths": 16,
            "retry_unsafe_paths": paths,
            "total_retry_unsafe_paths": 16,
        }
    )
    assert len(json.dumps(value).encode()) > MAX_RUNTIME_ENV_IDENTITY_BYTES
    with pytest.raises(WorkflowPlanValidationError, match="byte limit"):
        validate_runtime_env_transport(value)
