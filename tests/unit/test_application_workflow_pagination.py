"""Paginated oversized detail must remain finite and bound to one publication."""

import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from qualification.application.workflow_pagination import read_display_limit_pages

TASK_ID = "00000000-0000-4000-8000-000000000001"
IDENTITY = {"schema_version": 1, "run_id": TASK_ID, "attempt_number": 1, "execution_generation": 1}
PUBLICATION = {"summary_revision": 1, "topology_version": 1, "detail_revision": 1}


def page(count, cursor=None):
    return {
        "schema": "django-ray.workflow-progress-page",
        "schema_version": 1,
        "task_id": TASK_ID,
        "run_identity": dict(IDENTITY),
        "publication": dict(PUBLICATION),
        "availability": "AVAILABLE",
        "complete": True,
        "collection": "topology_nodes",
        "returned_count": count,
        "items": [{"node_id": str(i)} for i in range(count)],
        "next_cursor": cursor,
    }


def read(pages):
    request = Mock(side_effect=[(200, json.dumps(p).encode()) for p in pages])
    return request, lambda: read_display_limit_pages(
        request,
        task_id=TASK_ID,
        run_identity=IDENTITY,
        publication=PUBLICATION,
        token="private-credential",
        collection="topology_nodes",
    )


def test_pages_use_pinned_attempt_and_opaque_escaped_cursor():
    cursor = "opaque&attempt_number=99/+"
    request, run = read([page(100, cursor), page(1)])
    assert len(run()) == 101
    query = parse_qs(urlsplit(request.call_args_list[1].args[0]).query)
    assert query == {"limit": ["100"], "attempt_number": ["1"], "cursor": [cursor]}
    for call in request.call_args_list:
        assert call.kwargs["response_limit"] == 256 * 1024
        assert call.kwargs["headers"] == {"Authorization": "Bearer private-credential"}


@pytest.mark.parametrize(
    "fault", ["attempt", "publication", "count", "empty", "early-end", "too-many"]
)
def test_rejects_wrong_or_incomplete_pages(fault):
    value = page(100, "next")
    if fault == "attempt":
        value["run_identity"]["attempt_number"] = 2
    elif fault == "publication":
        value["publication"]["detail_revision"] = 2
    elif fault == "count":
        value["returned_count"] = True
    elif fault == "empty":
        value["items"] = []
        value["returned_count"] = 0
    elif fault == "early-end":
        value["next_cursor"] = None
    else:
        value["items"] *= 2
        value["returned_count"] = 200
    request, run = read([value])
    with pytest.raises(ValueError):
        run()
    assert request.call_count == 1


def test_repeated_cursor_stops_without_another_request():
    request, run = read([page(20, "same"), page(20, "same")])
    with pytest.raises(ValueError, match="repeated cursor"):
        run()
    assert request.call_count == 2


def test_small_pages_cannot_exceed_fixed_request_budget():
    request, run = read([page(20, str(i)) for i in range(4)])
    with pytest.raises(ValueError, match="request budget"):
        run()
    assert request.call_count == 4
