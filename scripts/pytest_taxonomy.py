"""Worker-loaded pytest selector for manifest-backed taxonomy lanes.

The plugin is inert unless ``--taxonomy-lane`` is supplied. Loading it from
the root test conftest makes the same fixture-aware selector available in the
controller and every pytest-xdist worker.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.test_suite_taxonomy import (
    CollectedTest,
    ExecutionPolicy,
    Group,
    InventoryError,
    collection_contract_digest,
    load_manifest,
    nodeid_digest,
)

WORKER_INPUT_KEY = "django_ray_taxonomy"
WORKER_OUTPUT_KEY = "django_ray_taxonomy_collection"


@dataclass(frozen=True)
class TaxonomyConfiguration:
    """Serializable selection and execution identity shared with workers."""

    manifest: str
    lane: str
    selection: dict[str, list[str]]
    expression: str
    execution: dict[str, object]

    def as_mapping(self) -> dict[str, object]:
        return {
            "manifest": self.manifest,
            "lane": self.lane,
            "selection": self.selection,
            "expression": self.expression,
            "execution": self.execution,
        }


@dataclass
class _PluginState:
    root: Path
    group: Group
    configuration: TaxonomyConfiguration
    worker_id: str | None
    collected: list[CollectedTest] = field(default_factory=list)
    selected: list[CollectedTest] = field(default_factory=list)
    deselected_count: int = 0
    started_workers: list[str] = field(default_factory=list)
    controller_collections: dict[str, tuple[str, ...]] = field(default_factory=dict)
    worker_collections: dict[str, dict[str, object]] = field(default_factory=dict)
    worker_errors: dict[str, str] = field(default_factory=dict)


_STATE_KEY: pytest.StashKey[_PluginState] = pytest.StashKey()
_LAST_RUN_REPORT: dict[str, object] | None = None


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register explicit options without activating taxonomy selection."""
    group = parser.getgroup("django-ray taxonomy")
    group.addoption(
        "--taxonomy-manifest",
        default=".github/test-suite-taxonomy.json",
        help="repository-relative taxonomy manifest used with --taxonomy-lane",
    )
    group.addoption(
        "--taxonomy-lane",
        default=None,
        help="activate one manifest-backed taxonomy selection",
    )
    group.addoption(
        "--taxonomy-execution",
        choices=("serial", "xdist"),
        default="serial",
        help="run serially or use the selected lane's fixed xdist policy",
    )


def _worker_id(config: pytest.Config) -> str | None:
    worker_input = getattr(config, "workerinput", None)
    if not isinstance(worker_input, dict):
        return None
    value = worker_input.get("workerid")
    return value if isinstance(value, str) and value else "unknown-worker"


def _manifest_path(root: Path, value: object) -> tuple[Path, str]:
    if not isinstance(value, str) or not value.strip():
        raise pytest.UsageError("--taxonomy-manifest must be a non-empty path")
    candidate = Path(value.strip())
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise pytest.UsageError("taxonomy manifest must stay inside the repository") from error
    return resolved, relative


def _requested_execution(config: pytest.Config, group: Group) -> ExecutionPolicy:
    mode = config.getoption("taxonomy_execution")
    execution = ExecutionPolicy() if mode == "serial" else group.execution
    if mode == "xdist" and group.execution.mode != "xdist":
        raise pytest.UsageError(
            f"taxonomy lane {group.id!r} does not declare an xdist execution policy"
        )
    configured_workers = getattr(config.option, "numprocesses", None)
    configured_distribution = getattr(config.option, "dist", "no")
    configured_restarts_value = getattr(config.option, "maxworkerrestart", None)
    try:
        configured_restarts = (
            None if configured_restarts_value is None else int(configured_restarts_value)
        )
    except (TypeError, ValueError) as error:
        raise pytest.UsageError("--max-worker-restart must be an integer") from error
    if execution.mode == "serial":
        if configured_workers not in (None, 0):
            raise pytest.UsageError("serial taxonomy execution cannot activate pytest-xdist")
        return execution
    actual = {
        "workers": configured_workers,
        "distribution": configured_distribution,
        "max_worker_restart": configured_restarts,
    }
    expected = {
        "workers": execution.workers,
        "distribution": execution.distribution,
        "max_worker_restart": execution.max_worker_restart,
    }
    if actual != expected:
        raise pytest.UsageError(
            "taxonomy xdist execution must match the manifest exactly: "
            f"{expected['workers']} workers, {expected['distribution']}, "
            f"max-worker-restart={expected['max_worker_restart']}"
        )
    return execution


def _configuration(config: pytest.Config) -> tuple[Path, Group, TaxonomyConfiguration] | None:
    lane = config.getoption("taxonomy_lane")
    if lane is None:
        return None
    if not isinstance(lane, str) or not lane.strip():
        raise pytest.UsageError("--taxonomy-lane must be a non-empty manifest id")
    root = Path(str(config.rootpath)).resolve()
    manifest_path, manifest_relative = _manifest_path(root, config.getoption("taxonomy_manifest"))
    try:
        manifest = load_manifest(manifest_path)
        group = manifest.group(lane.strip())
    except InventoryError as error:
        raise pytest.UsageError(str(error)) from error
    requested_mode = config.getoption("taxonomy_execution")
    if _worker_id(config) is not None:
        execution = ExecutionPolicy() if requested_mode == "serial" else group.execution
        if requested_mode == "xdist" and group.execution.mode != "xdist":
            raise pytest.UsageError(
                f"taxonomy lane {group.id!r} does not declare an xdist execution policy"
            )
    else:
        execution = _requested_execution(config, group)
    configuration = TaxonomyConfiguration(
        manifest=manifest_relative,
        lane=group.id,
        selection=group.selection.as_mapping(),
        expression=group.selection.expression(),
        execution=execution.as_mapping(),
    )
    return root, group, configuration


def pytest_configure(config: pytest.Config) -> None:
    """Load active configuration and verify the controller-to-worker copy."""
    global _LAST_RUN_REPORT

    configured = _configuration(config)
    if configured is None:
        return
    root, group, configuration = configured
    worker_id = _worker_id(config)
    if worker_id is None:
        _LAST_RUN_REPORT = None
    else:
        worker_input = config.workerinput.get(WORKER_INPUT_KEY)
        if worker_input != configuration.as_mapping():
            raise pytest.UsageError(
                "taxonomy worker configuration differs from the controller selection"
            )
    config.stash[_STATE_KEY] = _PluginState(
        root=root,
        group=group,
        configuration=configuration,
        worker_id=worker_id,
    )


def _state(config: pytest.Config) -> _PluginState | None:
    return config.stash.get(_STATE_KEY, None)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    """Copy the validated controller configuration into each worker."""
    state = _state(node.config)
    if state is None:
        return
    worker_id = str(node.gateway.id)
    state.started_workers.append(worker_id)
    node.workerinput[WORKER_INPUT_KEY] = state.configuration.as_mapping()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply the fixture-aware selection after ownership guards inspect items."""
    state = _state(config)
    if state is None:
        return
    selected_items: list[pytest.Item] = []
    selected_records: list[CollectedTest] = []
    deselected: list[pytest.Item] = []
    collected_records: list[CollectedTest] = []
    for item in items:
        record = CollectedTest.from_pytest_item(item, state.root)
        collected_records.append(record)
        if state.group.selection.matches(record):
            selected_items.append(item)
            selected_records.append(record)
        else:
            deselected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected_items
    state.collected = sorted(collected_records, key=lambda item: item.nodeid)
    state.selected = sorted(selected_records, key=lambda item: item.nodeid)
    state.deselected_count = len(deselected)


def _selected_report(state: _PluginState) -> dict[str, object]:
    nodeids = [item.nodeid for item in state.selected]
    collected_nodeids = [item.nodeid for item in state.collected]
    return {
        "selected_count": len(nodeids),
        "deselected_count": state.deselected_count,
        "nodeid_digest": nodeid_digest(nodeids),
        "contract_digest": collection_contract_digest(state.selected),
        "collected_count": len(collected_nodeids),
        "collected_nodeid_digest": nodeid_digest(collected_nodeids),
        "collected_contract_digest": collection_contract_digest(state.collected),
    }


def pytest_collection_finish(session: pytest.Session) -> None:
    """Retain worker fixture-contract evidence for controller validation."""
    state = _state(session.config)
    if state is None or state.worker_id is None:
        return
    session.config.workeroutput[WORKER_OUTPUT_KEY] = {
        "configuration": state.configuration.as_mapping(),
        **_selected_report(state),
    }


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node: Any, ids: list[str]) -> None:
    """Capture each worker's selected node IDs before scheduling begins."""
    state = _state(node.config)
    if state is None:
        return
    state.controller_collections[str(node.gateway.id)] = tuple(
        nodeid.replace("\\", "/") for nodeid in ids
    )


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object | None) -> None:
    """Capture worker contract evidence and any terminal worker failure."""
    state = _state(node.config)
    if state is None:
        return
    worker_id = str(node.gateway.id)
    if error is not None:
        state.worker_errors[worker_id] = str(error)
    value = getattr(node, "workeroutput", {}).get(WORKER_OUTPUT_KEY)
    if isinstance(value, dict):
        state.worker_collections[worker_id] = copy.deepcopy(value)


def finalize_xdist_collection(state: _PluginState) -> dict[str, object]:
    """Validate exact worker collection and fixture-contract parity."""
    expected_workers = int(state.configuration.execution["workers"])
    errors: list[str] = []
    started = state.started_workers
    if len(started) != expected_workers or len(set(started)) != expected_workers:
        errors.append(
            f"expected exactly {expected_workers} workers without restart; started {started}"
        )
    if state.worker_errors:
        errors.append("worker failures: " + ", ".join(sorted(state.worker_errors)))
    if len(state.controller_collections) != expected_workers:
        errors.append("controller did not receive every worker collection")
    if len(state.worker_collections) != expected_workers:
        errors.append("controller did not receive every worker fixture contract")

    controller_values = list(state.controller_collections.values())
    canonical_nodeids = controller_values[0] if controller_values else ()
    if any(value != canonical_nodeids for value in controller_values[1:]):
        errors.append("xdist workers collected different selected node IDs")
    canonical_nodeid_digest = nodeid_digest(list(canonical_nodeids))

    contract_digests: set[str] = set()
    deselected_counts: set[int] = set()
    collected_counts: set[int] = set()
    collected_nodeid_digests: set[str] = set()
    collected_contract_digests: set[str] = set()
    worker_collections: list[dict[str, object]] = []
    for worker_id, worker_report in sorted(state.worker_collections.items()):
        if worker_report.get("configuration") != state.configuration.as_mapping():
            errors.append(f"{worker_id} reported a different taxonomy configuration")
        if worker_report.get("selected_count") != len(canonical_nodeids):
            errors.append(f"{worker_id} selected count differs from controller collection")
        if worker_report.get("nodeid_digest") != canonical_nodeid_digest:
            errors.append(f"{worker_id} selected node IDs differ from controller collection")
        contract_digest = worker_report.get("contract_digest")
        if not isinstance(contract_digest, str) or not contract_digest:
            errors.append(f"{worker_id} omitted its fixture-contract digest")
        else:
            contract_digests.add(contract_digest)
        deselected_count = worker_report.get("deselected_count")
        collected_count = worker_report.get("collected_count")
        collected_nodeid_value = worker_report.get("collected_nodeid_digest")
        collected_contract_value = worker_report.get("collected_contract_digest")
        if type(deselected_count) is not int or deselected_count < 0:
            errors.append(f"{worker_id} omitted its full pre-selection deselected count")
        else:
            deselected_counts.add(deselected_count)
        if type(collected_count) is not int or collected_count < 0:
            errors.append(f"{worker_id} omitted its full pre-selection collected count")
        else:
            collected_counts.add(collected_count)
            if (
                type(deselected_count) is int
                and collected_count != len(canonical_nodeids) + deselected_count
            ):
                errors.append(f"{worker_id} full pre-selection count is internally inconsistent")
        if not isinstance(collected_nodeid_value, str) or not collected_nodeid_value:
            errors.append(f"{worker_id} omitted its full pre-selection node-ID digest")
        else:
            collected_nodeid_digests.add(collected_nodeid_value)
        if not isinstance(collected_contract_value, str) or not collected_contract_value:
            errors.append(f"{worker_id} omitted its full pre-selection contract digest")
        else:
            collected_contract_digests.add(collected_contract_value)
        worker_collections.append(
            {
                "worker": worker_id,
                "selected_count": worker_report.get("selected_count"),
                "deselected_count": deselected_count,
                "nodeid_digest": worker_report.get("nodeid_digest"),
                "contract_digest": contract_digest,
                "collected_count": collected_count,
                "collected_nodeid_digest": collected_nodeid_value,
                "collected_contract_digest": collected_contract_value,
            }
        )
    if len(contract_digests) > 1:
        errors.append("xdist workers reported different fixture contracts")
    if len(deselected_counts) > 1 or len(collected_counts) > 1:
        errors.append("xdist workers reported different full pre-selection counts")
    if len(collected_nodeid_digests) > 1:
        errors.append("xdist workers reported different full pre-selection node IDs")
    if len(collected_contract_digests) > 1:
        errors.append("xdist workers reported different full pre-selection contracts")

    return {
        "mode": "xdist",
        "execution": state.configuration.execution,
        "selected_count": len(canonical_nodeids),
        "deselected_count": next(iter(deselected_counts), 0),
        "nodeid_digest": canonical_nodeid_digest,
        "contract_digest": next(iter(contract_digests), ""),
        "collected_count": next(iter(collected_counts), 0),
        "collected_nodeid_digest": next(iter(collected_nodeid_digests), ""),
        "collected_contract_digest": next(iter(collected_contract_digests), ""),
        "worker_collections": worker_collections,
        "valid": not errors,
        "errors": errors,
    }


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Publish collection evidence and fail a passing run on parity drift."""
    global _LAST_RUN_REPORT

    state = _state(session.config)
    if state is None:
        return
    if state.worker_id is not None:
        session.config.workeroutput[WORKER_OUTPUT_KEY] = {
            "configuration": state.configuration.as_mapping(),
            **_selected_report(state),
        }
        return
    if state.configuration.execution["mode"] == "xdist":
        report = finalize_xdist_collection(state)
    else:
        report = {
            "mode": "serial",
            "execution": state.configuration.execution,
            **_selected_report(state),
            "worker_collections": [],
            "valid": True,
            "errors": [],
        }
    _LAST_RUN_REPORT = report
    if not report["valid"]:
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_sep("=", "taxonomy collection parity failure")
            for error in cast(list[str], report["errors"]):
                terminal.write_line(f"ERROR: {error}")
    if not report["valid"] and exitstatus == int(pytest.ExitCode.OK):
        session.exitstatus = pytest.ExitCode.USAGE_ERROR


def consume_last_run_report() -> dict[str, object] | None:
    """Return and clear the controller report from the latest pytest.main call."""
    global _LAST_RUN_REPORT

    report = copy.deepcopy(_LAST_RUN_REPORT)
    _LAST_RUN_REPORT = None
    return report
