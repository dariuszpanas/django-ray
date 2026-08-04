# Compatibility and Version Policy

## Supported Versions

| Component | Supported |
|---|---|
| Python | 3.12, 3.13, 3.14 |
| Django | 6.0 or newer compatible release |
| Ray | 2.56.0 or newer compatible release |
| Production operating system | Linux recommended |

Python 3.12 is the floor because Django 6.0 requires Python 3.12+, not because Ray does.
Current [Ray releases support a wider Python range](https://pypi.org/project/ray/).

Ray 2.56.0 is the django-ray 0.4 security floor. Earlier releases fall below fixes in
published upstream Ray advisories for the
[dashboard and Jobs boundary](https://github.com/ray-project/ray/security/advisories/GHSA-q5fh-2hc8-f6rq),
[Ray Data Parquet reads](https://github.com/ray-project/ray/security/advisories/GHSA-mw35-8rx3-xf9r),
and the
[Ray Data WebDataset reader](https://github.com/ray-project/ray/security/advisories/GHSA-hhrp-gw25-jr43).
Upgrade the task managers, Ray head, and Ray workers together before installing
django-ray 0.4; do not use a mixed Ray minor-version cluster as a rolling-upgrade
shortcut.

Ray Client and cluster execution are most predictable when the task manager and Ray
cluster use the same Ray version and Python minor version. Patch-version differences
may produce a warning; different minor versions should not be treated as compatible.

The general version range and base `ray[default]` dependency do not install or promise
every optional Ray component. See the
[Ray Ecosystem Support and Install Matrix](ray-ecosystem.md) before adding Data, Train,
Tune, RLlib, Serve, or Compiled Graph to an application workload.

Ray Compiled Graph has a separate, exact, fail-closed capability policy because its
native beta channels have narrower version, platform, transport, and process-owner
constraints. The general Ray version range in this table does not enable compilation.
Generic or unresolved host/container context is also insufficient: an eligible row
requires an immutable deployment/image digest plus explicit shared-memory and Ray
object-store profiles.
See [Compiled Graph Compatibility](compiled-graph-compatibility.md).

## Dependency Policy

`pyproject.toml` uses lower bounds so applications can resolve compatible updates
instead of being locked to the versions used for one django-ray release. The committed
`uv.lock` gives contributors and CI a reproducible current environment.

CI covers:

- the committed lock on every supported Python minor;
- minimum direct dependencies on the oldest supported Python;
- the newest resolvable dependencies on the newest supported Python;
- matching wheel and sdist security metadata plus package installation from the built
  wheel on every supported Python minor.

Updating the lock is therefore separate from raising a package's minimum supported
version. A lower bound should move only when django-ray uses a newer API or the older
dependency is no longer supportable. A published dependency security fix is such a
support boundary: the repository lock protects its own reproducible environment, while
the declared lower bound controls what a downstream fresh install may resolve.

## Platforms

Ray publishes platform-specific wheels. A pure-Python django-ray wheel does not imply
that Ray is available on every Python/platform combination.

- Linux is the production target for clusters and Kubernetes.
- Ray's
  [native Windows support remains beta](https://docs.ray.io/en/latest/ray-overview/installation.html#windows-support),
  and multi-node Windows clusters are untested upstream. django-ray retains Windows for
  best-effort local development and
  test visibility, not as a production or release-certification target. Repeated native
  local-Ray lifecycles have intermittently aborted during startup before any job, worker,
  object, or application task registered; the upstream investigation is
  [ray-project/ray#65181](https://github.com/ray-project/ray/issues/65181). No Ray release
  is currently identified as the fix. Use Linux, WSL2, or the documented Docker path for
  repeatable evaluation, and keep one native local-Ray owner on a Windows host at a time.
- Ray publishes Linux aarch64 wheels for supported Python versions, but users must
  confirm their OS, architecture, and Python ABI match an available Ray wheel.

Compiled Graph is more restrictive than ordinary Ray use: policy version 3 rejects
Windows, aarch64, Ray Client, GPU transport, and every unverified native tuple before
calling `experimental_compile()`. Dynamic workflows remain supported according to the
version matrix above.
