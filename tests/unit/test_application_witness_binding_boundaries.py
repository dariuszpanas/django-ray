"""Host-tool data fixtures and fake HTTPS; no live services or native workloads."""

from __future__ import annotations

import io
import json
import shutil
import socket
import subprocess
import sys
import threading
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qualification.witness_binding import bounds, git_data, http_transport
from qualification.witness_binding import verify as checker


def test_invalid_process_input_is_rejected_before_starting_child(monkeypatch):
    child = Mock(side_effect=AssertionError("Process must not start"))
    monkeypatch.setattr(bounds.subprocess, "Popen", child)
    for invalid in (1, bytearray(b"x"), b"x" * (512 * 1024 + 1)):
        with pytest.raises(bounds.BindingError, match="request_invalid"):
            bounds.process(["unused"], 1024, bounds.Budget(), input_data=invalid)
    child.assert_not_called()


def test_process_timeout_kills_reaps_and_closes_owned_pipes(monkeypatch):
    child = Mock()
    child.stdout, child.stdin, child.returncode = io.BytesIO(b"fixed"), None, None
    child.wait.side_effect = [subprocess.TimeoutExpired("fixed", 1), 0]
    child.poll.return_value = None
    monkeypatch.setattr(bounds.subprocess, "Popen", Mock(return_value=child))
    with pytest.raises(bounds.BindingError, match="verification_budget_exceeded"):
        bounds.process(["fixed"], 1024, bounds.Budget())
    child.kill.assert_called_once()
    assert child.wait.call_count == 2 and child.stdout.closed


def test_request_and_total_body_budgets_do_not_reset():
    budget = bounds.Budget(clock=lambda: 0)
    for _ in range(48):
        budget.request()
    with pytest.raises(bounds.BindingError, match="verification_budget_exceeded"):
        budget.request()
    with pytest.raises(bounds.BindingError, match="verification_budget_exceeded"):
        budget.received(14 * bounds.MIB + 1)
    now = [0]
    deadline = bounds.Budget(clock=lambda: now[0])
    now[0] = 234
    with pytest.raises(bounds.BindingError, match="verification_budget_exceeded"):
        deadline.remaining()
    assert deadline.deadline == 240  # Seven seconds are reserved for owned cleanup.


@pytest.mark.parametrize(
    "raw", [b"X:" + b"x" * 8192 + b"\r\n", (b"X:" + b"x" * 8000 + b"\r\n") * 5, b"X:x\r\n" * 65]
)
def test_header_line_total_and_count_bounds(raw):
    reader = http_transport.HeaderReader(io.BytesIO(raw))
    with pytest.raises(bounds.BindingError, match="transport_response_invalid"):
        while reader.readline(65537):
            pass


@pytest.mark.parametrize("status", ["200 OK", "302 Found"])
def test_real_http_response_closes_bounded_header_adapter(status):
    stream = io.BytesIO((f"HTTP/1.1 {status}\r\nContent-Length: 0\r\n\r\n").encode())
    sock = Mock()
    sock.makefile.return_value = stream
    response = http_transport.Response(sock)
    response.begin()
    response.close()
    assert stream.closed


@pytest.mark.parametrize(
    "trailers",
    [b"X:" + b"x" * 8192 + b"\r\n", (b"X:" + b"x" * 8000 + b"\r\n") * 5, b"X:x\r\n" * 61],
)
def test_real_chunked_trailers_share_initial_header_limits(trailers):
    raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    sock = Mock()
    sock.makefile.return_value = io.BytesIO(raw + b"1\r\nx\r\n0\r\n" + trailers + b"\r\n")
    response = http_transport.Response(sock)
    response.begin()
    assert response.read1(65536) == b"x"
    with pytest.raises(bounds.BindingError, match="transport_response_invalid"):
        response.read1(65536)
    response.close()


def test_real_bounded_chunked_body_does_not_count_as_headers():
    payload = b"x" * 65536
    raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    sock = Mock()
    sock.makefile.return_value = io.BytesIO(
        raw + b"10000\r\n" + payload + b"\r\n0\r\nX-Trailer: bounded\r\n\r\n"
    )
    response = http_transport.Response(sock)
    reader = response.fp
    assert isinstance(reader, http_transport.HeaderReader)
    response.begin()
    assert response.read1(65536) == payload and response.read1(65536) == b""
    assert reader.bytes == len(raw + b"X-Trailer: bounded\r\n\r\n")
    assert reader.lines == 6
    response.close()


@pytest.mark.parametrize("window", ["before_connect", "inside_socket_factory"])
def test_expired_guard_refuses_both_raw_socket_publication_windows(monkeypatch, window):
    connection = object.__new__(http_transport.Connection)
    connection.address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.1", 443))
    connection.timeout, connection.active, connection.aborted = 30, None, threading.Event()
    connection.deadline, connection.clock = 30, lambda: 0
    connection._context = Mock()
    raw = Mock()
    raw.connect.side_effect = AssertionError("Fresh TCP I/O after an expired guard")

    def create_socket(*_args):
        assert connection.active is None
        if window == "inside_socket_factory":
            connection.abort()
        return raw

    factory = Mock(side_effect=create_socket)
    monkeypatch.setattr(http_transport.socket, "socket", factory)
    if window == "before_connect":
        connection.abort()
    with pytest.raises(TimeoutError):
        connection.connect()
    assert factory.call_count == (0 if window == "before_connect" else 1)
    assert raw.close.call_count == (0 if window == "before_connect" else 1)
    raw.settimeout.assert_not_called()
    raw.connect.assert_not_called()
    connection._context.wrap_socket.assert_not_called()


@pytest.mark.parametrize("phase", ["before_socket", "socket_factory", "after_tcp", "before_tls"])
def test_clock_expiry_refuses_io_even_when_watchdog_has_not_run(monkeypatch, phase):
    now = [30 if phase == "before_socket" else 0]
    connection = object.__new__(http_transport.Connection)
    connection.address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.1", 443))
    connection.timeout, connection.active, connection.aborted = 30, None, threading.Event()
    connection.deadline, connection.clock = 30, lambda: now[0]
    connection.host = "reviewed.invalid"
    raw, wrapped = Mock(), Mock()
    connection._context = Mock()

    def create_socket(*_args):
        if phase == "socket_factory":
            now[0] = 30
        return raw

    def tcp_connect(_address):
        assert now[0] < 30
        if phase == "after_tcp":
            now[0] = 30

    def wrap(*_args, **_kwargs):
        if phase == "before_tls":
            now[0] = 30
        return wrapped

    factory = Mock(side_effect=create_socket)
    monkeypatch.setattr(http_transport.socket, "socket", factory)
    raw.connect.side_effect = tcp_connect
    connection._context.wrap_socket.side_effect = wrap
    with pytest.raises(TimeoutError):
        connection.connect()
    assert not connection.aborted.is_set()  # No watchdog callback rescued this check.
    assert raw.connect.call_count == (1 if phase in {"after_tcp", "before_tls"} else 0)
    assert factory.call_count == (0 if phase == "before_socket" else 1)
    assert raw.close.call_count == (0 if phase == "before_socket" else 1)
    wrapped.do_handshake.assert_not_called()
    if phase == "before_tls":
        wrapped.close.assert_called_once()


@pytest.mark.parametrize("handshake_seconds", [7, 30])
def test_post_handshake_deadline_fences_inherited_header_send(monkeypatch, handshake_seconds):
    now = [0]
    connection = object.__new__(http_transport.Connection)
    connection.address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.1", 443))
    connection.timeout, connection.active, connection.aborted = 30, None, threading.Event()
    connection.deadline, connection.clock = 30, lambda: now[0]
    connection.host, connection.sock, connection.auto_open = "reviewed.invalid", None, 1
    raw, wrapped = Mock(), Mock()
    connection._context = Mock()
    connection._context.wrap_socket.return_value = wrapped
    monkeypatch.setattr(http_transport.socket, "socket", Mock(return_value=raw))

    def handshake():
        now[0] = handshake_seconds

    wrapped.do_handshake.side_effect = handshake
    headers = b"GET /synthetic HTTP/1.1\r\n\r\n"
    if handshake_seconds == 30:
        with pytest.raises(TimeoutError):
            connection.send(headers)  # Actual inherited send calls connect then sendall.
        wrapped.sendall.assert_not_called()
        wrapped.close.assert_called_once()
        raw.close.assert_called_once()
    else:
        connection.send(headers)
        wrapped.sendall.assert_called_once_with(headers)
        assert [call.args[0] for call in wrapped.settimeout.call_args_list] == [30, 23]
    assert not connection.aborted.is_set()  # Expiry is checked without a watchdog callback.


class FakeTimer:
    instances = []

    def __init__(self, seconds, callback):
        self.seconds, self.callback, self.cancelled = seconds, callback, False
        self.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def join(self, timeout):
        assert timeout <= 1

    def is_alive(self):
        return False


class FakeResponse:
    def __init__(self, status=200, raw=b"{}", headers=None, read_hook=None):
        self.status, self.raw = status, io.BytesIO(raw)
        self.headers, self.read_hook = headers or {}, read_hook

    def getheader(self, name):
        return self.headers.get(name)

    def read1(self, size):
        if self.read_hook:
            self.read_hook()
        return self.raw.read(min(size, 7))

    def read(self, _size):
        raise AssertionError("Whole-response reads can reset a dribbling peer's timeout")


@pytest.fixture
def fake_https(monkeypatch):
    queue, calls, connections = [], [], []

    class Connection:
        def __init__(self, host, address, timeout, *, deadline, clock):
            self.host, self.closed, self.aborted = host, False, False
            assert 0 < timeout <= 30
            assert deadline > clock()
            connections.append(self)

        def request(self, method, path, headers):
            assert method == "GET"
            calls.append((self.host, path, dict(headers)))

        def getresponse(self):
            response = queue.pop(0)
            return response() if callable(response) else response

        def abort(self):
            self.aborted = True

        def close(self):
            self.closed = True

    FakeTimer.instances = []
    monkeypatch.setattr(http_transport, "Timer", FakeTimer)
    monkeypatch.setattr(http_transport, "Connection", Connection)
    monkeypatch.setattr(
        http_transport, "resolve", lambda _host, _budget, _deadline: "pinned public address"
    )
    return queue, calls, connections


def test_expired_tls_context_construction_never_starts_tcp(monkeypatch):
    now = [0]
    budget = bounds.Budget(clock=lambda: now[0])

    def context():
        now[0] = 30
        return Mock()

    factory = Mock(side_effect=AssertionError("TCP after expired TLS context construction"))
    monkeypatch.setattr(http_transport.ssl, "create_default_context", context)
    monkeypatch.setattr(http_transport.socket, "socket", factory)
    monkeypatch.setattr(http_transport, "resolve", lambda *_args: "unused address")
    timer = Mock(side_effect=AssertionError("Stale watchdog duration"))
    monkeypatch.setattr(http_transport, "Timer", timer)
    with pytest.raises(bounds.BindingError, match="verification_budget_exceeded"):
        http_transport.Github(budget, "synthetic-token").get(bounds.PREFIX)
    factory.assert_not_called()
    timer.assert_not_called()


def test_delayed_watchdog_start_never_starts_request(fake_https, monkeypatch):
    _, calls, connections = fake_https
    now = [0]

    def delayed_start(_timer):
        now[0] = 30

    monkeypatch.setattr(FakeTimer, "start", delayed_start)
    with pytest.raises(bounds.BindingError, match="verification_budget_exceeded"):
        http_transport.Github(bounds.Budget(clock=lambda: now[0]), "synthetic-token").get(
            bounds.PREFIX
        )
    assert calls == [] and connections[0].closed
    assert FakeTimer.instances[0].cancelled


def test_constructor_time_and_redirects_share_one_request_deadline(fake_https, monkeypatch):
    queue, _, connections = fake_https
    now, deadlines = [0], []
    original = http_transport.Connection

    def construct(host, address, timeout, *, deadline, clock):
        deadlines.append(deadline)
        connection = original(host, address, timeout, deadline=deadline, clock=clock)
        now[0] += 2
        return connection

    monkeypatch.setattr(http_transport, "Connection", construct)
    queue.extend(
        [
            FakeResponse(
                302,
                headers={"Location": "https://productionresultssa1.blob.core.windows.net/fixed"},
            ),
            FakeResponse(raw=b"ZIP"),
        ]
    )
    assert (
        http_transport.Github(bounds.Budget(clock=lambda: now[0]), "synthetic-token").get(
            bounds.PREFIX + "/actions/artifacts/1/zip", binary=True
        )
        == b"ZIP"
    )
    assert deadlines == [30, 30]
    assert [timer.seconds for timer in FakeTimer.instances] == [28, 26]
    assert all(connection.closed for connection in connections)


def test_api_credentials_never_follow_signed_artifact_redirect(fake_https):
    queue, calls, connections = fake_https
    queue.extend(
        [
            FakeResponse(
                302,
                headers={
                    "Location": "https://productionresultssa1.blob.core.windows.net/fixed?sig=private"
                },
            ),
            FakeResponse(raw=b"ZIP"),
        ]
    )
    client = http_transport.Github(bounds.Budget(), "synthetic-token")
    assert client.get(bounds.PREFIX + "/actions/artifacts/1/zip", binary=True) == b"ZIP"
    assert calls[0][2]["Authorization"] == "Bearer synthetic-token"
    assert "Authorization" not in calls[1][2] and "X-GitHub-Api-Version" not in calls[1][2]
    assert all(item.closed for item in connections)
    assert all(item.cancelled for item in FakeTimer.instances)


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_absolute_guard_interrupts_header_or_body_dribble(fake_https, phase):
    queue, _, connections = fake_https

    def expire():
        FakeTimer.instances[-1].callback()

    if phase == "headers":

        def headers():
            expire()
            raise TimeoutError("private signed URL or response must never escape")

        queue.append(headers)
    else:
        queue.append(FakeResponse(read_hook=expire))
    with pytest.raises(bounds.BindingError, match="verification_budget_exceeded") as error:
        http_transport.Github(bounds.Budget(), "synthetic-token").get(bounds.PREFIX)
    assert "private" not in str(error.value)
    assert connections[0].aborted and connections[0].closed


def test_redirect_limit_is_not_a_retry_loop(fake_https):
    queue, calls, connections = fake_https
    queue.extend(
        FakeResponse(
            302,
            headers={
                "Location": "https://productionresultssa1.blob.core.windows.net/fixed?sig=private"
            },
        )
        for _ in range(4)
    )
    with pytest.raises(bounds.BindingError, match="verification_budget_exceeded"):
        http_transport.Github(bounds.Budget(), "synthetic-token").get(
            bounds.PREFIX + "/actions/artifacts/1/zip", binary=True
        )
    assert len(calls) == 4 and all(item.closed for item in connections)


def test_http_error_does_not_read_or_publish_response_body(fake_https):
    queue, _, _ = fake_https
    queue.append(
        FakeResponse(
            403, raw=b"private credentials", read_hook=lambda: pytest.fail("Error body read")
        )
    )
    with pytest.raises(bounds.BindingError, match="transport_response_invalid") as error:
        http_transport.Github(bounds.Budget(), "synthetic-token").get(bounds.PREFIX)
    assert "private" not in str(error.value)


def test_dns_private_addresses_are_refused_before_connect(monkeypatch):
    monkeypatch.setattr(
        bounds.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(bounds.BindingError, match="redirect_target_invalid"):
        bounds.resolve("reviewed.invalid", bounds.Budget(), bounds.time.monotonic() + 30)


@pytest.fixture
def object_store(tmp_path):
    executable = git_data.host_program("git", tmp_path)
    repository = tmp_path / "objects.git"
    env = git_data.git_environment()
    env.pop("GIT_NO_REPLACE_OBJECTS", None)

    def git(*args, data=None):
        result = subprocess.run(
            [
                executable,
                "--git-dir=" + str(repository),
                "-c",
                "user.name=Witness fixture",
                "-c",
                "user.email=witness@example.invalid",
                *args,
            ],
            input=data,
            capture_output=True,
            check=True,
            env=env,
            timeout=10,
        )
        return result.stdout

    subprocess.run(
        [executable, "init", "--bare", "--template=", str(repository)],
        check=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    blob = git("hash-object", "-w", "--stdin", data=b"Original data\n").decode().strip()
    tree = git("mktree", data=("100644 blob " + blob + "\tREADME\n").encode()).decode().strip()
    commit = git("commit-tree", tree, data=b"Synthetic original object fixture\n").decode().strip()
    return repository, git, commit, tree, blob


def test_private_reader_ignores_replace_refs_and_never_mutates_source(object_store, monkeypatch):
    repository, git, commit, tree, blob = object_store
    replacement_blob = git("hash-object", "-w", "--stdin", data=b"Replaced data\n").decode().strip()
    replacement_tree = (
        git("mktree", data=("100644 blob " + replacement_blob + "\tREADME\n").encode())
        .decode()
        .strip()
    )
    replacement = (
        git("commit-tree", replacement_tree, data=b"Synthetic replacement\n").decode().strip()
    )
    git("replace", commit, replacement)
    # The replacement is demonstrably active in an ordinary source-repo read.
    assert git("rev-parse", commit + "^{tree}").decode().strip() == replacement_tree
    monkeypatch.setenv("GIT_TRACE", str(repository / "must-not-exist"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "unreviewed program")
    before = {
        str(path.relative_to(repository)): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    }
    reader = git_data.Git(repository, bounds.Budget())
    owned = reader._reader
    try:
        assert reader.environment["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert (
            "GIT_TRACE" not in reader.environment and "GIT_CONFIG_COUNT" not in reader.environment
        )
        assert reader.tree(commit) == tree
        assert reader.entries(commit) == {"README": blob}
        _, files = reader.source_archive(commit)
        assert files == {"README": b"Original data\n"}
        assert not (owned / "hooks").exists()  # No git-init/template execution.
    finally:
        reader.close()
    assert not owned.exists()
    assert before == {
        str(path.relative_to(repository)): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("kind", ["commit", "tree", "blob"])
def test_corrupt_loose_object_under_expected_id_is_never_trusted(object_store, kind):
    repository, _, commit, tree, blob = object_store
    oid = {"commit": commit, "tree": tree, "blob": blob}[kind]
    path = repository / "objects" / oid[:2] / oid[2:]
    payload = (
        b"tree " + tree.encode() + b"\n\nforged commit\n" if kind == "commit" else b"forged bytes"
    )
    path.chmod(0o600)
    path.write_bytes(
        zlib.compress(kind.encode() + b" " + str(len(payload)).encode() + b"\0" + payload)
    )
    reader = git_data.Git(repository, bounds.Budget())
    owned = reader._reader
    try:
        with pytest.raises(bounds.BindingError, match="source_archive_invalid"):
            reader.entries(commit)
    finally:
        reader.close()
    assert not owned.exists()


@pytest.mark.parametrize("marker", [".git", "commondir"])
def test_repository_pointer_reads_are_bounded_before_parsing(tmp_path, marker, monkeypatch):
    (tmp_path / marker).write_bytes(b"gitdir: " + b"x" * 5000)
    monkeypatch.setattr(
        Path, "read_bytes", Mock(side_effect=AssertionError("Unbounded pointer read"))
    )
    with pytest.raises(bounds.BindingError, match="local_git_unavailable"):
        git_data.Git(tmp_path, bounds.Budget())


def test_private_reader_does_not_lazy_fetch_missing_objects(object_store):
    repository, git, commit, _, blob = object_store
    available = repository.parent / "available.git"
    shutil.copytree(repository, available)
    git("config", "extensions.partialClone", "forbidden")
    git("config", "remote.forbidden.promisor", "true")
    git("config", "remote.forbidden.url", str(available))
    git("config", "protocol.file.allow", "always")
    missing = repository / "objects" / blob[:2] / blob[2:]
    missing.chmod(0o600)
    missing.unlink()
    before = {
        path.relative_to(repository): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    }
    reader = git_data.Git(repository, bounds.Budget())
    try:
        assert reader.environment["GIT_NO_LAZY_FETCH"] == "1"
        assert reader.environment["GIT_ALLOW_PROTOCOL"] == ""
        # Model Git 2.43 ignoring the newer environment guard. Configuration
        # isolation and transport denial must independently preserve refusal.
        reader.environment.pop("GIT_NO_LAZY_FETCH")
        with pytest.raises(bounds.BindingError, match="local_git_unavailable"):
            reader.entries(commit)
    finally:
        reader.close()
    assert not missing.exists()
    assert (available / "objects" / blob[:2] / blob[2:]).is_file()
    assert before == {
        path.relative_to(repository): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    }


def test_private_reader_supports_git_243_argument_surface(object_store, monkeypatch):
    repository, _, commit, tree, blob = object_store
    run = git_data.process
    calls = []

    def compatible(argv, maximum, budget, **kwargs):
        # An argument-surface shim, not a claim that this host has Git 2.43.
        assert "--no-lazy-fetch" not in argv
        assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == ""
        kwargs["env"] = {
            key: value for key, value in kwargs["env"].items() if key != "GIT_NO_LAZY_FETCH"
        }
        calls.append(tuple(argv))
        return run(argv, maximum, budget, **kwargs)

    monkeypatch.setattr(git_data, "process", compatible)
    reader = git_data.Git(repository, bounds.Budget())
    try:
        assert reader.tree(commit) == tree
        assert reader.entries(commit) == {"README": blob}
        _, files = reader.source_archive(commit)
        assert files == {"README": b"Original data\n"}
        assert calls
    finally:
        reader.close()


def test_empty_transport_allowlist_overrides_inherited_configuration(monkeypatch):
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    env = git_data.git_environment()
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert not any(key.startswith("GIT_CONFIG_") and key.endswith(("COUNT", "_0")) for key in env)


def test_empty_allowlist_denies_even_explicit_local_transport_permission(object_store):
    repository, _, _, _, _ = object_store
    reader = git_data.Git(repository, bounds.Budget())
    try:
        reader.environment.pop("GIT_NO_LAZY_FETCH")
        # The existing local bare store would be a valid ls-remote target.
        # No network route or external helper is part of this control.
        with pytest.raises(bounds.BindingError, match="local_git_unavailable"):
            reader.read("-c", "protocol.file.allow=always", "ls-remote", str(repository))
    finally:
        reader.close()


def test_unisolated_entrypoint_never_authenticates_or_opens_candidate(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/verify_application_witness.py"
    result = subprocess.run(
        [sys.executable, str(script), "--source", "private-invalid-input"],
        cwd=tmp_path,
        capture_output=True,
        timeout=5,
        check=False,
    )
    value = json.loads(result.stdout)
    assert result.returncode == 2 and value["failure_codes"] == ["trusted_isolation_required"]
    assert value["qualification_admission"] is value["reuse_enabled"] is False
    assert b"private" not in result.stdout and not result.stderr


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_cli_binding_exit_and_report_follow_confirmed_owned_cleanup(
    tmp_path, monkeypatch, capsys, cleanup_fails
):
    original, owned = tmp_path / "original", tmp_path / "owned"
    original.mkdir()
    owned.mkdir()
    closed = Mock(side_effect=OSError("private cleanup detail") if cleanup_fails else None)
    reader = SimpleNamespace(repository=original, _reader=owned, environment={}, close=closed)
    monkeypatch.setattr(checker, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=True)))
    monkeypatch.setattr(checker, "Git", Mock(return_value=reader))
    monkeypatch.setattr(checker, "host_program", Mock(return_value="/trusted/gh"))
    monkeypatch.setattr(checker, "process", Mock(return_value=b"synthetic-token"))
    monkeypatch.setattr(checker, "Github", Mock())
    monkeypatch.setattr(
        checker,
        "verify",
        Mock(
            return_value={
                "status": "metadata_artifact_binding_verified",
                "metadata_binding_verified": True,
                **checker.BOUNDARIES,
                "failure_codes": [],
            }
        ),
    )
    report = tmp_path / "report.json"
    result = checker.main(
        [
            "--repository-id",
            str(checker.REPOSITORY_ID),
            "--pr",
            "7",
            "--run",
            "10",
            "--attempt",
            "1",
            "--receipt-artifact",
            "11",
            "--witness-artifact",
            "12",
            "--source",
            "a" * 40,
            "--git-repository",
            str(original),
            "--output",
            str(report),
        ]
    )
    closed.assert_called_once()
    value = json.loads(report.read_bytes())
    assert result == (2 if cleanup_fails else 3)
    assert value["metadata_binding_verified"] is (not cleanup_fails)
    assert value["semantic_validation"] == "not_performed"
    assert value["qualification_admission"] is value["reuse_enabled"] is False
    if cleanup_fails:
        assert value["failure_codes"] == ["local_git_unavailable"]
    output = capsys.readouterr()
    assert "private" not in output.out and not output.err
