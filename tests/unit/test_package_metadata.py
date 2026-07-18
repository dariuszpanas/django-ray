"""Packaging metadata regression tests."""

import re
from pathlib import Path


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
