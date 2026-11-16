"""Candidate 1.0 public API and stability-policy contracts."""

from __future__ import annotations

import importlib
import inspect
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
INVENTORY_PATH = ROOT / "tests" / "contracts" / "public_api_v1.json"
POLICY_PATH = ROOT / "docs" / "stability.md"


def _inventory() -> dict[str, Any]:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def test_public_api_inventory_is_versioned_and_deterministic() -> None:
    inventory = _inventory()

    assert inventory["schema_version"] == 2
    assert inventory["contract"] == "django-ray-public-api-v1"
    assert inventory["status"] == "candidate"

    identities = [(entry["module"], entry["name"]) for entry in inventory["symbols"]]
    assert identities == sorted(identities)
    assert len(identities) == len(set(identities))
    for entry in inventory["symbols"]:
        if entry["kind"] == "function":
            assert all(
                set(parameter) == {"name", "kind", "has_default"}
                and type(parameter["has_default"]) is bool
                for parameter in entry["parameters"]
            )


def test_candidate_public_symbols_and_parameters_remain_available() -> None:
    for entry in _inventory()["symbols"]:
        module = importlib.import_module(entry["module"])
        symbol = getattr(module, entry["name"])

        if entry["kind"] == "class":
            assert inspect.isclass(symbol), f"{entry['module']}.{entry['name']} is not a class"
            continue

        assert inspect.isfunction(symbol), f"{entry['module']}.{entry['name']} is not a function"
        actual_parameters = [
            {
                "name": parameter.name,
                "kind": parameter.kind.name,
                "has_default": parameter.default is not inspect.Parameter.empty,
            }
            for parameter in inspect.signature(symbol).parameters.values()
        ]
        assert actual_parameters == entry["parameters"], (
            f"{entry['module']}.{entry['name']} changed parameters; preserve compatibility "
            "or update the candidate contract with an explicit migration decision"
        )


def _optional_timestamp(*, generated_at=None):
    return generated_at


def _required_timestamp(*, generated_at):
    return generated_at


def _positional_timestamp(generated_at=None, /):
    return generated_at


def _added_required_parameter(*, generated_at=None, required):
    return generated_at, required


def _added_optional_parameter(*, generated_at=None, optional=None):
    return generated_at, optional


def _changed_default_value(*, generated_at="different default"):
    return generated_at


@pytest.fixture
def synthetic_inventory(monkeypatch):
    entry = deepcopy(
        next(
            entry
            for entry in _inventory()["symbols"]
            if entry["module"] == "django_ray.observability" and entry["name"] == "get_queue_depths"
        )
    )
    module = ModuleType("candidate_signature_fixture")
    entry.update(module=module.__name__, name="candidate")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(globals(), "_inventory", lambda: {"symbols": [entry]})
    return module, entry


@pytest.mark.parametrize(
    "changed, kwargs",
    [(_required_timestamp, {}), (_positional_timestamp, {"generated_at": None})],
)
def test_signature_oracle_rejects_same_name_call_breakage(synthetic_inventory, changed, kwargs):
    module, _ = synthetic_inventory
    original = inspect.signature(_optional_timestamp)
    replacement = inspect.signature(changed)
    assert list(original.parameters) == list(replacement.parameters)
    original.bind(**kwargs)
    with pytest.raises(TypeError):
        replacement.bind(**kwargs)
    module.candidate = changed
    with pytest.raises(AssertionError, match="changed parameters"):
        test_candidate_public_symbols_and_parameters_remain_available()


def test_signature_oracle_rejects_an_added_required_name(synthetic_inventory):
    module, _ = synthetic_inventory
    inspect.signature(_optional_timestamp).bind()
    with pytest.raises(TypeError):
        inspect.signature(_added_required_parameter).bind()
    module.candidate = _added_required_parameter
    with pytest.raises(AssertionError, match="changed parameters"):
        test_candidate_public_symbols_and_parameters_remain_available()


def test_optional_addition_requires_a_reviewed_inventory_update(synthetic_inventory):
    module, entry = synthetic_inventory
    inspect.signature(_optional_timestamp).bind()
    inspect.signature(_added_optional_parameter).bind()
    module.candidate = _added_optional_parameter
    with pytest.raises(AssertionError, match="changed parameters"):
        test_candidate_public_symbols_and_parameters_remain_available()
    entry["parameters"].append({"name": "optional", "kind": "KEYWORD_ONLY", "has_default": True})
    test_candidate_public_symbols_and_parameters_remain_available()


def test_signature_oracle_does_not_freeze_default_values(synthetic_inventory):
    module, _ = synthetic_inventory
    assert inspect.signature(_changed_default_value).parameters["generated_at"].default is not None
    module.candidate = _changed_default_value
    test_candidate_public_symbols_and_parameters_remain_available()


def test_stability_policy_defines_every_contract_class_and_boundary() -> None:
    policy = POLICY_PATH.read_text(encoding="utf-8")
    normalized_policy = " ".join(policy.split())

    for heading in (
        "## Proposed stable 1.0 surface",
        "## Experimental surface",
        "## Private and example surface",
        "## Deprecation policy",
        "## Versioning boundaries",
        "## Release enforcement",
    ):
        assert heading in policy

    for required_boundary in (
        "at least one feature release and at least 90 days",
        "Package SemVer describes the public application contract",
        "It is not the durable execution protocol",
        "Ray Compiled Graph",
        "native Windows Ray execution",
        "bundled `testproject` HTTP API",
        "tests/contracts/public_api_v1.json",
    ):
        assert required_boundary in normalized_policy
