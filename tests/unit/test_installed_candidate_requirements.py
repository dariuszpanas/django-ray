"""The artifact audit must scan the actual resolver result without the local candidate."""

import pytest

from scripts.installed_candidate_requirements import requirements


def test_candidate_is_the_only_excluded_distribution():
    assert (
        requirements(
            [
                {"name": "django_ray", "version": "0.7.0"},
                {"name": "Ray", "version": "2.58.0"},
                {"name": "some_dependency", "version": "1.2.0"},
            ],
            candidate_version="0.7.0",
        )
        == "ray==2.58.0\nsome-dependency==1.2.0\n"
    )


@pytest.mark.parametrize(
    "inventory",
    [
        [],
        {},
        [{"name": "ray", "version": "2.58.0"}],
        [{"name": "django-ray", "version": "0.6.0"}],
        [{"name": "django-ray", "version": "0.7.0"}],
        [{"name": "django-ray", "version": "0.7.0", "extra": True}],
        [{"name": "ray\nother-package", "version": "2.58.0"}],
        [{"name": "ray", "version": "2.*"}],
        [{"name": "ray", "version": 2}],
        [{"name": "Ray", "version": "2.58.0"}, {"name": "ray", "version": "2.58.0"}],
        [{"name": "ray", "version": "2.58.0"}] * 513,
    ],
)
def test_incomplete_ambiguous_or_unpinned_inventory_is_refused(inventory):
    with pytest.raises(ValueError):
        requirements(inventory, candidate_version="0.7.0")
