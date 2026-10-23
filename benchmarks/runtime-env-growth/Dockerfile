FROM python:3.13.11-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
WORKDIR /workspace
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
RUN uv sync --frozen --no-dev --python /usr/local/bin/python
COPY scripts/ scripts/
COPY tests/__init__.py tests/real_ray_ownership.py tests/
ARG SOURCE_COMMIT
ARG SOURCE_TREE
ARG COPIED_SOURCE_SHA256
RUN .venv/bin/python -m scripts.runtime_env_growth.identity \
    "$SOURCE_COMMIT" "$SOURCE_TREE" "$COPIED_SOURCE_SHA256"
LABEL django-ray.growth.source-commit=$SOURCE_COMMIT \
      django-ray.growth.source-tree=$SOURCE_TREE \
      django-ray.growth.copied-source=$COPIED_SOURCE_SHA256
RUN groupadd --gid 10001 experiment \
    && useradd --uid 10001 --gid experiment --no-log-init --no-create-home experiment
ENV PATH=/workspace/.venv/bin:$PATH TMPDIR=/scratch HOME=/scratch
USER 10001:10001
CMD ["python", "scripts/benchmark_runtime_env_growth.py", "--container"]
