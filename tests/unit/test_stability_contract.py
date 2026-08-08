"""Candidate 1.0 public API and stability-policy contracts."""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
INVENTORY_PATH = ROOT / "tests" / "contracts" / "public_api_v1.json"
POLICY_PATH = ROOT / "docs" / "stability.md"


def _inventory() -> dict[str, Any]:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def test_public_api_inventory_is_versioned_and_deterministic() -> None:
    inventory = _inventory()

    assert inventory["schema_version"] == 1
    assert inventory["contract"] == "django-ray-public-api-v1"
    assert inventory["status"] == "candidate"

    identities = [(entry["module"], entry["name"]) for entry in inventory["symbols"]]
    assert identities == sorted(identities)
    assert len(identities) == len(set(identities))


def test_candidate_public_symbols_and_parameters_remain_available() -> None:
    for entry in _inventory()["symbols"]:
        module = importlib.import_module(entry["module"])
        symbol = getattr(module, entry["name"])

        if entry["kind"] == "class":
            assert inspect.isclass(symbol), f"{entry['module']}.{entry['name']} is not a class"
            continue

        assert inspect.isfunction(symbol), f"{entry['module']}.{entry['name']} is not a function"
        actual_parameters = list(inspect.signature(symbol).parameters)
        assert actual_parameters == entry["parameters"], (
            f"{entry['module']}.{entry['name']} changed parameters; preserve compatibility "
            "or update the candidate contract with an explicit migration decision"
        )


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
