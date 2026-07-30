"""Contracts for the automatic Zensical documentation palettes."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]
CONFIG_PATH = ROOT / "zensical.toml"
STYLESHEET_PATH = ROOT / "docs/stylesheets/extra.css"


def _contrast_ratio(foreground: str, background: str) -> float:
    def relative_luminance(color: str) -> float:
        channels = [int(color[index : index + 2], 16) / 255 for index in range(1, len(color), 2)]
        linear = [
            channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    light, dark = sorted(
        (relative_luminance(foreground), relative_luminance(background)),
        reverse=True,
    )
    return (light + 0.05) / (dark + 0.05)


def _css_block(stylesheet: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]+)\}}", stylesheet)
    assert match is not None
    return match.group("body")


def test_docs_offer_accessible_automatic_light_and_dark_controls() -> None:
    config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    theme = config["project"]["theme"]

    assert theme["logo"] == "assets/images/django-ray.svg"
    assert theme["favicon"] == "assets/images/django-ray.svg"
    assert theme["palette"] == [
        {
            "media": "(prefers-color-scheme)",
            "toggle": {
                "icon": "lucide/sun-moon",
                "name": "Switch to light mode",
            },
        },
        {
            "media": "(prefers-color-scheme: light)",
            "scheme": "default",
            "primary": "custom",
            "accent": "custom",
            "toggle": {
                "icon": "lucide/sun",
                "name": "Switch to dark mode",
            },
        },
        {
            "media": "(prefers-color-scheme: dark)",
            "scheme": "slate",
            "primary": "custom",
            "accent": "custom",
            "toggle": {
                "icon": "lucide/moon",
                "name": "Switch to system preference",
            },
        },
    ]


def test_docs_dark_palette_keeps_neutrals_and_reserves_blue_for_accents() -> None:
    stylesheet = STYLESHEET_PATH.read_text(encoding="utf-8")
    light = _css_block(stylesheet, '[data-md-color-scheme="default"]')
    dark = _css_block(stylesheet, '[data-md-color-scheme="slate"]')

    assert "--md-typeset-a-color: #0369a1;" in light
    assert {
        "--django-ray-docs-backdrop: #0b0c0f;",
        "--django-ray-docs-canvas: #16171a;",
        "--django-ray-docs-surface: #212226;",
        "--django-ray-docs-border: #303238;",
        "--django-ray-docs-heading: #f4f4f5;",
        "--django-ray-docs-muted: #a1a1aa;",
        "--django-ray-docs-accent: #38bdf8;",
        "--color-backdrop: 11 12 15;",
        "--color-background: 22 23 26;",
        "--color-background-subtle: 33 34 38;",
        "--md-code-bg-color: var(--django-ray-docs-surface);",
        "--md-typeset-table-color: var(--django-ray-docs-border);",
        "--md-admonition-bg-color: var(--django-ray-docs-surface);",
    } <= {declaration.strip() + ";" for declaration in dark.split(";") if declaration.strip()}

    for selector in (
        '[data-md-color-scheme="slate"] .md-header',
        '[data-md-color-scheme="slate"] .md-typeset table:not([class])',
        '[data-md-color-scheme="slate"] .md-typeset .mermaid',
    ):
        assert _css_block(stylesheet, selector)
    assert '[data-md-color-scheme="slate"] .md-nav__link--active' in stylesheet
    assert '[data-md-color-scheme="slate"] .md-typeset .admonition,' in stylesheet
    assert '[data-md-color-scheme="slate"] .md-search__button' in stylesheet
    assert '[data-md-color-scheme="slate"] :focus-visible' in stylesheet

    assert _contrast_ratio("#0369a1", "#ffffff") >= 4.5
    for foreground in ("#38bdf8", "#a1a1aa", "#f4f4f5"):
        assert _contrast_ratio(foreground, "#16171a") >= 4.5


def test_docs_keep_navigation_clear_of_fixed_hosted_footer() -> None:
    stylesheet = STYLESHEET_PATH.read_text(encoding="utf-8")
    root = _css_block(stylesheet, ":root")

    assert "--django-ray-docs-fixed-footer-clearance: 3.5rem;" in root
    assert "scroll-padding-bottom: var(--django-ray-docs-fixed-footer-clearance);" in _css_block(
        stylesheet, ".md-sidebar__scrollwrap"
    )
    for selector in (
        ".md-sidebar--primary .md-sidebar__inner",
        ".md-sidebar--secondary .md-sidebar__inner",
    ):
        assert "padding-bottom: var(--django-ray-docs-fixed-footer-clearance);" in _css_block(
            stylesheet, selector
        )
    assert "padding-bottom: var(--django-ray-docs-fixed-footer-clearance);" in _css_block(
        stylesheet, ".md-footer"
    )
