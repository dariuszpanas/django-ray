"""Resource-free inventory contracts and fake process boundary checks."""

from __future__ import annotations

import io
import json
import signal
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_ray.payload_inventory import contracts, process


def summary(**updates):
    return {
        "kind": "summary",
        "code": "observed",
        "entries": 0,
        "objects": 0,
        "counts": dict.fromkeys(contracts.OUTCOMES, 0),
        **updates,
    }


def detail(outcome="unregistered_aged"):
    return {"kind": "detail", "object_sha256": "a" * 64, "outcome": outcome}


@pytest.mark.parametrize(
    "options",
    [{"total_seconds": value} for value in (True, float("nan"), float("inf"), 4, 61, "5")]
    + [
        {field: value}
        for field, value in (
            ("max_entries", 0),
            ("max_entries", 10001),
            ("max_objects", 0),
            ("max_objects", 501),
            ("max_details", -1),
            ("max_details", 101),
            ("grace_hours", -1),
            ("grace_hours", 87601),
            ("shard", "abcd/private"),
            ("shard", "ABCD"),
            ("shard", True),
            ("max_objects", True),
        )
    ],
)
def test_inventory_limits_refuse_invalid_values(options):
    with pytest.raises(ValueError):
        contracts.Limits(**options)


def test_maximum_report_is_bounded_and_partial_details_are_explicit():
    frames = contracts.Frames(contracts.Limits(max_details=100))
    for _ in range(100):
        frames.receive(json.dumps(detail()).encode())
    partial = contracts.report(frames, code="probe_timeout", cleanup=False, elapsed=4)
    assert partial["counts"]["unregistered_aged"] == 100
    assert not partial["counts_complete"] and partial["code"] == "cleanup_unconfirmed"
    assert partial["exit_code"] == 2 and not partial["complete"]
    frames.receive(
        json.dumps(
            summary(
                entries=100,
                objects=100,
                counts={
                    **dict.fromkeys(contracts.OUTCOMES, 0),
                    "unregistered_aged": 100,
                },
            )
        ).encode()
    )
    complete = contracts.report(frames, code="observed", cleanup=True, elapsed=1)
    assert complete["counts_complete"] and complete["exit_code"] == 0
    assert complete["observed_at"].endswith("+00:00")
    for value in (partial, complete):
        for as_json in (False, True):
            assert len(contracts.render(value, as_json=as_json).encode()) < contracts.OUTPUT_BYTES


@pytest.mark.parametrize(
    "invalid",
    [
        [],
        {},
        {**detail(), "secret": "private"},
        {**detail(), "object_sha256": "private"},
        detail("private"),
        summary(code="private"),
        summary(entries=True),
        summary(objects=-1),
        summary(counts={"private": 1}),
        summary(counts=dict.fromkeys(contracts.OUTCOMES, 10000)),
    ],
)
def test_child_frames_reject_unbounded_or_unknown_values(invalid):
    with pytest.raises(ValueError):
        contracts.Frames(contracts.Limits()).receive(json.dumps(invalid).encode())


def test_frame_order_and_detail_budget_are_enforced():
    frames = contracts.Frames(contracts.Limits(max_details=0))
    with pytest.raises(ValueError):
        frames.receive(json.dumps(detail()).encode())
    frames.receive(json.dumps(summary()).encode())
    with pytest.raises(ValueError):
        frames.receive(json.dumps(summary()).encode())
    assert (
        contracts.report(
            contracts.Frames(contracts.Limits()), code="observed", cleanup=True, elapsed=0
        )["code"]
        == "probe_protocol_invalid"
    )


def fake_process():
    return SimpleNamespace(pid=12345, stdout=io.BytesIO(), stderr=io.BytesIO(), wait=Mock())


@pytest.mark.parametrize("cleanup", [True, False])
def test_parent_preserves_findings_and_requires_confirmed_cleanup(monkeypatch, cleanup):
    child = fake_process()
    monkeypatch.setattr(process.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(process, "peek_status", lambda _process: 0)

    def receive(_child, _deadline, publish, _interrupted):
        for value in (
            {"kind": "started", "pid": 12346},
            detail(),
            summary(
                entries=3,
                objects=1,
                counts={**dict.fromkeys(contracts.OUTCOMES, 0), "unregistered_aged": 1},
            ),
            {"kind": "done", "code": "observed", "cleanup_confirmed": cleanup},
        ):
            publish(json.dumps(value).encode())
        return "observed"

    monkeypatch.setattr(process, "read_frames", receive)
    frames = contracts.Frames(contracts.Limits())
    code, confirmed = process._run(
        settings_module="owned.settings",
        using="default",
        deadline=time.monotonic() + 5,
        limits=frames.limits,
        frames=frames,
        interrupted=lambda: False,
    )
    assert confirmed is cleanup and code == "observed"
    assert frames.details == [{"object_sha256": "a" * 64, "outcome": "unregistered_aged"}]
    assert child.stdout.closed and child.stderr.closed


def test_success_frame_cannot_hide_supervisor_failure(monkeypatch):
    child = fake_process()
    monkeypatch.setattr(process.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(process, "peek_status", lambda _process: 1)

    def receive(_child, _deadline, publish, _interrupted):
        for value in (
            {"kind": "started", "pid": 12346},
            summary(),
            {"kind": "done", "code": "observed", "cleanup_confirmed": True},
        ):
            publish(json.dumps(value).encode())
        return "probe_failed"

    monkeypatch.setattr(process, "read_frames", receive)
    frames = contracts.Frames(contracts.Limits())
    code, cleanup = process._run(
        settings_module="owned.settings",
        using="default",
        deadline=time.monotonic() + 5,
        limits=frames.limits,
        frames=frames,
        interrupted=lambda: False,
    )
    value = contracts.report(frames, code=code, cleanup=cleanup, elapsed=1)
    assert value["code"] == "probe_failed" and value["exit_code"] == 2


@pytest.mark.parametrize("mode", ["supervisor", "probe"])
def test_private_cli_refuses_non_linux_before_loading_any_settings(monkeypatch, mode):
    monkeypatch.setattr(process.sys, "platform", "win32")
    monkeypatch.setattr(process.sys, "argv", ["inventory", "--mode=" + mode])
    monkeypatch.setattr(process, "supervise", Mock(side_effect=AssertionError("No work")))
    assert process.main() == 2


@pytest.mark.parametrize(
    "invalid",
    [
        [],
        {"kind": "started", "pid": True},
        {"kind": "started", "pid": 1},
        {"kind": "done", "code": "private", "cleanup_confirmed": True},
        detail(),
    ],
)
def test_parent_refuses_bad_protocol_and_does_not_print_child_text(monkeypatch, invalid):
    child = fake_process()
    monkeypatch.setattr(process.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(process, "peek_status", lambda _process: 0)

    def receive(_child, _deadline, publish, _interrupted):
        with pytest.raises(ValueError):
            publish(json.dumps(invalid).encode())
        publish(
            json.dumps(
                {"kind": "done", "code": "probe_protocol_invalid", "cleanup_confirmed": True}
            ).encode()
        )
        return "probe_protocol_invalid"

    monkeypatch.setattr(process, "read_frames", receive)
    frames = contracts.Frames(contracts.Limits())
    code, cleanup = process._run(
        settings_module="owned.settings",
        using="default",
        deadline=time.monotonic() + 5,
        limits=frames.limits,
        frames=frames,
        interrupted=lambda: False,
    )
    assert code == "probe_protocol_invalid" and cleanup and not frames.details


def test_process_launch_failure_has_no_cleanup_or_private_diagnostics(monkeypatch):
    monkeypatch.setattr(process.subprocess, "Popen", Mock(side_effect=OSError("private")))
    frames = contracts.Frames(contracts.Limits())
    assert process._run(
        settings_module="owned.settings",
        using="default",
        deadline=0,
        limits=frames.limits,
        frames=frames,
        interrupted=lambda: False,
    ) == ("probe_failed", True)


@pytest.mark.parametrize("stop", [True, OSError("private cleanup")])
def test_missing_cleanup_receipt_fails_closed(monkeypatch, stop):
    monkeypatch.setattr(process.subprocess, "Popen", lambda *_args, **_kwargs: fake_process())
    monkeypatch.setattr(process, "read_frames", lambda *_args: "probe_timeout")
    monkeypatch.setattr(
        process,
        "_stop_supervisor",
        Mock(side_effect=stop if isinstance(stop, Exception) else None, return_value=stop),
    )
    frames = contracts.Frames(contracts.Limits())
    assert process._run(
        settings_module="owned.settings",
        using="default",
        deadline=0,
        limits=frames.limits,
        frames=frames,
        interrupted=lambda: False,
    ) == ("probe_timeout", False)


@pytest.mark.parametrize(
    "platform,module,using,policy,expected",
    [
        ("win32", "owned.settings", "default", True, "linux_required"),
        ("linux", None, "default", True, "settings_not_reproducible"),
        ("linux", "owned.settings", "bad\n", True, "settings_not_reproducible"),
        ("linux", "owned.settings", "default", False, "supervision_unavailable"),
    ],
)
def test_unsupported_calls_do_not_launch_processes(
    monkeypatch, platform, module, using, policy, expected
):
    monkeypatch.setattr(process.sys, "platform", platform)
    monkeypatch.setattr(process, "_caller_sigchld_default", lambda: policy)
    monkeypatch.setattr(process.subprocess, "Popen", Mock(side_effect=AssertionError("No launch")))
    assert process.inventory(settings_module=module, using=using)["code"] == expected


def test_parent_restores_signal_handlers_after_interrupt(monkeypatch):
    monkeypatch.setattr(process.sys, "platform", "linux")
    monkeypatch.setattr(process, "_caller_sigchld_default", lambda: True)
    handlers = {}

    def install(number, value):
        previous = handlers.get(number, "original")
        handlers[number] = value
        return previous

    def run(**options):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert options["interrupted"]()
        return "interrupted", True

    monkeypatch.setattr(process.signal, "signal", install)
    monkeypatch.setattr(process, "_run", run)
    assert process.inventory(settings_module="owned.settings")["code"] == "interrupted"
    assert set(handlers.values()) == {"original"}


@pytest.mark.parametrize("options", [{"max_entries": 0}, {"shard": "private"}])
def test_command_validates_before_starting_inventory(options):
    with pytest.raises(CommandError, match="Invalid inventory limits"):
        call_command("django_ray_inventory_inputs", **options)


def test_command_publishes_report_before_fixed_incomplete_error(monkeypatch):
    from django_ray.management.commands import django_ray_inventory_inputs as command

    value = contracts.report(
        contracts.Frames(contracts.Limits()), code="filesystem_required", cleanup=True, elapsed=0
    )
    monkeypatch.setattr(command, "inventory", lambda **_kwargs: value)
    output = io.StringIO()
    with pytest.raises(CommandError, match="incomplete or unavailable"):
        call_command("django_ray_inventory_inputs", format="json", stdout=output)
    assert json.loads(output.getvalue())["code"] == "filesystem_required"
