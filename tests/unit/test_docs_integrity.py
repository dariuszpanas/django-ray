"""Contracts that keep the growing live documentation discoverable and current."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path
from typing import Any

from django_ray.conf.defaults import DEFAULTS

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs"
EVIDENCE_DIRECTORIES = {"benchmarks", "investigations"}
SETTINGS_OUTSIDE_DJANGO_RAY = {"RAY_DASHBOARD_URL"}


def test_repository_llms_guide_matches_published_copy() -> None:
    assert (ROOT / "llms.txt").read_bytes() == (DOCS / "llms.txt").read_bytes()


def _nav_markdown_paths(value: Any) -> set[Path]:
    if isinstance(value, str):
        return {Path(value)} if value.endswith(".md") else set()
    if isinstance(value, list):
        return set().union(*(_nav_markdown_paths(item) for item in value))
    if isinstance(value, dict):
        return set().union(*(_nav_markdown_paths(item) for item in value.values()))
    return set()


def test_live_documentation_pages_are_navigable() -> None:
    config = tomllib.loads((ROOT / "zensical.toml").read_text(encoding="utf-8"))
    nav_paths = _nav_markdown_paths(config["project"]["nav"])
    all_pages = {path.relative_to(DOCS) for path in DOCS.rglob("*.md")}
    live_pages = {
        path for path in all_pages if not path.parts or path.parts[0] not in EVIDENCE_DIRECTORIES
    }

    assert nav_paths <= all_pages
    assert live_pages <= nav_paths


def test_docs_use_one_rich_homepage_source() -> None:
    config = tomllib.loads((ROOT / "zensical.toml").read_text(encoding="utf-8"))
    homepage = DOCS / "README.md"
    homepage_text = homepage.read_text(encoding="utf-8")

    assert config["project"]["nav"][0] == {"Home": "README.md"}
    assert homepage.is_file()
    assert not (DOCS / "index.md").exists()
    assert "# django-ray Documentation" in homepage_text
    assert "## What is django-ray?" in homepage_text
    assert "assets/images/testproject-landing.png" in homepage_text
    assert "# Documentation source" not in homepage_text


def test_settings_reference_tracks_every_package_default() -> None:
    reference = (DOCS / "reference" / "settings.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^### ([A-Z][A-Z0-9_]*)\s*$", reference, re.MULTILINE))

    assert documented == set(DEFAULTS) | SETTINGS_OUTSIDE_DJANGO_RAY


def test_management_commands_are_discoverable_in_live_guides() -> None:
    command_directory = ROOT / "src" / "django_ray" / "management" / "commands"
    commands = {path.stem for path in command_directory.glob("*.py") if path.name != "__init__.py"}
    live_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in DOCS.rglob("*")
        if path.suffix in {".md", ".txt"}
        and path.name != "changelog.md"
        and (
            not path.relative_to(DOCS).parts
            or path.relative_to(DOCS).parts[0] not in EVIDENCE_DIRECTORIES
        )
    )

    assert {command for command in commands if command not in live_text} == set()


def test_result_storage_guide_covers_integrity_rotation_and_incidents() -> None:
    guide = (DOCS / "reference" / "result-storage.md").read_text(encoding="utf-8")

    assert "## Integrity and Authority Contract" in guide
    assert "## Configuration Rotation and Legacy References" in guide
    assert "## Corruption Incident Recovery" in guide
    assert "SHA-256" in guide
    assert "archived-attempt" in guide
    assert "Never change the stored digest or byte count" in guide
    assert "leading and trailing `/`" in guide
    assert "concurrently replace digest" in guide


def test_input_storage_guide_states_filesystem_trust_boundary() -> None:
    guide = (DOCS / "reference" / "input-storage.md").read_text(encoding="utf-8")

    assert "INPUT_STORAGE_FILESYSTEM_PATH" in guide
    assert "concurrently" in guide
    assert "symlinks or Windows reparse points" in guide


def test_celery_ray_portfolio_recipe_is_parseable_and_complete() -> None:
    guide = (DOCS / "celery-migration.md").read_text(encoding="utf-8")
    section = guide.split("### Configure Celery and Ray together", maxsplit=1)[1]
    match = re.search(r"```python\n(?P<source>.*?)\n```", section, re.DOTALL)

    assert match is not None
    module = ast.parse(match.group("source"))
    assignments = {
        statement.targets[0].id: statement.value
        for statement in module.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    }

    assert ast.literal_eval(assignments["CELERY_BROKER_URL"])
    assert ast.literal_eval(assignments["CELERY_RESULT_BACKEND"])
    assert ast.literal_eval(assignments["CELERY_RESULT_EXTENDED"]) is True

    tasks = ast.literal_eval(assignments["TASKS"])
    assert tasks["default"]["BACKEND"] == "django_tasks_celery.CeleryBackend"
    assert "default" in tasks["default"]["QUEUES"]
    assert tasks["default"]["OPTIONS"]["CELERY_APP"] == "myproject.celery.app"
    assert tasks["ray"]["BACKEND"] == "django_ray.backends.RayTaskBackend"
    assert "ray-batch" in tasks["ray"]["QUEUES"]
    assert tasks["ray"]["OPTIONS"]["RAY_ADDRESS"] == "auto"

    django_ray = ast.literal_eval(assignments["DJANGO_RAY"])
    assert django_ray["RAY_ADDRESS"] == "auto"
    assert django_ray["RUNNER"] == "ray_core"

    route_section = guide.split("### Route only new work with an allowlisted flag", maxsplit=1)[
        1
    ].split("### Refresh a Django `TaskResult`", maxsplit=1)[0]
    route_sources = re.findall(r"```python\n(.*?)\n```", route_section, re.DOTALL)
    service_source = next(source for source in route_sources if "_REPORT_ROUTES" in source)
    service_module = ast.parse(service_source)
    route_assignment = next(
        statement
        for statement in service_module.body
        if isinstance(statement, ast.Assign)
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "_REPORT_ROUTES"
    )

    assert ast.literal_eval(route_assignment.value) == {
        "default": "default",
        "ray": "ray-batch",
    }

    using_call = next(
        node
        for node in ast.walk(service_module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "using"
    )
    assert {keyword.arg: ast.unparse(keyword.value) for keyword in using_call.keywords} == {
        "backend": "backend_alias",
        "queue_name": "queue_name",
    }

    receipt_call = next(
        node
        for node in ast.walk(service_module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create"
    )
    assert {keyword.arg: ast.unparse(keyword.value) for keyword in receipt_call.keywords} == {
        "backend_alias": "enqueued.backend",
        "task_id": "enqueued.id",
    }

    result_section = guide.split("### Refresh a Django `TaskResult`", maxsplit=1)[1].split(
        "### Configure retrievable oversized results", maxsplit=1
    )[0]
    result_sources = re.findall(r"```python\n(.*?)\n```", result_section, re.DOTALL)
    tracked_source = next(source for source in result_sources if "task_backends" in source)
    tracked_module = ast.parse(tracked_source)
    expressions = {ast.unparse(node) for node in ast.walk(tracked_module)}

    assert "task_backends[receipt.backend_alias]" in expressions
    assert "backend.get_result(receipt.task_id)" in expressions
