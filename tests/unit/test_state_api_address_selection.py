"""Selected State API addresses retain priority without starting Ray or HTTP."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from django_ray import observability


@pytest.fixture(autouse=True)
def no_external_work(monkeypatch):
    import socket

    import ray
    from ray.dashboard.modules.dashboard_sdk import SubmissionClient

    def refuse(*args, **kwargs):
        raise AssertionError("State address unit tests must not start Ray or use HTTP")

    monkeypatch.setattr(ray, "init", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(SubmissionClient, "_do_request", refuse)
    monkeypatch.setattr(SubmissionClient, "_check_connection_and_version", refuse)


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    "ambient",
    [
        {"RAY_ADDRESS": "http://ambient:8265"},
        {"RAY_API_SERVER_ADDRESS": "http://ambient:8265"},
        {
            "RAY_ADDRESS": "http://other-ambient:8265",
            "RAY_API_SERVER_ADDRESS": "http://ambient:8265",
        },
    ],
)
def test_selected_dashboard_wins_over_sdk_ambient_address(
    monkeypatch, settings, explicit, ambient
) -> None:
    from ray.dashboard import utils
    from ray.dashboard.modules.dashboard_sdk import SubmissionClient
    from ray.util.state import StateApiClient
    from ray.util.state.common import GetApiOptions, StateResource

    settings.DJANGO_RAY = {
        "RAY_STATE_API_ADDRESS": "http://configured:8265" if explicit else "http://selected:8265",
        "RAY_STATE_API_TIMEOUT_SECONDS": 5,
    }
    # Keep the SDK's resolver body intact, using only synthetic ambient values.
    monkeypatch.setattr(utils, "os", SimpleNamespace(environ=ambient.copy()))
    initialized = []
    queries = []

    def initialize(client, **kwargs):
        initialized.append((client, kwargs))

    def get(client, resource, task_id, options, *, _explain):
        queries.append((client, resource, task_id, options, _explain))
        return {"task_id": task_id, "state": "RUNNING"}

    monkeypatch.setattr(SubmissionClient, "__init__", initialize)
    monkeypatch.setattr(StateApiClient, "get", get)

    result = observability.get_ray_task_state(
        "task-1", address="http://selected:8265" if explicit else None
    )

    assert result == [{"task_id": "task-1", "state": "RUNNING"}]
    assert len(initialized) == len(queries) == 1
    client, kwargs = initialized[0]
    assert isinstance(client, StateApiClient)
    assert kwargs == {
        "address": "http://selected:8265",
        "create_cluster_if_needed": False,
        "headers": {"Content-Type": "application/json"},
        "cookies": None,
    }
    assert queries[0] == (client, StateResource.TASKS, "task-1", GetApiOptions(timeout=5), False)
    assert utils.os.environ == ambient


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_selected_client_preserves_sdk_base_initialization(monkeypatch, scheme) -> None:
    from ray.dashboard.modules import dashboard_sdk
    from ray.util.state import StateApiClient

    monkeypatch.setattr(
        dashboard_sdk,
        "get_auth_headers_if_auth_enabled",
        lambda headers: {"X-Synthetic-Auth": "test-only"},
    )

    client = observability._address_pinned_state_client(f"{scheme}://selected:8265/")

    assert isinstance(client, StateApiClient)
    assert client._address == f"{scheme}://selected:8265"
    assert client._headers == {
        "Content-Type": "application/json",
        "X-Synthetic-Auth": "test-only",
    }
    assert client._cookies is None
    assert client._default_metadata == {}
    assert client._verify is True
    assert client._ssl_context is None
    assert client._server_ray_version is None


@pytest.mark.parametrize("selected", [None, ""])
def test_discovery_sentinels_keep_public_sdk_query(monkeypatch, settings, selected) -> None:
    import ray
    from ray.util import state

    settings.DJANGO_RAY = (
        {"RAY_STATE_API_ADDRESS": "http://configured:8265"} if selected == "" else {}
    )
    monkeypatch.setattr(ray, "is_initialized", lambda: True)
    calls = []

    def get_task(**kwargs):
        calls.append(kwargs)
        return None

    def refuse(address):
        raise AssertionError("discovery must keep the public SDK path")

    monkeypatch.setattr(state, "get_task", get_task)
    monkeypatch.setattr(observability, "_address_pinned_state_client", refuse)

    assert observability.get_ray_task_state("task-1", address=selected) == []
    assert calls == [{"id": "task-1", "address": selected, "timeout": 5}]


def test_selected_query_uses_initialized_sdk_get_and_filters(monkeypatch, settings) -> None:
    from ray.dashboard.modules import dashboard_sdk
    from ray.util.state import StateApiClient
    from ray.util.state.common import GetApiOptions

    settings.DJANGO_RAY = {"RAY_STATE_API_ADDRESS": "http://selected:8265"}
    monkeypatch.setattr(dashboard_sdk, "get_auth_headers_if_auth_enabled", lambda headers: {})
    requests = []

    def request(client, **kwargs):
        requests.append((client._address, client._headers, kwargs))
        return {"result": []}

    monkeypatch.setattr(StateApiClient, "_make_http_get_request", request)

    assert observability.get_ray_task_state("task-1") == []
    options = GetApiOptions(timeout=5)
    assert requests == [
        (
            "http://selected:8265",
            {"Content-Type": "application/json"},
            {
                "endpoint": "/api/v0/tasks",
                "params": {
                    "timeout": options.timeout,
                    "server_timeout_multiplier": options.server_timeout_multiplier,
                    "filter_keys": ["task_id"],
                    "filter_predicates": ["="],
                    "filter_values": ["task-1"],
                    "detail": True,
                },
                "timeout": options.timeout,
                "_explain": False,
            },
        )
    ]


@pytest.mark.parametrize("selected", ["ray://selected:10001", "selected:6379", "auto"])
def test_non_http_selection_uses_existing_independent_resolver(monkeypatch, selected) -> None:
    from ray.dashboard.modules.dashboard_sdk import SubmissionClient

    from django_ray.runner import ray_job

    resolved = []
    initialized = []

    def resolve(address):
        resolved.append(address)
        return "http://resolved:8265"

    monkeypatch.setattr(ray_job, "_resolve_submission_address", resolve)
    monkeypatch.setattr(
        SubmissionClient, "__init__", lambda client, **kwargs: initialized.append(kwargs)
    )

    observability._address_pinned_state_client(selected)

    assert resolved == [selected]
    assert initialized[0]["address"] == "http://resolved:8265"


def test_selected_query_keeps_sdk_task_reference_normalization(monkeypatch, settings) -> None:
    from ray.util.state import StateApiClient

    settings.DJANGO_RAY = {"RAY_STATE_API_ADDRESS": "http://selected:8265"}
    calls = []
    client = StateApiClient.__new__(StateApiClient)
    monkeypatch.setattr(observability, "_address_pinned_state_client", lambda address: client)
    monkeypatch.setattr(
        client, "get", lambda resource, task_id, options, **kwargs: calls.append(task_id)
    )
    reference = SimpleNamespace(task_id=lambda: SimpleNamespace(hex=lambda: "reference-task"))

    assert observability.get_ray_task_state(reference) == []
    assert calls == ["reference-task"]


def test_missing_sdk_http_dependency_refuses_before_resolution(monkeypatch, settings) -> None:
    from ray.util.state import api as state_api

    from django_ray.runner import ray_job

    settings.DJANGO_RAY = {"RAY_STATE_API_ADDRESS": "http://selected:8265"}
    monkeypatch.setattr(state_api, "requests", None)

    def refuse(address):
        raise AssertionError("missing dependency must refuse before discovery")

    monkeypatch.setattr(ray_job, "_resolve_submission_address", refuse)

    with pytest.raises(
        observability.WorkflowObservabilityError, match="Ray State API is unavailable"
    ) as error:
        observability.get_ray_task_state("task-1")
    assert isinstance(error.value.__cause__, RuntimeError)
    assert "ray[default]" in str(error.value.__cause__)
