"""Closed host failure retention without a runtime or subprocess."""

import json
from pathlib import Path

import pytest

from qualification.application import protected_log_diagnostics as diagnostics
from qualification.application import protected_logs as logs
from tests.unit.test_application_protected_surfaces import IMAGE
from tests.unit.test_application_worker_resource_diagnostics import record


def arguments(output, generation="before"):
    return [
        "--context=owned",
        "--kubeconfig-paths=/owned",
        "--namespace=django-ray-core-owned",
        "--image=" + IMAGE,
        "--generation=" + generation,
        "--output=" + str(output),
    ]


def sidecar(output):
    return output.with_name(output.name + ".failure.json")


@diagnostics.tracked("host")
def fail(**kwargs):
    diagnostics.phase("inventory")
    diagnostics.inventory("manager", "container_not_ready")
    raise ValueError("private-canary")


@pytest.mark.parametrize("generation", ["before", "after"])
def test_host_failure_retains_exact_stderr_without_a_success_receipt(
    tmp_path, monkeypatch, capsys, generation
):
    output = tmp_path / "caller-selected.custom"
    calls = []

    def observe(**kwargs):
        calls.append(kwargs)
        return fail(**kwargs)

    monkeypatch.setattr(logs, "observe", observe)
    assert logs.main(arguments(output, generation)) == 1
    captured = capsys.readouterr()
    raw = sidecar(output).read_bytes()
    assert raw == captured.err.encode("utf-8")
    assert len(raw) <= diagnostics.MAX_BYTES
    assert b"private-canary" not in raw
    assert captured.out == ""
    value = json.loads(raw)
    assert diagnostics._valid(value, "host")
    assert value["failure_phase"] == "inventory"
    assert value["inventory_reason"] == "container_not_ready"
    assert not output.exists()
    assert calls == [
        {
            "context": "owned",
            "kubeconfig_paths": "/owned",
            "namespace": "django-ray-core-owned",
            "image": IMAGE,
            "generation": generation,
        }
    ]


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_existing_sidecar_is_never_overwritten_or_followed(tmp_path, monkeypatch, capsys, kind):
    output = tmp_path / "receipt.json"
    target = tmp_path / "original"
    target.write_bytes(b"original-private-canary")
    retained = sidecar(output)
    if kind == "symlink":
        try:
            retained.symlink_to(target)
        except OSError:
            pytest.skip("Host does not permit the resource-free symlink fixture")
    else:
        retained.write_bytes(b"earlier-evidence")
    monkeypatch.setattr(logs, "observe", fail)
    assert logs.main(arguments(output)) == 1
    assert target.read_bytes() == b"original-private-canary"
    assert retained.is_symlink() is (kind == "symlink")
    assert retained.read_bytes() == (
        b"original-private-canary" if kind == "symlink" else b"earlier-evidence"
    )
    assert json.loads(capsys.readouterr().err)["inventory_reason"] == "container_not_ready"
    assert not output.exists()


@pytest.mark.parametrize("phase", ["open", "write", "close"])
def test_storage_errors_cannot_replace_original_failure(tmp_path, monkeypatch, capsys, phase):
    output = tmp_path / "receipt.json"

    class FailedWriter:
        def __enter__(self):
            return self

        def write(self, raw):
            if phase == "write":
                raise OSError("storage-private-canary")

        def __exit__(self, *args):
            if phase == "close":
                raise RuntimeError("storage-private-canary")

    def open_sidecar(path, mode):
        assert path == sidecar(output)
        assert mode == "xb"
        if phase == "open":
            raise PermissionError("storage-private-canary")
        return FailedWriter()

    monkeypatch.setattr(Path, "open", open_sidecar)
    monkeypatch.setattr(logs, "observe", fail)
    assert logs.main(arguments(output)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-canary" not in captured.err
    assert json.loads(captured.err)["inventory_reason"] == "container_not_ready"


@pytest.mark.parametrize("missing", ["namespace", "context", "kubeconfig-paths", "image", "output"])
def test_invalid_host_arguments_never_arm_retention(tmp_path, monkeypatch, capsys, missing):
    output = tmp_path / "receipt.json"
    argv = [arg for arg in arguments(output) if not arg.startswith("--" + missing + "=")]
    monkeypatch.setattr(logs, "observe", lambda **kwargs: pytest.fail("unexpected observation"))
    assert logs.main(argv) == 1
    assert diagnostics._valid(json.loads(capsys.readouterr().err), "host")
    assert list(tmp_path.iterdir()) == []


def test_invalid_generation_is_rejected_before_retention(tmp_path, capsys):
    output = tmp_path / "receipt.json"
    with pytest.raises(SystemExit) as caught:
        logs.main(arguments(output, "invalid"))
    assert caught.value.code == 2
    capsys.readouterr()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("flag", ["--inspect-stdin", "--inspect-status"])
def test_private_child_failure_never_writes_a_sidecar(tmp_path, monkeypatch, capsys, flag):
    output = tmp_path / "receipt.json"

    def inspect(**kwargs):
        raise TimeoutError("private-canary")

    monkeypatch.setattr(logs, "inspect_stdin", inspect)
    assert logs.main([*arguments(output), flag]) == 1
    captured = capsys.readouterr()
    assert diagnostics.parse_child(captured.out.encode()) is not None
    assert captured.err == ""
    assert "private-canary" not in captured.out
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("generation", ["before", "after"])
def test_success_receipt_bytes_and_streams_are_unchanged(tmp_path, monkeypatch, capsys, generation):
    output = tmp_path / "caller-selected.custom"
    receipt = {
        "schema_version": 1,
        "layer": "application_protected_logs",
        "status": "passed",
        "complete_application_gate": False,
        "generation": generation,
        "assertions": dict.fromkeys(logs.FACTS, True),
    }
    monkeypatch.setattr(logs, "observe", lambda **kwargs: receipt)
    assert logs.main(arguments(output, generation)) == 0
    assert output.read_bytes() == json.dumps(receipt, sort_keys=True).encode()
    assert not sidecar(output).exists()
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("mutation", ["extra", "duplicate", "trailing", "child", "oversized"])
def test_retention_requires_exact_closed_bounded_host_record(tmp_path, mutation):
    output = tmp_path / "receipt.json"
    encoded = diagnostics.diagnostic(ValueError("private-canary"), "host")
    if mutation == "extra":
        encoded = encoded[:-1] + ',"extra":"private-canary"}'
    elif mutation == "duplicate":
        encoded = '{"failure_phase":"private-canary",' + encoded[1:]
    elif mutation == "trailing":
        encoded += " private-canary"
    elif mutation == "child":
        encoded = diagnostics.diagnostic(ValueError("private-canary"), "child")
    else:
        encoded += " " * diagnostics.MAX_BYTES
    logs._retain_host_failure(output, encoded)
    assert list(tmp_path.iterdir()) == []


def test_long_current_readiness_record_fits_sidecar(tmp_path):
    output = tmp_path / "receipt.json"
    value = diagnostics._record(
        "host",
        "final_inventory",
        "unavailable",
        "private_compare",
        "manager",
        "container_not_ready",
        {
            "outcome": "incomplete",
            "phase": "django_setup_complete",
            "status": "unknown",
            "reason": "unknown",
            "exit_code": 137,
            "elapsed_ms": 10000,
        },
    )
    assert diagnostics._valid(value, "host")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    logs._retain_host_failure(output, encoded)
    assert sidecar(output).read_bytes() == (encoded + "\n").encode()
    assert len(sidecar(output).read_bytes()) <= diagnostics.MAX_BYTES


@pytest.mark.parametrize("fits", [False, True])
def test_retention_byte_limit_includes_the_line_ending(tmp_path, monkeypatch, fits):
    output = tmp_path / "receipt.json"
    encoded = diagnostics.diagnostic(ValueError("private-canary"), "host")
    monkeypatch.setattr(diagnostics, "MAX_BYTES", len(encoded.encode()) + int(fits))
    logs._retain_host_failure(output, encoded)
    assert sidecar(output).exists() is fits


def resource_sidecar(output):
    return output.with_name(output.name + ".failure-resources.json")


@diagnostics.tracked("host")
def fail_with_resources(**kwargs):
    diagnostics.phase("inventory")
    diagnostics.inventory("manager", "container_not_ready")
    diagnostics.worker_resources(record())
    raise ValueError("private-canary")


def test_resources_are_separate_and_paired_with_current_host_failure(tmp_path, monkeypatch, capsys):
    output = tmp_path / "before-protected-logs.json"
    monkeypatch.setattr(logs, "observe", fail_with_resources)
    assert logs.main(arguments(output)) == 1
    assert sidecar(output).read_bytes() == capsys.readouterr().err.encode()
    assert json.loads(sidecar(output).read_bytes())["schema_version"] == 5
    assert (
        resource_sidecar(output).read_bytes()
        == (json.dumps(record(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    assert len(resource_sidecar(output).read_bytes()) <= 512
    assert not output.exists()


@pytest.mark.parametrize("existing", ["host", "resources"])
def test_previous_sidecars_are_not_overwritten_or_paired_with_new_metrics(
    tmp_path, monkeypatch, capsys, existing
):
    output = tmp_path / "receipt.json"
    previous = sidecar(output) if existing == "host" else resource_sidecar(output)
    previous.write_bytes(b"earlier-evidence")
    monkeypatch.setattr(logs, "observe", fail_with_resources)
    assert logs.main(arguments(output)) == 1
    assert previous.read_bytes() == b"earlier-evidence"
    if existing == "host":
        assert not resource_sidecar(output).exists()
    else:
        assert not sidecar(output).exists()
    assert json.loads(capsys.readouterr().err)["inventory_reason"] == "container_not_ready"


def test_orphaned_broken_resource_symlink_prevents_new_pair(tmp_path, monkeypatch, capsys):
    output = tmp_path / "receipt.json"
    try:
        resource_sidecar(output).symlink_to(tmp_path / "absent")
    except OSError:
        pytest.skip("Host does not permit the resource-free symlink fixture")
    monkeypatch.setattr(logs, "observe", fail_with_resources)
    assert logs.main(arguments(output)) == 1
    assert resource_sidecar(output).is_symlink()
    assert not sidecar(output).exists()
    assert json.loads(capsys.readouterr().err)["inventory_reason"] == "container_not_ready"


@pytest.mark.parametrize("phase", ["open", "write", "close"])
def test_resource_storage_failure_preserves_host_failure(tmp_path, monkeypatch, capsys, phase):
    output = tmp_path / "receipt.json"
    original_open = Path.open

    class FailedWriter:
        def __enter__(self):
            return self

        def write(self, raw):
            if phase == "write":
                raise OSError("private-canary")
            return len(raw)

        def __exit__(self, *args):
            if phase == "close":
                raise OSError("private-canary")

    def open_sidecar(path, mode, *args, **kwargs):
        if path != resource_sidecar(output):
            return original_open(path, mode, *args, **kwargs)
        assert mode == "xb"
        if phase == "open":
            raise OSError("private-canary")
        return FailedWriter()

    monkeypatch.setattr(Path, "open", open_sidecar)
    monkeypatch.setattr(logs, "observe", fail_with_resources)
    assert logs.main(arguments(output)) == 1
    raw = capsys.readouterr().err.encode()
    assert sidecar(output).read_bytes() == raw
    assert b"private-canary" not in raw


@pytest.mark.parametrize("change", ["phase", "inventory", "child"])
def test_resources_do_not_survive_a_different_diagnostic_context(change):
    @diagnostics.tracked("host")
    def outer():
        diagnostics.phase("inventory")
        diagnostics.inventory("manager", "container_not_ready")
        diagnostics.worker_resources(record())
        if change == "phase":
            diagnostics.phase("manager_log")
        elif change == "inventory":
            diagnostics.inventory("web", "container_not_ready")
        else:

            @diagnostics.tracked("child")
            def child():
                with pytest.raises(ValueError, match="Invalid worker resource"):
                    diagnostics.worker_resources(record())
                raise ValueError("child")

            diagnostics.phase("status_check")
            child()
        raise ValueError("private-canary")

    with pytest.raises(ValueError) as caught:
        outer()
    assert diagnostics.resource_diagnostic(caught.value) is None
