"""Failure diagnostics use bounded HTTP only and never export raw Ray records."""

import io
import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from qualification.application import actor_diagnostics as diagnostic


def envelope(rows, **changes):
    return json.dumps(
        {
            "result": True,
            "data": {
                "result": {
                    "result": rows,
                    "total": len(rows),
                    "num_after_truncation": len(rows),
                    "num_filtered": len(rows),
                    **changes,
                }
            },
        }
    ).encode()


def opener_for(*responses):
    opener = Mock()
    opener.open.side_effect = [io.BytesIO(raw) for raw in responses]
    return opener


def test_three_fixed_loopback_queries_retain_only_allowlisted_scalar_evidence():
    actor = {"actor_id": "actor", "class_name": "WorkflowProgressActor", "state": "ALIVE"}
    task = {
        "task_id": "task",
        "actor_id": "actor",
        "node_id": "node",
        "worker_id": "worker",
        "func_or_class_name": "WorkflowProgressActor.snapshot",
        "creation_time_ms": 100,
        "start_time_ms": 200,
        "end_time_ms": 300,
    }
    node = {"node_id": "node", "node_ip": "10.0.0.1", "start_time_ms": 1}
    secret_fields = {
        "runtime_env_info": {"env_vars": {"TOKEN": "secret"}},
        "serialized_runtime_env": "secret",
        "error_message": "secret",
        "repr_name": "secret",
        "arguments": ["secret"],
        "payload": "secret",
        "events": [{"secret": True}],
        "name": "secret",
        "state_message": "secret",
        "death_cause": "secret",
    }
    opener = opener_for(*(envelope([{**row, **secret_fields}]) for row in (actor, task, node)))
    receipt = diagnostic.collect(opener)
    assert receipt["diagnostic_only"] is True
    assert receipt["status"] == "available"
    assert [r["records"] for r in receipt["resources"].values()] == [[actor], [task], [node]]
    assert b"secret" not in diagnostic.encode(receipt)
    assert opener.open.call_count == 3
    for call, (kind, key, value) in zip(
        opener.open.call_args_list, diagnostic.QUERIES, strict=True
    ):
        url = urlsplit(call.args[0])
        assert (url.scheme, url.netloc, url.path) == ("http", "127.0.0.1:8265", f"/api/v0/{kind}")
        expected = {"limit": ["32"], "timeout": ["2"], "detail": ["true"]}
        if key:
            expected.update(filter_keys=[key], filter_predicates=["="], filter_values=[value])
        assert parse_qs(url.query) == expected
        assert call.kwargs == {"timeout": 3}


def test_default_opener_disables_proxies_and_redirects(monkeypatch):
    opener = opener_for(*(envelope([]) for _ in range(3)))
    build = Mock(return_value=opener)
    monkeypatch.setattr(diagnostic, "build_opener", build)
    monkeypatch.setenv("http_proxy", "http://secret.invalid")
    diagnostic.collect()
    proxy, redirect = build.call_args.args
    assert proxy.proxies == {}
    assert isinstance(redirect, diagnostic.NoRedirect)
    assert redirect.redirect_request(None, None, 302, None, None, "http://secret.invalid") is None


@pytest.mark.parametrize(
    "raw",
    [
        b"secret",
        b"[]",
        b'{"result": false, "msg": "secret"}',
        envelope([], total=-1),
        envelope([], num_filtered=True),
        envelope({}),
    ],
)
def test_unusable_responses_do_not_emit_errors_or_infer_success(raw):
    receipt = diagnostic.collect(opener_for(raw, raw, raw))
    assert receipt["status"] == "unavailable"
    assert all(r == {"status": "unavailable", "records": []} for r in receipt["resources"].values())
    assert b"secret" not in diagnostic.encode(receipt)


def test_transport_failure_is_diagnostic_only_and_other_queries_still_run():
    opener = Mock()
    opener.open.side_effect = [TimeoutError("secret"), io.BytesIO(envelope([])), OSError("secret")]
    receipt = diagnostic.collect(opener)
    assert opener.open.call_count == 3
    assert receipt["status"] == "partial"
    assert receipt["resources"]["tasks"] == {"status": "available", "records": []}
    assert b"secret" not in diagnostic.encode(receipt)


def test_read_limit_is_enforced_even_when_response_claims_success():
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b"x" * (diagnostic.RESPONSE_MAX_BYTES + 1)
    opener = Mock()
    opener.open.return_value = response
    receipt = diagnostic.collect(opener)
    assert receipt["status"] == "unavailable"
    assert response.read.call_count == 3
    assert all(c.args == (diagnostic.RESPONSE_MAX_BYTES + 1,) for c in response.read.call_args_list)


@pytest.mark.parametrize(
    "changes", [{"partial_failure_warning": "secret"}, {"total": 10}, {"num_filtered": 10}]
)
def test_partial_state_api_coverage_is_explicit_without_raw_warning(changes):
    receipt = diagnostic.collect(opener_for(envelope([], **changes), envelope([]), envelope([])))
    assert receipt["resources"]["actors"]["status"] == receipt["status"] == "partial"
    assert b"secret" not in diagnostic.encode(receipt)


def test_each_kind_and_entire_output_are_bounded():
    rows = []
    for kind, key, value in diagnostic.QUERIES:
        row = dict.fromkeys(diagnostic.FIELDS[kind], "x" * 128)
        if key:
            row[key] = value
        rows.append(envelope([row] * 40))
    receipt = diagnostic.collect(opener_for(*rows))
    assert len(diagnostic.encode(receipt)) <= 16 * 1024
    assert receipt["status"] == "partial"
    for resource in receipt["resources"].values():
        assert len(resource["records"]) <= 32
        assert resource["status"] == "partial"


def test_wrong_class_nested_and_invalid_scalar_values_are_omitted():
    rows = [
        {"class_name": "other", "actor_id": "secret"},
        {
            "class_name": "WorkflowProgressActor",
            "actor_id": {"secret": True},
            "node_id": "secret" * 128,
            "pid": float("nan"),
            "state": "secret\n",
        },
    ]
    receipt = diagnostic.collect(opener_for(envelope(rows), envelope([]), envelope([])))
    assert receipt["resources"]["actors"] == {
        "status": "partial",
        "records": [{"class_name": "WorkflowProgressActor"}],
    }
    assert b"secret" not in diagnostic.encode(receipt)


def test_main_prints_one_bounded_json_document_even_without_evidence(monkeypatch, capsys):
    receipt = diagnostic.collect(opener_for(*(envelope([]) for _ in range(3))))
    monkeypatch.setattr(diagnostic, "collect", lambda: receipt)
    assert diagnostic.main() is None
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert json.loads(output) == receipt
    assert len(output.encode()) <= diagnostic.OUTPUT_MAX_BYTES
