"""Resource-free application admission tests, repeated on PostgreSQL in CI."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.utils import timezone

from django_ray.models import RayTaskExecution, TaskInputPayload, TaskState
from testproject import tasks
from testproject.admission import SampleAdmissionError, enqueue_sample
from testproject.models import SampleAdmissionBudget

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(
    autouse=True, params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)]
)
def budget(request):
    if connection.vendor != request.param:
        pytest.skip(f"requires {request.param}")
    # TransactionTestCase flush removes seed data; deployment migrations seed it.
    SampleAdmissionBudget.objects.update_or_create(
        pk=1,
        defaults={"window_started_at": timezone.now(), "request_count": 0},
    )


@pytest.fixture
def operator(settings):
    return Client(HTTP_AUTHORIZATION=f"Bearer {settings.DJANGO_API_TOKEN}")


def test_concurrent_connections_cannot_exceed_last_work_slot(settings):
    settings.SAMPLE_MAX_OUTSTANDING_EXECUTIONS = 1
    ready = Barrier(4)

    def submit(index):
        close_old_connections()
        try:
            ready.wait(timeout=10)
            enqueue_sample(tasks.add_numbers, index, 1)
            return 200
        except SampleAdmissionError as error:
            return error.status
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(submit, range(4)))
    # SQLite may reject all simultaneous write contenders; retry then succeeds.
    if 200 not in statuses:
        enqueue_sample(tasks.add_numbers, 1, 1)
    assert statuses.count(200) <= 1
    assert set(statuses) <= {200, 503}
    assert RayTaskExecution.objects.count() == 1
    assert SampleAdmissionBudget.objects.get(pk=1).request_count == 1


def test_request_window_and_outstanding_budget_are_shared(settings, operator):
    settings.SAMPLE_MAX_REQUESTS_PER_WINDOW = 2
    for _ in range(2):
        assert operator.post("/api/enqueue/add/1/2").status_code == 200
    RayTaskExecution.objects.update(state=TaskState.SUCCEEDED)
    other_worker = Client(HTTP_AUTHORIZATION=f"Bearer {settings.DJANGO_API_TOKEN}")
    response = other_worker.post("/api/enqueue/multiply/2/3")
    assert response.status_code == 429
    assert response["Retry-After"] == "60"
    assert response.json() == {
        "code": "ADMISSION_LIMITED",
        "message": "Sample admission is temporarily unavailable.",
    }
    assert RayTaskExecution.objects.count() == 2
    SampleAdmissionBudget.objects.update(window_started_at=timezone.now() - timedelta(seconds=61))
    assert other_worker.post("/api/enqueue/multiply/2/3").status_code == 200
    assert SampleAdmissionBudget.objects.get(pk=1).request_count == 1


def test_capacity_refusal_keeps_cancellation_available(settings, operator):
    settings.SAMPLE_MAX_OUTSTANDING_EXECUTIONS = 1
    first = operator.post("/api/enqueue/add/1/2")
    assert first.status_code == 200
    response = operator.post("/api/enqueue/add/2/3")
    assert response.status_code == 503
    assert response["Retry-After"] == "5"
    assert response.json()["code"] == "ADMISSION_UNAVAILABLE"
    execution = RayTaskExecution.objects.get()
    assert operator.post(f"/api/executions/{execution.pk}/cancel").status_code == 202
    execution.refresh_from_db()
    assert execution.state == TaskState.CANCELLED
    assert operator.post("/api/enqueue/add/2/3").status_code == 200


def test_missing_budget_fails_closed_without_enqueuing(operator):
    SampleAdmissionBudget.objects.all().delete()
    response = operator.post("/api/enqueue/add/1/2")
    assert response.status_code == 503
    assert response["Retry-After"] == "5"
    assert not RayTaskExecution.objects.exists()


def test_enqueue_failure_rolls_back_budget(monkeypatch):
    backend = tasks.add_numbers.get_backend()

    def fail(*args, **kwargs):
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(backend, "enqueue", fail)
    with pytest.raises(RuntimeError, match="enqueue failed"):
        enqueue_sample(tasks.add_numbers, 1, 2)
    assert SampleAdmissionBudget.objects.get(pk=1).request_count == 0
    assert not RayTaskExecution.objects.exists()


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/enqueue/slow/NaN", None),
        ("/api/enqueue/slow/301", None),
        ("/api/local/fibonacci/10001", None),
        ("/api/enqueue/add/1000000001/1", None),
        ("/api/cluster/process-chunk", {"data": list(range(101))}),
        ("/api/cluster/process-chunk", {"data": [1], "chunk_id": True}),
        ("/api/cluster/batch-http", {"urls": ["x"], "timeout_seconds": True}),
        ("/api/ml/train", {"dataset_id": "data", "epochs": True}),
        ("/api/ml/train", {"dataset_id": "data", "epochs": 101}),
        ("/api/ml/inference", {"model_id": "model", "samples": [{"x": float("inf")}]}),
        ("/api/local/urgent?message=" + "x" * 2049, None),
    ],
)
def test_invalid_input_has_no_execution_or_admission_effect(operator, path, payload):
    response = operator.post(path, data=payload, content_type="application/json")
    assert response.status_code == 422
    assert not RayTaskExecution.objects.exists()
    assert not TaskInputPayload.objects.exists()
    assert SampleAdmissionBudget.objects.get(pk=1).request_count == 0


@pytest.mark.parametrize("path", ["/api/enqueue/add/1/2", "/api/cluster/process-chunk"])
@pytest.mark.parametrize(
    "body",
    [b" " * 65537, b'{"data":' + b"[" * 10 + b"0" + b"]" * 10 + b"}"],
    ids=["oversized", "too-deep"],
)
def test_byte_and_depth_limits_precede_enqueue(operator, path, body):
    assert operator.post(path, data=body, content_type="application/json").status_code == 422
    assert not RayTaskExecution.objects.exists()


def test_metrics_demo_and_operator_authorities_do_not_overlap(settings, operator):
    settings.DJANGO_METRICS_TOKEN = "metrics-credential"
    settings.DJANGO_DEMO_TOKEN = "demo-credential"
    settings.DJANGO_DEMO_WORKLOADS_ENABLED = False
    metrics = Client(HTTP_AUTHORIZATION="Bearer metrics-credential")
    demo = Client(HTTP_AUTHORIZATION="Bearer demo-credential")
    assert metrics.get("/api/metrics").status_code == 200
    for client in (metrics, demo):
        assert client.get("/api/executions").status_code == 401
        assert client.post("/api/enqueue/add/1/2").status_code == 401
        assert client.post("/api/executions/1/cancel").status_code == 401
        assert client.post("/api/executions/1/retry").status_code == 401
    for client in (operator, metrics, demo):
        assert client.post("/api/stress/cpu?duration_seconds=0").status_code == 401
    schema = operator.get("/api/openapi.json").json()
    assert "/api/stress/cpu" not in schema["paths"]
    settings.DEPLOYMENT_MODE = "demo"
    settings.DJANGO_DEMO_WORKLOADS_ENABLED = True
    assert operator.post("/api/stress/cpu?duration_seconds=0").status_code == 401
    assert demo.post("/api/stress/cpu?duration_seconds=0").status_code == 200
    assert "/api/stress/cpu" in operator.get("/api/openapi.json").json()["paths"]
    settings.DEPLOYMENT_MODE = "production"
    assert demo.post("/api/stress/cpu?duration_seconds=0").status_code == 401
    settings.DJANGO_API_ENABLED = False
    assert operator.post("/api/enqueue/add/1/2").status_code == 401
    assert metrics.get("/api/metrics").status_code == 200


def test_accepted_boundary_and_excessive_grid_product(settings, operator):
    assert (
        operator.post(
            "/api/cluster/process-chunk",
            data={"data": list(range(100))},
            content_type="application/json",
        ).status_code
        == 200
    )
    settings.DEPLOYMENT_MODE = "demo"
    settings.DJANGO_DEMO_WORKLOADS_ENABLED = True
    settings.DJANGO_DEMO_TOKEN = "demo-credential"
    demo = Client(HTTP_AUTHORIZATION="Bearer demo-credential")
    for size, expected in ((10, 200), (11, 422)):
        response = demo.post(
            "/api/ml/hyperparam-search",
            data={
                "dataset_id": "data",
                "param_grid": {"a": list(range(size)), "b": list(range(10))},
            },
            content_type="application/json",
        )
        assert response.status_code == expected
    assert RayTaskExecution.objects.count() == 2


@pytest.mark.parametrize(
    "setting", ["SAMPLE_MAX_REQUESTS_PER_WINDOW", "SAMPLE_MAX_OUTSTANDING_EXECUTIONS"]
)
@pytest.mark.parametrize("value", [True, 0, -1, 100, float("nan"), "2"])
def test_invalid_budget_configuration_fails_closed(settings, operator, setting, value):
    setattr(settings, setting, value)
    response = operator.post("/api/enqueue/add/1/2")
    assert response.status_code == 503
    assert response["Retry-After"] == "5"
    assert not RayTaskExecution.objects.exists()


def test_clock_correction_does_not_strand_request_window(settings, operator):
    settings.SAMPLE_MAX_REQUESTS_PER_WINDOW = 1
    SampleAdmissionBudget.objects.update(
        window_started_at=timezone.now() + timedelta(days=1), request_count=1
    )
    assert operator.post("/api/enqueue/add/1/2").status_code == 200
    assert SampleAdmissionBudget.objects.get().window_started_at <= timezone.now()


def test_database_router_cannot_split_budget_from_execution(monkeypatch, operator):
    from django.db import router

    original = router.db_for_write
    monkeypatch.setattr(
        router,
        "db_for_write",
        lambda model, **hints: (
            "other" if model is SampleAdmissionBudget else original(model, **hints)
        ),
    )
    assert operator.post("/api/enqueue/add/1/2").status_code == 503
    assert not RayTaskExecution.objects.exists()


def test_completed_sync_work_still_consumes_request_budget(settings, operator):
    settings.SAMPLE_MAX_REQUESTS_PER_WINDOW = 1
    settings.TASKS = {
        **settings.TASKS,
        "default": {
            "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
            "QUEUES": ["default", "sync"],
        },
    }
    assert operator.post("/api/enqueue/add/1/2").status_code == 200
    assert not RayTaskExecution.objects.exists()
    assert operator.post("/api/enqueue/add/1/2").status_code == 429


def test_retry_budget_refusal_keeps_execution_generation_unchanged(settings, operator):
    settings.SAMPLE_MAX_REQUESTS_PER_WINDOW = 1
    assert operator.post("/api/enqueue/add/1/2").status_code == 200
    execution = RayTaskExecution.objects.get()
    execution.state = TaskState.FAILED
    execution.save(update_fields=["state"])
    identity = (execution.attempt_number, execution.execution_generation)
    response = operator.post(f"/api/executions/{execution.pk}/retry")
    assert response.status_code == 429
    execution.refresh_from_db()
    assert (execution.attempt_number, execution.execution_generation) == identity
    assert execution.state == TaskState.FAILED


def test_mounted_api_preserves_demo_authority(settings, operator):
    settings.DEPLOYMENT_MODE = "demo"
    settings.DJANGO_DEMO_WORKLOADS_ENABLED = True
    settings.DJANGO_DEMO_TOKEN = "demo-credential"
    path = "/api/stress/cpu?duration_seconds=0"
    assert operator.post(path, SCRIPT_NAME="/mounted").status_code == 401
    demo = Client(HTTP_AUTHORIZATION="Bearer demo-credential")
    assert demo.post(path, SCRIPT_NAME="/mounted").status_code == 200
    assert operator.post("/api/stress/cpu/", SCRIPT_NAME="/mounted").status_code == 404
    assert operator.post("/api/stress/%63pu", SCRIPT_NAME="/mounted").status_code in {401, 404}
    settings.DJANGO_DEMO_WORKLOADS_ENABLED = False
    assert all(
        "/stress/" not in path
        for path in operator.get("/api/openapi.json", SCRIPT_NAME="/mounted").json()["paths"]
    )
