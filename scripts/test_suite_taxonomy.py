"""Pure manifest and selection contracts for the pytest suite taxonomy."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

MANIFEST_SCHEMA_VERSION = 4


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
class CollectedTest:
    """Stable pytest collection metadata used for classification."""

    nodeid: str
    path: str
    markers: tuple[str, ...]
    fixtures: tuple[str, ...]
    parameter_keys: tuple[str, ...] = ()

    @classmethod
    def from_pytest_item(cls, item: Any, root: Path) -> CollectedTest:
        """Build one shared collection record without importing runner state."""
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

    def contract_mapping(self) -> dict[str, object]:
        """Return the collection identity used by source-fenced timing evidence."""
        return {
            "nodeid": self.nodeid,
            "path": self.path,
            "markers": list(self.markers),
            "fixtures": list(self.fixtures),
        }


@dataclass(frozen=True)
class Selection:
    """Declarative path, marker, and fixture selection."""

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
            any(path_matches(item.path, path) for path in self.paths)
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


def path_matches(item_path: str, selected_path: str) -> bool:
    """Return whether a collected path is inside a selected path."""
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
    if "execution" in value:
        raise InventoryError(
            f"{label} cannot declare an execution policy; manifest-backed runs are serial"
        )
    normalized_group_id = group_id.strip()
    selection = Selection.from_mapping(value.get("selection"), f"{label} selection")
    return Group(
        id=normalized_group_id,
        kind=kind,
        owner=" ".join(owner.split()),
        contract=" ".join(contract.split()),
        selection=selection,
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
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != MANIFEST_SCHEMA_VERSION
    ):
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


def collection_contract_digest(items: tuple[CollectedTest, ...] | list[CollectedTest]) -> str:
    """Hash exact node, marker, and fixture ownership for parity evidence."""
    payload = [item.contract_mapping() for item in sorted(items, key=lambda item: item.nodeid)]
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def nodeid_digest(nodeids: tuple[str, ...] | list[str]) -> str:
    """Hash a sorted exact selected node-ID set."""
    encoded = json.dumps(sorted(nodeids), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
