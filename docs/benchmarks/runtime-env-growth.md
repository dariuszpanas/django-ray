# RuntimeEnv extraction attribution experiment

The fixed Linux experiment for [issue #181](https://github.com/P4n45/django-ray/issues/181)
compares raw Ray packaging with django-ray's snapshot and Core preparation path.
It is an investigation recipe, with no measured result checked in. It does not
change production exclusions, cache lifetimes, or cleanup defaults. A completed
sample does not establish a production storage budget or resolve issue #181.
The historical approximately 1.2 GB across 18 sessions remains an unrefreshed
observation; valid extracted files and active process residue are different facts.

## Run and interpret

The **RuntimeEnv Growth Investigation** workflow runs on its narrow affected PR
paths and supports manual dispatch once present on the default branch. PR runs
check out the exact event head with a read-only token and no retained Git
credentials. There are no dispatch inputs, supplied commands, target clusters,
source directories, or resource overrides. It uses a dedicated hosted Linux VM
and one bounded, network-disabled experiment container. The image build installs
the locked dependencies before disabling the experiment's network.

On a separately admitted, dedicated Linux Docker host, from a clean committed
checkout with locked contributor dependencies installed:

```bash
uv run python scripts/benchmark_runtime_env_growth.py
```

This fixed command refuses non-Linux hosts before doing work. It creates only
the experiment on the local `/var/run/docker.sock` daemon; Docker target/context
environment overrides cannot select a different host. It writes only
`artifacts/runtime-env-growth/report.json`; the output directory must not exist.
The workflow retains that bounded JSON for 14 days. It never uploads raw logs,
package contents, filenames, environment values, or exception text. Read the JSON
as data; do not execute artifacts. Exit zero requires complete observations,
source matching and successful owned cleanup. Failed or partial observations
remain failed, including a primary command refusal accompanied by cleanup failure.

The host hashes the fixed source copied into the image, package files, and
implementation separately. Image labels and the immutable image/container
identities bind the build to that source. Inside the image, a build-time seal is
checked against the copied bytes and installed editable-package location. The
host compares this producer identity with its clean checkout before accepting
the report, and checks source cleanliness again, excluding only its owned output
directory. These are producer attestations, separate from the measured extraction
and archive observations. Run-specific identities belong in the artifact, not
durable release acceptance claims.

## Controls and measurements

Two serial arms each keep one initialized driver interpreter across three
restarted Ray sessions. Each session runs exactly five small task controls:

1. Package and upload fixture A from a fresh local specification.
2. Reuse A's prepared URI in the same session.
3. Independently repackage unchanged A from another fresh local specification.
4. Package B, differing by one application byte.
5. Reuse the original A URI again.

Each new session uploads first. Thus the restart comparison measures re-upload
and extraction of the same content into a new GCS; it does **not** test whether
a previous session's URI survives. There are at most 30 tasks across six sessions.
Every task reads its extracted working directory, verifies the expected A/B
marker and an included file, and checks that all six exclusion canaries are absent.

Inputs are generated fixtures with `working_dir` only: no `py_modules`, pip, uv,
plugins, credentials or application checkout. Both arms freeze Ray 2.58.0's
default exclusions (`.git`, `.venv`, `venv`, `__pycache__`), explicit experiment
exclusions (`artifacts`, `ray-tmp`, `.vault`), `.gitignore` (`build/`) and
`.rayignore` (`dist/`). The parent supplies a fixed driver environment without
packaging overrides; the driver refuses unexpected overrides. These are experiment
controls, not proposed additions to django-ray defaults.

The raw arm delegates to Ray's actual working-directory uploader. That function
mutates its input to a URI, so independent repackaging always receives a fresh
copy of the local specification. The Django arm calls the actual normalization,
snapshot and Core preparation helpers. It checks `runtime_env_plan_identity`
for the original source before snapshotting, the snapshot before upload, and the
original source after snapshot cleanup, before task dispatch.

The observer delegates to Ray's actual archive writer and upload operation. It
records each emitted ZIP's compressed bytes, uncompressed bytes, members and
digest before Ray removes it. An upload cache hit records no newly emitted upload
archive; it never reuses an older archive's digest as a new measurement. Django
may still emit its separate snapshot archive on that cache hit. Explicit URI reuse
has no archive. Complete reports require this fixed sequence and its supporting
records, rather than accepting a producer's success flag alone.

Filesystem scans distinguish original source, temporary Django snapshots,
upload scratch, Ray working-directory extraction, and other session files. They
record file/directory/link counts, logical and allocated bytes without names or
file contents. Each task is followed by an owned-root and current-session scan;
post-shutdown scans retain older-session extraction in the root totals. The
task timing covers preparation plus task completion, not isolated compute time.
Compare role counts at equivalent control positions. Partial scans are explicit
and cannot support a complete result or a leak conclusion.

## Admission, deadlines and cleanup

These finite, deliberately small limits are proposed experiment admission and
observation stops. They are uncalibrated and are not production acceptance thresholds.

| Boundary | Fixed limit |
|---|---|
| Dedicated host admission | At least 4 available CPUs, 14 GB RAM and 5 GB free disk |
| Container | 2 CPUs, 6 GiB memory with no additional swap, 256 PIDs, 256 MiB shared memory, core files disabled |
| Scratch | 1 GiB tmpfs and 50,000 inodes; read-only container root |
| Observation stop | 512 MiB logical bytes or 25,000 entries under the owned root |
| Two generated sources | 32 MiB and 2,048 entries combined |
| Individual archive | 16 MiB compressed and uncompressed, 1,024 members |
| Native experiment | 20 minutes total, including a cleanup reserve |
| Each session | 180 seconds, never extended by progress events |
| Startup / preparation / task / shutdown | 60 / 20 / 15 / 20 seconds per phase |
| Each filesystem scan | 2 seconds |
| Post-scan quiescence / final driver exit | 2 / 5 seconds, both inside the original session deadline |
| Retained evidence | 256 KiB JSON and at most 256 driver events |

The container verifies effective cgroup and tmpfs ceilings before importing Ray.
A parent outside the persistent driver owns startup, shutdown and whole-session
deadlines, including blocking SDK calls. The host separately owns the whole
container deadline. Build admission has five minutes; the hosted job has 35
minutes including dependency installation, focused tests and cleanup.

Isolated Linux subreapers retain driver and CLI descendants even if they detach
or their launcher exits. Cleanup uses owned process identities; it does not run
`ray stop`, kill by process name, or inspect arbitrary PIDs for deletion. Between
sessions, the persistent driver is the only permitted child. Bounded snapshots
also compare listeners, `/dev/shm` names and the global `/tmp/ray` names inside the
isolated container. These observations do not delete foreign state.

Only an exclusively created scratch root with unchanged parent/root identity may
be removed, after process and boundary checks. Linux directory descriptors prevent
following replaced paths or symlinks during deletion. Failure to prove cleanup is
a failed observation; forced disposal is not graceful shutdown evidence. The host
removes only the verified owned container ID and unique image. Ambiguous container
creation requires matching the private owner label and immutable image before any
removal. Docker build-cache layers are outside the measured scratch root and are
discarded with the dedicated hosted VM.

## Validation boundary

The pure tests use actual orchestration with fake SDK seams; they do not run Ray
or certify extraction behavior. Six Linux-only filesystem/process checks exercise
real ownership and deadline failures without Ray or a database. They skip on
non-Linux workstations. A real hosted experiment and those Linux checks must be
reviewed before drawing any conclusion from this recipe.

This new workflow changes CI composition, so an exact-head hosted Linux **CI Gate**
is the full-suite checkpoint when no admitted local Linux environment is available.
The experiment is separate diagnostic evidence. It does not replace required CI
or deployed-behavior qualification, and does not add production package, worker,
deployment or RuntimeEnv behavior. No cold-Ray deployment claim follows from this
standalone local-Ray experiment.
