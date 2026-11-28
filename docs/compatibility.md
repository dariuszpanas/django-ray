# Compatibility and Version Policy

This page defines the tested dependency and platform matrix. The separate
[Stability and Deprecation Policy](stability.md) defines the proposed 1.0 public
contract, experimental boundary, and removal process while django-ray remains Beta.

Beta upgrades follow the [coordinated upgrade procedure](stability.md#coordinated-beta-upgrades):
stop submissions, drain work, back up, stop old writers, migrate and update all components
together. Historical data preservation and current-version failure recovery remain required;
mixed-version managers and old-payload execution are not the Beta upgrade commitment.
The dormant target and historical migration details below do not add a rolling-upgrade
requirement. Transitional execution readers are retired at the announced release boundary,
with preserved-data evidence, rather than removed by this documentation change.

## Ray Client startup limitation

Ray 2.60 does not resolve the intermittent Linux Client startup race tracked in
[Ray #63202](https://github.com/ray-project/ray/issues/63202). Earlier native investigation
reproduced the matching gRPC assertion with stock Ray Client; different Python
matrix lanes failed on different runs. This is not evidence of a deterministic
Python-version incompatibility.

Prefer direct Ray Core/GCS or explicit HTTP(S) Ray Jobs configuration for Beta
deployments that need to avoid this Client startup path. Using `ray://` starts a
Client connection and remains subject to the limitation. A passing retry does
not establish that the race is fixed, and an ambiguous task submission must not
be replayed merely because connection setup failed.

[Upstream PR #65982](https://github.com/ray-project/ray/pull/65982) remained unmerged
when reviewed on 2026-11-07; its process-spawn change is absent from the Ray 2.60.0
tag and has not been adopted or backported here. Further
investigation and requalification require an available upstream fix; the 0.7.x
releases do not establish a repair. Verify startup and abrupt-parent-death cleanup before
removing this warning: the proposed patch changes child fate sharing. This is
not a promise that a particular upstream release will contain the fix. Ray 2.60's
[completed-chunk replay fix](https://github.com/ray-project/ray/pull/66476) repairs a
different reconnect path and does not establish a startup repair.

### Ray 2.60.0 baseline for 0.8.0

The django-ray 0.8.0 baseline requires Ray 2.60.0 or a newer compatible
release. The contributor lock, deployment images, Ray Data recipe, and CI minimum
move together from 2.58.0 to 2.60.0. Released django-ray 0.7.x retains its 2.58.0
minimum. This source update is not a release or a claim that upgrade qualification
has passed.

Follow the [0.7.1 to 0.8.0 candidate upgrade guide](deployment/upgrading-from-0.7.md)
for the stopped-writer sequence, migration `0029_lease_engine_and_hot_indexes`
(DB-clock `lease_expires_at`, `lease_policy_revision`, widened `queue_name`, and
partial hot-path/purge indexes), and rollback decisions. Managers, node images, and
RuntimeEnv dependencies must move together. Selected Ray 2.60 PR qualification does
not replace the final versioned candidate's complete applicable release evidence.

The [Ray 2.60.0 release](https://github.com/ray-project/ray/releases/tag/ray-2.60.0)
was reviewed on November 7, 2026. Changes relevant to django-ray include:

- Ray 2.59 enabled token authentication for newly started local clusters, and
  2.60 retains that behavior. Isolate token-file state when testing repeated
  `ray.init()` and shutdown. Explicit-address clients and joining workers still
  need `RAY_AUTH_MODE=token` and the cluster's matching token. The bundled
  Kubernetes manifests already distribute both. The tagged
  [authentication guide](https://github.com/ray-project/ray/blob/ray-2.60.0/doc/source/ray-security/token-auth.md)
  schedules the broader default for remote clusters in Ray 2.61.
- RuntimeEnv adds `local://` URIs, `.tar.xz` archives, and environment-variable
  expansion in uv package inputs. django-ray retains its existing immutable
  source and dependency identities; a new upstream input form does not bypass
  those checks or become a qualified django-ray delivery path automatically.
- Core repairs include runtime-env-agent file-handle cleanup, object-reference
  and lineage handling, and use of the worker-local GCS client in JobSupervisor.
  Retain cancellation, durable completion, reconnect, and shutdown regression
  checks rather than inferring that these changes resolve django-ray's open
  investigations.
- Ray Data rejects pickled-object columns in more external Arrow readers and
  changes shuffle, Parquet read planning, and actor-pool autoscaling defaults.
  Re-run the bounded Data Job recipe; its JSON/scalar-Parquet contract does not
  qualify arbitrary connectors or application deserializers. Ray's refreshed
  wheels/images also carry dependency security updates; django-ray keeps its
  own stricter dependency floors and audits.
- Ray's RuntimeEnv agent bundles its own `aiohttp`: 2.60 upgrades that copy
  from 3.14.3 to 3.14.4 in the tagged
  [wheel build configuration](https://github.com/ray-project/ray/blob/ray-2.60.0/python/setup.py#L39).
  Updating the application's external `aiohttp` lock does not replace this
  vendored copy, so upgrading Ray itself carries a separate security benefit.

Before release acceptance, run the Linux checkpoint and the full applicable
[deployment and coordinated upgrade gates](deployment/local-kuberay-gate.md),
including cold Ray startup, authenticated Core/Jobs control, task/workflow
execution, cancellation, and preservation checks with matching managers, head,
and workers. Existing 2.58 receipts are historical evidence, not 2.60 product
qualification. Python 3.15 and native Compiled Graph remain unqualified.

### Ray 2.59.0 review

The [Ray 2.59.0 release](https://github.com/ray-project/ray/releases/tag/ray-2.59.0)
was reviewed on October 10, 2026, while django-ray's reproducible lock, minimum
version, and deployment baseline remained 2.58.0. That historical review allowed
the latest-dependency lane to exercise 2.59 without advancing the baseline; the
0.8.0 baseline above supersedes that minimum-version decision.

- New local clusters enable token authentication. Token-file persistence and
  inherited process environment affect repeated `ray.init()` and child startup;
  the default for remote/multi-node clusters is unchanged in 2.59. Qualification
  must cover isolated token files, existing clusters, and authenticated Jobs
  submission, polling, cancellation, and address precedence. See the
  [authentication change](https://github.com/ray-project/ray/pull/64755).
- The [dashboard forkserver change](https://github.com/ray-project/ray/pull/65159)
  concerns a different startup path from Ray Client. Client fix
  [#65982](https://github.com/ray-project/ray/pull/65982) remained open and unmerged
  on the review date, so the Client race warning above still applies.
- [Dashboard redaction](https://github.com/ray-project/ray/pull/65226) protects
  browser-facing RuntimeEnv values. It does not replace django-ray's stored
  diagnostic redaction. [Worker-lease cancellation changes](https://github.com/ray-project/ray/pull/65613)
  also warrant cancellation and durable-completion regression evidence.

This review does not certify a Ray upgrade. Before changing the pinned deployment
tuple, run the Linux checkpoint and affected cold-Ray application/upgrade scenarios
with matching managers, head, and workers. Native Compiled Graph remains outside
the admitted exact-version capability policy, and no shutdown-crash repair is
established by these release notes.

## Module Path Compatibility

`django_ray.workflows` remains the public defining module for workflow builders. Private
workflow planning, execution support, progress, storage, and Admin rendering live under the
inert `django_ray.workflow` package, while private target contracts and coordination live
under `django_ray.target`. The former flat internal module paths were removed during Beta;
they were never part of the checked public API inventory and have no compatibility shims.
Persisted workflow schemas and execution protocols retain their independent compatibility
rules.

Beta users that imported those private modules must move to the canonical package paths:

| Removed private path | Canonical path |
|---|---|
| `django_ray.workflow_plans` | `django_ray.workflow.plans` |
| `django_ray.admin_workflow_graph` | `django_ray.workflow.admin_graph` |
| `django_ray.workflow_output_previews` | `django_ray.workflow.previews` |
| `django_ray.workflow_progress` | `django_ray.workflow.progress.runs` |
| `django_ray.workflow_progress_storage.prepare_workflow_progress_topology` | `django_ray.workflow.progress.preparation.prepare_workflow_progress_topology` |
| `django_ray.workflow_progress_<name>` | `django_ray.workflow.progress.<name>` |
| `django_ray.ray_target_probe` | `django_ray.target.probe` |
| `django_ray.target_<name>` | `django_ray.target.<name>` |

## Supported Versions

| Component | Supported |
|---|---|
| Python | 3.12, 3.13, 3.14 |
| Django | 6.0.8 or newer compatible release |
| Ray | 2.60.0 or newer compatible release (0.8.0 baseline) |
| Production operating system | Linux |

Python 3.12 is the floor because Django 6.0 requires Python 3.12+, not because Ray does.
Current [Ray releases support a wider Python range](https://pypi.org/project/ray/).

### Python 3.15 preparation

[Python 3.15.0 was released on October 9, 2026](https://www.python.org/downloads/release/python-3150/).
As reviewed on November 7, [Ray 2.60.0's files](https://pypi.org/project/ray/2.60.0/#files)
still include no CPython 3.15 Core wheel.
Python 3.15 is therefore a compatibility preparation target, not a supported
django-ray runtime. The ordinary test, dependency, release, and container matrices
remain on Python 3.12–3.14, and the package does not advertise a 3.15 classifier.

The blocking **Python 3.15 Compatibility (without Ray Core)** Linux CI job builds
the wheel and sdist on released CPython 3.15, compiles repository Python source,
checks archive metadata, and runs selected resource-free tests against the installed
wheel. It installs the project's non-Ray direct dependencies and the selected test
tools separately. Ray remains a mandatory package dependency; this test-only
`--no-deps` installation is not an application installation procedure.

The check rejects editable/source imports, mismatched installed Python files, Ray
imports or stubs, external-resource tests, skips, and expected failures. It exercises
Django startup, serialization, callable imports, retry policy, redaction, encryption,
and selected workflow/target codecs. It does not establish Ray Core, Client, Jobs,
cloudpickle, distributed execution, or free-threaded Python compatibility.
See [contributor instructions](contributing.md#python-315-compatibility-check).

Before promoting 3.15 to supported status, require upstream Core wheels and compatible
mandatory extras, run the complete Linux test/dependency and installed-release matrices,
and qualify a matching Python/Ray tuple across the manager, head, and workers. Update
the classifiers and supported-version documentation only with that evidence. Native
Compiled Graph retains its separate exact-tuple capability policy.

### Runtime dependency boundaries

Django 6.0.8 is the django-ray security floor. Django 6.0.0 through 6.0.7 are not
supported; newer compatible releases are exercised by the latest-dependency lane.

Ray 2.60.0 is the security and runtime baseline for django-ray 0.8.0.
The released 0.7.x baseline was 2.58.0, unchanged from 0.5.0 and 0.6.0. Ray 2.57 added
[Dashboard log-path validation](https://github.com/ray-project/ray/pull/64701),
which is absent from 2.56.0 and 2.56.1. Ray 2.58 additionally fixes
[nested Parquet/Lance pickle handling](https://github.com/ray-project/ray/pull/64881)
and the [Serve internal authentication/TLS boundary](https://github.com/ray-project/ray/pull/65189).
The latter fixes concern optional application-owned Data and Serve workloads; the
bundled Data recipe reads JSON, not Lance or nested pickle data. These extend the
earlier fixes behind django-ray 0.4's Ray 2.56.0 floor. Ray 2.60 extends the Data
reader defenses to additional external Arrow input paths; see the review above.

Keep the task managers, Ray head, and Ray workers on one compatible tuple when
installing django-ray; follow the [coordinated Beta upgrade
procedure](stability.md#coordinated-beta-upgrades). Do not use a mixed Ray minor-version
cluster as a rolling-upgrade shortcut. The Ray upgrade does not activate Compiled
Graph, task-event migration, Sandbox, or a new execution protocol. Existing
cancellation, ownership and durable completion fences remain necessary.

The package dependency range is a resolver boundary, not permission to mix remote
runtime tuples. Keep the configured Ray version and Python `major.minor.patch`
aligned across the task manager and every live schedulable cluster node — Ray's
connection-time warning or `RAY_IGNORE_VERSION_MISMATCH` does not relax that rule.
Upgrade task managers and every cluster node together, and treat any Ray or Python
patch difference as unsupported.

Migrations `0022` through `0026` add dormant, unseeded target-persistence, route,
capability, and protocol-`2` provenance tables as preparatory infrastructure. Active
production execution remains protocol `1` (`1..1`), and no production enqueue, worker,
runner, or routing path creates or consumes those dormant tables. See
[Architecture — Rolling upgrades](architecture.md#rolling-upgrades) and
[Local KubeRay Final Gate](deployment/local-kuberay-gate.md) for maintainer details on
dormant schema bounds, `PROTECT` deletion ordering, and protocol-`2` staging.

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

The runtime security floors also require `certifi>=2026.10.19`, `filelock>=4.2.1`,
`msgpack>=1.2.3`, `multidict>=6.9.1`, `opentelemetry-api>=1.45.1`, `urllib3>=2.8.0`, and
`virtualenv>=21.14.13`. These dependencies arrive through Ray and its mandatory
extras; explicit package requirements protect fresh downstream installs as well
as the committed lock. Updating an unrelated dependency does not necessarily
refresh these transitive packages. Current advisory checks can therefore fail
on an unchanged source commit even when its Python tests continue to pass.

The committed OpenTelemetry packages move together to the compatible 1.45.1/0.66b1
release family. Install the optional `django-ray[otel]` extra when you want the
OpenTelemetry SDK (`opentelemetry-sdk>=1.45.1`) alongside `django_ray.tracing`.
Rebuild images and recreate affected virtual environments after
upgrading: patched virtualenv applies to newly generated launchers and does not
retroactively repair launchers in existing environments. See the
[certifi advisory](https://github.com/pypa/advisory-database/blob/main/vulns/certifi/PYSEC-2026-4320.yaml),
[OpenTelemetry advisory](https://github.com/open-telemetry/opentelemetry-python/security/advisories/GHSA-xcq7-p3pf-p88h),
[msgpack advisory](https://github.com/msgpack/msgpack-python/security/advisories/GHSA-j586-36cw-2gc2),
[virtualenv launcher advisory](https://github.com/pypa/virtualenv/security/advisories/GHSA-c978-59m8-c77v),
and [virtualenv Bash activation advisory](https://github.com/pypa/virtualenv/security/advisories/GHSA-cx6f-vjhg-g3cc).

### Projects that prohibit prerelease dependencies {#uv-prerelease-disallow}

Ordinary installs using uv's default resolver policy work without extra configuration.
If your project sets `prerelease = "disallow"` under `[tool.uv]`, resolution can fail on
`opentelemetry-exporter-prometheus` because `ray[default]` depends on OpenTelemetry
exporter packages whose upstream versions carry beta suffixes (such as the `0.66b1`
family paired with `opentelemetry-api>=1.45.1`).

Keep your stable-only policy intact instead of passing a global `--prerelease allow`:
scope prerelease permission to only the required OpenTelemetry packages using uv's
[per-package `prerelease-package` setting](https://docs.astral.sh/uv/reference/settings/#prerelease-package):

```toml
[project]
dependencies = [
    "django-ray>=0.8.0",
    "opentelemetry-exporter-prometheus==0.66b1",
    "opentelemetry-semantic-conventions==0.66b1",
]

[tool.uv]
prerelease = "disallow"
prerelease-package = { opentelemetry-exporter-prometheus = "explicit", opentelemetry-semantic-conventions = "explicit" }
```

Review the resolver's dependency chain and match the OpenTelemetry version family
expected by your installed Ray release whenever upgrading Ray or django-ray. Verify
resolution cleanly with `uv lock && uv sync --frozen && uv pip check`.

The filelock 4.2.1 floor includes the upstream lock-file and Windows timeout fixes;
virtualenv 21.14.10 copies seed packages from temporary app-data even when symlink
seeding is enabled in configuration or environment variables. Recreate affected
environments so their seed packages no longer reference deleted temporary caches.
See the [filelock release notes](https://github.com/tox-dev/filelock/releases/tag/4.2.1)
and [virtualenv release notes](https://github.com/pypa/virtualenv/releases/tag/21.14.10).
Applications that also use filelock's `SoftReadWriteLock` must stop all participants
sharing a lock path, upgrade them together, and then restart them: filelock 4 changed
that lock's on-disk protocol, so older and newer participants do not exclude each
other. See the [filelock 4 migration note](https://github.com/tox-dev/filelock/releases/tag/4.0.0).

## Platforms

Ray publishes platform-specific wheels. A pure-Python django-ray wheel does not imply
that Ray is available on every Python/platform combination.

- Linux is the supported execution target, including local Ray, clusters, and Kubernetes.
- Windows compatibility is best effort, observed only through a small advisory GitHub Actions
  Windows packaging/import lane. Native Windows/macOS execution is outside production and release
  certification. Full CI, broad test suites, and native-Ray validation must not run on non-Linux
  workstations. Focused resource-free checks and host-side tools remain usable there; use an explicitly
  bounded Linux environment for execution tests. No container or cluster is started automatically.
- Historical native Windows startup-abort investigation is retained in
  [ray-project/ray#65181](https://github.com/ray-project/ray/issues/65181). Keeping existing platform
  accommodations does not expand the supported execution promise; legacy Windows-specific
  harness accommodations are retired only after Linux replacement qualification.
- Ray publishes Linux aarch64 wheels for supported Python versions, but users must
  confirm their OS, architecture, and Python ABI match an available Ray wheel.

Compiled Graph is more restrictive than ordinary Ray use: policy version 4 rejects
Windows, aarch64, Ray Client, GPU transport, and every unverified native tuple before
calling `experimental_compile()`. Dynamic workflows remain supported according to the
version matrix above.
