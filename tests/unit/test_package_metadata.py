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
        package_root / "static" / "django_ray" / "admin" / "workflow_diagnostics.js",
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
    assert "--django-ray-live-action-bg: #e0f2fe;" in stylesheet
    assert "--django-ray-live-action-fg: #075985;" in stylesheet
    assert "--django-ray-live-action-hover-fg: #0c4a6e;" in stylesheet
    assert "--django-ray-live-state-neutral-fg: inherit;" in stylesheet
    assert "--django-ray-live-status-error-bg: #fef2f2;" in stylesheet
    assert "--django-ray-live-status-error-border: #ef4444;" in stylesheet
    assert "--django-ray-live-workflow-body-bg: transparent;" in stylesheet
    for neutral_dark_token in (
        "--django-ray-live-bg: #16171a;",
        "--django-ray-live-header-bg: #0b0c0f;",
        "--django-ray-live-surface-bg: #212226;",
        "--django-ray-live-border: #303238;",
        "--django-ray-live-heading: #f4f4f5;",
        "--django-ray-live-muted: #a1a1aa;",
        "--django-ray-live-accent: #38bdf8;",
    ):
        assert neutral_dark_token in stylesheet
    assert "background: #0c4a6e;" not in stylesheet
    assert "background: #075985;" not in stylesheet

    summary_arrow_rule = re.search(
        r"\.django-ray-workflow__summary-arrow\s*\{(?P<body>[^}]*)\}",
        stylesheet,
    )
    assert summary_arrow_rule is not None
    assert "color: var(--django-ray-live-accent-strong);" in summary_arrow_rule.group("body")

    action_hover_rule = re.search(
        r":is\(\s*\.django-ray-workflow__actions a,\s*"
        r"\.django-ray-workflow__copy\s*\):hover\s*\{(?P<body>[^}]*)\}",
        stylesheet,
    )
    assert action_hover_rule is not None
    assert "color: var(--django-ray-live-action-hover-fg);" in action_hover_rule.group("body")

    explicit_dark_rule = re.search(
        r":is\(html\.dark, html\[data-theme=\"dark\"\]\) "
        r"#django-ray-live-observability\s*\{(?P<body>[^}]*)\}",
        stylesheet,
    )
    auto_dark_rule = re.search(
        r"@media \(prefers-color-scheme: dark\)\s*\{\s*"
        r"html\[data-theme=\"auto\"\] #django-ray-live-observability\s*"
        r"\{(?P<body>[^}]*)\}",
        stylesheet,
    )
    assert explicit_dark_rule is not None
    assert auto_dark_rule is not None
    variable_pattern = r"(--django-ray-live-[\w-]+):\s*([^;]+);"
    explicit_dark_variables = dict(re.findall(variable_pattern, explicit_dark_rule.group("body")))
    auto_dark_variables = dict(re.findall(variable_pattern, auto_dark_rule.group("body")))
    assert len(explicit_dark_variables) >= 40
    assert auto_dark_variables == explicit_dark_variables

    script = expected_assets[2].read_text(encoding="utf-8")
    assert "setTimeout(refresh, 3000)" in script
    assert "document.hidden" in script
    assert 'credentials: "same-origin"' in script
    assert "textContent" in script
    assert "stateNode.dataset.state = state" in script
    assert "innerHTML" not in script
    assert all(state in script for state in ("SUCCEEDED", "FAILED", "CANCELLED", "LOST"))

    diagnostics_script = expected_assets[3].read_text(encoding="utf-8")
    assert "django-ray-workflow-diagnostics" in diagnostics_script
    assert 'credentials: "same-origin"' in diagnostics_script
    assert 'cache: "no-store"' in diagnostics_script
    assert "textContent" in diagnostics_script
    assert "innerHTML" not in diagnostics_script


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
