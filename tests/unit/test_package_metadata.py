"""Packaging metadata regression tests."""

import re
from pathlib import Path


def test_readme_images_use_absolute_urls() -> None:
    """PyPI cannot resolve repository-relative image paths in the long description."""
    readme = Path(__file__).parents[2] / "README.md"
    image_targets = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", readme.read_text(encoding="utf-8"))

    assert image_targets
    assert all(target.startswith(("https://", "http://")) for target in image_targets)
