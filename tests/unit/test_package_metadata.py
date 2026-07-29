"""Packaging metadata regression tests."""

import re
import tomllib
from pathlib import Path

from coverage import Coverage
from coverage.results import should_fail_under


def test_coverage_floor_uses_central_two_decimal_precision() -> None:
    """Coverage must enforce the configured floor at its displayed precision."""
    project_root = Path(__file__).parents[2]
    coverage_config = Coverage(config_file=str(project_root / "pyproject.toml"))
    fail_under = coverage_config.get_option("report:fail_under")
    precision = coverage_config.get_option("report:precision")

    assert fail_under == 95
    assert precision == 2
    assert should_fail_under(94.91, fail_under, precision)
    assert not should_fail_under(95.00, fail_under, precision)


def test_readme_images_use_absolute_urls() -> None:
    """PyPI cannot resolve repository-relative image paths in the long description."""
    readme = Path(__file__).parents[2] / "README.md"
    readme_text = readme.read_text(encoding="utf-8")
    image_targets = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", readme_text)
    image_targets.extend(re.findall(r'<img[^>]+src="([^"]+)"', readme_text))

    assert image_targets
    assert all(target.startswith(("https://", "http://")) for target in image_targets)


def test_readme_logo_uses_pypi_compatible_absolute_url() -> None:
    """The package logo must use an absolute URL so PyPI can proxy it."""
    readme = Path(__file__).parents[2] / "README.md"
    readme_text = readme.read_text(encoding="utf-8")

    assert (
        "https://raw.githubusercontent.com/dariuszpanas/django-ray/main/"
        "docs/assets/images/django-ray.svg"
    ) in readme_text


def test_admin_observability_assets_are_inside_the_wheel_package() -> None:
    """Hatch includes the admin template and assets through the package selector."""
    project_root = Path(__file__).parents[2]
    config = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]

    assert packages == ["src/django_ray"]
    package_root = project_root / packages[0]
    expected_assets = [
        package_root
        / "templates"
        / "admin"
        / "django_ray"
        / "raytaskexecution"
        / "change_form.html",
        package_root / "static" / "django_ray" / "admin" / "task_live.css",
        package_root / "static" / "django_ray" / "admin" / "task_live.js",
    ]
    assert all(asset.is_file() and asset.is_relative_to(package_root) for asset in expected_assets)

    stylesheet = expected_assets[1].read_text(encoding="utf-8")
    assert "#django-ray-live-observability" in stylesheet
    assert "grid-template-columns" in stylesheet
    assert ":focus-visible" in stylesheet
    assert 'html[data-theme="dark"]' in stylesheet
    assert 'html[data-theme="auto"]' in stylesheet
    assert "@media (prefers-color-scheme: dark)" in stylesheet
    assert "@media (max-width: 640px)" in stylesheet

    script = expected_assets[2].read_text(encoding="utf-8")
    assert "setTimeout(refresh, 3000)" in script
    assert "document.hidden" in script
    assert 'credentials: "same-origin"' in script
    assert "textContent" in script
    assert "stateNode.dataset.state = state" in script
    assert "innerHTML" not in script
    assert all(state in script for state in ("SUCCEEDED", "FAILED", "CANCELLED", "LOST"))


def test_unfold_is_testproject_only_and_reproducibly_pinned() -> None:
    """The modern sample admin must not become a required package dependency."""
    project_root = Path(__file__).parents[2]
    config = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    unfold_requirement = "django-unfold==0.102.0"

    assert all("django-unfold" not in requirement for requirement in project["dependencies"])
    assert unfold_requirement in project["optional-dependencies"]["sample"]
    assert unfold_requirement in config["dependency-groups"]["dev"]
    assert all(
        all("django-unfold" not in requirement for requirement in requirements)
        for extra, requirements in project["optional-dependencies"].items()
        if extra != "sample"
    )
