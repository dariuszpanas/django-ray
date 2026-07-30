from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from locust.env import Environment
from locust.exception import StopTest

import locustfile

ROOT = Path(__file__).parents[2]
LOADTEST_MAKEFILE = ROOT / "mk" / "loadtest.mk"
TESTPROJECT_README = ROOT / "testproject" / "README.md"


class _ConcreteAuthenticatedTaskUser(locustfile.AuthenticatedTaskUser):
    host = "http://localhost:30080"


def _target_body(makefile: str, target: str) -> str:
    body = makefile.split(f"{target}:\n", maxsplit=1)[1]
    return body.split("\n# ", maxsplit=1)[0]


def test_authenticated_task_user_sets_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "locust-test-token"
    monkeypatch.setenv("DJANGO_API_TOKEN", token)
    user = _ConcreteAuthenticatedTaskUser(Environment())

    user.on_start()

    assert locustfile.AuthenticatedTaskUser.abstract is True
    assert user.client.headers["Authorization"] == f"Bearer {token}"


def test_authenticated_task_user_fails_secret_safely_without_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DJANGO_API_TOKEN", raising=False)
    user = _ConcreteAuthenticatedTaskUser(Environment())

    with pytest.raises(Exception, match="DJANGO_API_TOKEN"):
        user.on_start()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert "Authorization" not in captured.out + captured.err


def test_load_start_stops_before_spawning_when_token_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DJANGO_API_TOKEN", raising=False)
    environment = SimpleNamespace(process_exit_code=None)

    with pytest.raises(StopTest) as exc_info:
        locustfile._require_api_token_before_load(environment)

    message = str(exc_info.value)
    captured = capsys.readouterr()
    assert environment.process_exit_code == 2
    assert "DJANGO_API_TOKEN" in message
    assert "Authorization" not in message
    assert captured.out == ""
    assert captured.err == ""


def test_broad_capacity_mix_is_rejected_before_spawning() -> None:
    environment = SimpleNamespace(
        process_exit_code=None,
        user_classes=[
            locustfile.BasicTaskUser,
            locustfile.StressTestUser,
        ],
    )

    with pytest.raises(StopTest, match="StressTestUser must be selected explicitly"):
        locustfile._reject_accidental_broad_capacity_mix(environment)

    assert environment.process_exit_code == 2


@pytest.mark.parametrize(
    "user_class",
    [
        locustfile.BurstTaskUser,
        locustfile.DistributedComputingUser,
        locustfile.StressTestUser,
        locustfile.SustainedLoadUser,
    ],
)
def test_capacity_user_class_can_run_when_selected_alone(
    user_class: type[locustfile.AuthenticatedTaskUser],
) -> None:
    environment = SimpleNamespace(
        process_exit_code=None,
        user_classes=[user_class],
    )

    locustfile._reject_accidental_broad_capacity_mix(environment)

    assert environment.process_exit_code is None
    assert user_class.weight > 0


def test_observability_demo_uses_low_resource_pacing() -> None:
    user = object.__new__(locustfile.ObservabilityDemoUser)

    waits = [locustfile.ObservabilityDemoUser.wait_time(user) for _ in range(25)]

    assert all(2 <= wait <= 4 for wait in waits)


def test_observability_demo_rotates_required_scenarios_deterministically() -> None:
    expected_scenarios = (
        "show_basic_add",
        "show_slow_task",
        "show_priority_task",
        "show_sync_task",
        "show_cluster_search",
        "show_workflow",
        "show_runtime_env",
        "show_ml_inference",
        "show_monitoring",
    )
    user = object.__new__(locustfile.ObservabilityDemoUser)
    observed: list[str] = []
    for scenario_name in expected_scenarios:
        setattr(
            user,
            scenario_name,
            lambda scenario_name=scenario_name: observed.append(scenario_name),
        )
    user._scenario_index = 0
    locust_tasks = [
        task_callable
        for task_callable in locustfile.ObservabilityDemoUser.tasks
        if isinstance(task_callable, Callable)
    ]

    assert locustfile.ObservabilityDemoUser._SCENARIOS == expected_scenarios
    assert len(locust_tasks) == 1

    demo_task = cast(
        Callable[[locustfile.ObservabilityDemoUser], None],
        locust_tasks[0],
    )
    for _ in range(len(expected_scenarios) * 2):
        demo_task(user)

    assert observed == [*expected_scenarios, *expected_scenarios]


@pytest.mark.parametrize(
    ("scenario", "expected_request", "expected_follow"),
    [
        (
            "show_basic_add",
            (
                "/api/enqueue/add/21/21",
                "/api/enqueue/add/[a]/[b]",
                None,
            ),
            ("basic add", 60.0),
        ),
        (
            "show_slow_task",
            (
                "/api/enqueue/slow/1.5",
                "/api/enqueue/slow/[seconds]",
                None,
            ),
            ("slow task", 60.0),
        ),
        (
            "show_priority_task",
            (
                "/api/local/urgent?message=locust-observability-demo",
                "/api/local/urgent",
                None,
            ),
            ("priority task", 60.0),
        ),
        (
            "show_sync_task",
            (
                "/api/sync/calculate?a=42&b=6&operation=divide",
                "/api/sync/calculate",
                None,
            ),
            ("sync task", 60.0),
        ),
        (
            "show_cluster_search",
            (
                "/api/cluster/search",
                None,
                {
                    "pattern": "demo",
                    "data_sources": [
                        "demo-source-a",
                        "other-source",
                        "demo-source-b",
                    ],
                    "case_sensitive": False,
                },
            ),
            ("cluster search", 60.0),
        ),
        (
            "show_workflow",
            (
                "/api/cluster/workflow-benchmark?num_items=3&seconds_per_item=0.25",
                "/api/cluster/workflow-benchmark",
                None,
            ),
            ("tiny workflow", 60.0),
        ),
        (
            "show_runtime_env",
            (
                "/api/cluster/runtime-env/probe?profile=thin",
                "/api/cluster/runtime-env/probe",
                None,
            ),
            ("RuntimeEnv probe", 120.0),
        ),
        (
            "show_ml_inference",
            (
                "/api/ml/inference",
                None,
                {
                    "model_id": "locust-demo-model",
                    "samples": [
                        {
                            "features": [
                                index / 10,
                                (index + 1) / 10,
                            ]
                        }
                        for index in range(12)
                    ],
                },
            ),
            ("ML inference", 60.0),
        ),
    ],
)
def test_observability_demo_routes_and_resource_caps_are_stable(
    scenario: str,
    expected_request: tuple[str, str | None, dict[str, Any] | None],
    expected_follow: tuple[str, float],
) -> None:
    user = object.__new__(locustfile.ObservabilityDemoUser)
    requests: list[tuple[str, str | None, dict[str, Any] | None]] = []
    follows: list[tuple[dict[str, str] | None, str, float]] = []

    def record_post(
        endpoint: str,
        name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        requests.append((endpoint, name, payload))
        return {"task_id": "bounded-demo-task"}

    def record_follow(
        result: dict[str, str] | None,
        *,
        scenario_name: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        follows.append((result, scenario_name, timeout_seconds))

    user._post_task = record_post  # type: ignore[method-assign]
    user._submit_and_follow = record_follow  # type: ignore[method-assign]

    getattr(user, scenario)()

    assert requests == [expected_request]
    assert follows == [
        (
            {"task_id": "bounded-demo-task"},
            expected_follow[0],
            expected_follow[1],
        )
    ]


def test_observability_demo_monitoring_checks_stats_then_metrics() -> None:
    user = object.__new__(locustfile.ObservabilityDemoUser)
    checks: list[str] = []
    user._get_stats = lambda: checks.append("stats")  # type: ignore[method-assign]
    user._get_metrics = lambda: checks.append("metrics")  # type: ignore[method-assign]

    user.show_monitoring()

    assert checks == ["stats", "metrics"]


def test_observability_demo_follows_every_successful_enqueue() -> None:
    user = object.__new__(locustfile.ObservabilityDemoUser)
    user.environment = SimpleNamespace(process_exit_code=None)
    polls: list[tuple[str, str, float]] = []
    user._poll_task_to_terminal = (  # type: ignore[method-assign]
        lambda task_id, *, scenario_name, timeout_seconds: (
            polls.append((task_id, scenario_name, timeout_seconds))
            or {"task_id": task_id, "status": "SUCCEEDED"}
        )
    )

    user._submit_and_follow(
        {"task_id": "demo-task-id"},
        scenario_name="demo family",
        timeout_seconds=12.0,
    )

    assert polls == [("demo-task-id", "demo family", 12.0)]


@pytest.mark.parametrize("enqueue_result", [None, {"task_id": "demo-task-id"}])
def test_observability_demo_stops_before_advancing_after_indeterminate_task(
    enqueue_result: dict[str, str] | None,
) -> None:
    user = object.__new__(locustfile.ObservabilityDemoUser)
    user.environment = SimpleNamespace(process_exit_code=None)
    user._poll_task_to_terminal = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    with pytest.raises(StopTest, match="could not be enqueued|indeterminate result"):
        user._submit_and_follow(
            enqueue_result,
            scenario_name="indeterminate demo",
            timeout_seconds=12.0,
        )

    assert user.environment.process_exit_code == 1


def test_stress_locust_tasks_do_not_shadow_creation_helpers() -> None:
    helper_names = {
        name for name, value in vars(locustfile.TaskCreationMixin).items() if callable(value)
    }
    task_names = {
        task_callable.__name__
        for task_callable in locustfile.StressTestUser.tasks
        if isinstance(task_callable, Callable)
    }

    assert task_names.isdisjoint(helper_names)


def test_loadtest_defaults_to_the_explicit_low_resource_demo() -> None:
    makefile = LOADTEST_MAKEFILE.read_text(encoding="utf-8")
    interactive = _target_body(makefile, "loadtest")
    demo = _target_body(makefile, "loadtest-demo")
    headless = _target_body(makefile, "loadtest-headless")

    assert "LOADTEST_USERS ?= 1" in makefile
    assert "LOADTEST_SPAWN_RATE ?= 1" in makefile
    assert "LOADTEST_CLASSES ?= ObservabilityDemoUser" in makefile
    assert "$(LOADTEST_CLASSES)" in interactive
    assert "-u $(LOADTEST_USERS)" in interactive
    assert "-r $(LOADTEST_SPAWN_RATE)" in interactive
    assert "$(LOADTEST_CLASSES)" in headless
    assert "--headless -u 1 -r 1 -t 300s ObservabilityDemoUser" in demo


def test_powershell_loadtest_example_clears_plaintext_and_encoded_token() -> None:
    readme = TESTPROJECT_README.read_text(encoding="utf-8")
    loadtest_section = readme.split(
        "Load the current Kubernetes secret into the Locust process",
        maxsplit=1,
    )[1]
    powershell = loadtest_section.split("### PowerShell", maxsplit=1)[1].split(
        "For an interactive Locust session",
        maxsplit=1,
    )[0]

    assert powershell.index("try {") < powershell.index("kubectl --context")
    assert "Remove-Item Env:DJANGO_API_TOKEN -ErrorAction SilentlyContinue" in powershell
    assert "Remove-Variable djangoRayEncodedToken -ErrorAction SilentlyContinue" in powershell


@pytest.mark.parametrize(
    ("target", "expected_class"),
    [
        ("loadtest-quick", "BasicTaskUser"),
        ("loadtest-moderate", "SustainedLoadUser"),
        ("loadtest-stress", "StressTestUser"),
    ],
)
def test_capacity_targets_select_one_intended_user_class(
    target: str,
    expected_class: str,
) -> None:
    makefile = LOADTEST_MAKEFILE.read_text(encoding="utf-8")
    recipe = _target_body(makefile, target)

    assert expected_class in recipe
    assert "ObservabilityDemoUser" not in recipe
    assert "$(LOADTEST_CLASSES)" not in recipe


class _Response:
    def __init__(self, payload: dict[str, Any], *, text: str = "") -> None:
        self.status_code = 200
        self._payload = payload
        self.text = text
        self.failures: list[str] = []
        self.successes = 0

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload

    def success(self) -> None:
        self.successes += 1

    def failure(self, message: str) -> None:
        self.failures.append(message)


class _PollingClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls = 0
        self.request_kwargs: list[dict[str, object]] = []

    def get(self, *args: object, **kwargs: object) -> _Response:
        response = self.responses[self.calls]
        self.calls += 1
        self.request_kwargs.append(kwargs)
        return response


def _polling_user(*responses: _Response) -> locustfile.AuthenticatedTaskUser:
    user = object.__new__(locustfile.AuthenticatedTaskUser)
    user.client = _PollingClient(list(responses))  # type: ignore[assignment]
    return user


def test_metrics_request_uses_shared_client_timeout() -> None:
    response = _Response({}, text="# django_ray_tasks_total")
    user = _polling_user(response)

    result = user._get_metrics()

    assert result == "# django_ray_tasks_total"
    assert response.successes == 1
    assert response.failures == []
    assert user.client.request_kwargs == [
        {
            "name": "/api/metrics",
            "catch_response": True,
            "timeout": locustfile._REQUEST_TIMEOUT_SECONDS,
        }
    ]


@pytest.mark.parametrize("terminal_state", ["FAILED", "CANCELLED", "LOST"])
def test_terminal_polling_marks_unsuccessful_states_as_failures(
    terminal_state: str,
) -> None:
    response = _Response({"task_id": "task-id", "status": terminal_state})
    user = _polling_user(response)

    result = user._poll_task_to_terminal(
        "task-id",
        timeout_seconds=1,
        poll_interval_seconds=0,
        scenario_name="demo scenario",
    )

    assert result == {"task_id": "task-id", "status": terminal_state}
    assert response.successes == 0
    assert response.failures
    assert terminal_state in response.failures[0]


def test_terminal_polling_accepts_only_successful_completion() -> None:
    response = _Response({"task_id": "task-id", "status": "SUCCESSFUL"})
    user = _polling_user(response)

    result = user._poll_task_to_terminal(
        "task-id",
        timeout_seconds=1,
        poll_interval_seconds=0,
        scenario_name="demo scenario",
    )

    assert result == {"task_id": "task-id", "status": "SUCCESSFUL"}
    assert response.successes == 1
    assert response.failures == []


def test_terminal_polling_is_bounded_and_records_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _Response({"task_id": "task-id", "status": "RUNNING"})
    second = _Response({"task_id": "task-id", "status": "RUNNING"})
    user = _polling_user(first, second)
    monotonic_values = iter([0.0, 0.0, 0.0, 0.25, 1.0])
    sleeps: list[float] = []
    monkeypatch.setattr(locustfile.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(locustfile.time, "sleep", sleeps.append)

    result = user._poll_task_to_terminal(
        "task-id",
        timeout_seconds=0.5,
        poll_interval_seconds=0.25,
        scenario_name="demo scenario",
    )

    assert result is None
    assert user.client.calls == 2
    assert sleeps == [0.25]
    assert first.successes == 1
    assert second.failures
    assert "terminal state" in second.failures[0].lower()
    assert user.client.request_kwargs[0]["timeout"] == 0.5
    assert user.client.request_kwargs[1]["timeout"] == 0.25


def test_terminal_polling_follows_active_state_to_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = _Response({"task_id": "task-id", "status": "RUNNING"})
    succeeded = _Response({"task_id": "task-id", "status": "SUCCEEDED"})
    user = _polling_user(running, succeeded)
    monkeypatch.setattr(locustfile.time, "sleep", lambda _seconds: None)

    result = user._poll_task_to_terminal(
        "task-id",
        timeout_seconds=1,
        poll_interval_seconds=0,
        scenario_name="demo scenario",
    )

    assert result == {"task_id": "task-id", "status": "SUCCEEDED"}
    assert user.client.calls == 2
    assert running.successes == 1
    assert succeeded.successes == 1


def test_terminal_polling_rejects_mismatched_task_identity() -> None:
    response = _Response({"task_id": "different-task", "status": "SUCCEEDED"})
    user = _polling_user(response)

    result = user._poll_task_to_terminal(
        "task-id",
        timeout_seconds=1,
        poll_interval_seconds=0,
        scenario_name="demo scenario",
    )

    assert result is None
    assert response.successes == 0
    assert response.failures
    assert "mismatched task ID" in response.failures[0]
