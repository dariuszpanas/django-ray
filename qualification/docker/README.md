# Source-owned Docker qualification

This directory owns the shared bounded scenario and its Docker operational definition for an
external validation control plane. The companion
[Kubernetes runbook](../kubernetes/README.md) selects the same scenario through its own source-owned
runtime contract. The scenario installs the candidate wheel into a fresh temporary target and then
invokes the existing exact source test:

`tests/unit/test_result_fold.py::test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup`

That test is already in the mandatory serial `real_ray` lane. It verifies exact actor resources,
runtime-environment delivery, ordered folding of out-of-order inputs, direct `ObjectRef` return,
actor termination, and final `ray.shutdown()`. This definition does not copy those assertions into
another workflow engine and does not replace or weaken any existing django-ray gate.

## Exact candidate build

The image must be built from a temporary directory extracted from `git archive <full-commit>`, not
from an ordinary working-tree directory. This is a correctness boundary on Windows: with
`core.autocrlf=true`, working-tree files may contain CRLF while both Git objects and the control
plane's materialized archive contain LF. Building from the working tree would therefore bake a
wheel whose package-tree digest cannot match the exact archived source later mounted at
`/workspace`.

One PowerShell-safe outline is:

```powershell
$Commit = git rev-parse --verify 'HEAD^{commit}'
if ($LASTEXITCODE -ne 0) {
  throw "Git could not resolve the candidate commit"
}
$Commit = "$Commit".Trim()
if ($Commit -notmatch '^(?:[0-9a-f]{40}|[0-9a-f]{64})$') {
  throw "Git returned a non-canonical full commit"
}

$QualificationRun = [guid]::NewGuid().ToString("N")
$TemporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$ResolvedTemporaryRoot = $TemporaryRoot.TrimEnd(
  [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
)
$BaseName = "django-ray-qualification-$QualificationRun"
$Context = [IO.Path]::GetFullPath((Join-Path $TemporaryRoot $BaseName))
$Archive = [IO.Path]::GetFullPath((Join-Path $TemporaryRoot "$BaseName.tar"))
$IidFile = [IO.Path]::GetFullPath((Join-Path $TemporaryRoot "$BaseName.iid"))
$DockerContext = "desktop-linux"
$SourceTag = "django-ray-qualification:candidate-$QualificationRun"

foreach ($Path in @($Context, $Archive, $IidFile)) {
  if ([IO.Path]::GetDirectoryName($Path) -ine $ResolvedTemporaryRoot) {
    throw "Refusing a qualification path outside the resolved temporary root"
  }
  if (Test-Path -LiteralPath $Path) {
    throw "Refusing to reuse a qualification temporary path"
  }
}

try {
  New-Item -ItemType Directory -Path $Context -ErrorAction Stop | Out-Null
  git archive --format=tar --output=$Archive $Commit
  if ($LASTEXITCODE -ne 0) {
    throw "Git could not archive the candidate commit"
  }
  tar -xf $Archive -C $Context
  if ($LASTEXITCODE -ne 0) {
    throw "Tar could not extract the candidate archive"
  }
  docker --context $DockerContext build --file "$Context/qualification/docker/Dockerfile" `
    --tag $SourceTag --iidfile $IidFile $Context
  if ($LASTEXITCODE -ne 0) {
    throw "Docker could not build the candidate image"
  }

  $ImageId = (Get-Content -LiteralPath $IidFile -Raw -ErrorAction Stop).Trim()
  if ($ImageId -notmatch '^sha256:[0-9a-f]{64}$') {
    throw "Docker wrote a non-canonical image ID"
  }
  $InspectedImageId = docker --context $DockerContext image inspect --format '{{.Id}}' $SourceTag
  if ($LASTEXITCODE -ne 0) {
    throw "Docker could not inspect the candidate image"
  }
  $InspectedImageId = "$InspectedImageId".Trim()
  if ($InspectedImageId -notmatch '^sha256:[0-9a-f]{64}$') {
    throw "Docker inspected a non-canonical image ID"
  }
  if ($InspectedImageId -cne $ImageId) {
    throw "The built and inspected image IDs differ"
  }
  "Built unique source tag $SourceTag"
  "Use exact control-plane --image $ImageId"
}
finally {
  if (Test-Path -LiteralPath $IidFile) {
    Remove-Item -LiteralPath $IidFile -Force
  }
  if (Test-Path -LiteralPath $Archive) {
    Remove-Item -LiteralPath $Archive -Force
  }
  if (Test-Path -LiteralPath $Context) {
    Remove-Item -LiteralPath $Context -Recurse -Force
  }
}
```

The generated GUID confines cleanup to this command's exact archive, context, and image-ID file;
the recipe verifies that each resolves directly beneath the selected temporary root and refuses to
reuse any of those paths. Every native command is checked before continuing, and the ID written by
`docker build --iidfile` must exactly match a fresh inspection of the unique tag in the same named
Docker context. The `finally` block removes only those GUID-specific temporary paths after the raw
image ID has been captured and validated. Pass that raw
`sha256:<64-hex>` ID directly to the control-plane `--image` option; do not synthesize a repository
digest from the unique source tag. The final image carries the dependency environment and exactly one
locally built wheel. It does not preinstall django-ray as the tested package. The virtual
environment is created at `/opt/qualification/.venv` in the builder and copied to that same absolute
path so its Python entry-point shebangs remain valid. The build removes uv's output-directory
`.gitignore` and asserts that the runtime wheel directory contains exactly one entry and that entry
is a regular wheel before copying it into the final image.

At runtime, the wrapper runs as UID/GID 65532, selects exactly one baked wheel, and uses `uv pip
install --offline --no-index --no-deps` into `/tmp/django-ray-qualification/target`. It rejects a
pre-existing target, a pre-imported `django_ray`, an unexpected distribution or import location, a
version mismatch, or any difference between the installed and archived `src/django_ray` trees.

## Runtime profile and evidence

The schema-v1 runbook requires the `docker`, `linux`, `python`, and `ray` capabilities; a 420-second
execution timeout; an always-run 180-second cleanup timeout; and exactly the `junit`, `log`, and
`manifest` evidence kinds. The corresponding capacity-one Docker pool must bound the target to:

- 2 CPUs (the selected test declares a two-CPU local Ray runtime);
- 2 GiB memory and 512 PIDs;
- 512 MiB writable `/tmp` and 1 GiB `/dev/shm`;
- an internal network with no published ports, no cloud credentials, and no AWS resources.

The successful JUnit file is reduced to one stable suite/case identity after pytest proves the exact
node passed, removing host names, timestamps, durations, and captured output. The deterministic JSON
manifest records the selected allowlisted definition path, wheel SHA-256, installed/source tree
SHA-256, distribution and import paths, direct Python/Django/Ray/django-ray/pytest versions, and a
bounded sorted inventory of all installed distributions. The Docker command defaults to
`qualification/docker/runbook.yaml`; the Kubernetes runbook passes its one separately accepted
literal path. Its generic `target` block repeats only Python and canonical dependency names and
versions so an adapter can expose that allowlisted summary without understanding this source-owned
schema. Repository, full commit, schema version, normalized definition digest, image digest, and
exact cleanup remain control-plane-owned run snapshot evidence.

Pytest stdout and stderr are not replayed on success because their duration text is volatile. On
failure they are emitted to the adapter-owned log with a 32 KiB cap per stream. The Linux wrapper
starts each installer or pytest command in a new process group; timeout, output overflow, and capture
failures terminate that complete group so local Ray descendants cannot outlive the scenario. The
image default `DJANGO_RAY_QUALIFICATION_HOLD_SECONDS=10` creates a short post-success cancellation
window; the module default is zero when the environment variable is absent, keeping resource-free
unit tests instant. Values outside zero through 30 seconds fail closed.

One counted comparison pairs this exact application run with the unchanged django-ray local-Ray
gate on the same frozen source commit, wheel/image digest, and dependency tuple. Three consecutive
pairs count only after the final control-plane candidate is frozen; a failed or interrupted half
resets the sequence. This definition provisions no Docker, Kubernetes, database, or cloud resources
itself. The external adapter owns container lifecycle, evidence collection, and exact cleanup.
