"""Validate that a release ref matches every package version source."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

_VERSION_RE = re.compile(r"^v?(?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")
_CHANGELOG_RELEASE_HEADING_RE = re.compile(
    r"^## \[(?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\] - "
    r"(?P<date>\d{4}-\d{2}-\d{2})\s*$",
    re.MULTILINE,
)
_REVIEW_FILE_RE = re.compile(
    r"^compiled-graph-capability-review-(?P<date>\d{4}-\d{2}-\d{2})\.json$"
)
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BARE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROBE_STATUSES = frozenset(
    {"success", "unsupported_guard", "python_failure", "timeout", "signal", "native_crash"}
)
_CAPABILITY_FIELDS = frozenset(
    {
        "ray_version",
        "python_version",
        "operating_system",
        "architecture",
        "python_implementation",
        "python_abi",
        "dependency_profile",
        "platform_profile",
        "libc_profile",
        "container_profile",
        "deployment_profile",
        "shared_memory_profile",
        "object_store_profile",
        "topology",
        "submission_transport",
        "transport",
    }
)
_EVIDENCE_FILES = frozenset({"environment.json", "packages.txt", "probe.json"})
_READINESS_PATH = Path("tests/contracts/one_zero_readiness_v1.json")
_READINESS_DOC_PATH = Path("docs/one-zero-readiness.md")
_READINESS_MARKER_RE = re.compile(
    r"<!--\s+readiness-criterion:\s+(?P<identifier>[a-z][a-z0-9.-]+)\s+-->"
)
_READINESS_CRITERION_ID_RE = re.compile(r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*$")
_READINESS_CATEGORIES = frozenset({"adoption", "operations", "product", "release", "support"})
_READINESS_OWNERS = frozenset({"external", "maintainer", "repository"})
_READINESS_STATUSES = frozenset({"blocked", "deferred", "pending", "satisfied"})
_READINESS_INTRODUCED_VERSION = "0.4.0"
_BETA_CLASSIFIER = "Development Status :: 4 - Beta"
_PRODUCTION_CLASSIFIER = "Development Status :: 5 - Production/Stable"
_DEVELOPMENT_CLASSIFIER_PREFIX = "Development Status :: "
_READINESS_CRITERIA_V1_OWNERS = {
    "adoption.documentation-review": "external",
    "adoption.external-applications": "external",
    "adoption.kuberay-deployment": "external",
    "adoption.observation-window": "maintainer",
    "adoption.preserved-state-upgrade": "external",
    "operations.operator-controls": "repository",
    "operations.preserved-data-upgrade": "repository",
    "operations.protocol-fencing": "repository",
    "operations.soak-certification": "repository",
    "product.compatibility-matrix": "repository",
    "product.experimental-boundary": "repository",
    "product.no-severe-defects": "maintainer",
    "product.stable-api": "repository",
    "release.final-evidence": "repository",
    "support.contributor-triage": "repository",
    "support.maintainer-capacity": "maintainer",
    "support.release-and-rollback": "repository",
    "support.security-fix-policy": "maintainer",
    "support.vulnerability-reporting": "repository",
}


def _semantic_version_key(version: str) -> tuple[Any, ...]:
    """Return a SemVer ordering key without depending on packaging tooling."""
    public_version = version.split("+", 1)[0]
    core, separator, prerelease = public_version.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    prerelease_key: tuple[tuple[int, int | str], ...] = ()
    if separator:
        prerelease_key = tuple(
            (0, int(identifier)) if identifier.isdigit() else (1, identifier)
            for identifier in prerelease.split(".")
        )
    return major, minor, patch, int(not separator), prerelease_key


def _readiness_registry_required(version: str) -> bool:
    return (
        _semantic_version_key(version)[:3]
        >= _semantic_version_key(_READINESS_INTRODUCED_VERSION)[:3]
    )


def _read_pyproject_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _read_module_version(root: Path) -> str:
    source = (root / "src" / "django_ray" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("src/django_ray/__init__.py does not define __version__")


def _read_lock_version(root: Path) -> str:
    with (root / "uv.lock").open("rb") as handle:
        packages = tomllib.load(handle).get("package")
    if not isinstance(packages, list):
        raise ValueError("uv.lock does not contain a package list")
    matches = [
        package
        for package in packages
        if isinstance(package, dict)
        and package.get("name") == "django-ray"
        and isinstance(package.get("source"), dict)
        and package["source"].get("editable") == "."
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("version"), str):
        raise ValueError("uv.lock must contain one editable django-ray package version")
    return str(matches[0]["version"])


def _read_project_classifiers(root: Path) -> set[str]:
    with (root / "pyproject.toml").open("rb") as handle:
        classifiers = tomllib.load(handle)["project"].get("classifiers")
    if not isinstance(classifiers, list) or not all(
        isinstance(classifier, str) for classifier in classifiers
    ):
        raise ValueError("pyproject.toml project classifiers must be a list of strings")
    return {classifier for classifier in classifiers if isinstance(classifier, str)}


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError(f"duplicate JSON object key {key!r}")
        record[key] = value
    return record


def _validate_readiness_evidence(root: Path, criterion_id: str, value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"kind", "value"}:
        raise ValueError(f"readiness criterion {criterion_id} evidence must contain kind and value")
    kind = value["kind"]
    reference = value["value"]
    if kind not in {"issue", "path"} or not isinstance(reference, str) or not reference:
        raise ValueError(
            f"readiness criterion {criterion_id} evidence must be a non-empty issue or path"
        )
    if kind == "issue":
        if (
            re.fullmatch(
                r"https://github\.com/dariuszpanas/django-ray/issues/[1-9][0-9]*",
                reference,
            )
            is None
        ):
            raise ValueError(f"readiness criterion {criterion_id} has an invalid issue reference")
        return

    path = Path(reference)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"readiness criterion {criterion_id} references missing repository path {reference}"
        )
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = (root / path).resolve(strict=True)
    except OSError as exc:
        raise ValueError(
            f"readiness criterion {criterion_id} references missing repository path {reference}"
        ) from exc
    if not resolved_path.is_relative_to(resolved_root) or not resolved_path.is_file():
        raise ValueError(
            f"readiness criterion {criterion_id} path must remain inside the repository"
        )
    if resolved_root / path != resolved_path:
        raise ValueError(f"readiness criterion {criterion_id} path must not use symlinks")
    candidate_entry = subprocess.run(
        [
            "git",
            "--literal-pathspecs",
            "-C",
            str(root),
            "ls-tree",
            "--full-tree",
            "-z",
            "HEAD",
            "--",
            path.as_posix(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    entries = [entry for entry in candidate_entry.stdout.split("\0") if entry]
    if candidate_entry.returncode != 0 or len(entries) != 1:
        raise ValueError(
            f"readiness criterion {criterion_id} references untracked repository path {reference}; "
            "evidence must be committed in the candidate tree"
        )
    metadata, separator, candidate_path = entries[0].partition("\t")
    metadata_parts = metadata.split()
    if (
        separator != "\t"
        or candidate_path != path.as_posix()
        or len(metadata_parts) != 3
        or metadata_parts[0] not in {"100644", "100755"}
        or metadata_parts[1] != "blob"
    ):
        raise ValueError(
            f"readiness criterion {criterion_id} path must be a regular file in the candidate tree"
        )
    candidate_match = subprocess.run(
        [
            "git",
            "--literal-pathspecs",
            "-C",
            str(root),
            "diff",
            "--no-ext-diff",
            "--quiet",
            "HEAD",
            "--",
            path.as_posix(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if candidate_match.returncode == 1:
        raise ValueError(
            f"readiness criterion {criterion_id} path does not match the candidate tree"
        )
    if candidate_match.returncode != 0:
        raise ValueError(
            f"readiness criterion {criterion_id} candidate-tree evidence could not be verified"
        )


def validate_one_zero_readiness(
    root: Path,
    *,
    require_accepted: bool = False,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Validate the checked 1.0 graduation registry and classifier latch."""
    evaluation_date = as_of or date.today()
    path = root / _READINESS_PATH
    try:
        record = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"1.0 readiness registry is unavailable or invalid: {exc}") from exc
    if not isinstance(record, dict) or set(record) != {
        "contract",
        "criteria",
        "decision",
        "schema_version",
        "target_version",
    }:
        raise ValueError("1.0 readiness registry has an unsupported top-level shape")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("1.0 readiness registry schema_version must be 1")
    if record["contract"] != "django-ray-1.0-readiness-v1":
        raise ValueError("1.0 readiness registry contract identity is invalid")
    if record["target_version"] != "1.0.0":
        raise ValueError("1.0 readiness registry target_version must be 1.0.0")

    decision = record["decision"]
    if not isinstance(decision, dict) or set(decision) != {
        "accepted_by",
        "accepted_on",
        "status",
    }:
        raise ValueError("1.0 readiness decision has an unsupported shape")
    decision_status = decision["status"]
    if not isinstance(decision_status, str) or decision_status not in {"accepted", "tracking"}:
        raise ValueError("1.0 readiness decision status must be accepted or tracking")
    if decision_status == "tracking":
        if decision["accepted_on"] is not None or decision["accepted_by"] is not None:
            raise ValueError("a tracking 1.0 readiness decision cannot carry acceptance metadata")
    else:
        accepted_on_value = decision["accepted_on"]
        accepted_on = _parse_date(accepted_on_value, field="1.0 readiness accepted_on")
        if accepted_on.isoformat() != accepted_on_value:
            raise ValueError("1.0 readiness accepted_on must use YYYY-MM-DD")
        if accepted_on > evaluation_date:
            raise ValueError("1.0 readiness accepted_on cannot be in the future")
        if not isinstance(decision["accepted_by"], str) or not decision["accepted_by"].strip():
            raise ValueError("an accepted 1.0 readiness decision needs accepted_by")

    criteria = record["criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("1.0 readiness registry must contain criteria")
    expected_fields = {
        "category",
        "disposition",
        "evidence",
        "id",
        "owner",
        "required",
        "status",
    }
    identifiers: list[str] = []
    for criterion in criteria:
        if not isinstance(criterion, dict) or set(criterion) != expected_fields:
            raise ValueError("1.0 readiness criterion has an unsupported shape")
        identifier = criterion["id"]
        if (
            not isinstance(identifier, str)
            or _READINESS_CRITERION_ID_RE.fullmatch(identifier) is None
        ):
            raise ValueError("1.0 readiness criterion id is invalid")
        identifiers.append(identifier)
        expected_owner = _READINESS_CRITERIA_V1_OWNERS.get(identifier)
        if expected_owner is None:
            raise ValueError(f"readiness criterion {identifier} is not part of the v1 contract")
        if (
            not isinstance(criterion["category"], str)
            or criterion["category"] not in _READINESS_CATEGORIES
        ):
            raise ValueError(f"readiness criterion {identifier} has an invalid category")
        if identifier.partition(".")[0] != criterion["category"]:
            raise ValueError(f"readiness criterion {identifier} category must match its id")
        if not isinstance(criterion["owner"], str) or criterion["owner"] not in _READINESS_OWNERS:
            raise ValueError(f"readiness criterion {identifier} has an invalid owner")
        if criterion["owner"] != expected_owner:
            raise ValueError(f"readiness criterion {identifier} owner must match the v1 contract")
        if type(criterion["required"]) is not bool:
            raise ValueError(f"readiness criterion {identifier} required must be boolean")
        if criterion["required"] is not True:
            raise ValueError(f"readiness criterion {identifier} must remain required in v1")
        if (
            not isinstance(criterion["status"], str)
            or criterion["status"] not in _READINESS_STATUSES
        ):
            raise ValueError(f"readiness criterion {identifier} has an invalid status")
        disposition = criterion["disposition"]
        if not isinstance(disposition, str) or len(disposition.split()) < 8:
            raise ValueError(
                f"readiness criterion {identifier} needs a specific maintainer disposition"
            )
        evidence = criterion["evidence"]
        if not isinstance(evidence, list):
            raise ValueError(f"readiness criterion {identifier} evidence must be a list")
        for evidence_item in evidence:
            _validate_readiness_evidence(root, identifier, evidence_item)
        if criterion["status"] == "satisfied" and not evidence:
            raise ValueError(f"satisfied readiness criterion {identifier} needs evidence")
        if (
            criterion["status"] == "satisfied"
            and criterion["owner"] == "external"
            and not any(item.get("kind") == "path" for item in evidence)
        ):
            raise ValueError(
                f"satisfied external criterion {identifier} needs repository-path evidence"
            )
        if (
            criterion["status"] == "satisfied"
            and criterion["owner"] == "repository"
            and not any(item.get("kind") == "path" for item in evidence)
        ):
            raise ValueError(
                f"satisfied repository criterion {identifier} needs repository-path evidence"
            )

    expected_identifiers = sorted(_READINESS_CRITERIA_V1_OWNERS)
    if identifiers != expected_identifiers or len(identifiers) != len(set(identifiers)):
        missing = sorted(set(expected_identifiers) - set(identifiers))
        unexpected = sorted(set(identifiers) - set(expected_identifiers))
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        if not details:
            details.append("order or duplicate identifiers")
        raise ValueError(
            "1.0 readiness criterion ids must exactly match the v1 contract: " + "; ".join(details)
        )

    try:
        readiness_document = (root / _READINESS_DOC_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"canonical 1.0 readiness document is unavailable: {exc}") from exc
    markers = _READINESS_MARKER_RE.findall(readiness_document)
    if sorted(markers) != identifiers or len(markers) != len(set(markers)):
        raise ValueError(
            "canonical 1.0 readiness document markers must exactly match the registry criteria"
        )

    classifiers = _read_project_classifiers(root)
    development_classifiers = {
        classifier
        for classifier in classifiers
        if classifier.startswith(_DEVELOPMENT_CLASSIFIER_PREFIX)
    }
    if _BETA_CLASSIFIER in classifiers and _PRODUCTION_CLASSIFIER in classifiers:
        raise ValueError("django-ray cannot be classified as both Beta and Production")
    if decision_status != "accepted" and _BETA_CLASSIFIER not in classifiers:
        raise ValueError("the Beta classifier cannot be removed before 1.0 readiness is accepted")
    if decision_status != "accepted" and development_classifiers != {_BETA_CLASSIFIER}:
        raise ValueError("tracking readiness requires only the Beta development classifier")

    requested_major = int(_read_pyproject_version(root).split(".", 1)[0])
    acceptance_required = require_accepted or requested_major >= 1
    if decision_status == "accepted":
        incomplete = [
            criterion["id"]
            for criterion in criteria
            if criterion["required"] and criterion["status"] != "satisfied"
        ]
        if incomplete:
            raise ValueError(
                "accepted 1.0 readiness has incomplete required criteria: " + ", ".join(incomplete)
            )
    elif acceptance_required:
        raise ValueError("django-ray 1.x requires an accepted 1.0 readiness decision")

    if acceptance_required:
        if _BETA_CLASSIFIER in classifiers:
            raise ValueError("django-ray 1.x cannot retain the Beta classifier")
        if _PRODUCTION_CLASSIFIER not in classifiers:
            raise ValueError("django-ray 1.x requires the Production classifier")
        if development_classifiers != {_PRODUCTION_CLASSIFIER}:
            raise ValueError(
                "django-ray 1.x requires only the Production/Stable development classifier"
            )
    return record


def _validate_changelog_release(root: Path, version: str) -> None:
    changelog = (root / "docs" / "changelog.md").read_text(encoding="utf-8")
    unreleased_heading = re.search(r"^## \[Unreleased\]\s*$", changelog, re.MULTILINE)
    if unreleased_heading is None:
        raise ValueError("docs/changelog.md must contain an Unreleased heading")

    heading_pattern = re.compile(
        rf"^## \[{re.escape(version)}\] - (?P<date>\d{{4}}-\d{{2}}-\d{{2}})\s*$",
        re.MULTILINE,
    )
    release_headings = list(heading_pattern.finditer(changelog))
    if len(release_headings) != 1:
        raise ValueError(f"docs/changelog.md must contain one dated [{version}] release heading")
    release_heading = release_headings[0]
    if release_heading.start() <= unreleased_heading.end():
        raise ValueError("the dated release must follow the Unreleased heading")
    if changelog[unreleased_heading.end() : release_heading.start()].strip():
        raise ValueError("the Unreleased changelog section must be empty for a release")
    date.fromisoformat(release_heading.group("date"))

    link_matches = re.findall(r"^\[([^\]]+)\]:\s+(\S+)\s*$", changelog, re.MULTILINE)
    release_links = [url for label, url in link_matches if label == version]
    unreleased_links = [url for label, url in link_matches if label == "Unreleased"]
    if len(unreleased_links) != 1 or not unreleased_links[0].endswith(
        f"/compare/v{version}...HEAD"
    ):
        raise ValueError(f"the Unreleased changelog link must compare v{version} with HEAD")
    if (
        len(release_links) != 1
        or "/compare/" not in release_links[0]
        or not release_links[0].endswith(f"...v{version}")
    ):
        raise ValueError(
            f"the [{version}] changelog link must compare the previous tag with v{version}"
        )


def _read_git_release_versions(
    root: Path,
    *,
    require_complete: bool = False,
) -> set[str] | None:
    """Return local semantic release tags when complete Git metadata is available."""

    def unavailable(reason: str) -> None:
        if require_complete:
            raise ValueError(
                "complete Git tag metadata is required for changelog validation; "
                f"{reason}. Fetch the full history and tags first"
            )

    try:
        checkout = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        unavailable(f"Git could not be executed ({exc})")
        return None
    if checkout.returncode != 0:
        unavailable("the source tree is not a Git checkout")
        return None
    if Path(checkout.stdout.strip()).resolve() != root.resolve():
        unavailable("the source root is nested inside a different Git checkout")
        return None

    shallow = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-shallow-repository"],
        check=False,
        capture_output=True,
        text=True,
    )
    if shallow.returncode != 0:
        unavailable("the Git checkout depth could not be determined")
        return None
    if shallow.stdout.strip() == "true":
        unavailable("the Git checkout is shallow")
        return None

    tags = subprocess.run(
        ["git", "-C", str(root), "tag", "--list", "v*"],
        check=False,
        capture_output=True,
        text=True,
    )
    if tags.returncode != 0:
        unavailable("release tags could not be listed")
        return None
    versions = {
        match.group("version")
        for tag in tags.stdout.splitlines()
        if (match := _VERSION_RE.fullmatch(tag.strip())) is not None
    }
    if not versions:
        unavailable("no semantic vX.Y.Z release tags are available")
        return None
    return versions


def _validate_changelog_development(
    root: Path,
    *,
    as_of: date | None = None,
    released_versions: set[str] | None = None,
    pending_release_version: str | None = None,
) -> bool:
    """Reject internally inconsistent or future-dated development changelogs."""
    changelog = (root / "docs" / "changelog.md").read_text(encoding="utf-8")
    unreleased_headings = list(re.finditer(r"^## \[Unreleased\]\s*$", changelog, re.MULTILINE))
    if len(unreleased_headings) != 1:
        raise ValueError("docs/changelog.md must contain one Unreleased heading")
    unreleased_heading = unreleased_headings[0]

    release_headings = list(_CHANGELOG_RELEASE_HEADING_RE.finditer(changelog))
    if not release_headings:
        raise ValueError("docs/changelog.md must contain at least one dated release heading")
    latest_release = release_headings[0]
    if latest_release.start() <= unreleased_heading.end():
        raise ValueError("the latest dated release must follow the Unreleased heading")

    evaluation_date = as_of or date.today()
    for heading in release_headings:
        released_on = date.fromisoformat(heading.group("date"))
        if released_on > evaluation_date:
            raise ValueError(
                f"changelog release [{heading.group('version')}] is future-dated "
                f"{released_on.isoformat()}"
            )

    heading_versions = [heading.group("version") for heading in release_headings]
    dated_versions = set(heading_versions)
    if len(dated_versions) != len(heading_versions):
        duplicates = sorted(
            version for version in dated_versions if heading_versions.count(version) > 1
        )
        raise ValueError(
            "docs/changelog.md contains duplicate dated release headings: "
            + ", ".join(f"[{version}]" for version in duplicates)
        )
    expected_order = sorted(heading_versions, key=_semantic_version_key, reverse=True)
    if heading_versions != expected_order:
        raise ValueError("dated changelog release headings must be ordered newest version first")
    current_version = _read_pyproject_version(root)
    unreleased_body = changelog[unreleased_heading.end() : latest_release.start()].strip()
    if unreleased_body and current_version in dated_versions:
        raise ValueError(
            f"current development version [{current_version}] cannot be dated while "
            "Unreleased still contains changes"
        )
    pending_release_accepted = False
    if released_versions is not None:
        undocumented_tags = released_versions - dated_versions
        if undocumented_tags:
            raise ValueError(
                "Git release tags must have matching dated changelog headings; missing "
                + ", ".join(f"[{version}]" for version in sorted(undocumented_tags))
            )
        missing_tags = dated_versions - released_versions
        pending_release_is_valid = (
            pending_release_version is not None
            and missing_tags == {pending_release_version}
            and pending_release_version == current_version
            and pending_release_version == latest_release.group("version")
            and not unreleased_body
        )
        if pending_release_is_valid:
            _validate_changelog_release(root, pending_release_version)
            pending_release_accepted = True
            missing_tags.clear()
        if missing_tags:
            raise ValueError(
                "dated changelog releases must have matching Git tags; missing "
                + ", ".join(f"v{version}" for version in sorted(missing_tags))
            )

    link_matches = re.findall(r"^\[([^\]]+)\]:\s+(\S+)\s*$", changelog, re.MULTILINE)
    for version, previous_version in zip(heading_versions, heading_versions[1:], strict=False):
        release_links = [url for label, url in link_matches if label == version]
        expected_release_suffix = f"/compare/v{previous_version}...v{version}"
        if len(release_links) != 1 or not release_links[0].endswith(expected_release_suffix):
            raise ValueError(
                f"the [{version}] changelog link must compare v{previous_version} with v{version}"
            )
    unreleased_links = [url for label, url in link_matches if label == "Unreleased"]
    latest_released_version = latest_release.group("version")
    expected_suffix = f"/compare/v{latest_released_version}...HEAD"
    if len(unreleased_links) != 1 or not unreleased_links[0].endswith(expected_suffix):
        raise ValueError(
            "the Unreleased changelog link must compare the latest dated release "
            f"v{latest_released_version} with HEAD"
        )
    return pending_release_accepted


def validate_development_changelog(
    root: Path,
    *,
    as_of: date | None = None,
    require_git_tags: bool = False,
    allow_release_candidate: bool = False,
) -> None:
    """Validate the in-development changelog and any available release tags."""
    evaluation_date = as_of or date.today()
    if _readiness_registry_required(_read_pyproject_version(root)):
        validate_one_zero_readiness(root, as_of=evaluation_date)
    released_versions = _read_git_release_versions(
        root,
        require_complete=require_git_tags,
    )
    pending_release_version = _read_pyproject_version(root) if allow_release_candidate else None
    pending_release_accepted = _validate_changelog_development(
        root,
        as_of=evaluation_date,
        released_versions=released_versions,
        pending_release_version=pending_release_version,
    )
    if pending_release_accepted and pending_release_version is not None:
        validate_release_version(root, pending_release_version, as_of=evaluation_date)


def normalize_version(value: str) -> str:
    """Return a tag/input version without its optional leading ``v``."""
    match = _VERSION_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"release version must look like vX.Y.Z (received {value!r})")
    return match.group("version")


def _validate_version_sources(root: Path, requested: str) -> str:
    requested_version = normalize_version(requested)
    versions = {
        "release ref": requested_version,
        "pyproject.toml": _read_pyproject_version(root),
        "django_ray.__version__": _read_module_version(root),
        "uv.lock": _read_lock_version(root),
    }
    if len(set(versions.values())) != 1:
        details = ", ".join(f"{name}={version}" for name, version in versions.items())
        raise ValueError(f"release versions do not agree: {details}")
    return requested_version


def _load_runtime_policy(root: Path) -> tuple[int, int, list[dict[str, Any]]]:
    source = root / "src" / "django_ray" / "runtime" / "compiled_graph.py"
    module_name = "_django_ray_release_compiled_graph"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load Compiled Graph policy from {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return _runtime_policy_snapshot(module)


def _runtime_policy_snapshot(module: ModuleType) -> tuple[int, int, list[dict[str, Any]]]:
    try:
        policy_version = int(module.COMPILED_GRAPH_POLICY_VERSION)
        schema_version = int(module.COMPILED_GRAPH_CAPABILITY_SCHEMA_VERSION)
        rows = module.verified_compiled_graph_capability_rows()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Compiled Graph policy does not expose a valid policy snapshot") from exc
    if not isinstance(rows, tuple) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("verified Compiled Graph capability rows must be a tuple of objects")
    return policy_version, schema_version, [dict(row) for row in rows]


def _parse_date(value: object, *, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def _parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field} must be an ISO UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO UTC timestamp ending in Z") from exc


def _latest_compiled_graph_review(root: Path) -> tuple[Path, dict[str, Any], date]:
    directory = root / "docs" / "investigations"
    candidates: list[tuple[date, Path]] = []
    for path in directory.glob("compiled-graph-capability-review-*.json"):
        match = _REVIEW_FILE_RE.fullmatch(path.name)
        if match is not None:
            candidates.append((date.fromisoformat(match.group("date")), path))
    if not candidates:
        raise ValueError("no Compiled Graph capability review record exists")
    review_date, path = max(candidates, key=lambda item: item[0])
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON") from exc
    if not isinstance(record, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    if record.get("review_id") != path.stem:
        raise ValueError(f"{path.name} review_id must match its filename")
    if _parse_date(record.get("reviewed_on"), field="reviewed_on") != review_date:
        raise ValueError(f"{path.name} reviewed_on must match its filename date")
    return path, record, review_date


def _validate_review_artifacts(
    record: dict[str, Any],
) -> tuple[
    dict[str, datetime],
    set[str],
    dict[str, dict[str, Any] | None],
    dict[str, dict[str, Any]],
]:
    workflow_run = record.get("workflow_run")
    if not isinstance(workflow_run, dict) or not isinstance(workflow_run.get("run_id"), int):
        raise ValueError("latest Compiled Graph review must identify its workflow run")
    head_sha = workflow_run.get("head_sha")
    if not isinstance(head_sha, str) or _GIT_OBJECT_RE.fullmatch(head_sha) is None:
        raise ValueError("latest Compiled Graph review head_sha must be a full Git object ID")

    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("latest Compiled Graph review must retain at least one artifact")
    expiries: dict[str, datetime] = {}
    quarantined_artifacts: set[str] = set()
    observed_capabilities: dict[str, dict[str, Any] | None] = {}
    observations: dict[str, dict[str, Any]] = {}
    run_id = workflow_run["run_id"]
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("Compiled Graph review artifacts must be objects")
        evidence_id = artifact.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            raise ValueError("every Compiled Graph artifact requires an evidence_id")
        if evidence_id in expiries:
            raise ValueError(f"duplicate Compiled Graph evidence_id: {evidence_id}")
        if not isinstance(artifact.get("quarantined"), bool):
            raise ValueError(f"artifact {evidence_id} must record an explicit quarantine state")
        if artifact["quarantined"]:
            quarantined_artifacts.add(evidence_id)
        artifact_id = artifact.get("artifact_id")
        if not isinstance(artifact_id, int):
            raise ValueError(f"artifact {evidence_id} requires a numeric artifact_id")
        expected_evidence_id = f"github-actions:{run_id}:artifact:{artifact_id}"
        if evidence_id != expected_evidence_id:
            raise ValueError(f"artifact {evidence_id} does not match its run and artifact IDs")
        if _SHA256_RE.fullmatch(str(artifact.get("archive_digest"))) is None:
            raise ValueError(f"artifact {evidence_id} requires a SHA-256 archive digest")
        archive_size = artifact.get("archive_size_bytes")
        if not isinstance(archive_size, int) or archive_size <= 0:
            raise ValueError(f"artifact {evidence_id} requires a positive archive byte size")
        if not isinstance(artifact.get("job_id"), int):
            raise ValueError(f"artifact {evidence_id} requires a numeric job_id")
        artifact_url = artifact.get("artifact_url")
        if not isinstance(artifact_url, str) or not artifact_url.endswith(
            f"/artifacts/{artifact_id}"
        ):
            raise ValueError(f"artifact {evidence_id} requires its exact artifact URL")
        expiries[evidence_id] = _parse_timestamp(
            artifact.get("expires_at"), field=f"artifact {evidence_id} expires_at"
        )
        _parse_timestamp(artifact.get("created_at"), field=f"artifact {evidence_id} created_at")

        observation = artifact.get("observation")
        if not isinstance(observation, dict):
            raise ValueError(f"artifact {evidence_id} requires a probe observation")
        native_status = observation.get("native_probe_status")
        if not isinstance(native_status, str) or native_status not in _PROBE_STATUSES:
            raise ValueError(f"artifact {evidence_id} has an invalid native probe status")
        if not isinstance(observation.get("result_verified"), bool):
            raise ValueError(f"artifact {evidence_id} must record whether its result was verified")
        if not isinstance(observation.get("adapter_eligible"), bool):
            raise ValueError(f"artifact {evidence_id} must record adapter eligibility")
        adapter_reason = observation.get("adapter_reason")
        if not isinstance(adapter_reason, str) or not adapter_reason:
            raise ValueError(f"artifact {evidence_id} must record an adapter reason")
        missing_dimensions = observation.get("missing_dimensions")
        if not isinstance(missing_dimensions, list) or any(
            not isinstance(item, str) or not item for item in missing_dimensions
        ):
            raise ValueError(f"artifact {evidence_id} missing_dimensions must be a list of names")
        if len(set(missing_dimensions)) != len(missing_dimensions):
            raise ValueError(f"artifact {evidence_id} missing_dimensions cannot contain duplicates")
        for field in ("topology", "submission_transport", "transport"):
            if not isinstance(observation.get(field), str) or not observation[field]:
                raise ValueError(f"artifact {evidence_id} must record observation {field}")
        observations[evidence_id] = observation

        observed_capability = artifact.get("observed_capability")
        if observed_capability is not None:
            if not isinstance(observed_capability, dict) or set(observed_capability) != (
                _CAPABILITY_FIELDS
            ):
                raise ValueError(
                    f"artifact {evidence_id} observed_capability must contain every exact dimension"
                )
            if any(
                not isinstance(value, str) or not value for value in observed_capability.values()
            ):
                raise ValueError(
                    f"artifact {evidence_id} observed capability dimensions must be non-empty"
                )
            expected_dimensions = {
                "ray_version": artifact.get("ray_version"),
                "topology": observation["topology"],
                "submission_transport": observation["submission_transport"],
                "transport": observation["transport"],
            }
            if any(
                observed_capability[field] != expected
                for field, expected in expected_dimensions.items()
            ):
                raise ValueError(
                    f"artifact {evidence_id} observed capability conflicts with its observation"
                )
        observed_capabilities[evidence_id] = observed_capability

        files = artifact.get("files")
        if not isinstance(files, list):
            raise ValueError(f"artifact {evidence_id} requires a file manifest")
        paths: set[str] = set()
        for retained_file in files:
            if not isinstance(retained_file, dict):
                raise ValueError(f"artifact {evidence_id} file entries must be objects")
            file_path = retained_file.get("path")
            if not isinstance(file_path, str):
                raise ValueError(f"artifact {evidence_id} file paths must be strings")
            if file_path in paths:
                raise ValueError(f"artifact {evidence_id} contains a duplicate file path")
            paths.add(file_path)
            file_size = retained_file.get("size_bytes")
            if not isinstance(file_size, int) or file_size < 0:
                raise ValueError(f"artifact {evidence_id} file sizes must be integers")
            if _BARE_SHA256_RE.fullmatch(str(retained_file.get("sha256"))) is None:
                raise ValueError(f"artifact {evidence_id} files require SHA-256 hashes")
        if paths != _EVIDENCE_FILES:
            raise ValueError(
                f"artifact {evidence_id} must retain environment.json, packages.txt, and probe.json"
            )
    return expiries, quarantined_artifacts, observed_capabilities, observations


def _validate_maintenance_policy(record: dict[str, Any]) -> None:
    maintenance = record.get("maintenance_policy")
    if not isinstance(maintenance, dict) or maintenance.get("latest_review_wins") is not True:
        raise ValueError("Compiled Graph review must declare that the latest review wins")
    no_promotion = maintenance.get("no_promotion")
    if (
        not isinstance(no_promotion, dict)
        or no_promotion.get("artifact_expiry_invalidates_policy") is not False
    ):
        raise ValueError("no-promotion reviews must remain safe after artifact expiry")
    verified = maintenance.get("verified_rows")
    required_true = {
        "must_match_runtime_policy_exactly",
        "evidence_ids_required",
        "reviewed_on_required",
        "revalidate_on_or_before_required",
        "unexpired_artifacts_required",
    }
    if not isinstance(verified, dict) or any(
        verified.get(field) is not True for field in required_true
    ):
        raise ValueError("verified Compiled Graph rows must retain strict maintenance gates")
    if verified.get("quarantined_rows_allowed") is not False:
        raise ValueError("quarantined Compiled Graph rows cannot remain verified")
    triggers = maintenance.get("quarantine_triggers")
    if not isinstance(triggers, list) or not triggers:
        raise ValueError("Compiled Graph review must define quarantine triggers")


def validate_compiled_graph_capability_review(root: Path, *, as_of: date | None = None) -> Path:
    """Validate the latest review against the executable fail-closed policy."""
    path, record, review_date = _latest_compiled_graph_review(root)
    if record.get("schema_version") != 1:
        raise ValueError(f"{path.name} has an unsupported schema version")
    runtime_policy_version, runtime_schema_version, runtime_rows = _load_runtime_policy(root)
    if record.get("policy_version") != runtime_policy_version:
        raise ValueError("latest Compiled Graph review does not match the runtime policy version")
    if record.get("capability_schema_version") != runtime_schema_version:
        raise ValueError(
            "latest Compiled Graph review does not match the capability schema version"
        )
    _validate_maintenance_policy(record)
    (
        artifact_expiries,
        artifact_quarantines,
        observed_capabilities,
        observations,
    ) = _validate_review_artifacts(record)

    reviewed_rows = record.get("verified_capability_rows")
    if not isinstance(reviewed_rows, list):
        raise ValueError("verified_capability_rows must be a list")
    capability_rows: list[dict[str, Any]] = []
    today = as_of or date.today()
    quarantined = record.get("quarantined_evidence_ids")
    if not isinstance(quarantined, list) or any(not isinstance(item, str) for item in quarantined):
        raise ValueError("quarantined_evidence_ids must be a list of evidence IDs")
    if len(set(quarantined)) != len(quarantined):
        raise ValueError("quarantined_evidence_ids cannot contain duplicates")
    if set(quarantined) != artifact_quarantines:
        raise ValueError("quarantined_evidence_ids must exactly match artifact quarantine states")

    for row in reviewed_rows:
        if not isinstance(row, dict):
            raise ValueError("verified Compiled Graph rows must be objects")
        capability = row.get("capability")
        if not isinstance(capability, dict) or set(capability) != _CAPABILITY_FIELDS:
            raise ValueError("each verified row must contain every exact capability dimension")
        if any(not isinstance(value, str) or not value for value in capability.values()):
            raise ValueError("verified capability dimensions must be non-empty strings")
        capability_rows.append(capability)

        evidence_ids = row.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValueError("every verified row requires at least one evidence ID")
        if any(not isinstance(item, str) or item not in artifact_expiries for item in evidence_ids):
            raise ValueError("verified row references unknown evidence")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("verified row evidence IDs cannot contain duplicates")
        if any(observed_capabilities[item] != capability for item in evidence_ids):
            raise ValueError("verified row does not exactly match its retained evidence")
        if any(observations[item]["native_probe_status"] != "success" for item in evidence_ids):
            raise ValueError("verified row evidence did not complete a successful native probe")
        if any(observations[item]["result_verified"] is not True for item in evidence_ids):
            raise ValueError("verified row evidence did not verify its native result")
        if any(observations[item]["missing_dimensions"] for item in evidence_ids):
            raise ValueError("verified row evidence has unresolved capability dimensions")
        if any(
            (
                observation["adapter_eligible"] is True
                and observation["adapter_reason"] != "ELIGIBLE"
            )
            or (
                observation["adapter_eligible"] is False
                and observation["adapter_reason"] != "CANDIDATE_REQUIRES_SMOKE"
            )
            for observation in (observations[item] for item in evidence_ids)
        ):
            raise ValueError(
                "verified row evidence was neither eligible nor a complete unpromoted candidate"
            )
        row_reviewed_on = _parse_date(row.get("reviewed_on"), field="row reviewed_on")
        if row_reviewed_on != review_date:
            raise ValueError("verified row reviewed_on must match the latest review")
        revalidate_on = _parse_date(
            row.get("revalidate_on_or_before"), field="row revalidate_on_or_before"
        )
        if revalidate_on < review_date or today > revalidate_on:
            raise ValueError("verified Compiled Graph evidence requires revalidation")
        if row.get("quarantined") is not False:
            raise ValueError("quarantined Compiled Graph rows cannot remain verified")
        if any(item in quarantined for item in evidence_ids):
            raise ValueError("verified row references quarantined evidence")
        if any(artifact_expiries[item].date() <= today for item in evidence_ids):
            raise ValueError("verified row references expired evidence")

    canonical_review = sorted(
        json.dumps(row, sort_keys=True, separators=(",", ":")) for row in capability_rows
    )
    canonical_runtime = sorted(
        json.dumps(row, sort_keys=True, separators=(",", ":")) for row in runtime_rows
    )
    if canonical_review != canonical_runtime:
        raise ValueError("reviewed capability rows do not exactly match the runtime policy")
    expected_decision = "promote" if runtime_rows else "no_promotion"
    if record.get("decision") != expected_decision:
        raise ValueError(f"latest Compiled Graph review decision must be {expected_decision}")
    return path


def validate_release_version(
    root: Path,
    requested: str,
    *,
    as_of: date | None = None,
) -> str:
    """Validate a tag/manual input against every release version source."""
    evaluation_date = as_of or date.today()
    requested_version = _validate_version_sources(root, requested)
    _validate_changelog_development(root, as_of=evaluation_date)
    _validate_changelog_release(root, requested_version)
    validate_compiled_graph_capability_review(root, as_of=evaluation_date)
    if _readiness_registry_required(requested_version):
        validate_one_zero_readiness(
            root,
            require_accepted=int(requested_version.split(".", 1)[0]) >= 1,
            as_of=evaluation_date,
        )
    return requested_version


def validate_testpypi_candidate(
    root: Path,
    requested: str,
    *,
    as_of: date | None = None,
) -> str:
    """Validate an Unreleased, pre-tag candidate for a TestPyPI rehearsal."""
    evaluation_date = as_of or date.today()
    requested_version = _validate_version_sources(root, requested)
    released_versions = _read_git_release_versions(root, require_complete=True)
    if released_versions is None:  # pragma: no cover - require_complete raises instead
        raise ValueError("complete Git tag metadata is required")
    if requested_version in released_versions:
        raise ValueError(f"TestPyPI candidate v{requested_version} is already tagged")
    _validate_changelog_development(
        root,
        as_of=evaluation_date,
        released_versions=released_versions,
        pending_release_version=requested_version,
    )
    validate_compiled_graph_capability_review(root, as_of=evaluation_date)
    if _readiness_registry_required(requested_version):
        validate_one_zero_readiness(
            root,
            require_accepted=int(requested_version.split(".", 1)[0]) >= 1,
            as_of=evaluation_date,
        )
    return requested_version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?", help="tag or manual release version, such as v0.3.0")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--development",
        action="store_true",
        help="validate the current Unreleased changelog instead of a release candidate",
    )
    parser.add_argument(
        "--require-git-tags",
        action="store_true",
        help="fail unless complete Git metadata is available for dated-heading tag checks",
    )
    parser.add_argument(
        "--allow-release-candidate",
        action="store_true",
        help="allow one fully validated, current release candidate to precede its tag",
    )
    parser.add_argument(
        "--testpypi-candidate",
        action="store_true",
        help="validate an Unreleased manual TestPyPI candidate with complete Git tags",
    )
    args = parser.parse_args()
    try:
        if args.development:
            if args.version is not None:
                parser.error("version cannot be used with --development")
            if args.testpypi_candidate:
                parser.error("--testpypi-candidate cannot be used with --development")
            validate_development_changelog(
                args.root,
                require_git_tags=args.require_git_tags,
                allow_release_candidate=args.allow_release_candidate,
            )
            print("development changelog valid")
        else:
            if args.version is None:
                parser.error("version is required unless --development is used")
            if args.require_git_tags:
                parser.error("--require-git-tags can only be used with --development")
            if args.allow_release_candidate:
                parser.error("--allow-release-candidate can only be used with --development")
            if args.testpypi_candidate:
                print(validate_testpypi_candidate(args.root, args.version))
            else:
                print(validate_release_version(args.root, args.version))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"Release validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
