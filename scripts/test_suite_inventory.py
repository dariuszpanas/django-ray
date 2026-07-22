"""Collect, classify, select, and time the pytest suite by execution contract."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

MANIFEST_SCHEMA_VERSION = 2
REPORT_SCHEMA_VERSION = 2
TIMING_SCHEMA_VERSION = 2
DEFAULT_MANIFEST = Path(".github/test-suite-taxonomy.json")
GENERATED_BASELINE_RE = re.compile(
    r"^docs/investigations/test-suite-baseline-\d{4}-\d{2}-\d{2}\.(?:json|md)$"
)
BINARY_SUFFIXES = frozenset(
    {".gif", ".gz", ".ico", ".jpeg", ".jpg", ".pdf", ".png", ".whl", ".zip"}
)
ENVIRONMENT_PACKAGES = (
    "coverage",
    "django",
    "pytest",
    "pytest-cov",
    "pytest-django",
    "pytest-xdist",
    "ray",
)
OUTCOME_NAMES = ("passed", "failed", "skipped", "xfailed", "xpassed")
SKIPPED_OUTCOMES = frozenset({"skipped", "xfailed"})
PYTEST_PHASE_NAMES = ("setup", "call", "teardown")
TIMING_INTERVAL_FIELDS = (
    "process_seconds",
    "initialization_seconds",
    "collection_seconds",
    "execution_wall_seconds",
    "setup_phase_seconds",
    "call_phase_seconds",
    "teardown_phase_seconds",
    "post_test_reporting_seconds",
    "terminal_reporting_seconds",
    "cleanup_seconds",
)
PROMISED_RUNTIME_INTERVAL_FIELDS = tuple(
    field
    for field in TIMING_INTERVAL_FIELDS
    if field not in {"setup_phase_seconds", "call_phase_seconds", "teardown_phase_seconds"}
)


class InventoryError(ValueError):
    """Raised when taxonomy input or collected evidence is inconsistent."""


def _normalized_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InventoryError(f"{label} must be a non-empty path")
    normalized = value.strip().replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise InventoryError(f"{label} must be repository-relative")
    normalized = normalized.strip("/")
    parts = PurePosixPath(normalized).parts
    if not parts or ".." in parts:
        raise InventoryError(f"{label} must stay inside the repository")
    return PurePosixPath(*parts).as_posix()


def _string_tuple(value: object, label: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or (required and not value):
        qualifier = "a non-empty" if required else "a"
        raise InventoryError(f"{label} must be {qualifier} list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise InventoryError(f"{label} entries must be non-empty strings")
    normalized = tuple(cast(str, item).strip() for item in value)
    if len(normalized) != len(set(normalized)):
        raise InventoryError(f"{label} contains duplicate entries")
    return normalized


@dataclass(frozen=True)
class Selection:
    """Declarative path and marker selection shared by reports and later CI work."""

    paths: tuple[str, ...]
    include_markers: tuple[str, ...] = ()
    include_any_markers: tuple[str, ...] = ()
    exclude_markers: tuple[str, ...] = ()
    include_fixtures: tuple[str, ...] = ()
    include_any_fixtures: tuple[str, ...] = ()
    exclude_fixtures: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: object, label: str) -> Selection:
        if not isinstance(value, dict):
            raise InventoryError(f"{label} must be an object")
        paths = tuple(
            _normalized_path(path, f"{label} path")
            for path in _string_tuple(value.get("paths"), f"{label} paths", required=True)
        )
        if len(paths) != len(set(paths)):
            raise InventoryError(f"{label} paths contain canonical duplicates")
        include = _string_tuple(value.get("include_markers"), f"{label} include_markers")
        include_any = _string_tuple(
            value.get("include_any_markers"), f"{label} include_any_markers"
        )
        exclude = _string_tuple(value.get("exclude_markers"), f"{label} exclude_markers")
        if (set(include) | set(include_any)) & set(exclude):
            raise InventoryError(f"{label} includes and excludes the same marker")
        include_fixtures = _string_tuple(value.get("include_fixtures"), f"{label} include_fixtures")
        include_any_fixtures = _string_tuple(
            value.get("include_any_fixtures"), f"{label} include_any_fixtures"
        )
        exclude_fixtures = _string_tuple(value.get("exclude_fixtures"), f"{label} exclude_fixtures")
        if (set(include_fixtures) | set(include_any_fixtures)) & set(exclude_fixtures):
            raise InventoryError(f"{label} includes and excludes the same fixture")
        return cls(
            paths,
            include,
            include_any,
            exclude,
            include_fixtures,
            include_any_fixtures,
            exclude_fixtures,
        )

    def matches(self, item: CollectedTest) -> bool:
        markers = set(item.markers)
        fixtures = set(item.fixtures)
        include_any = bool(self.include_any_markers or self.include_any_fixtures)
        matches_any = bool(
            set(self.include_any_markers) & markers or set(self.include_any_fixtures) & fixtures
        )
        return (
            any(_path_matches(item.path, path) for path in self.paths)
            and set(self.include_markers) <= markers
            and set(self.include_fixtures) <= fixtures
            and (not include_any or matches_any)
            and not bool(set(self.exclude_markers) & markers)
            and not bool(set(self.exclude_fixtures) & fixtures)
        )

    def expression(self) -> str:
        path_expression = " OR ".join(f"path:{path}" for path in self.paths)
        clauses = [f"({path_expression})"]
        clauses.extend(f"marker:{marker}" for marker in self.include_markers)
        clauses.extend(f"fixture:{fixture}" for fixture in self.include_fixtures)
        if self.include_any_markers or self.include_any_fixtures:
            any_expression = " OR ".join(
                [f"marker:{marker}" for marker in self.include_any_markers]
                + [f"fixture:{fixture}" for fixture in self.include_any_fixtures]
            )
            clauses.append(f"({any_expression})")
        clauses.extend(f"NOT marker:{marker}" for marker in self.exclude_markers)
        clauses.extend(f"NOT fixture:{fixture}" for fixture in self.exclude_fixtures)
        return " AND ".join(clauses)

    def pytest_arguments(self) -> list[str]:
        if self.include_fixtures or self.include_any_fixtures or self.exclude_fixtures:
            raise InventoryError("fixture-aware selection requires the manifest-backed run command")
        arguments = list(self.paths)
        marker_clauses = list(self.include_markers)
        if self.include_any_markers:
            marker_clauses.append("(" + " or ".join(self.include_any_markers) + ")")
        marker_clauses.extend(f"not {marker}" for marker in self.exclude_markers)
        if marker_clauses:
            arguments.extend(["-m", " and ".join(marker_clauses)])
        return arguments

    def as_mapping(self) -> dict[str, list[str]]:
        return {
            "paths": list(self.paths),
            "include_markers": list(self.include_markers),
            "include_any_markers": list(self.include_any_markers),
            "exclude_markers": list(self.exclude_markers),
            "include_fixtures": list(self.include_fixtures),
            "include_any_fixtures": list(self.include_any_fixtures),
            "exclude_fixtures": list(self.exclude_fixtures),
        }


def _path_matches(item_path: str, selected_path: str) -> bool:
    return item_path == selected_path or item_path.startswith(f"{selected_path.rstrip('/')}/")


@dataclass(frozen=True)
class SkipPolicy:
    """Whether a successful timing observation may contain skipped cases."""

    mode: str
    reason: str


@dataclass(frozen=True)
class Group:
    """One named execution contract, product boundary, or CI lane."""

    id: str
    kind: str
    owner: str
    contract: str
    selection: Selection
    skip_policy: SkipPolicy
    django_settings_modules: tuple[str, ...]
    variants: int = 1


@dataclass(frozen=True)
class OverlapCandidate:
    """A review target, not a claim that cases are redundant."""

    id: str
    owner: str
    paths: tuple[str, ...]
    reason: str
    review: str


@dataclass(frozen=True)
class Manifest:
    """Validated taxonomy manifest."""

    execution_contracts: tuple[Group, ...]
    domains: tuple[Group, ...]
    boundaries: tuple[Group, ...]
    profiles: tuple[Group, ...]
    ci_lanes: tuple[Group, ...]
    overlap_candidates: tuple[OverlapCandidate, ...]

    @property
    def groups(self) -> tuple[Group, ...]:
        return (
            self.execution_contracts
            + self.domains
            + self.boundaries
            + self.profiles
            + self.ci_lanes
        )

    def group(self, group_id: str) -> Group:
        matches = [group for group in self.groups if group.id == group_id]
        if len(matches) != 1:
            raise InventoryError(f"unknown taxonomy group: {group_id}")
        return matches[0]


def _group(value: object, kind: str, index: int) -> Group:
    label = f"{kind} {index}"
    if not isinstance(value, dict):
        raise InventoryError(f"{label} must be an object")
    group_id = value.get("id")
    owner = value.get("owner")
    contract = value.get("contract")
    if not isinstance(group_id, str) or not group_id.strip():
        raise InventoryError(f"{label} needs a non-empty id")
    if not isinstance(owner, str) or not owner.strip():
        raise InventoryError(f"{label} needs a non-empty owner")
    if not isinstance(contract, str) or not contract.strip():
        raise InventoryError(f"{label} needs a non-empty contract")
    variants = value.get("variants", 1)
    if isinstance(variants, bool) or not isinstance(variants, int) or variants < 1:
        raise InventoryError(f"{label} variants must be a positive integer")
    if kind != "ci_lane" and variants != 1:
        raise InventoryError(f"{label} may only set variants for a CI lane")
    raw_skip_policy = value.get("skip_policy")
    if raw_skip_policy is None:
        raise InventoryError(f"{label} needs an explicit skip_policy")
    else:
        if not isinstance(raw_skip_policy, dict):
            raise InventoryError(f"{label} skip_policy must be an object")
        mode = raw_skip_policy.get("mode")
        reason = raw_skip_policy.get("reason")
        if mode not in {"allow", "forbid"}:
            raise InventoryError(f"{label} skip_policy mode must be allow or forbid")
        if not isinstance(reason, str) or not reason.strip():
            raise InventoryError(f"{label} skip_policy needs a non-empty reason")
        skip_policy = SkipPolicy(mode=mode, reason=" ".join(reason.split()))
    django_settings_modules = _string_tuple(
        value.get("django_settings_modules", ["unset"]),
        f"{label} django_settings_modules",
        required=True,
    )
    if not all(
        module == "unset" or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", module)
        for module in django_settings_modules
    ):
        raise InventoryError(f"{label} has an invalid Django settings module identity")
    return Group(
        id=group_id.strip(),
        kind=kind,
        owner=" ".join(owner.split()),
        contract=" ".join(contract.split()),
        selection=Selection.from_mapping(value.get("selection"), f"{label} selection"),
        skip_policy=skip_policy,
        django_settings_modules=django_settings_modules,
        variants=variants,
    )


def load_manifest(path: Path) -> Manifest:
    """Load and fail closed on a malformed taxonomy manifest."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InventoryError(f"cannot load taxonomy manifest from {path}") from error
    if not isinstance(document, dict) or document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise InventoryError("unsupported taxonomy manifest schema")

    def groups(key: str, kind: str, *, required: bool = True) -> tuple[Group, ...]:
        raw = document.get(key)
        if not isinstance(raw, list) or (required and not raw):
            raise InventoryError(f"manifest {key} must be a non-empty list")
        return tuple(_group(item, kind, index) for index, item in enumerate(raw))

    execution_contracts = groups("execution_contracts", "execution_contract")
    domains = groups("domains", "domain")
    boundaries = groups("boundaries", "boundary")
    profiles = groups("profiles", "profile")
    ci_lanes = groups("ci_lanes", "ci_lane")
    raw_candidates = document.get("overlap_candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise InventoryError("manifest overlap_candidates must be a non-empty list")
    candidates: list[OverlapCandidate] = []
    for index, value in enumerate(raw_candidates):
        label = f"overlap candidate {index}"
        if not isinstance(value, dict):
            raise InventoryError(f"{label} must be an object")
        candidate_id = value.get("id")
        owner = value.get("owner")
        reason = value.get("reason")
        review = value.get("review")
        if not all(
            isinstance(field, str) and field.strip()
            for field in (candidate_id, owner, reason, review)
        ):
            raise InventoryError(f"{label} needs non-empty id, owner, reason, and review")
        paths = tuple(
            _normalized_path(item, f"{label} path")
            for item in _string_tuple(value.get("paths"), f"{label} paths", required=True)
        )
        if len(paths) != len(set(paths)):
            raise InventoryError(f"{label} paths contain canonical duplicates")
        candidates.append(
            OverlapCandidate(
                id=cast(str, candidate_id).strip(),
                owner=" ".join(cast(str, owner).split()),
                paths=paths,
                reason=" ".join(cast(str, reason).split()),
                review=" ".join(cast(str, review).split()),
            )
        )
    manifest = Manifest(
        execution_contracts,
        domains,
        boundaries,
        profiles,
        ci_lanes,
        tuple(candidates),
    )
    ids = [group.id for group in manifest.groups] + [candidate.id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise InventoryError("taxonomy ids must be unique across groups and candidates")
    return manifest


@dataclass(frozen=True)
class CollectedTest:
    """Stable pytest collection metadata used for classification."""

    nodeid: str
    path: str
    markers: tuple[str, ...]
    fixtures: tuple[str, ...]
    parameter_keys: tuple[str, ...] = ()

    @classmethod
    def from_pytest_item(cls, item: pytest.Item, root: Path) -> CollectedTest:
        """Build one shared collection record for inventory and runtime selection."""
        try:
            relative_path = Path(item.path).resolve().relative_to(root.resolve()).as_posix()
        except ValueError as error:
            raise InventoryError(f"collected path leaves repository: {item.path}") from error
        callspec = getattr(item, "callspec", None)
        parameter_keys = tuple(sorted(callspec.params)) if callspec is not None else ()
        fixture_info = getattr(item, "_fixtureinfo", None)
        fixture_defs = getattr(fixture_info, "name2fixturedefs", {})
        fixtures: list[str] = []
        for name in item.fixturenames:
            definitions = fixture_defs.get(name) or ()
            active_definition = definitions[-1] if definitions else None
            fixture_function = getattr(active_definition, "func", None)
            if getattr(fixture_function, "__name__", "") == "get_direct_param_fixture_func":
                continue
            fixtures.append(name)
        return cls(
            nodeid=item.nodeid.replace("\\", "/"),
            path=relative_path,
            markers=tuple(sorted({marker.name for marker in item.iter_markers()})),
            fixtures=tuple(sorted(set(fixtures))),
            parameter_keys=parameter_keys,
        )

    @property
    def parameterized(self) -> bool:
        return bool(self.parameter_keys)

    @property
    def family(self) -> str:
        return self.nodeid.split("[", maxsplit=1)[0]


class _CollectionPlugin:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.items: list[CollectedTest] = []

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
            self.items.append(CollectedTest.from_pytest_item(item, self.root))


def collect_tests(root: Path) -> list[CollectedTest]:
    """Collect the suite once without executing it or parsing terminal output."""
    _reject_pytest_environment()
    plugin = _CollectionPlugin(root)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = pytest.main(["--collect-only", "-q", "tests"], plugins=[plugin])
    if exit_code != pytest.ExitCode.OK:
        detail = stderr.getvalue().strip() or stdout.getvalue().strip()
        raise InventoryError(f"pytest collection failed with exit code {int(exit_code)}: {detail}")
    return sorted(plugin.items, key=lambda item: item.nodeid)


def _django_settings_identity() -> str:
    return os.environ.get("DJANGO_SETTINGS_MODULE", "").strip() or "unset"


def _reject_pytest_environment(
    allowed_django_settings_modules: tuple[str, ...] = ("unset",),
) -> None:
    for variable in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        if os.environ.get(variable, "").strip():
            raise InventoryError(
                f"{variable} is not allowed because ambient state can change taxonomy collection"
            )
    if "PYTEST_DISABLE_PLUGIN_AUTOLOAD" in os.environ:
        raise InventoryError(
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD is not allowed because ambient state can "
            "change taxonomy collection"
        )
    if _django_settings_identity() not in allowed_django_settings_modules:
        allowed = ", ".join(allowed_django_settings_modules)
        raise InventoryError(
            f"DJANGO_SETTINGS_MODULE identity must match the selected profile: {allowed}"
        )


def _validate_pytest_passthrough(arguments: list[str]) -> None:
    xdist_options = (
        "-d",
        "-n",
        "--dist",
        "--max-worker-restart",
        "--numprocesses",
        "--rsyncdir",
        "--tx",
    )
    if any(
        argument == option
        or argument.startswith(f"{option}=")
        or (option == "-n" and argument.startswith("-n"))
        for argument in arguments
        for option in xdist_options
    ):
        raise InventoryError(
            "xdist is not supported by the serial taxonomy runner; issue #169 owns "
            "worker-loaded selection"
        )
    forbidden = (
        "--co",
        "--collect-in-virtualenv",
        "--collect-only",
        "--confcutdir",
        "--deselect",
        "--disable-plugin-autoload",
        "--doctest-modules",
        "--failed-first",
        "--ff",
        "--ignore",
        "--ignore-glob",
        "--keep-duplicates",
        "--last-failed",
        "--lf",
        "--new-first",
        "--nf",
        "--noconftest",
        "--override-ini",
        "--pyargs",
        "--rootdir",
        "--setup-only",
        "--setup-plan",
        "--stepwise",
        "--stepwise-reset",
        "--stepwise-skip",
        "--sw",
        "--sw-reset",
        "--sw-skip",
        "-k",
        "-m",
        "-o",
        "-p",
    )
    rejected = [
        argument
        for argument in arguments
        if any(
            argument == option
            or argument.startswith(f"{option}=")
            or (option in {"-k", "-m", "-o", "-p"} and argument.startswith(option))
            for option in forbidden
        )
    ]
    if rejected:
        raise InventoryError(
            "unsupported pytest passthrough can change taxonomy selection: " + ", ".join(rejected)
        )


def _source_digest(root: Path, manifest_path: Path) -> dict[str, object]:
    root = root.resolve()
    try:
        manifest_relative = manifest_path.resolve().relative_to(root).as_posix()
    except ValueError as error:
        raise InventoryError("taxonomy manifest must stay inside the repository") from error
    try:
        result = subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise InventoryError("cannot enumerate Git-visible taxonomy source inputs") from error
    relative_paths = sorted(
        {
            os.fsdecode(raw_path).replace("\\", "/")
            for raw_path in result.stdout.split(b"\0")
            if raw_path
        }
        | {manifest_relative}
    )
    relative_paths = [path for path in relative_paths if not GENERATED_BASELINE_RE.fullmatch(path)]
    digest = hashlib.sha256()
    for relative in relative_paths:
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if not path.exists():
            digest.update(b"missing\0")
            continue
        if not path.is_file():
            digest.update(b"non-file\0")
            continue
        try:
            content = path.read_bytes()
        except OSError as error:
            raise InventoryError(f"cannot hash taxonomy source input {path}") from error
        if path.suffix.lower() not in BINARY_SUFFIXES and b"\0" not in content:
            content = content.replace(b"\r\n", b"\n")
        digest.update(b"file\0")
        digest.update(hashlib.sha256(content).digest())
    return {
        "algorithm": "sha256",
        "digest": digest.hexdigest(),
        "file_count": len(relative_paths),
        "roots": [
            "git ls-files --cached --others --exclude-standard",
            "excluding generated test-suite baseline JSON and Markdown",
        ],
    }


def _validate_output_path(
    root: Path,
    path: Path,
    label: str,
    *,
    allow_generated_baseline: bool = False,
    generated_baseline_suffix: str | None = None,
) -> None:
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return
    if allow_generated_baseline and GENERATED_BASELINE_RE.fullmatch(relative):
        if generated_baseline_suffix is not None and resolved.suffix != generated_baseline_suffix:
            raise InventoryError(f"{label} generated baseline must use {generated_baseline_suffix}")
        return
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", relative],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return
    if result.returncode > 1:
        raise InventoryError(f"cannot verify {label} against Git ignore rules")
    raise InventoryError(
        f"{label} inside the repository must be ignored or a generated dated baseline"
    )


def _validate_collect_path_aliases(
    root: Path,
    json_output: Path,
    markdown_output: Path,
    timing_inputs: list[Path],
) -> None:
    def resolved(path: Path) -> Path:
        return path.resolve() if path.is_absolute() else (root / path).resolve()

    json_path = resolved(json_output)
    markdown_path = resolved(markdown_output)
    if json_path == markdown_path:
        raise InventoryError("JSON and Markdown outputs must use different paths")
    timing_paths = {resolved(path) for path in timing_inputs}
    aliases = [path for path in (json_path, markdown_path) if path in timing_paths]
    if aliases:
        raise InventoryError("collection outputs must not overwrite timing inputs")


def _environment_record(*, include_processor_count: bool = False) -> dict[str, object]:
    packages: dict[str, str] = {}
    for package in ENVIRONMENT_PACKAGES:
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = "not-installed"
    record: dict[str, object] = {
        "django_settings_module": _django_settings_identity(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": packages,
    }
    if include_processor_count:
        record["processor_count"] = os.cpu_count() or 0
    return record


def _group_record(group: Group, items: list[CollectedTest]) -> dict[str, object]:
    selected = [item for item in items if group.selection.matches(item)]
    try:
        pytest_arguments: list[str] | None = group.selection.pytest_arguments()
    except InventoryError:
        pytest_arguments = None
    return {
        "id": group.id,
        "kind": group.kind,
        "owner": group.owner,
        "contract": group.contract,
        "skip_policy": asdict(group.skip_policy),
        "django_settings_modules": list(group.django_settings_modules),
        "variants": group.variants,
        "selection": {
            "expression": group.selection.expression(),
            "pytest_arguments": pytest_arguments,
        },
        "selected_count": len(selected),
        "file_count": len({item.path for item in selected}),
        "estimated_ci_selected_case_slots": len(selected) * group.variants
        if group.kind == "ci_lane"
        else None,
    }


def _file_records(
    items: list[CollectedTest],
    contracts: tuple[Group, ...],
    domains: tuple[Group, ...],
) -> list[dict[str, object]]:
    by_path: dict[str, list[CollectedTest]] = defaultdict(list)
    for item in items:
        by_path[item.path].append(item)
    records: list[dict[str, object]] = []
    for path, file_items in by_path.items():
        contract_counts = {
            contract.id: sum(contract.selection.matches(item) for item in file_items)
            for contract in contracts
        }
        domain_counts = {
            domain.id: sum(domain.selection.matches(item) for item in file_items)
            for domain in domains
        }
        records.append(
            {
                "path": path,
                "collected_count": len(file_items),
                "parameterized_cases": sum(item.parameterized for item in file_items),
                "execution_contracts": {
                    key: value for key, value in contract_counts.items() if value
                },
                "domains": {key: value for key, value in domain_counts.items() if value},
            }
        )
    return sorted(
        records, key=lambda record: (-cast(int, record["collected_count"]), record["path"])
    )


def _directory_records(items: list[CollectedTest]) -> list[dict[str, object]]:
    counts: Counter[str] = Counter()
    for item in items:
        parts = Path(item.path).parts
        directory = "/".join(parts[:2]) if len(parts) > 2 else parts[0]
        counts[directory] += 1
    return [
        {"path": path, "collected_count": count}
        for path, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _require_nonnegative_number(
    value: object,
    label: str,
    *,
    allow_none: bool = False,
) -> None:
    if value is None and allow_none:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise InventoryError(f"{label} must be a finite nonnegative number")


def _require_nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InventoryError(f"{label} must be a nonnegative integer")
    return value


def _validate_timing_detail_records(
    timing: dict[str, Any],
    outcome_counts: dict[str, int],
    selected_items: list[CollectedTest],
) -> None:
    selected_nodeids = {item.nodeid for item in selected_items}
    selected_files = {item.path for item in selected_items}
    skipped = timing.get("skipped_tests")
    if not isinstance(skipped, list):
        raise InventoryError("timing evidence needs skipped-test detail")
    skipped_nodeids: set[str] = set()
    skipped_outcomes: Counter[str] = Counter()
    for record in skipped:
        if not isinstance(record, dict):
            raise InventoryError("timing skipped-test entries must be objects")
        nodeid = record.get("nodeid")
        outcome = record.get("outcome")
        if not isinstance(nodeid, str) or not nodeid.strip():
            raise InventoryError("timing skipped-test entries need nodeids")
        if outcome not in SKIPPED_OUTCOMES:
            raise InventoryError("timing skipped-test entries need skipped outcomes")
        if nodeid in skipped_nodeids:
            raise InventoryError("timing skipped-test nodeids must be unique")
        if nodeid not in selected_nodeids:
            raise InventoryError("timing skipped-test nodeids must belong to the selection")
        skipped_nodeids.add(nodeid)
        skipped_outcomes[outcome] += 1
    expected_skipped_outcomes = Counter(
        {outcome: outcome_counts[outcome] for outcome in SKIPPED_OUTCOMES}
    )
    if skipped_outcomes != expected_skipped_outcomes:
        raise InventoryError(
            "timing skipped-test outcome distribution does not match outcome counts"
        )

    for field, identity_field, selected_identities, maximum in (
        ("slowest_tests", "nodeid", selected_nodeids, 50),
        ("slowest_files", "path", selected_files, 30),
    ):
        records = timing.get(field)
        expected_length = min(len(selected_identities), maximum)
        if not isinstance(records, list) or len(records) != expected_length:
            raise InventoryError(f"timing {field} must retain {expected_length} selected entries")
        identities: set[str] = set()
        previous_total = math.inf
        for record in records:
            if not isinstance(record, dict):
                raise InventoryError(f"timing {field} entries must be objects")
            identity = record.get(identity_field)
            if not isinstance(identity, str) or not identity.strip():
                raise InventoryError(f"timing {field} entries need {identity_field}")
            if identity in identities:
                raise InventoryError(f"timing {field} identities must be unique")
            if identity not in selected_identities:
                raise InventoryError(f"timing {field} identities must belong to the selection")
            identities.add(identity)
            total = record.get("total_seconds")
            _require_nonnegative_number(total, f"timing {field} total_seconds")
            numeric_total = cast(float, total)
            if numeric_total > previous_total:
                raise InventoryError(f"timing {field} must be sorted slowest first")
            previous_total = numeric_total
            phases = record.get("phases")
            if (
                not isinstance(phases, dict)
                or not phases
                or not set(phases) <= set(PYTEST_PHASE_NAMES)
            ):
                raise InventoryError(f"timing {field} entries need pytest phase durations")
            for phase, duration in phases.items():
                _require_nonnegative_number(duration, f"timing {field} {phase} duration")
            if not math.isclose(
                numeric_total,
                sum(cast(float, duration) for duration in phases.values()),
                abs_tol=0.00002,
            ):
                raise InventoryError(f"timing {field} total must equal its phase sum")


def _validate_timing_record(
    timing: dict[str, Any],
    source: dict[str, object],
    manifest: Manifest,
    items: list[CollectedTest],
) -> str:
    if timing.get("schema_version") != TIMING_SCHEMA_VERSION:
        raise InventoryError("timing evidence has an unsupported schema")
    measured_at = timing.get("measured_at_utc")
    if not isinstance(measured_at, str):
        raise InventoryError("timing evidence needs a UTC measurement timestamp")
    try:
        measured_datetime = datetime.fromisoformat(measured_at)
    except ValueError as error:
        raise InventoryError("timing evidence has an invalid measurement timestamp") from error
    if measured_datetime.utcoffset() != UTC.utcoffset(measured_datetime):
        raise InventoryError("timing evidence timestamp must use UTC")
    sample_id = timing.get("sample_id")
    if not isinstance(sample_id, str):
        raise InventoryError("timing evidence needs a generated sample_id")
    try:
        parsed_sample_id = uuid.UUID(sample_id)
    except ValueError as error:
        raise InventoryError("timing evidence has an invalid sample_id") from error
    if str(parsed_sample_id) != sample_id:
        raise InventoryError("timing evidence sample_id must use canonical UUID form")
    timing_source = timing.get("source")
    if timing_source != source:
        raise InventoryError("timing evidence does not match the current source digest")
    if timing.get("source_after_digest") != source["digest"]:
        raise InventoryError("timing evidence source changed while pytest was running")
    identities: list[str] = []
    for field in ("lane", "observation", "variant"):
        value = timing.get(field)
        if not isinstance(value, str) or not value.strip():
            raise InventoryError(f"timing evidence needs a non-empty {field}")
        identities.append(value)
    lane_id = identities[0]
    group = manifest.group(lane_id)
    if timing.get("selection") != group.selection.expression():
        raise InventoryError("timing evidence selection does not match the current manifest")
    if timing.get("skip_policy") != asdict(group.skip_policy):
        raise InventoryError("timing evidence skip policy does not match the current manifest")
    pytest_arguments = timing.get("pytest_arguments")
    if not isinstance(pytest_arguments, list) or not all(
        isinstance(argument, str) for argument in pytest_arguments
    ):
        raise InventoryError("timing evidence needs string pytest arguments")
    _validate_pytest_passthrough(pytest_arguments)
    integrity = timing.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("valid") is not True:
        raise InventoryError("timing evidence failed its execution-integrity checks")
    if integrity.get("errors") != []:
        raise InventoryError("valid timing evidence cannot retain integrity errors")

    pytest_record = timing.get("pytest")
    if not isinstance(pytest_record, dict) or pytest_record.get("exit_code") != 0:
        raise InventoryError("timing evidence must come from a successful pytest run")
    selected_count = _require_nonnegative_integer(
        pytest_record.get("selected_count"), "timing selected_count"
    )
    selected_items = [item for item in items if group.selection.matches(item)]
    expected_count = len(selected_items)
    if selected_count != expected_count:
        raise InventoryError("timing evidence selected count does not match current collection")
    completed_count = _require_nonnegative_integer(
        pytest_record.get("completed_count"), "timing completed_count"
    )
    logfinished_count = _require_nonnegative_integer(
        pytest_record.get("logfinished_count"), "timing logfinished_count"
    )
    if completed_count != selected_count or logfinished_count != selected_count:
        raise InventoryError("timing evidence does not prove every selected test completed")
    outcomes = pytest_record.get("outcomes")
    if not isinstance(outcomes, dict):
        raise InventoryError("timing evidence needs outcome counts")
    outcome_counts = {
        name: _require_nonnegative_integer(outcomes.get(name), f"timing outcome {name}")
        for name in OUTCOME_NAMES
    }
    if sum(outcome_counts.values()) != selected_count or outcome_counts["failed"]:
        raise InventoryError("timing outcome counts do not match successful selected work")
    if group.skip_policy.mode == "forbid" and (
        outcome_counts["skipped"] or outcome_counts["xfailed"]
    ):
        raise InventoryError("timing evidence violates the group's skip policy")
    _validate_timing_detail_records(timing, outcome_counts, selected_items)
    deselected_count = _require_nonnegative_integer(
        pytest_record.get("deselected_count"), "timing deselected_count"
    )
    if deselected_count != len(items) - selected_count:
        raise InventoryError("timing deselected count does not match the full collection")
    coverage_enabled = pytest_record.get("coverage_enabled")
    if not isinstance(coverage_enabled, bool):
        raise InventoryError("timing coverage_enabled must be a boolean")
    requested_coverage = any(
        argument == "--cov" or argument.startswith("--cov=") for argument in pytest_arguments
    )
    if requested_coverage and "--no-cov" not in pytest_arguments and not coverage_enabled:
        raise InventoryError("timing coverage flag does not match pytest arguments")
    if "--no-cov" in pytest_arguments and coverage_enabled:
        raise InventoryError("timing no-cov flag does not match effective pytest config")
    for field in TIMING_INTERVAL_FIELDS:
        _require_nonnegative_number(pytest_record.get(field), f"timing {field}")
    external = timing.get("external")
    if (
        not isinstance(external, dict)
        or not isinstance(external.get("note"), str)
        or not external["note"].strip()
    ):
        raise InventoryError("timing evidence needs an external timing note")
    for field in ("runner_queue_seconds", "environment_setup_seconds"):
        _require_nonnegative_number(external.get(field), f"timing {field}", allow_none=True)
    environment = timing.get("environment")
    if not isinstance(environment, dict):
        raise InventoryError("timing evidence needs environment identity")
    for field in ("python", "platform"):
        if not isinstance(environment.get(field), str) or not environment[field].strip():
            raise InventoryError(f"timing environment needs a non-empty {field}")
    django_settings_module = environment.get("django_settings_module")
    if django_settings_module not in group.django_settings_modules:
        raise InventoryError("timing Django settings identity does not match the taxonomy group")
    packages = environment.get("packages")
    if not isinstance(packages, dict):
        raise InventoryError("timing environment needs package versions")
    for package in ENVIRONMENT_PACKAGES:
        if not isinstance(packages.get(package), str) or not packages[package].strip():
            raise InventoryError(f"timing environment needs package version {package}")
    _require_nonnegative_integer(environment.get("processor_count"), "timing processor_count")
    return sample_id


def build_inventory(
    root: Path,
    manifest_path: Path,
    manifest: Manifest,
    items: list[CollectedTest],
    timing_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build deterministic classification and overlap evidence."""
    invalid: list[tuple[str, list[str]]] = []
    for item in items:
        matches = [
            contract.id
            for contract in manifest.execution_contracts
            if contract.selection.matches(item)
        ]
        if len(matches) != 1:
            invalid.append((item.nodeid, matches))
    if invalid:
        examples = "; ".join(f"{nodeid} -> {matches}" for nodeid, matches in invalid[:5])
        raise InventoryError(
            f"execution contracts must partition every collected item exactly once: {examples}"
        )

    marker_counts = Counter(marker for item in items for marker in item.markers)
    fixture_counts = Counter(fixture for item in items for fixture in item.fixtures)
    families: dict[str, list[CollectedTest]] = defaultdict(list)
    for item in items:
        if item.parameterized:
            families[item.family].append(item)
    parameterized_families = [
        {
            "nodeid": family,
            "case_count": len(family_items),
            "parameter_keys": sorted({key for item in family_items for key in item.parameter_keys}),
        }
        for family, family_items in families.items()
    ]
    parameterized_families.sort(
        key=lambda record: (-cast(int, record["case_count"]), record["nodeid"])
    )

    group_records = [_group_record(group, items) for group in manifest.groups]
    ci_selected_case_slots = sum(
        cast(int, record["estimated_ci_selected_case_slots"])
        for record in group_records
        if record["kind"] == "ci_lane"
    )
    overlap_records: list[dict[str, object]] = []
    for candidate in manifest.overlap_candidates:
        selected = [
            item
            for item in items
            if any(_path_matches(item.path, path) for path in candidate.paths)
        ]
        overlap_records.append(
            {
                **asdict(candidate),
                "collected_count": len(selected),
                "file_count": len({item.path for item in selected}),
                "parameterized_cases": sum(item.parameterized for item in selected),
            }
        )

    timings = timing_records or []
    source = _source_digest(root, manifest_path)
    timing_identities: set[str] = set()
    for timing in timings:
        identity = _validate_timing_record(timing, source, manifest, items)
        if identity in timing_identities:
            raise InventoryError("timing evidence sample IDs must be unique")
        timing_identities.add(identity)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "source": source,
        "environment": _environment_record(),
        "totals": {
            "collected": len(items),
            "files": len({item.path for item in items}),
            "parameterized_cases": sum(item.parameterized for item in items),
            "parameterized_families": len(families),
            "estimated_blocking_ci_selected_case_slots": ci_selected_case_slots,
        },
        "groups": group_records,
        "markers": dict(sorted(marker_counts.items(), key=lambda pair: (-pair[1], pair[0]))),
        "fixtures": [
            {"name": name, "selected_count": count}
            for name, count in sorted(fixture_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "directories": _directory_records(items),
        "files": _file_records(items, manifest.execution_contracts, manifest.domains),
        "parameterized_families": parameterized_families,
        "overlap_candidates": overlap_records,
        "timings": timings,
    }


def _markdown_cell(value: object) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def render_markdown(report: dict[str, Any]) -> str:
    """Render a stable, reviewable summary from the machine report."""
    totals = cast(dict[str, object], report["totals"])
    source = cast(dict[str, object], report["source"])
    lines = [
        "# Test suite inventory",
        "",
        "This generated baseline classifies collected pytest cases by resource and isolation",
        "contract. Counts are cases after parametrization, not test functions.",
        "",
        f"Source digest: `{source['digest']}` ({source['file_count']} inputs).",
        "",
        "| Collected | Files | Parameterized cases | Families | Estimated blocking CI selected case slots |",
        "|---:|---:|---:|---:|---:|",
        (
            f"| {totals['collected']} | {totals['files']} | {totals['parameterized_cases']} | "
            f"{totals['parameterized_families']} | "
            f"{totals['estimated_blocking_ci_selected_case_slots']} |"
        ),
        "",
        "## Contracts and lanes",
        "",
        "| Kind | ID | Cases | Files | Variants | Skip policy | Django settings | Owner | Exact selection |",
        "|---|---|---:|---:|---:|---|---|---|---|",
    ]
    for raw_group in report["groups"]:
        group = cast(dict[str, Any], raw_group)
        selection = cast(dict[str, object], group["selection"])
        skip_policy = cast(dict[str, str], group["skip_policy"])
        lines.append(
            f"| {_markdown_cell(group['kind'])} | `{group['id']}` | {group['selected_count']} | "
            f"{group['file_count']} | {group['variants']} | `{skip_policy['mode']}` | "
            f"`{_markdown_cell(', '.join(group['django_settings_modules']))}` | "
            f"{_markdown_cell(group['owner'])} | "
            f"`{_markdown_cell(selection['expression'])}` |"
        )
    lines.extend(
        [
            "",
            "Execution contracts partition the collection. Boundaries and CI lanes overlap those",
            "contracts intentionally. The estimate multiplies selected cases by current CI variants;",
            "it measures selected pytest case slots before runtime skips, not completed execution",
            "or wall-clock time. Nested JavaScript subtests are outside this estimate.",
            "",
            "## Largest files",
            "",
            "| File | Cases | Parameterized | Logical domains | Execution contracts |",
            "|---|---:|---:|---|---|",
        ]
    )
    for raw_file in report["files"][:20]:
        file_record = cast(dict[str, object], raw_file)
        contracts = cast(dict[str, int], file_record["execution_contracts"])
        contract_text = ", ".join(f"{key}: {value}" for key, value in contracts.items())
        domains = cast(dict[str, int], file_record["domains"])
        domain_text = ", ".join(domains) or "unclassified"
        lines.append(
            f"| `{file_record['path']}` | {file_record['collected_count']} | "
            f"{file_record['parameterized_cases']} | {_markdown_cell(domain_text)} | "
            f"{_markdown_cell(contract_text)} |"
        )
    lines.extend(
        [
            "",
            "## Largest parameterized families",
            "",
            "| Family | Cases | Parameters |",
            "|---|---:|---|",
        ]
    )
    for raw_family in report["parameterized_families"][:20]:
        family = cast(dict[str, object], raw_family)
        parameters = ", ".join(cast(list[str], family["parameter_keys"]))
        lines.append(
            f"| `{family['nodeid']}` | {family['case_count']} | `{_markdown_cell(parameters)}` |"
        )
    lines.extend(
        [
            "",
            "## Most-used fixtures",
            "",
            "Fixture counts describe setup paths selected by collected cases; they are not timing",
            "attribution. Inherited and autouse fixtures therefore appear on many or all cases.",
            "",
            "| Fixture | Selected cases |",
            "|---|---:|",
        ]
    )
    for raw_fixture in report["fixtures"][:20]:
        fixture = cast(dict[str, object], raw_fixture)
        lines.append(f"| `{fixture['name']}` | {fixture['selected_count']} |")
    lines.extend(
        [
            "",
            "## Overlap review inventory",
            "",
            "These are review candidates, not pre-approved deletions.",
            "",
            "| Domain | Cases | Parameterized | Owner | Why inspect | Review boundary |",
            "|---|---:|---:|---|---|---|",
        ]
    )
    for raw_candidate in report["overlap_candidates"]:
        candidate = cast(dict[str, object], raw_candidate)
        lines.append(
            f"| `{candidate['id']}` | {candidate['collected_count']} | "
            f"{candidate['parameterized_cases']} | {_markdown_cell(candidate['owner'])} | "
            f"{_markdown_cell(candidate['reason'])} | {_markdown_cell(candidate['review'])} |"
        )
    lines.extend(
        [
            "",
            "## Runtime measurements",
            "",
        ]
    )
    timings = cast(list[dict[str, Any]], report.get("timings", []))
    if not timings:
        lines.append(
            "No runtime record was merged. Use the manifest-backed `run` command with a timing output."
        )
    else:
        lines.extend(
            [
                "| Lane | Observation | Variant | Measured UTC | Python | Django / Ray / pytest | Outcomes | Platform | Queue | Environment | Collection | Test execution wall | Setup sum | Call sum | Teardown sum | Post-test/coverage | Terminal rendering |",
                "|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        domain_by_path = {
            cast(str, file_record["path"]): ", ".join(cast(dict[str, int], file_record["domains"]))
            or "unclassified"
            for file_record in cast(list[dict[str, object]], report["files"])
        }
        for timing in timings:
            external = cast(dict[str, object], timing["external"])
            pytest_record = cast(dict[str, object], timing["pytest"])
            environment = cast(dict[str, object], timing["environment"])
            packages = cast(dict[str, str], environment["packages"])
            outcomes = cast(dict[str, int], pytest_record["outcomes"])
            outcome_text = ", ".join(f"{name}={count}" for name, count in outcomes.items() if count)
            package_text = (
                f"Django {packages['django']}; Ray {packages['ray']}; "
                f"pytest {packages['pytest']}; xdist {packages['pytest-xdist']}"
            )
            lines.append(
                f"| `{timing['lane']}` | `{timing['observation']}` | "
                f"`{timing['variant']}` | `{timing['measured_at_utc']}` | "
                f"`{environment['python']}` | "
                f"{_markdown_cell(package_text)} | {_markdown_cell(outcome_text)} | "
                f"{_markdown_cell(environment['platform'])} | "
                f"{_seconds(external.get('runner_queue_seconds'))} | "
                f"{_seconds(external.get('environment_setup_seconds'))} | "
                f"{_seconds(pytest_record.get('collection_seconds'))} | "
                f"{_seconds(pytest_record.get('execution_wall_seconds'))} | "
                f"{_seconds(pytest_record.get('setup_phase_seconds'))} | "
                f"{_seconds(pytest_record.get('call_phase_seconds'))} | "
                f"{_seconds(pytest_record.get('teardown_phase_seconds'))} | "
                f"{_seconds(pytest_record.get('post_test_reporting_seconds'))} | "
                f"{_seconds(pytest_record.get('terminal_reporting_seconds'))} |"
            )
        for timing in timings:
            external = cast(dict[str, object], timing["external"])
            lines.extend(
                [
                    "",
                    f"### `{timing['lane']}` / `{timing['observation']}` / "
                    f"`{timing['variant']}` slow paths at `{timing['measured_at_utc']}`",
                    "",
                    f"External timing note: {_markdown_cell(external.get('note') or 'none recorded')}.",
                    "",
                    "| Slow file | Domain | Phase total |",
                    "|---|---|---:|",
                ]
            )
            for raw_file in cast(list[dict[str, object]], timing.get("slowest_files", []))[:15]:
                path = cast(str, raw_file["path"])
                lines.append(
                    f"| `{path}` | {_markdown_cell(domain_by_path.get(path, 'unclassified'))} | "
                    f"{_seconds(raw_file['total_seconds'])} |"
                )
            lines.extend(["", "| Slow test | Domain | Phase total |", "|---|---|---:|"])
            for raw_test in cast(list[dict[str, object]], timing.get("slowest_tests", []))[:15]:
                nodeid = cast(str, raw_test["nodeid"])
                path = nodeid.split("::", maxsplit=1)[0]
                lines.append(
                    f"| `{nodeid}` | {_markdown_cell(domain_by_path.get(path, 'unclassified'))} | "
                    f"{_seconds(raw_test['total_seconds'])} |"
                )
    return "\n".join(lines) + "\n"


def _seconds(value: object) -> str:
    return "not measured" if value is None else f"{float(cast(float, value)):.3f}s"


class _RuntimePlugin:
    def __init__(self, root: Path, group: Group) -> None:
        self.root = root.resolve()
        self.group = group
        self.started = time.perf_counter()
        self.collection_started: float | None = None
        self.collection_finished: float | None = None
        self.test_execution_finished: float | None = None
        self.session_finished: float | None = None
        self.terminal_started: float | None = None
        self.terminal_finished: float | None = None
        self.selected_count = 0
        self.deselected_count = 0
        self.phase_seconds: Counter[str] = Counter()
        self.test_phases: dict[str, Counter[str]] = defaultdict(Counter)
        self.outcomes: dict[str, str] = {}
        self.logfinished: set[str] = set()
        self.coverage_enabled = False

    def pytest_configure(self, config: pytest.Config) -> None:
        configured_paths = [Path(str(argument)).resolve() for argument in config.args]
        canonical_tests_path = (self.root / "tests").resolve()
        if configured_paths != [canonical_tests_path]:
            raise pytest.UsageError(
                "taxonomy timing requires exactly the canonical tests root; "
                "additional positional selectors are not supported"
            )
        disabled_modes = [
            option
            for option in ("collectonly", "setuponly", "setupplan")
            if bool(getattr(config.option, option, False))
        ]
        if disabled_modes:
            rendered = ", ".join(disabled_modes)
            raise pytest.UsageError(
                f"timing evidence requires test execution; disabled modes: {rendered}"
            )
        self.coverage_enabled = bool(getattr(config.option, "cov_source", None)) and not bool(
            getattr(config.option, "no_cov", False)
        )

    def pytest_collection(self) -> None:
        self.collection_started = time.perf_counter()

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        selected: list[pytest.Item] = []
        deselected: list[pytest.Item] = []
        for item in items:
            collected = CollectedTest.from_pytest_item(item, self.root)
            (selected if self.group.selection.matches(collected) else deselected).append(item)
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = selected
        self.selected_count = len(selected)
        self.deselected_count = len(deselected)
        self.collection_finished = time.perf_counter()

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        nodeid = report.nodeid.replace("\\", "/")
        self.phase_seconds[report.when] += report.duration
        self.test_phases[nodeid][report.when] += report.duration
        if report.when == "call":
            if hasattr(report, "wasxfail"):
                self.outcomes[nodeid] = "xfailed" if report.skipped else "xpassed"
            else:
                self.outcomes[nodeid] = report.outcome
        elif report.when == "setup" and report.outcome != "passed":
            self.outcomes[nodeid] = "xfailed" if hasattr(report, "wasxfail") else report.outcome
        elif report.when == "teardown" and report.outcome != "passed":
            if report.failed:
                self.outcomes[nodeid] = "failed"
            else:
                self.outcomes[nodeid] = "xfailed" if hasattr(report, "wasxfail") else report.outcome

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        self.logfinished.add(nodeid.replace("\\", "/"))
        self.test_execution_finished = time.perf_counter()

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self) -> None:
        self.session_finished = time.perf_counter()

    @pytest.hookimpl(hookwrapper=True, tryfirst=True, optionalhook=True)
    def pytest_terminal_summary(self) -> Any:
        self.terminal_started = time.perf_counter()
        try:
            yield
        finally:
            self.terminal_finished = time.perf_counter()


def _elapsed(start: float | None, end: float | None) -> float | None:
    return None if start is None or end is None else round(max(0.0, end - start), 6)


def run_lane(
    root: Path,
    manifest_path: Path,
    manifest: Manifest,
    lane_id: str,
    pytest_arguments: list[str],
    *,
    observation: str,
    variant: str,
    runner_queue_seconds: float | None,
    environment_setup_seconds: float | None,
    external_note: str,
) -> tuple[int, dict[str, Any]]:
    """Run one named selection and retain phase-level timing evidence."""
    group = manifest.group(lane_id)
    _reject_pytest_environment(group.django_settings_modules)
    _validate_pytest_passthrough(pytest_arguments)
    source_before = _source_digest(root, manifest_path)
    plugin = _RuntimePlugin(root, group)
    arguments = [str(root / "tests"), *pytest_arguments]
    exit_code = pytest.main(arguments, plugins=[plugin])
    finished = time.perf_counter()
    source_after = _source_digest(root, manifest_path)
    per_test = [
        {
            "nodeid": nodeid,
            "total_seconds": round(sum(phases.values()), 6),
            "phases": {key: round(value, 6) for key, value in sorted(phases.items())},
        }
        for nodeid, phases in plugin.test_phases.items()
    ]
    per_test.sort(key=lambda record: (-cast(float, record["total_seconds"]), record["nodeid"]))
    file_seconds: dict[str, Counter[str]] = defaultdict(Counter)
    for nodeid, phases in plugin.test_phases.items():
        path = nodeid.split("::", maxsplit=1)[0]
        for phase, duration in phases.items():
            file_seconds[path][phase] += duration
    per_file = [
        {
            "path": path,
            "total_seconds": round(sum(phases.values()), 6),
            "phases": {key: round(value, 6) for key, value in sorted(phases.items())},
        }
        for path, phases in file_seconds.items()
    ]
    per_file.sort(key=lambda record: (-cast(float, record["total_seconds"]), record["path"]))
    outcome_counts = Counter(plugin.outcomes.values())
    rendered_outcomes = {name: outcome_counts[name] for name in OUTCOME_NAMES}
    pytest_timing = {
        "exit_code": int(exit_code),
        "selected_count": plugin.selected_count,
        "deselected_count": plugin.deselected_count,
        "completed_count": len(plugin.outcomes),
        "logfinished_count": len(plugin.logfinished),
        "outcomes": rendered_outcomes,
        "coverage_enabled": plugin.coverage_enabled,
        "process_seconds": _elapsed(plugin.started, finished),
        "initialization_seconds": _elapsed(plugin.started, plugin.collection_started),
        "collection_seconds": _elapsed(plugin.collection_started, plugin.collection_finished),
        "execution_wall_seconds": _elapsed(
            plugin.collection_finished,
            plugin.test_execution_finished or plugin.session_finished,
        ),
        "setup_phase_seconds": round(plugin.phase_seconds["setup"], 6),
        "call_phase_seconds": round(plugin.phase_seconds["call"], 6),
        "teardown_phase_seconds": round(plugin.phase_seconds["teardown"], 6),
        "post_test_reporting_seconds": _elapsed(
            plugin.test_execution_finished, plugin.session_finished
        ),
        "terminal_reporting_seconds": _elapsed(plugin.terminal_started, plugin.terminal_finished),
        "cleanup_seconds": _elapsed(plugin.terminal_finished, finished),
    }
    integrity_errors: list[str] = []
    if source_before["digest"] != source_after["digest"]:
        integrity_errors.append("Git-visible source changed while pytest was running")
    if int(exit_code) == 0:
        if plugin.selected_count < 1:
            integrity_errors.append("the selected taxonomy group contained no tests")
        if len(plugin.logfinished) != plugin.selected_count:
            integrity_errors.append("not every selected test reached pytest_runtest_logfinish")
        if len(plugin.outcomes) != plugin.selected_count:
            integrity_errors.append("not every selected test produced a final outcome")
        if group.skip_policy.mode == "forbid" and (
            rendered_outcomes["skipped"] or rendered_outcomes["xfailed"]
        ):
            integrity_errors.append("the taxonomy group's skip policy forbids skipped work")
        missing_phases = [
            field for field in PROMISED_RUNTIME_INTERVAL_FIELDS if pytest_timing[field] is None
        ]
        if missing_phases:
            integrity_errors.append(
                "promised timing phases were not observed: " + ", ".join(missing_phases)
            )
    timing = {
        "schema_version": TIMING_SCHEMA_VERSION,
        "sample_id": str(uuid.uuid4()),
        "measured_at_utc": datetime.now(UTC).isoformat(),
        "source": source_before,
        "source_after_digest": source_after["digest"],
        "lane": lane_id,
        "observation": observation,
        "variant": variant,
        "selection": group.selection.expression(),
        "skip_policy": asdict(group.skip_policy),
        "pytest_arguments": pytest_arguments,
        "environment": _environment_record(include_processor_count=True),
        "external": {
            "runner_queue_seconds": runner_queue_seconds,
            "environment_setup_seconds": environment_setup_seconds,
            "note": external_note,
        },
        "pytest": pytest_timing,
        "integrity": {
            "valid": int(exit_code) == 0 and not integrity_errors,
            "errors": integrity_errors,
        },
        "skipped_tests": [
            {"nodeid": nodeid, "outcome": outcome}
            for nodeid, outcome in sorted(plugin.outcomes.items())
            if outcome in SKIPPED_OUTCOMES
        ],
        "slowest_tests": per_test[:50],
        "slowest_files": per_file[:30],
    }
    effective_exit_code = int(exit_code) or (2 if integrity_errors else 0)
    return effective_exit_code, timing


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"invalid JSON number: {constant}")


def _load_timing(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, ValueError) as error:
        raise InventoryError(f"cannot load timing evidence from {path}") from error
    if not isinstance(value, dict) or value.get("schema_version") != TIMING_SCHEMA_VERSION:
        raise InventoryError(f"unsupported timing evidence in {path}")
    return cast(dict[str, Any], value)


def _render_json(path: Path, value: object) -> str:
    try:
        return json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError) as error:
        raise InventoryError(f"cannot serialize portable JSON for {path}") from error


def _write_json(path: Path, value: object) -> None:
    _write_text(path, _render_json(path, value))


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, pending_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".pending", dir=path.parent
    )
    os.close(descriptor)
    pending = Path(pending_name)
    try:
        pending.write_text(value, encoding="utf-8", newline="\n")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="write collection and classification evidence")
    collect.add_argument("--json-output", type=Path, required=True)
    collect.add_argument("--markdown-output", type=Path, required=True)
    collect.add_argument("--timing", action="append", type=Path, default=[])

    select = subparsers.add_parser("select", help="show one reusable manifest selection")
    select.add_argument("--lane", required=True)
    select.add_argument("--format", choices=("expression", "json", "shell"), default="expression")

    run = subparsers.add_parser("run", help="run and time one manifest-backed selection")
    run.add_argument("--lane", required=True)
    run.add_argument("--observation", required=True)
    run.add_argument("--variant", required=True)
    run.add_argument("--timing-output", type=Path, required=True)
    run.add_argument("--runner-queue-seconds", type=float)
    run.add_argument("--environment-setup-seconds", type=float)
    run.add_argument("--external-note", required=True)
    run.add_argument("pytest_arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = Path.cwd().resolve()
    manifest_path = (root / arguments.manifest).resolve()
    try:
        manifest = load_manifest(manifest_path)
        if arguments.command == "select":
            group = manifest.group(arguments.lane)
            if arguments.format == "expression":
                print(group.selection.expression())
            elif arguments.format == "json":
                try:
                    pytest_arguments = group.selection.pytest_arguments()
                except InventoryError:
                    pytest_arguments = None
                print(
                    json.dumps(
                        {
                            "lane": group.id,
                            "owner": group.owner,
                            "expression": group.selection.expression(),
                            "selection": group.selection.as_mapping(),
                            "skip_policy": asdict(group.skip_policy),
                            "django_settings_modules": list(group.django_settings_modules),
                            "pytest_arguments": pytest_arguments,
                            "manifest_runner": [
                                "python",
                                "scripts/test_suite_inventory.py",
                                "run",
                                "--lane",
                                group.id,
                            ],
                        },
                        sort_keys=True,
                    )
                )
            else:
                print("pytest " + shlex.join(group.selection.pytest_arguments()))
            return 0
        if arguments.command == "run":
            _validate_output_path(root, arguments.timing_output, "timing output")
            pytest_arguments = list(arguments.pytest_arguments)
            if pytest_arguments[:1] == ["--"]:
                pytest_arguments = pytest_arguments[1:]
            for label, value in (
                ("runner queue", arguments.runner_queue_seconds),
                ("environment setup", arguments.environment_setup_seconds),
            ):
                if value is not None and (not math.isfinite(value) or value < 0):
                    raise InventoryError(f"{label} seconds must be finite and nonnegative")
            observation = " ".join(arguments.observation.split())
            variant = " ".join(arguments.variant.split())
            external_note = " ".join(arguments.external_note.split())
            if not observation or not variant or not external_note:
                raise InventoryError("observation, variant, and external note must be non-empty")
            exit_code, timing = run_lane(
                root,
                manifest_path,
                manifest,
                arguments.lane,
                pytest_arguments,
                observation=observation,
                variant=variant,
                runner_queue_seconds=arguments.runner_queue_seconds,
                environment_setup_seconds=arguments.environment_setup_seconds,
                external_note=external_note,
            )
            _write_json(arguments.timing_output, timing)
            print(f"Wrote {arguments.timing_output} for taxonomy lane {arguments.lane}.")
            return exit_code

        _validate_collect_path_aliases(
            root,
            arguments.json_output,
            arguments.markdown_output,
            arguments.timing,
        )
        _validate_output_path(
            root,
            arguments.json_output,
            "JSON output",
            allow_generated_baseline=True,
            generated_baseline_suffix=".json",
        )
        _validate_output_path(
            root,
            arguments.markdown_output,
            "Markdown output",
            allow_generated_baseline=True,
            generated_baseline_suffix=".md",
        )
        timings = [_load_timing(path) for path in arguments.timing]
        report = build_inventory(
            root,
            manifest_path,
            manifest,
            collect_tests(root),
            timings,
        )
        rendered_json = _render_json(arguments.json_output, report)
        rendered_markdown = render_markdown(report)
        _write_text(arguments.json_output, rendered_json)
        _write_text(arguments.markdown_output, rendered_markdown)
        print(
            f"Wrote {arguments.json_output} and {arguments.markdown_output} for "
            f"{report['totals']['collected']} collected tests."
        )
        return 0
    except InventoryError as error:
        print(f"test suite inventory: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
