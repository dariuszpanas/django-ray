"""Resource-free checks for the by-value generic-node assertion boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import platform
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
import ray
from ray import cloudpickle

from qualification.application import generic_nodes

NODE_A, NODE_B, NODE_C = (char * 56 for char in "abc")


@pytest.fixture
def archives(tmp_path):
    remote = tmp_path / "remote.py"
    remote.write_bytes(b"# exact application remote source\n")
    paths = {name: tmp_path / f"{name}.zip" for name in ("source", "recovery")}
    for name, path in paths.items():
        with ZipFile(path, "w") as archive:
            prefix = "src/" if name == "source" else ""
            archive.writestr(f"{prefix}django_ray/runtime/remote.py", remote.read_bytes())
            archive.writestr("testproject/apps/cluster_tasks/workflows.py", "# workflow")
            if name == "recovery":
                for package in ("cryptography", "django", "psycopg", "unfold"):
                    archive.writestr(f"{package}/__init__.py", "# locked dependency")
    return {
        "source_archive": paths["source"],
        "recovery_archive": paths["recovery"],
        "remote_source": remote,
    }


@pytest.fixture
def generic_imports(monkeypatch):
    original = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: None if name == "django_ray" else original(name)
    )
    monkeypatch.setattr(
        ray, "get_runtime_context", lambda: SimpleNamespace(get_node_id=lambda: NODE_A)
    )


def test_probe_survives_by_value_serialization(archives, generic_imports):
    # A generic image must not import qualification or django_ray to load the
    # callable; cloudpickle serializes a nested function by value.
    probe = cloudpickle.loads(cloudpickle.dumps(generic_nodes.make_node_probe()))
    assert "<locals>" in probe.__qualname__
    expected = {
        name: generic_nodes.archive_identity(archives[f"{name}_archive"])
        for name in ("source", "recovery")
    }
    digest = hashlib.sha256(archives["remote_source"].read_bytes()).hexdigest()
    result = probe(
        str(archives["source_archive"]), str(archives["recovery_archive"]), expected, digest
    )
    assert result == {
        "node_id": NODE_A,
        "python_minor": list(sys.version_info[:2]),
        "python_implementation": platform.python_implementation(),
        "python_version": list(sys.version_info[:3]),
        "ray_version": ray.__version__,
        "django_ray_preinstalled": False,
        "remote_sha256": digest,
        "archives": expected,
    }


@pytest.mark.parametrize("failure", ["installed", "hash", "source", "missing", "oversize"])
def test_probe_rejects_unqualified_node(archives, generic_imports, monkeypatch, failure):
    expected = {
        name: generic_nodes.archive_identity(archives[f"{name}_archive"])
        for name in ("source", "recovery")
    }
    digest = hashlib.sha256(archives["remote_source"].read_bytes()).hexdigest()
    if failure == "installed":
        monkeypatch.setattr(importlib.util, "find_spec", lambda _: object())
    elif failure == "hash":
        expected["recovery"]["sha256"] = "0" * 64
    elif failure == "source":
        digest = "0" * 64
    else:
        with ZipFile(archives["source_archive"], "w") as archive:
            archive.writestr("src/django_ray/runtime/remote.py", b"x" * (1024 * 1024 + 1))
            if failure == "oversize":
                archive.writestr("testproject/apps/cluster_tasks/workflows.py", "# workflow")
        expected["source"] = generic_nodes.archive_identity(archives["source_archive"])
    with pytest.raises(ValueError):
        generic_nodes.make_node_probe()(
            str(archives["source_archive"]), str(archives["recovery_archive"]), expected, digest
        )


@pytest.mark.parametrize(
    "nodes",
    [
        None,
        [],
        [{"Alive": 1, "NodeID": NODE_A}],
        [{"Alive": True, "NodeID": "bad"}],
        [{"Alive": True, "NodeID": NODE_A}] * 2,
    ],
)
def test_membership_rejects_incomplete_or_ambiguous_observations(nodes):
    with pytest.raises(ValueError):
        generic_nodes.live_node_ids(nodes, expected_count=2)


def test_membership_ignores_dead_nodes():
    assert generic_nodes.live_node_ids(
        [{"Alive": True, "NodeID": NODE_A}, {"Alive": False, "NodeID": NODE_C}], expected_count=1
    ) == {NODE_A}


@pytest.fixture
def client(monkeypatch, generic_imports):
    state = SimpleNamespace(
        initialized=False,
        connects=[],
        shutdowns=0,
        cancelled=[],
        memberships=[[NODE_A, NODE_B], [NODE_A, NODE_B]],
        submitted=[],
        options=[],
        remote_options=[],
        fail_get=False,
        corrupt=False,
    )
    monkeypatch.setattr(ray, "is_initialized", lambda: state.initialized)
    monkeypatch.setattr(ray, "init", lambda **kwargs: state.connects.append(kwargs))

    def shutdown():
        state.shutdowns += 1

    def nodes():
        return [{"Alive": True, "NodeID": identity} for identity in state.memberships.pop(0)]

    def remote(**kwargs):
        state.remote_options.append(kwargs)

        def decorate(function):
            class Probe:
                def options(self, **options):
                    state.options.append(options)
                    self.identity = options["scheduling_strategy"].node_id
                    return self

                def remote(self, *args):
                    state.submitted.append((function, self.identity, args))
                    return len(state.submitted) - 1

            return Probe()

        return decorate

    def get(refs, timeout):
        assert 0 < timeout <= 120
        if state.fail_get:
            raise TimeoutError("probe unavailable")
        results = []
        for ref in refs:
            function, identity, args = state.submitted[ref]
            result = function(*args)
            result["node_id"] = identity
            results.append(result)
        if state.corrupt:
            results[-1]["remote_sha256"] = "0" * 64
        return results

    monkeypatch.setattr(ray, "shutdown", shutdown)
    monkeypatch.setattr(ray, "nodes", nodes)
    monkeypatch.setattr(ray, "remote", remote)
    monkeypatch.setattr(ray, "get", get)
    monkeypatch.setattr(ray, "cancel", lambda ref, **kwargs: state.cancelled.append(ref))
    return state


def test_probe_pins_each_node_and_releases_only_owned_connection(archives, client):
    result = generic_nodes.verify_generic_nodes(address="ray://ray-head:10001", **archives)
    assert [node["node_id"] for node in result] == [NODE_A, NODE_B]
    assert client.connects == [
        {"address": "ray://ray-head:10001", "runtime_env": {}, "logging_level": "ERROR"}
    ]
    assert client.remote_options == [{"num_cpus": 0, "max_retries": 0}]
    assert all(option["scheduling_strategy"].soft is False for option in client.options)
    assert all(option["runtime_env"] == {} for option in client.options)
    assert client.cancelled == [0, 1]
    assert client.shutdowns == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("python_version", [3, 12, 99]),
        ("python_implementation", "PyPy"),
    ],
)
def test_generic_nodes_require_full_current_cohort_interpreter(
    archives, client, monkeypatch, field, value
):
    original_get = ray.get

    def get(*args, **kwargs):
        result = original_get(*args, **kwargs)
        result[-1][field] = value
        return result

    monkeypatch.setattr(ray, "get", get)
    with pytest.raises(ValueError):
        generic_nodes.verify_generic_nodes(address="ray://ray-head:10001", **archives)
    assert client.shutdowns == 1 and client.cancelled == [0, 1]


@pytest.mark.parametrize("failure", ["timeout", "membership", "result", "cold"])
def test_probe_failure_cleans_up_without_adopting_other_work(archives, client, failure):
    kwargs = {}
    if failure == "timeout":
        client.fail_get = True
    elif failure == "membership":
        client.memberships[-1] = [NODE_A, NODE_C]
    elif failure == "result":
        client.corrupt = True
    else:
        kwargs["previous_node_ids"] = {NODE_A, NODE_C}
    with pytest.raises((ValueError, TimeoutError)):
        generic_nodes.verify_generic_nodes(address="ray://ray-head:10001", **archives, **kwargs)
    assert client.shutdowns == 1
    assert client.cancelled == ([] if failure == "cold" else [0, 1])


def test_probe_accepts_fully_replaced_generation(archives, client):
    result = generic_nodes.verify_generic_nodes(
        address="ray://ray-head:10001", previous_node_ids={NODE_C, "d" * 56}, **archives
    )
    assert len(result) == 2


def test_probe_never_disconnects_existing_client(archives, client):
    client.initialized = True
    with pytest.raises(ValueError, match="own Ray Client"):
        generic_nodes.verify_generic_nodes(address="ray://ray-head:10001", **archives)
    assert client.connects == []
    assert client.shutdowns == 0


@pytest.mark.parametrize(
    "address",
    [
        "auto",
        "local",
        "ray://host",
        "http://host:80",
        "ray://u:p@host:1",
        "ray://host:1/",
        "ray://host:1?",
        "ray://host:1#",
        "ray://host:1\n",
    ],
)
def test_probe_refuses_implicit_or_ambiguous_target(archives, client, address):
    with pytest.raises(ValueError):
        generic_nodes.verify_generic_nodes(address=address, **archives)
    assert client.connects == []
    assert client.shutdowns == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"timeout": float("nan")},
        {"timeout": 301},
        {"expected_count": True},
        {"expected_count": 4},
        {"previous_node_ids": {"bad"}},
    ],
)
def test_probe_refuses_invalid_bounds(archives, client, kwargs):
    with pytest.raises(ValueError):
        generic_nodes.verify_generic_nodes(address="ray://ray-head:10001", **archives, **kwargs)
    assert client.connects == []


def test_archive_identity_bounds_streamed_bytes(tmp_path, monkeypatch):
    path = tmp_path / "archive.zip"
    path.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        generic_nodes.archive_identity(path)
    path.write_bytes(b"12345")
    monkeypatch.setattr(generic_nodes, "MAX_ARCHIVE_BYTES", 4)
    with pytest.raises(ValueError, match="byte limit"):
        generic_nodes.archive_identity(path)


def cli_args(archives, receipt):
    return [
        "--address",
        "ray://ray-head:10001",
        "--source-archive",
        str(archives["source_archive"]),
        "--recovery-archive",
        str(archives["recovery_archive"]),
        "--remote-source",
        str(archives["remote_source"]),
        "--receipt",
        str(receipt),
    ]


def test_cli_binds_cold_receipt_to_exact_predecessor(archives, client, tmp_path, capsys):
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    assert generic_nodes.main(cli_args(archives, before)) == 0
    original = before.read_bytes()
    client.memberships = [[NODE_C, "d" * 56], [NODE_C, "d" * 56]]
    assert generic_nodes.main(cli_args(archives, after) + ["--previous-receipt", str(before)]) == 0
    receipt = json.loads(after.read_bytes())
    assert receipt["previous_receipt_sha256"] == hashlib.sha256(original).hexdigest()
    assert receipt["cold_replacement"] is True
    assert receipt["complete_application_gate"] is False
    assert before.read_bytes() == original
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(printed) == 2
    assert printed[-1] == receipt


@pytest.mark.parametrize(
    "failure",
    [
        "failed",
        "complete",
        "extra",
        "duplicate",
        "different-source",
        "oversize",
        "reused",
        "overwrite",
    ],
)
def test_cli_rejects_false_cold_evidence(archives, client, tmp_path, capsys, failure):
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    assert generic_nodes.main(cli_args(archives, before)) == 0
    receipt = json.loads(before.read_bytes())
    if failure == "failed":
        receipt["status"] = "failed"
    elif failure == "complete":
        receipt["complete_application_gate"] = True
    elif failure == "extra":
        receipt["unreviewed"] = True
    elif failure == "duplicate":
        receipt["observations"][-1]["node_id"] = NODE_A
    elif failure == "different-source":
        receipt["observations"][0]["remote_sha256"] = "0" * 64
    before.write_text(json.dumps(receipt), encoding="utf-8")
    if failure == "oversize":
        before.write_bytes(b" " * (generic_nodes.MAX_RECEIPT_BYTES + 1))
    if failure == "overwrite":
        after.write_text("preserve receipt", encoding="utf-8")
    client.memberships = (
        [[NODE_A, NODE_B], [NODE_A, NODE_B]]
        if failure == "reused"
        else [[NODE_C, "d" * 56], [NODE_C, "d" * 56]]
    )
    assert generic_nodes.main(cli_args(archives, after) + ["--previous-receipt", str(before)]) == 1
    if failure == "overwrite":
        assert after.read_text(encoding="utf-8") == "preserve receipt"
    else:
        assert not after.exists()
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["status"] == "failed"
    assert result["cold_replacement"] is False
    assert result["observations"] == []


def test_cli_omits_private_dependency_failure(archives, client, tmp_path, capsys, monkeypatch):
    def private_failure(*args, **kwargs):
        raise RuntimeError("private dependency response and application token")

    monkeypatch.setattr(ray, "get", private_failure)
    receipt = tmp_path / "failed.json"
    assert generic_nodes.main(cli_args(archives, receipt)) == 1
    assert "private" not in capsys.readouterr().out
    assert not receipt.exists()
    assert client.shutdowns == 1
