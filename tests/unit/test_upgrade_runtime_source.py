"""Source packaging proof with fake Git object reads; no builds or resources."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from qualification.upgrade import runtime_source as source

CANDIDATE = "a" * 40
BASELINE_TREE = "b" * 40
CANDIDATE_TREE = "c" * 40
IMAGE = "registry.example/python@sha256:" + "d" * 64
PYTHON = "3.12.14"


def blob_digest(body):
    return hashlib.sha1(f"blob {len(body)}\0".encode() + body, usedforsecurity=False).hexdigest()


def epoch_files(build):
    package, ray = ("0.4.0", "2.56.0") if build == "baseline" else ("0.5.0", "2.58.0")
    files = {
        "pyproject.toml": (
            '[project]\nname = "django-ray"\n'
            f'version = "{package}"\nrequires-python = ">=3.12"\n'
            f'dependencies = ["ray[default]>={ray}"]\n'
        ).encode(),
        "uv.lock": (
            'version = 1\nrequires-python = ">=3.12"\n'
            f'[[package]]\nname = "ray"\nversion = "{ray}"\n'
            'source = {registry = "https://pypi.org/simple"}\n'
            f'[[package]]\nname = "django-ray"\nversion = "{package}"\n'
            'source = {editable = "."}\n[package.metadata]\n'
            f'requires-dist = [{{name = "ray", extras = ["default"], specifier = ">={ray}"}}]\n'
        ).encode(),
        "src/django_ray/__init__.py": f'__version__ = "{package}"\n'.encode(),
        "src/django_ray/other.py": b"# complete source, not just version fields\n",
    }
    if build == "candidate":
        files.update(dict.fromkeys(source._OVERLAY, b"# fixed qualifier overlay\n"))
        files[source._DOCKERFILE] = (
            Path(__file__).resolve().parents[2].joinpath(source._DOCKERFILE).read_bytes()
        )
    return files


def tar_bytes(commit, files, *, comment=None, extra=()):
    stream = io.BytesIO()
    with tarfile.open(
        fileobj=stream,
        mode="w",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": comment or commit},
    ) as archive:
        for name, body in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
        for member in extra:
            archive.addfile(member)
    return stream.getvalue()


class FakeGit:
    def __init__(self):
        self.files = {
            source.BASELINE_COMMIT: epoch_files("baseline"),
            CANDIDATE: epoch_files("candidate"),
        }
        self.calls = []
        self.statuses = [b"", b"", b""]
        self.heads = [CANDIDATE] * 3
        self.baseline = source.BASELINE_COMMIT
        self.archive_transform = lambda _commit, value: value
        self.tree_transform = lambda value: value
        self.error = None

    def __call__(self, argv, **kwargs):
        assert argv[:8] == [
            "git",
            "--no-optional-locks",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.attributesFile={os.devnull}",
        ]
        assert kwargs["cwd"] == source.ROOT
        assert kwargs["stderr"] == subprocess.DEVNULL and kwargs["check"] is True
        assert kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert "GIT_DIR" not in kwargs["env"]
        assert "GIT_CONFIG_COUNT" not in kwargs["env"]
        arguments = argv[8:]
        self.calls.append(arguments)
        assert kwargs["timeout"] == (30 if arguments[0] == "archive" else 10)
        if self.error:
            raise self.error
        match arguments:
            case ["status", "--porcelain=v1", "-z", "--untracked-files=all"]:
                result = self.statuses.pop(0)
            case ["rev-parse", "--verify", "HEAD^{commit}"]:
                result = (self.heads.pop(0) + "\n").encode()
            case ["rev-parse", "--verify", "v0.4.0^{commit}"]:
                result = (self.baseline + "\n").encode()
            case ["rev-parse", "--verify", expression]:
                result = (
                    (
                        BASELINE_TREE
                        if expression == source.BASELINE_COMMIT + "^{tree}"
                        else CANDIDATE_TREE
                    )
                    + "\n"
                ).encode()
            case ["ls-tree", "-r", "-z", "--full-tree", commit]:
                result = self.tree_transform(
                    b"".join(
                        f"100644 blob {blob_digest(body)}\t{name}\0".encode()
                        for name, body in self.files[commit].items()
                    )
                )
            case ["show", specification]:
                commit, name = specification.split(":", 1)
                result = self.files[commit][name]
            case ["archive", "--format=tar", commit]:
                kwargs["stdout"].write(
                    self.archive_transform(commit, tar_bytes(commit, self.files[commit]))
                )
                result = None
            case _:
                pytest.fail(f"unexpected process arguments: {arguments!r}")
        return subprocess.CompletedProcess(argv, 0, stdout=result)


@pytest.fixture
def git(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setattr(source, "ROOT", root)
    fake = FakeGit()
    monkeypatch.setattr(source.subprocess, "run", fake)
    monkeypatch.setenv("GIT_DIR", "ignored-host-redirection")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "999")
    return fake


def export(tmp_path):
    return source.prepare_runtime_sources(
        tmp_path / "export", python_image=IMAGE, python_version=PYTHON
    )


def test_exports_full_bound_archives_and_exact_unexecuted_build_plans(tmp_path, git):
    result = export(tmp_path)
    destination = tmp_path / "export"
    assert json.loads((destination / "source-plan.json").read_bytes()) == result
    assert result["complete_upgrade_gate"] is False
    assert result["python_image"] == IMAGE and result["python_version"] == PYTHON
    assert (destination / "RuntimeDockerfile").read_bytes() == git.files[CANDIDATE][
        source._DOCKERFILE
    ]
    for build, commit, tree, package, ray in (
        ("baseline", source.BASELINE_COMMIT, BASELINE_TREE, "0.4.0", "2.56.0"),
        ("candidate", CANDIDATE, CANDIDATE_TREE, "0.5.0", "2.58.0"),
    ):
        path = destination / build / "source.tar"
        identity = result["sources"][build]
        assert identity == {
            "commit": commit,
            "tree": tree,
            "archive": str(path),
            "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "lock_sha256": hashlib.sha256(git.files[commit]["uv.lock"]).hexdigest(),
            "package_version": package,
            "ray_version": ray,
        }
        with tarfile.open(path) as archive:
            assert archive.pax_headers["comment"] == commit
            assert set(archive.getnames()) == set(git.files[commit])
        argv = result["build_argv"][build]
        assert argv[:5] == [
            "docker",
            "buildx",
            "build",
            "--file",
            str(destination / "RuntimeDockerfile"),
        ]
        assert argv[5:9] == [
            "--build-context",
            f"epoch-source={destination / build}",
            "--build-context",
            f"qualifier-source={destination / 'candidate'}",
        ]
        build_args = dict(
            argv[index + 1].split("=", 1) for index, arg in enumerate(argv) if arg == "--build-arg"
        )
        assert build_args == {
            "PYTHON_IMAGE": IMAGE,
            "EXPECTED_PYTHON_VERSION": PYTHON,
            "RUNTIME_BUILD": build,
            "EPOCH_SOURCE_COMMIT": commit,
            "EPOCH_SOURCE_SHA256": identity["archive_sha256"],
            "QUALIFIER_SOURCE_COMMIT": CANDIDATE,
            "QUALIFIER_SOURCE_SHA256": result["sources"]["candidate"]["archive_sha256"],
        }
        assert argv[-3:] == [
            "--output",
            f"type=oci,dest={destination / (build + '.oci.tar')}",
            str(destination / build),
        ]
        assert not (destination / (build + ".oci.tar")).exists()
    assert [call for call in git.calls if call[0] == "archive"] == [
        ["archive", "--format=tar", source.BASELINE_COMMIT],
        ["archive", "--format=tar", CANDIDATE],
    ]


@pytest.mark.parametrize(
    "image,version",
    [
        ("python:3.12", PYTHON),
        ("python:3.12@sha256:" + "d" * 64, PYTHON),
        (IMAGE + "\n", PYTHON),
        (None, PYTHON),
        (IMAGE, "3.12"),
        (IMAGE, "3.12.014"),
        (IMAGE, "3.11.10"),
        (IMAGE, None),
    ],
)
def test_invalid_build_inputs_refuse_before_git_or_writes(tmp_path, git, image, version):
    with pytest.raises(source.RuntimeSourceError):
        source.prepare_runtime_sources(
            tmp_path / "export", python_image=image, python_version=version
        )
    assert not git.calls and not (tmp_path / "export").exists()


@pytest.mark.parametrize("kind", ["existing", "inside", "no-parent", "comma", "symlink"])
def test_destination_is_new_external_and_never_reused(tmp_path, git, monkeypatch, kind):
    destination = tmp_path / "export"
    if kind == "existing":
        destination.mkdir()
        (destination / "retain").write_bytes(b"untouched")
    elif kind == "inside":
        destination = source.ROOT / "export"
    elif kind == "no-parent":
        destination = tmp_path / "missing" / "export"
    elif kind == "comma":
        destination = tmp_path / "export,unsafe-output-attribute"
    else:
        original = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink", lambda value: value == destination or original(value)
        )
    with pytest.raises(source.RuntimeSourceError, match="expected-new-source-directory"):
        source.prepare_runtime_sources(destination, python_image=IMAGE, python_version=PYTHON)
    assert not git.calls
    if kind == "existing":
        assert (destination / "retain").read_bytes() == b"untouched"


@pytest.mark.parametrize(
    "dirty",
    [b" M src/module.py\0", b"M  src/module.py\0", b"?? qualification/upgrade/runtime_steps.py\0"],
)
def test_dirty_candidate_cannot_export_any_epoch(tmp_path, git, dirty):
    git.statuses[0] = dirty
    with pytest.raises(source.RuntimeSourceError, match="must-be-clean"):
        export(tmp_path)
    assert not (tmp_path / "export").exists()
    assert not any(call[0] == "archive" for call in git.calls)


@pytest.mark.parametrize(
    "stage",
    [
        "baseline",
        "head",
        "missing-overlay",
        "missing-history",
        "missing-history-settings",
        "missing-artifacts",
        "missing-database",
        "missing-restore",
        "invalid-oid",
        "unsupported-tree",
    ],
)
def test_exact_release_candidate_and_complete_overlay_are_required_before_creation(
    tmp_path, git, stage
):
    if stage == "baseline":
        git.baseline = "e" * 40
    elif stage == "head":
        git.heads[0] = source.BASELINE_COMMIT
    elif stage == "missing-overlay":
        del git.files[CANDIDATE]["qualification/upgrade/runtime_steps.py"]
    elif stage == "missing-history":
        del git.files[CANDIDATE]["qualification/upgrade/runtime_history.py"]
    elif stage == "missing-history-settings":
        del git.files[CANDIDATE]["qualification/upgrade/runtime_history_settings.py"]
    elif stage in {"missing-artifacts", "missing-database", "missing-restore"}:
        del git.files[CANDIDATE][
            "qualification/upgrade/runtime_" + stage.removeprefix("missing-") + ".py"
        ]
    elif stage == "invalid-oid":
        git.heads[0] = "private-not-an-object"
    else:
        git.tree_transform = lambda value: value.replace(b"100644 blob", b"160000 commit", 1)
    with pytest.raises(source.RuntimeSourceError):
        export(tmp_path)
    assert not (tmp_path / "export").exists()


@pytest.mark.parametrize("build", ["baseline", "candidate"])
@pytest.mark.parametrize(
    "change",
    [
        "project",
        "floor",
        "lock",
        "own-lock",
        "source",
        "runtime-version",
        "bad-toml",
        "duplicate-ray",
    ],
)
def test_each_exact_epoch_graph_is_checked_before_any_archive(tmp_path, git, build, change):
    files = git.files[source.BASELINE_COMMIT if build == "baseline" else CANDIDATE]
    if change == "project":
        files["pyproject.toml"] = files["pyproject.toml"].replace(
            b"0.4.0" if build == "baseline" else b"0.5.0", b"0.6.0"
        )
    elif change == "floor":
        files["pyproject.toml"] = files["pyproject.toml"].replace(
            b"ray[default]>=", b"ray[default]=="
        )
    elif change == "lock":
        files["uv.lock"] = files["uv.lock"].replace(
            b"2.56.0" if build == "baseline" else b"2.58.0", b"2.59.0"
        )
    elif change == "own-lock":
        files["uv.lock"] = files["uv.lock"].replace(
            b"0.4.0" if build == "baseline" else b"0.5.0", b"0.6.0"
        )
    elif change == "source":
        files["uv.lock"] = files["uv.lock"].replace(
            b"https://pypi.org/simple", b"https://other.example/simple"
        )
    elif change == "runtime-version":
        files["src/django_ray/__init__.py"] = b'__version__ = "0.6.0"\n'
    elif change == "bad-toml":
        files["uv.lock"] = b"not valid TOML"
    else:
        files["uv.lock"] += b'\n[[package]]\nname = "ray"\nversion = "2.58.0"\n'
    with pytest.raises(source.RuntimeSourceError, match="epoch-metadata"):
        export(tmp_path)
    assert not (tmp_path / "export").exists()


@pytest.mark.parametrize(
    "change",
    [
        "commit",
        "omitted-file",
        "substituted-file",
        "extra-file",
        "symlink",
        "traversal",
        "duplicate",
    ],
)
def test_archive_matches_full_git_tree_not_just_pax_label(tmp_path, git, change):
    def transform(commit, _body):
        files = dict(git.files[commit])
        comment = commit
        extra = []
        if change == "commit":
            comment = "e" * 40
        elif change == "omitted-file":
            del files["src/django_ray/other.py"]
        elif change == "substituted-file":
            files["src/django_ray/other.py"] = b"# altered by export-subst\n"
        elif change == "extra-file":
            files["extra.py"] = b"# not in tree\n"
        elif change == "symlink":
            member = tarfile.TarInfo("link")
            member.type = tarfile.SYMTYPE
            member.linkname = "src/django_ray/other.py"
            extra.append(member)
        elif change == "traversal":
            files["../outside"] = b""
        else:
            extra.append(tarfile.TarInfo("src/django_ray/other.py"))
        return tar_bytes(commit, files, comment=comment, extra=extra)

    git.archive_transform = transform
    with pytest.raises(source.RuntimeSourceError):
        export(tmp_path)
    destination = tmp_path / "export"
    assert destination.is_dir() and not (destination / "source-plan.json").exists()
    calls = len(git.calls)
    with pytest.raises(source.RuntimeSourceError, match="expected-new-source-directory"):
        export(tmp_path)
    assert len(git.calls) == calls


@pytest.mark.parametrize("limit", ["_MAX_ARCHIVE", "_MAX_CONTENT", "_MAX_MEMBERS"])
def test_archive_limits_refuse_without_publishing_plan(tmp_path, git, monkeypatch, limit):
    if limit == "_MAX_MEMBERS":
        # Tree files are also bounded; add directory entries only at archive time.
        monkeypatch.setattr(source, limit, 20)
        git.archive_transform = lambda commit, _body: tar_bytes(
            commit, git.files[commit], extra=[tarfile.TarInfo(f"dir{index}") for index in range(30)]
        )
    else:
        monkeypatch.setattr(source, limit, 1)
    with pytest.raises(source.RuntimeSourceError):
        export(tmp_path)
    assert not (tmp_path / "export" / "source-plan.json").exists()


@pytest.mark.parametrize("before_write", [False, True])
def test_head_change_before_or_during_export_cannot_produce_plan(tmp_path, git, before_write):
    git.heads[1 if before_write else 2] = "e" * 40
    with pytest.raises(source.RuntimeSourceError, match="candidate-changed"):
        export(tmp_path)
    assert not (tmp_path / "export" / "source-plan.json").exists()
    assert (tmp_path / "export").exists() is not before_write


def test_dirty_change_after_archive_keeps_only_incomplete_output(tmp_path, git):
    git.statuses[2] = b" M changed.py\0"
    with pytest.raises(source.RuntimeSourceError, match="must-be-clean"):
        export(tmp_path)
    assert (tmp_path / "export" / "candidate" / "source.tar").is_file()
    assert not (tmp_path / "export" / "source-plan.json").exists()


@pytest.mark.parametrize(
    "error",
    [
        OSError("private host detail"),
        subprocess.TimeoutExpired("private argv", 10),
        subprocess.CalledProcessError(1, "private argv", stderr="private stderr"),
    ],
)
def test_git_failure_is_bounded_and_redacted_before_writes(tmp_path, git, error):
    git.error = error
    with pytest.raises(source.RuntimeSourceError, match="source-git-command-failed") as caught:
        export(tmp_path)
    assert "private" not in str(caught.value)
    assert not (tmp_path / "export").exists()


def test_cli_only_exports_and_reports_missing_image_native_proof(
    tmp_path, git, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runtime_source",
            str(tmp_path / "export"),
            "--python-image",
            IMAGE,
            "--python-version",
            PYTHON,
        ],
    )
    source.main()
    assert (
        capsys.readouterr().out
        == "runtime-source-exported; image-build-and-native-qualification-not-run\n"
    )
    with pytest.raises(SystemExit, match="partial directory"):
        source.main()
