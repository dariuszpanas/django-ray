"""An exclusively owned bare reader, with authenticated bounded Git object data."""

from __future__ import annotations

import hashlib
import io
import os
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from .bounds import MIB, BindingError, process, require, sha


def host_program(name, forbidden):
    filename = name + (".exe" if os.name == "nt" else "")
    for value in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(value)
        if not value or not directory.is_absolute():
            continue
        candidate = (directory / filename).resolve()
        if (
            candidate.is_file()
            and os.access(candidate, os.X_OK)
            and not candidate.is_relative_to(forbidden)
        ):
            return str(candidate)
    raise BindingError("local_git_unavailable", unavailable=True)


def git_environment():
    # No GIT_CONFIG_COUNT, trace, exec, SSH, attributes, replacement or fetch
    # overrides survive. No candidate config is opened by the private reader.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            # Git 2.43 predates NO_LAZY_FETCH. The private reader has no
            # promisor configuration; the empty allowlist also denies every
            # transport regardless of per-protocol configuration on all hosts.
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return env


def object_digest(kind, raw):
    return hashlib.sha1(
        kind.encode("ascii") + b" " + str(len(raw)).encode("ascii") + b"\0" + raw
    ).hexdigest()


class Git:
    """Never read original configuration, refs, templates, hooks, or worktree data."""

    def __init__(self, repository, budget):
        self.repository, self.budget = Path(repository).resolve(strict=True), budget
        self.executable = host_program("git", self.repository)
        self.environment = git_environment()
        self._temporary = None
        self._snapshots = {}
        try:
            directory = self.repository
            marker = directory / ".git"
            if marker.is_file():
                with marker.open("rb") as source:
                    raw = source.read(4097)
                require(
                    len(raw) <= 4096 and raw.startswith(b"gitdir: "),
                    "local_git_unavailable",
                    unavailable=True,
                )
                directory = (directory / raw[8:].decode().strip()).resolve(strict=True)
            elif marker.is_dir():
                directory = marker.resolve(strict=True)
            common = directory / "commondir"
            if common.is_file():
                with common.open("rb") as source:
                    raw = source.read(4097)
                require(0 < len(raw) <= 4096, "local_git_unavailable", unavailable=True)
                directory = (directory / raw.decode().strip()).resolve(strict=True)
            objects = (directory / "objects").resolve(strict=True)
            require(
                objects.is_dir() and not any(c in str(objects) for c in "\r\n\0"),
                "local_git_unavailable",
                unavailable=True,
            )
            self._temporary = tempfile.TemporaryDirectory(prefix="django-ray-witness-objects-")
            reader = Path(self._temporary.name)
            (reader / "objects/info").mkdir(parents=True)
            (reader / "refs").mkdir()
            (reader / "objects/info/alternates").write_text(
                objects.as_posix() + "\n", encoding="utf-8", newline="\n"
            )
            (reader / "HEAD").write_text("ref: refs/heads/unborn\n", encoding="ascii", newline="\n")
            (reader / "config").write_text(
                "[core]\nrepositoryformatversion = 0\nbare = true\n", encoding="ascii", newline="\n"
            )
            self._reader = reader
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def read(self, *args, maximum=4 * MIB, input_data=None):
        return process(
            [
                self.executable,
                "--no-replace-objects",
                "--no-optional-locks",
                "--git-dir=" + str(self._reader),
                "-c",
                "protocol.allow=never",
                "-c",
                "credential.helper=",
                "-c",
                "core.attributesFile=" + os.devnull,
                *args,
            ],
            maximum,
            self.budget,
            env=self.environment,
            input_data=input_data,
            cwd=self._reader,
        )

    def object(self, oid, kind, maximum=16 * MIB):
        raw = self.read("cat-file", kind, sha(oid), maximum=maximum)
        require(object_digest(kind, raw) == oid, "source_archive_invalid")
        return raw

    def tree(self, commit):
        raw = self.object(sha(commit), "commit", MIB)
        first = raw.split(b"\n", 1)[0]
        require(re.fullmatch(b"tree [a-f0-9]{40}", first), "source_archive_invalid")
        tree = first[5:].decode("ascii")
        self.object(tree, "tree", MIB)
        return tree

    def entries(self, commit):
        if commit in self._snapshots:
            return self._snapshots[commit]
        root = self.tree(commit)
        inventory = {commit, root}
        # This preliminary list is untrusted. Every object is then hashed, and
        # every tree edge is reconstructed independently from verified bytes.
        for row in self.read("ls-tree", "-r", "-t", "-z", sha(commit)).split(b"\0"):
            if row:
                metadata, _ = row.split(b"\t", 1)
                _, _, oid = metadata.decode("ascii").split()
                inventory.add(sha(oid))
                require(len(inventory) <= 4096, "source_archive_invalid")
        ordered = sorted(inventory)
        data = self.read(
            "cat-file",
            "--batch",
            maximum=68 * MIB,
            input_data="".join(oid + "\n" for oid in ordered).encode("ascii"),
        )
        cursor, total, objects = 0, 0, {}
        for oid in ordered:
            end = data.find(b"\n", cursor)
            require(cursor < end <= cursor + 128, "source_archive_invalid")
            header = data[cursor:end].decode("ascii").split()
            if header == [oid, "missing"]:
                raise BindingError("local_git_unavailable", unavailable=True)
            require(
                len(header) == 3
                and header[0] == oid
                and header[1] in {"commit", "tree", "blob"}
                and re.fullmatch("[0-9]{1,9}", header[2]),
                "source_archive_invalid",
            )
            size = int(header[2])
            total += size
            require(size <= 16 * MIB and total <= 64 * MIB, "source_archive_invalid")
            cursor = end + 1
            raw = data[cursor : cursor + size]
            require(
                len(raw) == size
                and data[cursor + size : cursor + size + 1] == b"\n"
                and object_digest(header[1], raw) == oid,
                "source_archive_invalid",
            )
            objects[oid] = (header[1], raw)
            cursor += size + 1
        require(cursor == len(data), "source_archive_invalid")
        entries, pending, visited = {}, [("", root)], set()
        while pending:
            prefix, oid = pending.pop()
            require((prefix, oid) not in visited and len(visited) <= 4096, "source_archive_invalid")
            visited.add((prefix, oid))
            kind, raw = objects[oid]
            require(kind == "tree", "source_archive_invalid")
            offset, names = 0, set()
            while offset < len(raw):
                separator, end = raw.find(b" ", offset), raw.find(b"\0", offset)
                require(offset < separator < end and end + 21 <= len(raw), "source_archive_invalid")
                mode, name = raw[offset:separator], raw[separator + 1 : end].decode("utf-8")
                require(
                    name
                    and name not in names
                    and name not in {".", ".."}
                    and not any(c in name for c in "/\\:\0")
                    and all(ord(c) >= 32 for c in name),
                    "source_archive_invalid",
                )
                names.add(name)
                child = raw[end + 1 : end + 21].hex()
                require(child in objects, "source_archive_invalid")
                path = prefix + name
                if mode == b"40000":
                    pending.append((path + "/", child))
                else:
                    require(
                        mode in {b"100644", b"100755"}
                        and objects[child][0] == "blob"
                        and len(entries) < 4096,
                        "source_archive_invalid",
                    )
                    entries[path] = child
                offset = end + 21
        self._snapshots[commit] = entries
        return entries

    def source_archive(self, commit):
        expected = self.entries(commit)
        raw = self.read(
            "-c", "core.autocrlf=false", "archive", "--format=tar", sha(commit), maximum=64 * MIB
        )
        files = {}
        with tarfile.open(fileobj=io.BytesIO(raw)) as source:
            require(source.pax_headers.get("comment") == commit, "source_archive_invalid")
            members = source.getmembers()
            require(
                len(members) <= 4096 and sum(item.size for item in members) <= 64 * MIB,
                "source_archive_invalid",
            )
            for member in members:
                path = PurePosixPath(member.name)
                require(
                    not path.is_absolute()
                    and ".." not in path.parts
                    and (member.isdir() or member.isfile()),
                    "source_archive_invalid",
                )
                if member.isfile():
                    require(
                        member.name not in files and 0 <= member.size <= 16 * MIB,
                        "source_archive_invalid",
                    )
                    stream = source.extractfile(member)
                    if stream is None:
                        raise BindingError("source_archive_invalid")
                    with stream:
                        data = stream.read()
                    require(
                        expected.get(member.name) == object_digest("blob", data),
                        "source_archive_invalid",
                    )
                    files[member.name] = data
        require(set(files) == set(expected), "source_archive_invalid")
        return raw, files
