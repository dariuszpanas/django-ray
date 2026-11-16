"""Resource-free command contracts for bounded reverse payload inventory."""

import json
from datetime import UTC, datetime
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_ray import input_storage, payload_registry
from django_ray.models import InputPayloadState, TaskInputPayload
from django_ray.result_storage import FilesystemResultStorage


def _reference(index: int) -> str:
    digest = f"{index:064x}"
    return f"resultfs://sha256/{digest}?rel={digest[:2]}/{digest[2:4]}/{digest}.json&bytes=2"


class _RegistryQuery:
    """Return finite synthetic projections without permitting database access."""

    def __init__(self, active_refs, filters=None, projections=None):
        self.active_refs = active_refs
        self.filters = filters or {}
        self.projections = projections if projections is not None else []

    def using(self, _using):
        return self

    def filter(self, **filters):
        return _RegistryQuery(self.active_refs, filters, self.projections)

    def order_by(self, *_fields):
        return self

    def annotate(self, **_annotations):
        return self

    def values_list(self, *fields, flat=False):
        self.projections.append((self.filters, fields))
        if fields == ("bounded_key", "key_complete"):
            return []  # Recent registry uses are not registered-purge candidates.
        if flat:
            assert fields == ("reference",)
            return self.active_refs
        assert fields == ("reference", "state", "digest", "size_bytes")
        reference = next(item for item in self.filters["reference__in"] if item in self.active_refs)
        return [(reference, InputPayloadState.ACTIVE, reference.split("/")[3].split("?")[0], 2)]


def _inventory_command(monkeypatch, *, limit, active_count, present_count, listing_has_more=False):
    active_refs = [_reference(index) for index in range(active_count)]
    query = _RegistryQuery(active_refs)
    monkeypatch.setattr(TaskInputPayload, "objects", query)
    items = [
        payload_registry._RawDiscoveredObject(
            fingerprint_sha256=f"{index:016x}",
            malformed=False,
            digest=f"{index:064x}",
            size_bytes=2,
            native_reference=active_refs[index],
            modified_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
        for index in range(present_count)
    ]
    monkeypatch.setattr(
        payload_registry,
        "_discover_filesystem_objects",
        lambda _storage, *, max_objects: (items, listing_has_more),
    )
    monkeypatch.setattr(
        input_storage,
        "_storage_backend",
        lambda _config: ("filesystem", object.__new__(FilesystemResultStorage)),
    )
    output = StringIO()
    exit_code = 0
    try:
        call_command(
            "django_ray_purge_inputs",
            discover_unregistered=True,
            max_inventory_objects=limit,
            format="json",
            stdout=output,
        )
    except CommandError as error:
        assert str(error) == "Purge pass incomplete; inspect the bounded report."
        exit_code = error.returncode
    return json.loads(output.getvalue()), exit_code, query.projections


@pytest.mark.parametrize("limit", [1, 100, 1000])
def test_reverse_registry_overflow_is_incomplete_with_partial_counts(monkeypatch, limit):
    report, exit_code, _ = _inventory_command(
        monkeypatch, limit=limit, active_count=limit + 1, present_count=limit
    )

    inventory = report["unregistered_inventory"]
    assert exit_code == 2
    assert inventory["has_more"] is True
    assert inventory["counts"]["registered_active"] == limit
    assert inventory["counts"]["registered_missing"] == 0  # The overflow row was not inspected.
    assert report["counts"]["processed"] == 0
    assert report["next_cursor"] is None  # The registered-purge cursor cannot resume inventory.


@pytest.mark.parametrize("limit", [1, 100])
def test_exact_reverse_registry_boundary_can_complete(monkeypatch, limit):
    report, exit_code, _ = _inventory_command(
        monkeypatch, limit=limit, active_count=limit, present_count=limit
    )

    assert exit_code == 0
    assert report["unregistered_inventory"]["has_more"] is False
    assert report["unregistered_inventory"]["counts"]["registered_missing"] == 0


def test_missing_registry_object_inside_bound_remains_visible(monkeypatch):
    report, exit_code, _ = _inventory_command(monkeypatch, limit=2, active_count=2, present_count=1)

    assert exit_code == 2
    assert report["unregistered_inventory"]["has_more"] is False
    assert report["unregistered_inventory"]["counts"]["registered_missing"] == 1


def test_incomplete_backend_listing_skips_reverse_registry_scan(monkeypatch):
    report, exit_code, projections = _inventory_command(
        monkeypatch, limit=1, active_count=2, present_count=1, listing_has_more=True
    )

    assert exit_code == 2
    assert report["unregistered_inventory"]["has_more"] is True
    assert all(fields != ("reference",) for _filters, fields in projections)
