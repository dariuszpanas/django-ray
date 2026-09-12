"""Active cohort execution closes generic task adapters before application work."""

import builtins
import json
import logging

import pytest

from django_ray import execution_protocol as protocols
from django_ray.ray_job_protocol import RAY_JOB_REQUEST_REJECTED_EXIT_CODE
from django_ray.runtime import cohort_execution, entrypoint, remote
from django_ray.target.cohort_transport import decode_cohort_execution_result
from tests.unit.test_cohort_execution import bindings, completed, prepared, sync_contract


@pytest.fixture
def active_cohort(monkeypatch):
    monkeypatch.setattr(protocols, "EXECUTION_PROTOCOL_VERSION", 3)
    monkeypatch.setattr(
        protocols, "SUPPORTED_EXECUTION_PROTOCOL_RANGE", protocols.ExecutionProtocolRange(3, 3)
    )


def _assert_closed(encoded):
    payload = json.loads(encoded)
    assert payload["success"] is False and payload["retryable"] is False
    assert payload["error"] == "execution request rejected: unsupported_protocol"
    assert payload["traceback"] is None
    assert "private" not in encoded and "application_invoked" not in payload
    assert "task_execution_pk" not in payload


@pytest.mark.parametrize("style", ["positional", "versioned", "cohort", "target"])
def test_active_cohort_generic_remote_refuses_before_django_input_or_decoder(
    active_cohort, monkeypatch, style
):
    from django_ray import execution_codec

    def forbidden(*_args, **_kwargs):
        pytest.fail("Generic task adapter crossed its closed execution boundary")

    monkeypatch.setattr(execution_codec, "decode_execution_request", forbidden)
    monkeypatch.setattr(remote, "_execute_target_bound_django_task_remote", forbidden)
    original = builtins.__import__

    def poison(name, *args, **kwargs):
        if name == "django" or name.startswith(
            ("django.", "django_ray.runtime.entrypoint", "django_ray.input_storage")
        ):
            forbidden()
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", poison)
    if style == "positional":
        result = remote.execute_django_task_remote("private.application", "[]", "{}", 19)
    else:
        result = remote.execute_django_task_remote(
            "private opaque request",
            expected_execution_protocol_version={"versioned": 1, "cohort": 3, "target": 2}[style],
            _target_execution_transport=style == "target",
        )
    _assert_closed(result)


@pytest.mark.parametrize(
    "name", ["execute_task_from_payload", "execute_task_from_reference", "_execute_legacy_payload"]
)
def test_active_cohort_jobs_generic_entrypoints_refuse_before_preflight_or_io(
    active_cohort, monkeypatch, name
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Closed Jobs entrypoint decoded, configured or executed application")

    for boundary in (
        "load_ray_job_request_expectation",
        "_decode_payload_b64",
        "bootstrap_django",
        "get_settings",
        "load_task_input",
        "execute_task",
    ):
        monkeypatch.setattr(entrypoint, boundary, forbidden)
    original = builtins.__import__

    def poison(module, *args, **kwargs):
        if module.startswith(("django_ray.ray_job_request_storage", "django_ray.input_storage")):
            forbidden()
        return original(module, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", poison)
    result = getattr(entrypoint, name)("private malformed payload or locator")
    assert type(result) is entrypoint._StrictRequestRejectionResult
    _assert_closed(result)


@pytest.mark.parametrize("flag", ["--payload-b64", "--request-ref-b64"])
def test_active_cohort_legacy_cli_exits_rejected_without_printing_payload(
    active_cohort, capsys, flag
):
    assert (
        entrypoint.main([flag, "private-malformed-control"]) == RAY_JOB_REQUEST_REJECTED_EXIT_CODE
    )
    captured = capsys.readouterr()
    assert "unsupported_protocol" in captured.err
    assert "private" not in captured.err + captured.out


def test_active_cohort_explicit_transport_still_checks_guard_before_primitive(
    active_cohort, monkeypatch
):
    value = prepared(sync_contract())
    called = []
    original = cohort_execution.verify_cohort_sync_runtime

    def guard(contract):
        result = original(contract)
        called.append("guard")
        return result

    def primitive(*args, **kwargs):
        assert called == ["guard"]
        called.append("application")
        return completed(value)

    monkeypatch.setattr(cohort_execution, "verify_cohort_sync_runtime", guard)
    monkeypatch.setattr(entrypoint, "execute_task", primitive)
    encoded = cohort_execution.execute_cohort_request(value.request_json, **bindings(value))
    result = decode_cohort_execution_result(encoded, **bindings(value))
    assert result.completion_json is not None and result.application_invoked is None
    assert called == ["guard", "application"]


def test_active_cohort_preserves_standalone_post_guard_execution_primitive(
    active_cohort, monkeypatch
):
    monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
    result = json.loads(entrypoint.execute_task("builtins.sum", "[[2,3]]", "{}"))
    assert result["success"] is True and result["result"] == 5


def test_active_cohort_rejects_legacy_nested_request_before_callable(active_cohort, monkeypatch):
    from django_ray.execution_codec import (
        NestedExecutionRequestRejected,
        NestedExecutionRequestRejection,
    )
    from tests.unit.test_remote import (
        _STRICT_IDENTITY,
        _strict_nested_workflow_kwargs,
        _strict_nested_workflow_request,
    )

    callable_path = "private.application"
    workflow_run_id = "00000000-0000-4000-8000-000000000514"
    node_id = "0.closed-legacy"
    serialized, runtime_identity = _strict_nested_workflow_request(
        callable_path=callable_path, workflow_run_id=workflow_run_id, node_id=node_id
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Old nested request crossed the application boundary")

    monkeypatch.setattr(entrypoint, "bootstrap_django", forbidden)
    monkeypatch.setattr(remote, "_execute_workflow_step", forbidden)
    with pytest.raises(NestedExecutionRequestRejected) as error:
        remote.execute_workflow_step_remote(
            callable_path,
            True,
            (),
            {},
            {},
            _STRICT_IDENTITY.task_execution_pk,
            None,
            node_id,
            **_strict_nested_workflow_kwargs(
                serialized, runtime_identity, workflow_run_id=workflow_run_id, node_id=node_id
            ),
        )
    assert error.value.classification is NestedExecutionRequestRejection.UNSUPPORTED_PROTOCOL


def test_active_cohort_preserves_standalone_workflow_leaf(active_cohort):
    logger = logging.getLogger("django_ray")
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    try:
        assert (
            remote.execute_workflow_step_remote(
                "builtins.sum", False, ([2, 3],), {}, {}, None, None, "0.standalone"
            )
            == 5
        )
    finally:
        # The real leaf may install the package's standalone logging handler.
        for handler in list(logger.handlers):
            if handler not in handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate
