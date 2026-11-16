"""Bounded data readers. No candidate Python, archive extraction or public writes."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import math
import queue
import re
import socket
import stat
import struct
import subprocess
import threading
import time
import unicodedata
import zipfile
import zlib
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import urlsplit

REPOSITORY = "dariuszpanas/django-ray"
PREFIX = "repos/" + REPOSITORY
MIB = 1024 * 1024
MAX_JSON = 2 * MIB
MAX_ZIP = MIB
MAX_OUTPUT = 64 * 1024
CODES = frozenset(
    """request_invalid verification_budget_exceeded transport_unavailable
    transport_route_invalid redirect_target_invalid transport_response_invalid
    original_artifact_unavailable json_invalid archive_structure_invalid
    archive_budget_exceeded artifact_integrity_mismatch local_git_unavailable
    source_archive_invalid output_invalid trusted_isolation_required
    metadata_invalid identity_mismatch workflow_definition_unsupported
    original_merge_context_unavailable merge_parent_mismatch original_source_mismatch
    direct_record_incomplete witness_schema_invalid receipt_manifest_mismatch
    direct_steps_mismatch artifact_provenance_mismatch attempt_unsupported
    qualification_profile_unsupported history_incomplete latest_relevant_run_not_successful
    context_changed semantic_validation_not_performed unexpected_input
    """.split()
)


class BindingError(ValueError):
    """Only fixed categories may leave the checker."""

    def __init__(self, code: str, *, unavailable: bool = False):
        self.code = code if code in CODES else "unexpected_input"
        self.unavailable = unavailable
        super().__init__(self.code)


def require(condition, code="metadata_invalid", *, unavailable=False):
    if not condition:
        raise BindingError(code, unavailable=unavailable)


def identity(value):
    require(type(value) is int and 0 < value <= 2**63 - 1, "request_invalid")
    return value


def sha(value):
    require(type(value) is str and re.fullmatch("[a-f0-9]{40}", value), "request_invalid")
    return value


def canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def decode(raw, maximum=MAX_JSON):
    require(type(raw) is bytes and 0 < len(raw) <= maximum, "json_invalid")

    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, "json_invalid")
            result[key] = value
        return result

    def integer(value):
        require(len(value) <= 20, "json_invalid")
        result = int(value)
        require(abs(result) <= 2**63 - 1, "json_invalid")
        return result

    def number(value):
        result = float(value)
        require(math.isfinite(result), "json_invalid")
        return result

    def constant(_value):
        raise BindingError("json_invalid")

    try:
        result = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_int=integer,
            parse_float=number,
            parse_constant=constant,
        )
        pending, count = [(result, 0)], 0
        while pending:
            value, depth = pending.pop()
            count += 1
            require(depth <= 32 and count <= 40000, "json_invalid")
            if type(value) is str:
                require(len(value) <= 65536, "json_invalid")
            elif type(value) is dict:
                pending.extend((key, depth + 1) for key in value)
                pending.extend((child, depth + 1) for child in value.values())
            elif type(value) is list:
                pending.extend((child, depth + 1) for child in value)
        return result
    except (ValueError, UnicodeError, OverflowError, RecursionError):
        raise BindingError("json_invalid") from None


def timestamp(value):
    require(
        type(value) is str
        and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?Z", value),
        "metadata_invalid",
    )
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise BindingError("metadata_invalid") from None


class Budget:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock() + 240
        self.operation_deadline = self.deadline - 7
        self.calls = self.bytes = 0

    def remaining(self, maximum=30):
        remaining = min(maximum, self.operation_deadline - self.clock())
        require(remaining > 0, "verification_budget_exceeded", unavailable=True)
        return remaining

    def request(self):
        self.remaining()
        self.calls += 1
        require(self.calls <= 48, "verification_budget_exceeded", unavailable=True)

    def received(self, count):
        self.remaining()
        self.bytes += count
        require(self.bytes <= 14 * MIB, "verification_budget_exceeded", unavailable=True)


def process(argv, maximum, budget, *, env=None, input_data=None, cwd=None):
    """Bound output and waiting; close/reap owned tools on every result."""
    require(type(maximum) is int and 0 < maximum <= 68 * MIB, "request_invalid")
    require(
        input_data is None or (type(input_data) is bytes and len(input_data) <= 512 * 1024),
        "request_invalid",
    )
    budget.remaining()
    output, threads = bytearray(), []
    overflow, failed = threading.Event(), threading.Event()
    child = None
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
        )
        stdout = child.stdout
        if stdout is None:
            raise BindingError("local_git_unavailable", unavailable=True)

        def read():
            try:
                while block := stdout.read(min(65536, maximum + 1 - len(output))):
                    output.extend(block)
                    if len(output) > maximum:
                        overflow.set()
                        try:
                            child.kill()
                        except OSError:
                            pass
                        break
            except (OSError, ValueError):
                failed.set()
            finally:
                try:
                    stdout.close()
                except (OSError, ValueError):
                    failed.set()

        reader = threading.Thread(target=read, daemon=True)
        threads.append(reader)
        reader.start()
        if input_data is not None:
            stdin = child.stdin
            if stdin is None:
                raise BindingError("local_git_unavailable", unavailable=True)

            def write():
                try:
                    stdin.write(input_data)
                    stdin.flush()
                except (OSError, ValueError):
                    failed.set()
                finally:
                    try:
                        stdin.close()
                    except (OSError, ValueError):
                        failed.set()

            writer = threading.Thread(target=write, daemon=True)
            threads.append(writer)
            writer.start()
        try:
            child.wait(timeout=budget.remaining())
        except subprocess.TimeoutExpired:
            raise BindingError("verification_budget_exceeded", unavailable=True) from None
        for thread in threads:
            thread.join(timeout=budget.remaining())
        require(
            not any(thread.is_alive() for thread in threads),
            "verification_budget_exceeded",
            unavailable=True,
        )
        require(not overflow.is_set(), "verification_budget_exceeded", unavailable=True)
        require(
            not failed.is_set() and child.returncode == 0, "local_git_unavailable", unavailable=True
        )
        return bytes(output)
    except (OSError, subprocess.SubprocessError):
        raise BindingError("local_git_unavailable", unavailable=True) from None
    finally:
        if child is not None:
            if child.poll() is None:
                try:
                    child.kill()
                except OSError:
                    pass
            try:
                child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                raise BindingError("verification_budget_exceeded", unavailable=True) from None
            for thread in threads:
                thread.join(timeout=1)
            require(
                not any(thread.is_alive() for thread in threads),
                "verification_budget_exceeded",
                unavailable=True,
            )
            for stream in (child.stdin, child.stdout):
                if stream is not None and not stream.closed:
                    stream.close()


def route(value: str) -> str:
    if value == PREFIX:
        return "https://api.github.com/" + PREFIX
    suffix = value.removeprefix(PREFIX + "/") if type(value) is str else ""
    patterns = (
        r"",
        r"pulls/[1-9][0-9]{0,18}",
        r"actions/workflows/(?:qualification-gate|ci)\.yml",
        r"actions/runs/[1-9][0-9]{0,18}(?:/attempts/1(?:/jobs\?per_page=64)?)?",
        r"actions/jobs/[1-9][0-9]{0,18}",
        r"actions/artifacts/[1-9][0-9]{0,18}(?:/zip)?",
        r"git/commits/[a-f0-9]{40}",
        r"git/trees/[a-f0-9]{40}\?recursive=1",
        r"actions/workflows/qualification-gate\.yml/runs\?head_sha=[a-f0-9]{40}&per_page=32&page=1",
    )
    require(
        type(value) is str
        and value.startswith(PREFIX + "/")
        and any(re.fullmatch(pattern, suffix) for pattern in patterns),
        "transport_route_invalid",
    )
    return "https://api.github.com/" + value


def redirect(value: str):
    require(type(value) is str and len(value) <= 16384, "redirect_target_invalid")
    parsed = urlsplit(value)
    require(
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
        and not parsed.fragment
        and parsed.hostname is not None
        and re.fullmatch(
            r"productionresultssa[0-9]{1,5}\.blob\.core\.windows\.net", parsed.hostname
        ),
        "redirect_target_invalid",
    )
    return parsed


def resolve(host, budget, deadline):
    result = queue.Queue(maxsize=1)

    def lookup():
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError:
            result.put(None)

    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    try:
        remaining = min(budget.remaining(), deadline - budget.clock())
        require(remaining > 0, "verification_budget_exceeded", unavailable=True)
        addresses = result.get(timeout=remaining)
    except queue.Empty:
        raise BindingError("verification_budget_exceeded", unavailable=True) from None
    require(addresses and len(addresses) <= 16, "transport_unavailable", unavailable=True)
    require(
        all(ipaddress.ip_address(row[4][0]).is_global for row in addresses),
        "redirect_target_invalid",
    )
    return addresses[0]


def archive(raw, names, *, witness=False):
    """Authenticate topology before reading only fixed members in memory."""
    require(type(raw) is bytes and 0 < len(raw) <= MAX_ZIP, "archive_budget_exceeded")
    try:
        end = raw.rfind(b"PK\x05\x06")
        require(end >= 0 and end + 22 <= len(raw), "archive_structure_invalid")
        _, disk, directory_disk, count, total, size, offset, comment = struct.unpack_from(
            "<4s4H2IH", raw, end
        )
        require(
            disk == directory_disk == 0 and count == total and 0 < count <= 128,
            "archive_structure_invalid",
        )
        require(
            end + 22 + comment == len(raw) and offset + size == end and size <= 128 * 1024,
            "archive_structure_invalid",
        )
        selected, selected_infos, aliases, spans = {}, {}, set(), []
        with zipfile.ZipFile(io.BytesIO(raw)) as zipped:
            entries = zipped.infolist()
            require(
                len(entries) == count and zipped.start_dir == offset, "archive_structure_invalid"
            )
            require(sum(item.file_size for item in entries) <= 4 * MIB, "archive_budget_exceeded")
            files = set()
            for item in entries:
                name = item.filename.removesuffix("/")
                path = PurePosixPath(name)
                require(
                    name
                    and not path.is_absolute()
                    and path.as_posix() == name
                    and "\\" not in name
                    and all(
                        part not in {".", ".."}
                        and not part.endswith((" ", "."))
                        and ":" not in part
                        for part in path.parts
                    )
                    and all(ord(c) >= 32 and ord(c) != 127 for c in name),
                    "archive_structure_invalid",
                )
                alias = unicodedata.normalize("NFC", name).casefold()
                require(alias not in aliases, "archive_structure_invalid")
                aliases.add(alias)
                kind = stat.S_IFMT(item.external_attr >> 16)
                require(
                    kind in (0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG),
                    "archive_structure_invalid",
                )
                require(
                    item.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    and not item.flag_bits & ~0x080E,
                    "archive_structure_invalid",
                )
                require(
                    0 <= item.file_size <= 4 * MIB and 0 <= item.compress_size <= MAX_ZIP,
                    "archive_budget_exceeded",
                )
                header = item.header_offset
                require(0 <= header and header + 30 <= offset, "archive_structure_invalid")
                values = struct.unpack_from("<4s5H3I2H", raw, header)
                signature, _, flags, compression, _, _, crc, compressed, expanded, nlen, xlen = (
                    values
                )
                start = header + 30 + nlen + xlen
                finish = start + item.compress_size
                require(
                    signature == b"PK\x03\x04"
                    and flags == item.flag_bits
                    and compression == item.compress_type
                    and finish <= offset,
                    "archive_structure_invalid",
                )
                encoded = item.filename.encode("utf-8" if flags & 0x800 else "cp437")
                require(
                    raw[header + 30 : header + 30 + nlen] == encoded, "archive_structure_invalid"
                )
                if flags & 8:
                    descriptor = finish + (4 if raw[finish : finish + 4] == b"PK\x07\x08" else 0)
                    wide = compressed == 0xFFFFFFFF or expanded == 0xFFFFFFFF
                    shape = "<IQQ" if wide else "<III"
                    finish = descriptor + struct.calcsize(shape)
                    require(
                        finish <= offset
                        and struct.unpack_from(shape, raw, descriptor)
                        == (item.CRC, item.compress_size, item.file_size),
                        "archive_structure_invalid",
                    )
                else:
                    require(
                        crc == item.CRC
                        and compressed == item.compress_size
                        and expanded == item.file_size,
                        "archive_structure_invalid",
                    )
                spans.append((header, finish))
                if not item.is_dir():
                    files.add(name)
                if item.filename in names:
                    limit = 32 * 1024 if witness else 256 * 1024
                    require(0 < item.file_size <= limit, "archive_budget_exceeded")
                    selected_infos[item.filename] = item
            spans.sort()
            require(
                all(left[1] <= right[0] for left, right in zip(spans, spans[1:], strict=False)),
                "archive_structure_invalid",
            )
            require(
                all(
                    not any(
                        str(parent) in files
                        for parent in PurePosixPath(name).parents
                        if str(parent) != "."
                    )
                    for name in files
                ),
                "archive_structure_invalid",
            )
            require(set(selected_infos) == set(names), "receipt_manifest_mismatch")
            require(
                sum(item.file_size for item in selected_infos.values()) <= 2 * MIB,
                "archive_budget_exceeded",
            )
            for name, item in selected_infos.items():
                limit = 32 * 1024 if witness else 256 * 1024
                with zipped.open(item) as stream:
                    payload = stream.read(limit + 1)
                require(len(payload) == item.file_size, "archive_structure_invalid")
                selected[name] = payload

            require(
                not witness or [item.filename for item in entries] == ["execution-witness.json"],
                "archive_structure_invalid",
            )
            require(sum(map(len, selected.values())) <= 2 * MIB, "archive_budget_exceeded")
        return selected
    except BindingError:
        raise
    except (
        OSError,
        ValueError,
        UnicodeError,
        RuntimeError,
        struct.error,
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
    ):
        raise BindingError("archive_structure_invalid") from None


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()
