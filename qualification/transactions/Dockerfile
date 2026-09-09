# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/qualification/.venv

WORKDIR /build

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra postgres

COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --wheel --out-dir /opt/django-ray-wheels \
    && rm -f /opt/django-ray-wheels/.gitignore \
    && test "$(find /opt/django-ray-wheels -mindepth 1 -maxdepth 1 | wc -l)" -eq 1 \
    && test "$(find /opt/django-ray-wheels -mindepth 1 -maxdepth 1 -type f -name '*.whl' | wc -l)" -eq 1

FROM python:${PYTHON_VERSION}-slim-trixie AS qualification

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

# PostgreSQL is a disposable child of the probe, never an image service.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod 755 /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-common \
    && printf 'create_main_cluster = false\n' > /etc/postgresql-common/createcluster.conf \
    && apt-get install -y --no-install-recommends \
        postgresql-17=17.11-0+deb13u1 postgresql-client-17=17.11-0+deb13u1 \
    && test ! -d /var/lib/postgresql/17/main \
    && rm -rf /var/lib/apt/lists/* \
    && rm /usr/sbin/policy-rc.d

# Use the same non-root identity in Compose and other container runners. PostgreSQL initdb
# requires an operating-system account for the effective UID, even with -U.
RUN groupadd --gid 10001 qualification \
    && useradd --uid 10001 --gid qualification --no-log-init \
        --home-dir /home/qualification --create-home --shell /usr/sbin/nologin qualification

ENV DJANGO_RAY_QUALIFICATION_HOLD_SECONDS=10 \
    HOME=/tmp \
    PATH=/opt/qualification/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY --from=builder /opt/qualification/.venv /opt/qualification/.venv
COPY --from=builder /opt/django-ray-wheels /opt/django-ray-wheels

WORKDIR /workspace
USER 10001:10001

CMD ["python", "-c", "import time; time.sleep(3600)"]
