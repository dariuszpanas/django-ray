# syntax=docker/dockerfile:1

# This digest is the linux/amd64 Ray 2.56.0 Python 3.12 image used by the
# django-ray policy-v3 pilot. Updating it requires new #102-style evidence.
ARG RAY_BASE_IMAGE=rayproject/ray@sha256:2951c07de396a8b746f9c678b52c6e2282e614e00f80e6846a9ccd12945ae6b0
FROM ${RAY_BASE_IMAGE}

ARG DJANGO_RAY_REVISION

LABEL org.opencontainers.image.title="django-ray Compiled Graph KubeRay pilot" \
      org.opencontainers.image.revision="${DJANGO_RAY_REVISION}" \
      org.opencontainers.image.source="https://github.com/dariuszpanas/django-ray"

USER root
WORKDIR /opt/django-ray

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
COPY scripts/kuberay_compiled_graph_pilot.py scripts/
COPY k8s/pilots/compiled-graph/ k8s/pilots/compiled-graph/

# Do not resolve or upgrade Ray dependencies here. The exact Ray image already
# carries the CPU Compiled Graph extra, including cupy-cuda12x and its required
# fastrlock dependency. Reinstall fastrlock explicitly so a changed base image
# cannot silently omit or substitute that transitive runtime boundary. Install
# only the exact Django import boundary required by django_ray.runtime and this
# source tree, with dependency resolution disabled.
RUN python -m pip install --disable-pip-version-check --no-cache-dir --no-deps \
        django==6.0.7 asgiref==3.11.1 sqlparse==0.5.5 fastrlock==0.8.3 && \
    python -m pip install --disable-pip-version-check --no-cache-dir --no-deps . && \
    python - <<'PY'
from importlib import metadata
import platform
import tomllib

with open("pyproject.toml", "rb") as project_file:
    project_version = tomllib.load(project_file)["project"]["version"]

expected = {
    "django-ray": project_version,
    "django": "6.0.7",
    "asgiref": "3.11.1",
    "sqlparse": "0.5.5",
    "ray": "2.56.0",
    "numpy": "1.26.4",
    "pyarrow": "19.0.1",
    "cupy-cuda12x": "13.4.0",
    "fastrlock": "0.8.3",
}
observed = {name: metadata.version(name) for name in expected}
if observed != expected:
    raise SystemExit(f"pilot dependency profile changed: {observed!r}")
if platform.python_version() != "3.12.12":
    raise SystemExit(f"pilot Python changed: {platform.python_version()}")
PY

ENV DJANGO_RAY_PILOT_SOURCE_REVISION="${DJANGO_RAY_REVISION}" \
    PYTHONUNBUFFERED=1 \
    RAY_USAGE_STATS_ENABLED=0

USER 1000
WORKDIR /home/ray
