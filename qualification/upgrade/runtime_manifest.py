"""Render finite native-upgrade manifests without contacting external resources.

Images are previously reviewed inputs. Digest syntax and manifest consistency do
not authenticate image contents, admission, cleanup, or any upgrade observation.
The caller owns the exact source archive, resource admission and serial lifecycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml
from yaml.resolver import BaseResolver

from qualification.upgrade.runtime_steps import (
    STORE_ACTIONS,
    RuntimeStoreArguments,
    StepError,
    store_cli_args,
)

_PROFILE = Path(__file__).with_name("runtime.yaml")
_TEMPLATES = frozenset(
    {
        "postgres_pvc",
        "artifact_pvc",
        "postgres_service",
        "postgres",
        "ray_service",
        "ray",
        "manager",
        "observer",
    }
)
_PLACEHOLDERS = frozenset(
    {
        "namespace",
        "storageClass",
        "admittedNode",
        "postgresImage",
        "postgresUser",
        "primaryDatabase",
        "scratchDatabase",
        "runtimeBuild",
        "epochRayVersion",
        "epochImage",
        "epochPythonVersion",
        "database",
        "jobName",
        "managerArgs",
        "observerArgs",
        "settingsModule",
    }
)
_CASES = frozenset(
    {
        "old-success",
        "old-failure",
        "old-cancel",
        "old-retry",
        "old-gated",
        "current-core",
        "current-jobs",
    }
)
_ACTIONS = frozenset(
    {
        "prepare",
        "enqueue",
        "inspect",
        "cancel",
        "release",
        "history",
        "compare-history",
        "migrate",
        "blocked-history",
        "blocked-migrate",
        "read-history",
        *STORE_ACTIONS,
    }
)
_CASE_ACTIONS = frozenset({"enqueue", "inspect", "cancel", "release"})
_BUILDS = {"baseline": "2.56.0", "candidate": "2.58.0"}
_RESTORE_POINTS = frozenset({"blocked", "final", "rollback"})
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
_VARIABLE = re.compile(r"\$\{([A-Za-z][A-Za-z0-9]*)\}\Z")


class RuntimeManifestError(ValueError):
    """A bounded refusal; do not expose untrusted configuration values."""


def _require(condition: bool) -> None:
    if not condition:
        raise RuntimeManifestError("invalid-native-upgrade-manifest")


@dataclass(frozen=True)
class RuntimeManifestConfig:
    namespace: str
    admitted_node: str
    storage_class: str
    postgres_image: str
    baseline_image: str
    candidate_image: str
    baseline_python_version: str
    candidate_python_version: str
    postgres_user: str
    primary_database: str
    scratch_database: str


def _dns(value: str, *, maximum: int, subdomains: bool = False) -> None:
    _require(type(value) is str and 0 < len(value) <= maximum)
    labels = value.split(".") if subdomains else [value]
    _require(all(0 < len(label) <= 63 and _LABEL.fullmatch(label) is not None for label in labels))


def _image(value: str) -> None:
    _require(type(value) is str and 0 < len(value) <= 512)
    match = re.fullmatch(r"([^@]+)@sha256:([0-9a-f]{64})", value)
    _require(match is not None)
    assert match is not None
    parts = match[1].split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        host, separator, port = parts.pop(0).partition(":")
        _dns(host, maximum=253, subdomains=True)
        if separator:
            _require(re.fullmatch(r"[1-9][0-9]{0,4}", port) is not None and int(port) <= 65535)
    _require(
        bool(parts)
        and all(
            re.fullmatch(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*", part) is not None
            for part in parts
        )
    )


def _config(value: RuntimeManifestConfig) -> None:
    _require(type(value) is RuntimeManifestConfig)
    _require(all(type(getattr(value, field.name)) is str for field in fields(value)))
    _dns(value.namespace, maximum=63)
    _dns(value.admitted_node, maximum=63, subdomains=True)
    _dns(value.storage_class, maximum=253, subdomains=True)
    for image in (value.postgres_image, value.baseline_image, value.candidate_image):
        _image(image)
    # One identical digest cannot contain both exact package/Ray epochs.
    _require(value.baseline_image.rsplit("@", 1)[1] != value.candidate_image.rsplit("@", 1)[1])
    for version in (value.baseline_python_version, value.candidate_python_version):
        _require(re.fullmatch(r"3\.(12|13|14)\.(0|[1-9][0-9]{0,2})", version) is not None)
    for name in (value.postgres_user, value.primary_database, value.scratch_database):
        _require(re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) is not None)
    _require(value.primary_database != value.scratch_database)


class _TemplateLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _TemplateLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        _require(type(key) is str and key not in result)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_TemplateLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _variables(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> set[str]:
    if budget is None:
        budget = [8192]
    budget[0] -= 1
    _require(depth <= 24 and budget[0] >= 0)
    if type(value) is str:
        match = _VARIABLE.fullmatch(value)
        _require("${" not in value or match is not None)
        return {match[1]} if match else set()
    if type(value) is dict:
        _require(all(type(key) is str and "${" not in key for key in value))
        children = value.values()
    elif type(value) is list:
        children = value
    else:
        _require(type(value) in {int, bool, type(None)})
        return set()
    found: set[str] = set()
    for child in children:
        found.update(_variables(child, depth=depth + 1, budget=budget))
    return found


def _templates() -> dict:
    try:
        with _PROFILE.open("rb") as stream:
            raw = stream.read(64 * 1024 + 1)
        _require(0 < len(raw) <= 64 * 1024)
        data = yaml.load(raw, Loader=_TemplateLoader)
        _require(type(data) is dict and set(data) == {"schema", "requirements", "templates"})
        _require(type(data["schema"]) is int and data["schema"] == 1)
        _require(
            data["requirements"]
            == {
                "maxLiveRayGenerations": 1,
                "maxLiveManagers": 1,
                "maxLiveObservers": 1,
                "maxAdmittedPodPids": 1024,
                "completeUpgradeGate": False,
            }
        )
        templates = data["templates"]
        _require(type(templates) is dict and set(templates) == _TEMPLATES)
        _require(_variables(templates) == _PLACEHOLDERS)
        return templates
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError, TypeError, KeyError):
        raise RuntimeManifestError("invalid-native-upgrade-template") from None


def _substitute(value: Any, replacements: dict[str, Any]) -> Any:
    required = _variables(value)
    _require(set(replacements) == required)

    def visit(node):
        if type(node) is dict:
            return {key: visit(child) for key, child in node.items()}
        if type(node) is list:
            return [visit(child) for child in node]
        if type(node) is str and (match := _VARIABLE.fullmatch(node)):
            replacement = replacements[match[1]]
            _require(
                type(replacement) is str
                or (type(replacement) is list and all(type(item) is str for item in replacement))
            )
            _require(not _variables(replacement))
            return list(replacement) if type(replacement) is list else replacement
        return node

    result = visit(value)
    _require(not _variables(result))
    return result


def _observer_args(
    action: str | None,
    case: str | None,
    build: str,
    database: str,
    store_arguments: RuntimeStoreArguments | None = None,
) -> list[str]:
    _require(type(action) is str and action in _ACTIONS)
    _require((action in _CASE_ACTIONS) == (case is not None))
    if case is not None:
        _require(type(case) is str and case in _CASES)
    if action in STORE_ACTIONS:
        _require(build == "baseline" and database == "primary")
        try:
            return store_cli_args(action, store_arguments)
        except StepError:
            raise RuntimeManifestError("invalid-native-upgrade-manifest") from None
    _require(store_arguments is None)
    if action == "enqueue":
        assert case is not None
        _require(case.startswith("old-") == (build == "baseline"))
    if action == "cancel":
        _require(case == "old-cancel" and build == "baseline")
    if action == "release":
        _require((case, build) in {("old-gated", "baseline"), ("current-jobs", "candidate")})
    if action == "migrate":
        _require(database == "primary")
    if action == "blocked-history":
        # The released wheel snapshots the restored copy, so live primary
        # heartbeats cannot change the original-field comparison baseline.
        _require(build == "baseline" and database == "scratch")
    if action == "blocked-migrate":
        _require(build == "candidate" and database == "scratch")
    if database == "scratch":
        _require(
            action
            in {"inspect", "compare-history", "blocked-history", "blocked-migrate", "read-history"}
        )
    assert action is not None
    return [action] if case is None else [action, "--case", case]


def _runtime_environment(
    config: RuntimeManifestConfig, build: str, database: str, settings_module: str
) -> dict[str, str]:
    return {
        "DJANGO_SETTINGS_MODULE": settings_module,
        "DJANGO_RAY_UPGRADE_BUILD": build,
        "DJANGO_RAY_UPGRADE_PYTHON_VERSION": getattr(config, build + "_python_version"),
        "DJANGO_RAY_UPGRADE_DATABASE": database,
        "DJANGO_RAY_UPGRADE_PRIMARY_DATABASE": config.primary_database,
        "DJANGO_RAY_UPGRADE_SCRATCH_DATABASE": config.scratch_database,
        "DJANGO_RAY_UPGRADE_POSTGRES_HOST": "postgres",
        "DJANGO_RAY_UPGRADE_POSTGRES_PORT": "5432",
        "DJANGO_RAY_UPGRADE_POSTGRES_USER": config.postgres_user,
        "DJANGO_RAY_UPGRADE_POSTGRES_PASSWORD_FILE": "/run/upgrade-secrets/postgres-password",
        "DJANGO_RAY_UPGRADE_ENCRYPTION_KEY_FILE": "/run/upgrade-secrets/runtime-env-key",
        "DJANGO_SECRET_KEY_FILE": "/run/upgrade-secrets/django-secret-key",
        "DJANGO_RAY_UPGRADE_CORE_ADDRESS": "ray-head:6379",
        "DJANGO_RAY_UPGRADE_JOBS_ADDRESS": "http://ray-head:8265",
        "DJANGO_RAY_UPGRADE_ARTIFACT_ROOT": "/artifacts",
        "HOME": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "RAY_USAGE_STATS_ENABLED": "0",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }


def _restore_selection(
    template: str, database: str, action: str | None, case: str | None, point: str | None
) -> str | None:
    if template != "observer" or database != "scratch":
        _require(point is None)
        return None
    _require(type(point) is str and point in _RESTORE_POINTS)
    if action in {"blocked-history", "blocked-migrate"}:
        _require(point == "blocked")
    elif action in {"read-history", "compare-history"}:
        _require(point in {"final", "rollback"})
    else:
        _require(action == "inspect" and case in _CASES and str(case).startswith("old-"))
    return point


def _select_artifact_mount(manifest: dict, point: str | None) -> None:
    """Bind only the existing artifact volume; never create or verify a restore.

    Every source template starts at the PVC root. Only an accepted scratch
    observer receives a fixed subPath in its newly rendered plain object.
    The caller must finish and independently verify that copy before admission.
    """
    observed = 0

    def visit(value):
        nonlocal observed
        if type(value) is dict:
            if "containers" in value:
                volumes = value.get("volumes")
                _require(type(volumes) is list and all(type(item) is dict for item in volumes))
                _require(
                    [
                        item
                        for item in volumes
                        if item.get("name") == "artifacts" or "persistentVolumeClaim" in item
                    ]
                    == [
                        {
                            "name": "artifacts",
                            "persistentVolumeClaim": {"claimName": "runtime-artifacts"},
                        }
                    ]
                )
                containers = value["containers"]
                _require(type(containers) is list and len(containers) == 1)
                _require(type(containers[0]) is dict)
                mounts = containers[0].get("volumeMounts")
                _require(type(mounts) is list and all(type(item) is dict for item in mounts))
                artifacts = [
                    item
                    for item in mounts
                    if item.get("name") == "artifacts"
                    or item.get("mountPath") == "/artifacts"
                    or (
                        type(item.get("mountPath")) is str
                        and item["mountPath"].startswith("/artifacts/")
                    )
                ]
                _require(artifacts == [{"name": "artifacts", "mountPath": "/artifacts"}])
                if point is not None:
                    artifacts[0]["subPath"] = ".upgrade-restores/" + point
                observed += 1
            for child in value.values():
                visit(child)
        elif type(value) is list:
            for child in value:
                visit(child)

    visit(manifest)
    _require(observed == (2 if manifest.get("kind") == "RayCluster" else 1))


def _verify_environment(manifest: dict, expected: dict[str, str], image: str) -> None:
    observed = 0

    def visit(value):
        nonlocal observed
        if type(value) is dict:
            _require("envFrom" not in value)
            if "containers" in value:
                containers = value["containers"]
                _require(type(containers) is list and len(containers) == 1)
                _require(
                    type(containers[0]) is dict
                    and containers[0].get("image") == image
                    and "env" in containers[0]
                )
            if "env" in value:
                rows = value["env"]
                _require(
                    type(rows) is list
                    and all(
                        type(row) is dict
                        and set(row) == {"name", "value"}
                        and type(row["name"]) is str
                        and type(row["value"]) is str
                        for row in rows
                    )
                )
                _require(len(rows) == len(expected))
                _require({row["name"]: row["value"] for row in rows} == expected)
                observed += 1
            for child in value.values():
                visit(child)
        elif type(value) is list:
            for child in value:
                visit(child)

    visit(manifest)
    _require(observed == (2 if manifest.get("kind") == "RayCluster" else 1))


def render_runtime_manifest(
    template: str,
    config: RuntimeManifestConfig,
    *,
    build: str | None = None,
    database: str | None = None,
    manager_mode: str | None = None,
    action: str | None = None,
    case: str | None = None,
    job_name: str | None = None,
    restore_point: str | None = None,
    store_arguments: RuntimeStoreArguments | None = None,
) -> dict:
    """Return one fresh manifest from reviewed inputs and fixed command choices.

    Ray and manager processes always use the primary database. Scratch observers
    select one fixed, already restored artifact copy for their accepted action.
    The mount remains /artifacts; primary processes retain the PVC root. Migration
    has no caller-supplied target. Image provenance, stopped-writer confirmation,
    resource creation, restore and no-overlap sequencing belong to the caller.
    """
    _require(type(template) is str and template in _TEMPLATES)
    _config(config)
    runtime = template in {"ray", "manager", "observer"}
    _require((build is not None) == runtime)
    _require(build is None or (type(build) is str and build in _BUILDS))
    _require((job_name is not None) == (template in {"manager", "observer"}))
    if job_name is not None:
        _dns(job_name, maximum=63)
    _require(manager_mode is None or template == "manager")
    _require((action is None and case is None) or template == "observer")
    _require(database is None or template == "observer")
    _require(store_arguments is None or template == "observer")
    values: dict[str, Any] = {
        "namespace": config.namespace,
        "storageClass": config.storage_class,
        "admittedNode": config.admitted_node,
        "postgresImage": config.postgres_image,
        "postgresUser": config.postgres_user,
        "primaryDatabase": config.primary_database,
        "scratchDatabase": config.scratch_database,
    }
    selected_database = "primary"
    settings_module = (
        "qualification.upgrade.runtime_history_settings"
        if template == "observer" and action == "read-history"
        else "qualification.upgrade.runtime_settings"
    )
    if runtime:
        assert build is not None
        if template == "observer":
            _require(type(database) is str and database in {"primary", "scratch"})
            assert database is not None
            selected_database = database
        values.update(
            runtimeBuild=build,
            epochRayVersion=_BUILDS[build],
            epochImage=getattr(config, build + "_image"),
            epochPythonVersion=getattr(config, build + "_python_version"),
            database=selected_database,
            settingsModule=settings_module,
        )
    if template == "manager":
        _require(type(manager_mode) is str and manager_mode in {"core", "jobs"})
        values["managerArgs"] = ["--queue", "upgrade-" + str(manager_mode), "--concurrency", "1"]
        if manager_mode == "core":
            values["managerArgs"].append("--cluster=ray-head:6379")
    if template == "observer":
        assert build is not None
        values["observerArgs"] = _observer_args(
            action, case, build, selected_database, store_arguments
        )
    restore_point = _restore_selection(template, selected_database, action, case, restore_point)
    if job_name is not None:
        values["jobName"] = job_name
    source = _templates()[template]
    required = _variables(source)
    _require(required <= values.keys())
    result = _substitute(source, {key: values[key] for key in required})
    _require(
        type(result) is dict and result.get("metadata", {}).get("namespace") == config.namespace
    )
    if runtime:
        assert build is not None
        _select_artifact_mount(result, restore_point)
        _verify_environment(
            result,
            _runtime_environment(config, build, selected_database, settings_module),
            getattr(config, build + "_image"),
        )
    elif template == "postgres":
        _verify_environment(
            result,
            {
                "POSTGRES_USER": config.postgres_user,
                "POSTGRES_DB": config.primary_database,
                "POSTGRES_PASSWORD_FILE": "/run/upgrade-secrets/postgres-password",
                "PGDATA": "/var/lib/postgresql/data/pgdata",
            },
            config.postgres_image,
        )
    return result
