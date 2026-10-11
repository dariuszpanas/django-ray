"""Fixed result presentation, transport boundaries and owned fixture cleanup."""

import html
import json
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session

from django_ray.models import RayTaskExecution, TaskAttempt
from qualification.application import result_presentation as results

TOKEN = "ordinary-result-qualification-token-123456789"


def projected(scope):
    return {"markup": results.fixture_payload(scope)["markup"], "<redacted>": "[REDACTED]"}


@pytest.mark.parametrize("scope", results.SCOPES)
def test_actual_inert_html_projection_requires_complete_fixed_canary(scope):
    encoded = html.escape(json.dumps(projected(scope)))
    results.validate_html(f"<html><body><div>{encoded}</div></body></html>".encode(), scope)


@pytest.mark.parametrize("attack", ["element", "attribute", "script", "missing", "secret", "large"])
def test_html_refuses_injection_missing_canaries_secrets_and_overflow(attack):
    expected = html.escape(json.dumps(projected("current")))
    extra = {
        "element": results.fixture_payload("current")["markup"],
        "attribute": f'<div title="{results.CANARY}"></div>',
        "script": f"<script>window.{results.CANARY}=1</script>",
        "missing": "",
        "secret": results.fixture_payload("archived")["password"],
        "large": "x" * results.HTML_MAX_BYTES,
    }[attack]
    source = "" if attack == "missing" else expected
    with pytest.raises(ValueError):
        results.validate_html((source + extra).encode(), "current")


@pytest.mark.parametrize(
    "change",
    [
        {"id": True},
        {"state": "FAILED"},
        {"task_id": "other"},
        {"result_data": "{}"},
        {"result_data_omission_reason": "external_result_not_loaded"},
    ],
)
def test_api_projection_cannot_pass_on_wrong_identity_or_omitted_result(change):
    value = {
        "id": 1,
        "task_id": "owned",
        "state": "SUCCEEDED",
        "result_data_omission_reason": None,
        "result_data": json.dumps(projected("current")),
    }
    results.validate_api(json.dumps(value).encode(), execution_pk=1, task_id="owned")
    with pytest.raises(ValueError):
        results.validate_api(
            json.dumps({**value, **change}).encode(), execution_pk=1, task_id="owned"
        )


@pytest.mark.parametrize(
    "change",
    [{"schema_version": True}, {"current_api": 1}, {"owned_rows_removed": False}, {"extra": "raw"}],
)
def test_evidence_requires_exact_scalar_contract(change):
    with pytest.raises(ValueError):
        results.validate_evidence({**results.EVIDENCE, **change})


def test_wrong_settings_refused_before_storage_or_http(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
    transport = Mock()
    monkeypatch.setattr(results, "ApplicationHttp", transport)
    with pytest.raises(ValueError, match="disposable"):
        results.observe_result_presentation(token=TOKEN)
    transport.assert_not_called()


@pytest.fixture
def rendered_service(settings, monkeypatch, client):
    settings.MIDDLEWARE = [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
    ]
    settings.SESSION_ENGINE = "django.contrib.sessions.backends.db"
    settings.AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
    settings.DJANGO_API_TOKEN = TOKEN
    settings.DJANGO_RAY = {**settings.DJANGO_RAY, "TASK_ATTEMPT_ADMIN_MODE": "inline"}
    get_user_model().objects.create_superuser(username="qualification")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    calls = []

    def transport(origin, **options):
        assert origin == results.FIXTURE_ORIGIN
        assert 0 < options["request_timeout"] <= 5

        def read(
            path,
            *,
            method,
            headers,
            response_limit,
            required_media_types,
            required_response_headers,
        ):
            assert method == "GET"
            response = client.get(
                path,
                **{
                    f"HTTP_{key.upper().replace('-', '_')}": value for key, value in headers.items()
                },
            )
            assert len(response.content) <= response_limit
            assert response.headers["Content-Type"].split(";", 1)[0] in required_media_types
            for key, value in (required_response_headers or {}).items():
                assert response.headers[key] == value
            calls.append((path, response.status_code, response.content))
            return response.status_code, response.content

        return read

    monkeypatch.setattr(results, "ApplicationHttp", transport)
    return calls


@pytest.mark.django_db
def test_real_django_templates_api_and_sessions_preserve_rows_and_cleanup(rendered_service):
    untouched = RayTaskExecution.objects.create(
        task_id="unrelated-presentation-row", callable_path="owned.elsewhere"
    )
    evidence = results.observe_result_presentation(token=TOKEN)
    results.validate_evidence(evidence)
    assert [status for _path, status, _body in rendered_service] == [302, 302, 401, 200, 200, 200]
    assert Session.objects.count() == 0
    assert list(RayTaskExecution.objects.values_list("pk", flat=True)) == [untouched.pk]
    assert not TaskAttempt.objects.exists()
    assert results.CANARY not in json.dumps(evidence)


@pytest.mark.django_db
@pytest.mark.parametrize("failure", ["http", "current_mutation", "archived_mutation", "budget"])
def test_failed_observation_still_checks_preservation_and_removes_owned_state(
    rendered_service, monkeypatch, failure
):
    original = results.validate_html
    snapshots = Mock(wraps=results._raw_fields)
    monkeypatch.setattr(results, "_raw_fields", snapshots)

    def validate(body, scope):
        if failure == "current_mutation":
            RayTaskExecution.objects.filter(queue_name="qualification-result-presentation").update(
                result_data='"changed"'
            )
        elif failure == "archived_mutation":
            TaskAttempt.objects.update(result_data='"changed"')
        elif failure == "budget":
            monkeypatch.setattr(results.time, "monotonic", lambda: 10**20)
        else:
            raise ValueError("fixed failed observation")
        original(body, scope)

    monkeypatch.setattr(results, "validate_html", validate)
    with pytest.raises(ValueError):
        results.observe_result_presentation(token=TOKEN)
    assert snapshots.call_count == 2
    assert not Session.objects.exists()
    assert not RayTaskExecution.objects.exists()
    assert not TaskAttempt.objects.exists()


@pytest.mark.django_db
def test_partial_creation_rolls_back_without_yielding_owned_state(monkeypatch):
    allocated = []
    original_create = RayTaskExecution.objects.create

    def create(**kwargs):
        value = original_create(**kwargs)
        allocated.append(value.pk)
        return value

    monkeypatch.setattr(RayTaskExecution.objects, "create", create)
    monkeypatch.setattr(
        TaskAttempt.objects, "create", Mock(side_effect=ValueError("fixed failure"))
    )
    with pytest.raises(ValueError):
        with results._owned_results():
            pytest.fail("partial creation cannot yield")
    assert allocated
    assert not RayTaskExecution.objects.exists()


@pytest.mark.django_db
def test_rollback_cleanup_retains_unrelated_row_reusing_allocated_primary_key(monkeypatch):
    from django.db import transaction

    real_atomic = transaction.atomic
    allocated = []
    create = RayTaskExecution.objects.create

    def create_owned(**kwargs):
        row = create(**kwargs)
        allocated.append(row.pk)
        return row

    @contextmanager
    def rollback_and_reuse(*args, **kwargs):
        try:
            with real_atomic(*args, **kwargs):
                yield
        except ValueError:
            create(pk=allocated[0], task_id="unrelated-reused-key", callable_path="owned.elsewhere")
            raise

    monkeypatch.setattr(RayTaskExecution.objects, "create", create_owned)
    monkeypatch.setattr(
        TaskAttempt.objects, "create", Mock(side_effect=ValueError("fixed failure"))
    )
    monkeypatch.setattr(transaction, "atomic", rollback_and_reuse)
    with pytest.raises(ValueError):
        with results._owned_results():
            pytest.fail("partial creation cannot yield")
    assert list(RayTaskExecution.objects.values_list("task_id", flat=True)) == [
        "unrelated-reused-key"
    ]
