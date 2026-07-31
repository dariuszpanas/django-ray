"""Serial pytest selector for manifest-backed taxonomy lanes.

The plugin is inert unless ``--taxonomy-lane`` is supplied. Loading it from
the root test conftest keeps fixture-aware selection available to the inventory
runner without changing ordinary pytest or pytest-xdist sessions.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.test_suite_taxonomy import (
    CollectedTest,
    Group,
    InventoryError,
    collection_contract_digest,
    load_manifest,
    nodeid_digest,
)


@dataclass
class _PluginState:
    root: Path
    group: Group
    collected: list[CollectedTest] = field(default_factory=list)
    selected: list[CollectedTest] = field(default_factory=list)
    deselected_count: int = 0


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


def _manifest_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise pytest.UsageError("--taxonomy-manifest must be a non-empty path")
    candidate = Path(value.strip())
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise pytest.UsageError("taxonomy manifest must stay inside the repository") from error
    return resolved


def _reject_parallel_execution(config: pytest.Config) -> None:
    """Keep manifest-backed timing serial, finite, and separate from pytest-xdist."""

    configured_workers = getattr(config.option, "numprocesses", None)
    configured_transports = getattr(config.option, "tx", None)
    configured_distribution = getattr(config.option, "dist", "no")
    loop_on_fail = getattr(config.option, "looponfail", False)
    if (
        configured_workers not in (None, 0)
        or configured_transports
        or configured_distribution not in (None, "no")
        or loop_on_fail
    ):
        raise pytest.UsageError(
            "manifest-backed taxonomy runs are serial; use ordinary pytest without "
            "--taxonomy-lane for pytest-xdist"
        )


def _configuration(config: pytest.Config) -> tuple[Path, Group] | None:
    lane = config.getoption("taxonomy_lane")
    if lane is None:
        return None
    if not isinstance(lane, str) or not lane.strip():
        raise pytest.UsageError("--taxonomy-lane must be a non-empty manifest id")
    _reject_parallel_execution(config)
    root = Path(str(config.rootpath)).resolve()
    manifest_path = _manifest_path(root, config.getoption("taxonomy_manifest"))
    try:
        manifest = load_manifest(manifest_path)
        group = manifest.group(lane.strip())
    except InventoryError as error:
        raise pytest.UsageError(str(error)) from error
    return root, group


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    """Reject xdist subprocess/watch mode before it bypasses normal configure hooks."""

    if config.getoption("taxonomy_lane") is not None:
        _reject_parallel_execution(config)


def pytest_configure(config: pytest.Config) -> None:
    """Load one active serial selection."""
    global _LAST_RUN_REPORT

    configured = _configuration(config)
    if configured is None:
        return
    root, group = configured
    _LAST_RUN_REPORT = None
    config.stash[_STATE_KEY] = _PluginState(
        root=root,
        group=group,
    )


def _state(config: pytest.Config) -> _PluginState | None:
    return config.stash.get(_STATE_KEY, None)


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


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Publish exact serial collection evidence for the inventory runner."""
    global _LAST_RUN_REPORT

    state = _state(session.config)
    if state is None:
        return
    report = {
        "mode": "serial",
        **_selected_report(state),
        "valid": True,
        "errors": [],
    }
    _LAST_RUN_REPORT = report


def consume_last_run_report() -> dict[str, object] | None:
    """Return and clear the controller report from the latest pytest.main call."""
    global _LAST_RUN_REPORT

    report = copy.deepcopy(_LAST_RUN_REPORT)
    _LAST_RUN_REPORT = None
    return report
