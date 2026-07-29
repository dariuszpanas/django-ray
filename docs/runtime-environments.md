# Runtime Environments

Ray RuntimeEnv lets one Ray cluster execute tasks with different Python packages,
uploaded project code, environment variables, or container images. django-ray adds
named profiles and a durable environment identity on top of Ray's native feature.

> **Cold-start warning:** The first task using a new profile can be substantially
> slower while Ray downloads code and creates the environment on the selected node.
> RuntimeEnv caches are node-local, so the first use on every node is cold; later
> executions of the identical environment on a warm node are usually much faster.

## RuntimeEnv Is More Than pip

django-ray passes the resolved profile to Ray as a native RuntimeEnv mapping. Use
the environment mechanism that fits the workload:

| Field | Best suited to |
| --- | --- |
| `working_dir` | Shipping an application directory or remote ZIP archive |
| `py_modules` | Adding reusable modules, wheels, or remote archives to `PYTHONPATH` |
| `pip` | Installing Python packages with pip |
| `uv` | Installing Python packages with uv |
| `conda` | Selecting a named Conda environment or defining one inline |
| `env_vars` | Setting non-secret worker environment variables |
| `image_uri` / `container` | Running workers in a prebuilt container image |
| `py_executable` | Selecting an alternate Python executable or launcher |
| `config` | Controlling setup timeout and other RuntimeEnv behavior |

See Ray's complete
[RuntimeEnv API reference](https://docs.ray.io/en/latest/ray-core/api/doc/ray.runtime_env.RuntimeEnv.html)
for supported fields, value formats, and version-specific constraints.

For example, profiles can use uv or Conda without changing django-ray's task API:

```python
DJANGO_RAY = {
    "RUNTIME_ENV_PROFILES": {
        "analytics-uv": {
            "working_dir": "s3://deployments/analytics/7f3a2c1.zip",
            "uv": ["numpy==2.3.5", "polars==1.31.0"],
            "env_vars": {"DJANGO_SETTINGS_MODULE": "config.settings"},
        },
        "analytics-conda": {
            "working_dir": "s3://deployments/analytics/7f3a2c1.zip",
            "conda": {
                "channels": ["conda-forge"],
                "dependencies": ["python=3.12", "numpy=2.3"],
            },
            "env_vars": {"DJANGO_SETTINGS_MODULE": "config.settings"},
        },
    },
}
```

Ray does not allow top-level `pip` and `conda` in the same RuntimeEnv. Its
container-based fields also have stricter combination rules: `image_uri` must
generally be used alone except for `env_vars` and `config`, so application code
and dependencies should already be present in that image. Consult the linked Ray
reference when mixing fields.

## Define Profiles

Profiles live in `DJANGO_RAY`. A profile can be a direct Ray RuntimeEnv mapping:

```python
# settings.py
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "RUNTIME_ENV_PROFILES": {
        "project": {
            "working_dir": os.environ.get(
                "DJANGO_RAY_WORKING_DIR_URI",
                str(BASE_DIR),
            ),
            "excludes": [".git", ".venv"],
            "pip": ["django>=6.0", "psycopg[binary]>=3.1"],
            "env_vars": {
                "DJANGO_SETTINGS_MODULE": "config.settings",
                "PYTHONPATH": "src",
            },
        },
    },
    "DEFAULT_RUNTIME_ENV_PROFILE": "project",
}
```

Profiles may extend another profile. Dictionary fields such as `env_vars` are
merged; list fields `pip`, `uv`, `py_modules`, and `excludes` are appended.
Other fields are replaced:

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "RUNTIME_ENV_PROFILES": {
        "project": {
            "working_dir": "s3://deployments/myapp/7f3a2c1.zip",
            "pip": ["django>=6.0"],
            "env_vars": {"DJANGO_SETTINGS_MODULE": "config.settings"},
        },
        "numpy-2-2": {
            "extends": "project",
            "runtime_env": {
                "pip": ["numpy==2.2.6"],
                "env_vars": {"APP_VARIANT": "numpy-2-2"},
            },
        },
        "numpy-2-3": {
            "extends": "project",
            "runtime_env": {
                "pip": ["numpy==2.3.5"],
                "env_vars": {"APP_VARIANT": "numpy-2-3"},
            },
        },
    },
}
```

Pin production dependencies and use immutable archive URIs for `working_dir` or
`py_modules` when reproducibility matters.

- Local mode can content-address and upload a local directory.
- Ray Job submission can upload its job-level local working directory.
- Ray Client (`ray://...`) cannot turn a task-level local path from the Django pod
  into a remote RuntimeEnv. Use `https://`, `s3://`, or `gs://`, or a `file://` ZIP
  on storage mounted at the same path on every Ray node.

Local `py_modules` directories and files remain available to dynamic Ray tasks, but
version 1 rejects them for reusable strategies. Their import basename changes Python
semantics, while Ray 2.56's package URI is not a strong identity for every such archive.
The plan fingerprints the basename and content so retry pinning remains conservative;
prefer an independently verified immutable artifact before enabling future reuse.

Do not point a standalone Django pod at the head node's GCS port as a substitute for a
Ray node. A direct Ray Core driver also expects a local raylet. The repository's
KubeRay overlay demonstrates the shared `file:///runtime-env/django-ray-source.zip`
pattern for local testing.

## Select a Profile for a Django Task

Django Tasks supports backend aliases. Bind each alias to one trusted profile:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
        "OPTIONS": {"RUNTIME_ENV_PROFILE": "project"},
    },
    "numpy-2-3": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
        "OPTIONS": {"RUNTIME_ENV_PROFILE": "numpy-2-3"},
    },
}
```

Select it with the standard Django API:

```python
# myapp/tasks.py
from django.tasks import task


@task(queue_name="default")
def average(values: list[float]) -> float:
    import numpy

    return float(numpy.mean(values))


result = average.using(backend="numpy-2-3").enqueue([1.0, 2.0, 3.0])
```

The backend resolves the profile during enqueue and stores its canonical JSON and
SHA-256 identity on `RayTaskExecution`. Retries use that immutable snapshot even
if Django settings change after enqueue.

`RAY_RUNTIME_ENV` remains supported as the unnamed default. A backend may also
provide an inline `RAY_RUNTIME_ENV`, but it cannot combine that option with
`RUNTIME_ENV_PROFILE`.

## Select a Profile for a Workflow Step

Workflow leaves inherit the outer task environment unless they request another:

```python
from django_ray.workflows import chain, step


def load_rows(values: list[float]) -> list[float]:
    return values


def run_numpy_model(values: list[float]) -> dict[str, float]:
    import numpy

    return {"mean": float(numpy.mean(values))}


def store_summary(summary: dict[str, float]) -> dict[str, float]:
    return summary


pipeline = chain(
    step(load_rows),
    step(run_numpy_model, runtime_env="numpy-2-3"),
    step(store_summary),
)
```

Inline environments are also accepted:

```python
def column_names(rows: list[dict[str, object]]) -> list[str]:
    import pandas

    return [str(name) for name in pandas.DataFrame(rows).columns]


step(
    column_names,
    runtime_env={"pip": ["pandas==2.3.0"]},
)
```

Use `signature.with_runtime_env("profile-name")` when constructing a workflow
dynamically. The older `ray_options={"runtime_env": ...}` form remains compatible.

### RuntimeEnv identity in effective workflow plans

Named and inline step environments are resolved when `WorkflowSignature.run()` creates
its effective plan, before the first workflow leaf is submitted. Changing a package,
archive, image, working-directory snapshot, or named profile content therefore changes
the plan identity or produces an explicit reusable-strategy rejection. Mutating Django
settings after materialization cannot change the environment submitted by that run.

Credential variable values and URI credentials never enter the plan and are never
hashed into its fingerprint. Credential-looking names remain visible as schema
metadata and are covered by a non-secret provider revision. Ordinary environment
values such as `MODE` are also kept out of the fingerprint because a variable name is
not proof that its value is non-secret. Reusable strategies require one declared
`environment_revision` that covers the full non-secret configuration contract:

```python
DJANGO_RAY = {
    "WORKFLOW_PLAN_CODE_REVISION": "container:sha256:0123456789abcdef",
    "WORKFLOW_PLAN_TRUST_IDENTITY": {
        "trust_domain": "cluster:production",
        "credential_provider": "kubernetes-service-account",
        "credential_revision": "provider-v3",
        "environment_revision": "namespace-sync-v8",
        "scheduling_revision": "placement-v2",
        "service_account_audience": "kubernetes.default.svc",
    },
}
```

`credential_revision` identifies the provider contract, not a token. Rotating a token
inside the same declared provider revision does not churn a plan or leak a secret-derived
digest. `environment_revision` is an operator promise to change the revision whenever
any covered ordinary runtime value changes. Changing either revision, the provider,
trust domain, or audience invalidates the plan.
If an environment has secret-dependent behavior but no safe provider revision, dynamic
Ray tasks remain available and reusable strategies receive `UNRESOLVED_RUNTIME_ENV`.
Ray label selectors use the separate `scheduling_revision`; dashboard names and task
labels are execution-only annotations and never enter the plan fingerprint.

The identity transported to a Ray worker is a strict versioned envelope containing only
bounded diagnostics and digests of the safe projection. The full RuntimeEnv and its
secret-bearing execution values travel only through Ray's execution channel. Local
per-step code paths are snapshotted with Ray's packaging semantics and rebound before
the first leaf is submitted; fenced durable plans are pinned before package upload so
stale attempts cannot create artifacts. A source change during packaging fails before
leaf submission. Ray's current local-package cache key is not a strong end-to-end
verification of django-ray's SHA-256 snapshot, so local `working_dir` and `py_modules`
inputs remain dynamic-only and receive `UNRESOLVED_RUNTIME_ENV` until a worker-verifiable
artifact identity is available.

Reusable-strategy eligibility and durable retry safety are separate decisions. A local
file or tree snapshot is not reusable because Ray does not yet verify django-ray's
content digest end to end, but it is safe to retry after django-ray rechecks the same
content before packaging. Conversely, a mutable package pin or opaque URI remains valid
for dynamic execution yet is retry-unsafe: its raw value is intentionally absent from
the secret-free plan, so a later attempt cannot prove that the execution binding stayed
the same. The effective plan persists bounded retry-safety paths and the attempt that
first pinned it; later attempts fail before submission when any binding is unsafe.
Provider and environment revisions restore retry safety only for the values their
documented contracts cover, without hashing raw credentials.

## Caching and Performance

Ray caches RuntimeEnv artifacts on each node. Reusing identical canonical specs
allows later tasks to avoid most environment setup work. django-ray's environment
hash makes this identity visible in the database and admin.

Keep the number of environment variants bounded:

- prefer named, stable profiles over a unique inline environment per task;
- group related work under one outer task or actor when it can reuse an environment;
- compare the first run with a repeated run before drawing latency conclusions;
- prebuild system libraries and very large common dependencies into the base image.

RuntimeEnv removes application-image rebuilds; it does not make dependency
installation free.

The cache is node-local. The first fan-out across four cold nodes may install the same
environment four times in parallel. Warm each node before a latency-sensitive rollout,
or keep large, common dependencies in the base image. See
[Performance](performance.md#runtime-environment-cost).

## Generic KubeRay Images

The KubeRay example uses the upstream `rayproject/ray` image for Ray head and
worker containers, plus a stock Python image for its dashboard-import helper.
The project profile uploads source and installs Python dependencies. The Django
web and task-manager images remain application-specific.

For Ray Client submissions, django-ray serializes only its small outer bootstrap
executor by value. The generic head can therefore deserialize the submission
before the task-level RuntimeEnv installs and exposes the full project package.

This separation works well for a shared cluster within one trust boundary:

1. The Django task manager resolves a named profile at enqueue time.
2. The task row stores the resolved profile snapshot and its content hash.
3. The generic Ray cluster creates that environment on the node selected for work.
4. That node can reuse its cached copy for later tasks with the same environment.

Mount credentials, certificates, and shared data through the cluster deployment.
Do not put secrets in profile URIs or `env_vars`: the resolved RuntimeEnv is stored
in plaintext on the task row for exact retry execution and may be available through
database access, backups, and the Ray runtime. The Django admin intentionally omits
the raw snapshot and shows only its profile and content hash; that presentation
boundary does not encrypt the stored value.

RuntimeEnv is packaging and dependency isolation, not a security boundary. Use
separate Ray clusters for mutually untrusted teams or workloads.

## Test Project

The sample project defines `project`, `thin`, `numpy-2-2`, and `numpy-2-3`
profiles and exposes:

```text
POST /api/cluster/runtime-env/probe?profile=thin
POST /api/cluster/runtime-env/probe?profile=numpy-2-3&package=numpy
POST /api/cluster/runtime-env/benchmark?profile=numpy-2-3&package=numpy&repeats=3
GET  /api/cluster/runtime-env/{task_id}
```

The benchmark runs repeated workflow leaves with the same profile and reports
per-run elapsed time so cold setup and cache reuse are easy to compare.
