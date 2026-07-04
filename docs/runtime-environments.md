# Runtime Environments

Ray RuntimeEnv lets one Ray cluster execute tasks with different Python packages,
uploaded project code, environment variables, or container images. django-ray adds
named profiles and a durable environment identity on top of Ray's native feature.

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

```text
Django task manager -> persisted profile snapshot -> generic Ray cluster
                                              \-> RuntimeEnv cache per Ray node
```

Mount credentials, certificates, and shared data through the cluster deployment.
Do not put secrets in profile URIs or `env_vars`: the resolved RuntimeEnv is stored
on the task row and is visible to operators.

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
